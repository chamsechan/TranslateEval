from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import select

from app import queue
from app.importers import (
    commit_dataset_import,
    commit_submission_import,
    validate_dataset_import,
    validate_submission_import,
)
from app.models import EvaluatorProfile
from app.models import (
    DatasetSample,
    EvaluatorJob,
    EvaluatorRevision,
    Prediction,
    PromptVersion,
    ScoreResult,
)
from app.queue import (
    cancel_task,
    create_evaluation_task,
    language_detection_summary,
    threshold_summary,
)
from app.schemas import EvaluatorSelection
from app.evaluators.base import BaseEvaluator, ScoreInput, ScoreOutput


ROOT = Path(__file__).resolve().parents[2]


def prepare_task(session_factory):
    with session_factory() as session:
        dataset_report = validate_dataset_import(
            session, ROOT / "examples" / "dataset" / "flores-demo"
        )
        assert dataset_report.report["valid"] is True
        version = commit_dataset_import(session, dataset_report.id)
        assert version.sample_count == 6

        submission_report = validate_submission_import(
            session, ROOT / "examples" / "results" / "demo-run"
        )
        assert submission_report.report["valid"] is True
        submission = commit_submission_import(session, submission_report.id)
        profile = session.scalar(
            select(EvaluatorProfile).where(EvaluatorProfile.evaluator_type == "sacrebleu_zh")
        )
        assert profile is not None
        task = create_evaluation_task(
            session,
            submission,
            [EvaluatorSelection(evaluator_revision_id=profile.revisions[0].id)],
            False,
        )
        return task.id


def test_full_bleu_queue_and_dynamic_threshold(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(queue, "SessionLocal", session_factory)
    task_id = prepare_task(session_factory)
    asyncio.run(queue.worker_loop(once=True))
    with session_factory() as session:
        task = session.get(queue.EvaluationTask, task_id)
        assert task is not None and task.status == "completed"
        evaluator_job = task.dataset_jobs[0].evaluator_jobs[0]
        low = threshold_summary(session, evaluator_job.id, 20.0)
        high = threshold_summary(session, evaluator_job.id, 80.0)
        assert low["successful"] == 6
        assert low["micro_mean"] == high["micro_mean"]
        assert low["micro_accuracy"] > high["micro_accuracy"]
        assert low["coverage"] == 1.0
        assert any(item["metric_name"] == "corpus_bleu" for item in low["aggregates"])
        detection = language_detection_summary(session, task.dataset_jobs[0].id)
        assert detection == {"applicable": False, "reason": "本次推理已提供源语种"}


def test_cancel_dataset_queue_before_scoring(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(queue, "SessionLocal", session_factory)
    task_id = prepare_task(session_factory)
    with session_factory() as session:
        cancel_task(session, task_id)
    asyncio.run(queue.worker_loop(once=True))
    with session_factory() as session:
        task = session.get(queue.EvaluationTask, task_id)
        assert task is not None and task.status == "cancelled"
        assert task.dataset_jobs[0].evaluator_jobs[0].status == "cancelled"


def test_language_detection_metrics_when_auto_detect_is_enabled(session_factory) -> None:
    task_id = prepare_task(session_factory)
    with session_factory() as session:
        task = session.get(queue.EvaluationTask, task_id)
        assert task is not None
        task.submission.model_run.inference_mode = "auto_detect"
        # New imports preserve detection behavior alongside the mode code.
        run = task.submission.model_run
        run.result_info = {**run.result_info, "inference": {
            **run.result_info["inference"], "mode": "auto_detect", "detects_language": True,
        }}
        predictions = list(
            session.scalars(
                select(Prediction).where(
                    Prediction.submission_dataset_id
                    == task.dataset_jobs[0].submission_dataset_id
                )
            )
        )
        for prediction in predictions:
            prediction.predicted_language = prediction.sample_id.split("-", 1)[0]
        predictions[0].predicted_language = "vi"
        predictions[-1].predicted_language = None
        session.commit()
        result = language_detection_summary(session, task.dataset_jobs[0].id)
        assert result["applicable"] is True
        assert result["covered"] == 5
        assert result["correct"] == 4
        assert result["accuracy"] == 0.8
        assert len(result["confusion_matrix"]) == 3


def test_llm_cache_uses_model_name_and_force_can_bypass(session_factory, monkeypatch) -> None:
    task_id = prepare_task(session_factory)

    class FakeLlmEvaluator(BaseEvaluator):
        evaluator_type = "openai_compatible_llm"

        def __init__(self):
            super().__init__({})
            self.calls = 0

        @classmethod
        def validate_config(cls, config):
            return config

        @property
        def model_name(self) -> str:
            return "judge-a"

        @property
        def base_url(self) -> str:
            return "https://new-endpoint.invalid/v1"

        async def evaluate_one(self, item: ScoreInput) -> ScoreOutput:
            self.calls += 1
            return ScoreOutput(7.5, 0, 10, "point", "fake")

    fake = FakeLlmEvaluator()
    monkeypatch.setattr(queue, "SessionLocal", session_factory)
    monkeypatch.setattr(queue, "build_evaluator", lambda *args, **kwargs: fake)
    with session_factory() as session:
        original_task = session.get(queue.EvaluationTask, task_id)
        assert original_task is not None
        profile = EvaluatorProfile(
            name="Mock Judge", evaluator_type="openai_compatible_llm", enabled=True
        )
        session.add(profile)
        session.flush()
        revision = EvaluatorRevision(
            profile_id=profile.id,
            revision=1,
            config={"model": "judge-a", "concurrency": 4, "max_retries": 0},
            default_threshold=8,
        )
        session.add(revision)
        prompt = session.scalar(select(PromptVersion).where(PromptVersion.published.is_(True)))
        assert prompt is not None
        session.flush()
        llm_task = create_evaluation_task(
            session,
            original_task.submission,
            [EvaluatorSelection(evaluator_revision_id=revision.id, prompt_version_id=prompt.id)],
            False,
        )
        job = llm_task.dataset_jobs[0].evaluator_jobs[0]
        first_prediction = session.scalar(
            select(Prediction).where(
                Prediction.submission_dataset_id == job.dataset_job.submission_dataset_id
            )
        )
        assert first_prediction is not None
        first_sample = session.scalar(
            select(DatasetSample).where(
                DatasetSample.dataset_version_id
                == job.dataset_job.submission_dataset.dataset_version_id,
                DatasetSample.sample_id == first_prediction.sample_id,
            )
        )
        assert first_sample is not None
        cached = ScoreResult(
            evaluator_type="openai_compatible_llm",
            evaluator_revision_id=revision.id,
            prompt_version_id=prompt.id,
            source_language=first_sample.source_language,
            source_hash=first_sample.source_hash,
            reference_hash=first_sample.reference_hash,
            translation_hash=first_prediction.translation_hash,
            evaluator_model="judge-a",
            base_url="https://old-endpoint.invalid/v1",
            score=9,
            score_min=0,
            score_max=10,
            unit="point",
            reason="cached",
            raw_response={},
        )
        session.add(cached)
        session.commit()
        job_id = job.id

    asyncio.run(queue.process_evaluator_job(job_id))
    assert fake.calls == 5
    with session_factory() as session:
        job = session.get(queue.EvaluatorJob, job_id)
        assert job is not None and job.cached_items == 1
        force_task = create_evaluation_task(
            session,
            job.dataset_job.task.submission,
            [EvaluatorSelection(evaluator_revision_id=job.evaluator_revision_id, prompt_version_id=job.prompt_version_id)],
            True,
        )
        force_job_id = force_task.dataset_jobs[0].evaluator_jobs[0].id
    asyncio.run(queue.process_evaluator_job(force_job_id))
    assert fake.calls == 11


def test_worker_recovers_jobs_interrupted_during_processing(session_factory) -> None:
    task_id = prepare_task(session_factory)
    with session_factory() as session:
        task = session.get(queue.EvaluationTask, task_id)
        assert task is not None
        job = task.dataset_jobs[0].evaluator_jobs[0]
        job.status = "running"
        job.dataset_job.status = "running"
        task.status = "running"
        session.commit()

    with session_factory() as session:
        assert queue.recover_interrupted_jobs(session) == 1
        task = session.get(queue.EvaluationTask, task_id)
        assert task is not None
        assert task.status == "queued"
        assert task.dataset_jobs[0].status == "queued"
        assert task.dataset_jobs[0].evaluator_jobs[0].status == "queued"


def test_evaluator_initialization_failure_is_persisted(session_factory, monkeypatch) -> None:
    task_id = prepare_task(session_factory)
    monkeypatch.setattr(queue, "SessionLocal", session_factory)
    monkeypatch.setattr(
        queue,
        "build_evaluator",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("配置不可用")),
    )
    with session_factory() as session:
        task = session.get(queue.EvaluationTask, task_id)
        assert task is not None
        job_id = task.dataset_jobs[0].evaluator_jobs[0].id

    asyncio.run(queue.process_evaluator_job(job_id))

    with session_factory() as session:
        job = session.get(EvaluatorJob, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.error == "配置不可用"
        assert job.failed_items == job.total_items
        retried = queue.retry_failed_job(session, job.id)
        assert retried.status == "queued"
        assert retried.failed_items == 0
        assert retried.dataset_job.failed_items == 0
        assert retried.dataset_job.task.failed_items == 0
        assert retried.dataset_job.task.finished_at is None


def test_mixed_completed_and_cancelled_is_not_marked_completed(session_factory) -> None:
    task_id = prepare_task(session_factory)
    with session_factory() as session:
        task = session.get(queue.EvaluationTask, task_id)
        assert task is not None
        dataset_job = task.dataset_jobs[0]
        completed_job = dataset_job.evaluator_jobs[0]
        completed_job.status = "completed"
        cancelled_job = EvaluatorJob(
            dataset_job_id=dataset_job.id,
            evaluator_revision_id=completed_job.evaluator_revision_id,
            status="cancelled",
            total_items=0,
        )
        session.add(cancelled_job)
        session.flush()
        queue._finish_parent_statuses(session, completed_job)
        session.commit()

        assert dataset_job.status == "partial_cancelled"
        assert task.status == "partial_cancelled"


def test_duplicate_evaluator_selection_is_rejected(session_factory) -> None:
    task_id = prepare_task(session_factory)
    with session_factory() as session:
        task = session.get(queue.EvaluationTask, task_id)
        assert task is not None
        revision_id = task.dataset_jobs[0].evaluator_jobs[0].evaluator_revision_id
        selection = EvaluatorSelection(evaluator_revision_id=revision_id)
        try:
            create_evaluation_task(session, task.submission, [selection, selection], False)
        except ValueError as exc:
            assert "不能重复选择" in str(exc)
        else:
            raise AssertionError("重复选择应被拒绝")
