from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, select

from app import queue
from app.evaluators.base import BaseEvaluator, ScoreInput, ScoreOutput
from app.evaluators.bleu import SacreBleuZhEvaluator
from app.evaluators.llm import OpenAICompatibleEvaluator
from app.models import DatasetSample, EvaluationItem, EvaluationTask, EvaluatorJob, EvaluatorProfile, EvaluatorRevision, Prediction, PromptVersion, ScoreResult
from app.normalization import sha256_text, sample_content_hash
from app.schemas import EvaluatorSelection
from app.worker import exclusive_worker
from test_import_and_queue import prepare_task


def expanded_task(factory, count=1200):
    initial = prepare_task(factory)
    with factory() as session:
        task = session.get(EvaluationTask, initial)
        dataset = task.dataset_jobs[0].submission_dataset
        version = dataset.dataset_version
        samples, predictions = [], []
        for index in range(count - 6):
            sample_id, source, reference = f"scale-{index:08d}", f"Source {index}", f"中文参考译文记录{index}。"
            samples.append(dict(dataset_version_id=version.id, sample_id=sample_id, source_language="de",
                source_text=source, reference_zh=reference, source_hash=sha256_text(source),
                reference_hash=sha256_text(reference), content_hash=sample_content_hash(sample_id, "de", source, reference)))
            predictions.append(dict(submission_dataset_id=dataset.id, sample_id=sample_id,
                translation_zh=reference, translation_hash=sha256_text(reference)))
        session.execute(insert(DatasetSample), samples)
        session.execute(insert(Prediction), predictions)
        version.sample_count = dataset.prediction_count = count
        new_task = queue.create_evaluation_task(session, task.submission,
            [EvaluatorSelection(evaluator_revision_id=task.dataset_jobs[0].evaluator_jobs[0].evaluator_revision_id)], True)
        return new_task.dataset_jobs[0].evaluator_jobs[0].id


@pytest.mark.parametrize("config", [
    {"smooth_method": "floor", "smooth_value": 2},
    {"smooth_method": "floor", "smooth_value": 0},
    {"smooth_method": "floor", "smooth_value": -1},
    {"smooth_method": "floor", "smooth_value": float("nan")},
    {"smooth_method": "add-k", "smooth_value": float("inf")},
    {"smooth_method": "floor", "smooth_value": "0.1"},
    {"smooth_method": "floor", "smooth_value": True},
    {"effective_order": "false"}, {"effective_order": 1},
])
def test_bleu_rejects_unsafe_config(config):
    with pytest.raises(ValueError):
        SacreBleuZhEvaluator.validate_config(config)


@pytest.mark.parametrize("field,value", [
    ("concurrency", True), ("concurrency", "8"), ("concurrency", 1.5),
    ("max_retries", -1), ("max_retries", 1.2), ("max_tokens", 0), ("max_tokens", False),
    ("timeout_seconds", float("nan")), ("timeout_seconds", float("inf")),
    ("temperature", float("inf")), ("temperature", -1), ("temperature", 2.1),
    ("cache_policy", "unknown"),
])
def test_llm_rejects_nonfinite_and_wrong_types(field, value):
    config = {"base_url": "https://judge.invalid/v1", "model": "test", "api_key": "test", field: value}
    with pytest.raises(ValueError):
        OpenAICompatibleEvaluator.validate_config(config)


@pytest.mark.parametrize("config", [
    {"smooth_method": "exp"}, {"smooth_method": "floor", "smooth_value": 0.1},
    {"smooth_method": "add-k", "smooth_value": 1}, {"smooth_method": "none", "effective_order": False},
])
def test_streaming_bleu_preserves_corpus_semantics(config):
    evaluator = SacreBleuZhEvaluator(config)
    items = [ScoreInput("de", "source", reference, translation) for reference, translation in [
        ("这是一个很长的参考译文，包含不同的词语。", "这是不同的短句。"),
        ("准确译文", "准确译文"), ("另一个例子。", "另一个例子，但是增加了内容。"),
    ]]
    totals = [0] * 10
    for item in items:
        totals = [a + b for a, b in zip(totals, evaluator.segment_statistics(item.translation_zh, item.reference_zh))]
    streamed = evaluator.corpus_from_statistics(totals)
    expected = evaluator.corpus_aggregates(items)
    assert streamed["corpus_bleu"] == pytest.approx(expected["corpus_bleu"])
    assert streamed["signature"] == expected["signature"]


@pytest.mark.parametrize("score", [160.69, float("nan"), float("inf"), True, "9"])
def test_invalid_evaluator_output_never_becomes_completed(session_factory, monkeypatch, score):
    task_id = prepare_task(session_factory)
    with session_factory() as session:
        job_id = session.get(EvaluationTask, task_id).dataset_jobs[0].evaluator_jobs[0].id

    class InvalidBleu(SacreBleuZhEvaluator):
        async def evaluate_one(self, item):
            return ScoreOutput(score, 0, 100, "BLEU")

    monkeypatch.setattr(queue, "SessionLocal", session_factory)
    monkeypatch.setattr(queue, "build_evaluator", lambda kind, config, **kw: InvalidBleu(config))
    asyncio.run(queue.process_evaluator_job(job_id))
    with session_factory() as session:
        job = session.get(EvaluatorJob, job_id)
        assert job.completed_items == 0 and job.failed_items == 6
        assert job.status == "failed" and job.error is None
        assert session.scalar(select(func.count(ScoreResult.id))) == 0


def test_bounded_worker_completes_many_batches_without_per_item_recounts(session_factory, monkeypatch):
    job_id = expanded_task(session_factory)
    monkeypatch.setattr(queue, "SessionLocal", session_factory)
    monkeypatch.setattr(queue, "ITEM_BATCH_SIZE", 128)
    original_rows, original_progress = queue._joined_item_rows, queue._refresh_progress
    fetched, identity_sizes, reconciliations = [], [], []

    def rows(session, job, **kwargs):
        result = original_rows(session, job, limit=128, **kwargs)
        fetched.append(len(result))
        identity_sizes.append(len(session.identity_map))
        return result

    def progress(session, job, **kwargs):
        if kwargs.get("delta") is None:
            reconciliations.append(job.id)
        return original_progress(session, job, **kwargs)

    monkeypatch.setattr(queue, "_joined_item_rows", rows)
    monkeypatch.setattr(queue, "_refresh_progress", progress)
    asyncio.run(queue.process_evaluator_job(job_id))
    with session_factory() as session:
        job = session.get(EvaluatorJob, job_id)
        assert job.status == "completed" and job.completed_items == 1200
        assert session.scalar(select(func.count(EvaluationItem.id)).where(
            EvaluationItem.evaluator_job_id == job_id, EvaluationItem.status == "running")) == 0
        assert session.scalar(select(func.count(ScoreResult.id))) == 1200
    assert sum(fetched) == 1200 and max(fetched) == 128
    assert max(identity_sizes) < 128 * 8 + 100
    assert len(reconciliations) == 2


def test_conditional_job_claim_prevents_duplicate_execution(session_factory, monkeypatch):
    task_id = prepare_task(session_factory)
    with session_factory() as session:
        job_id = session.get(EvaluationTask, task_id).dataset_jobs[0].evaluator_jobs[0].id
    monkeypatch.setattr(queue, "SessionLocal", session_factory)

    async def run():
        await asyncio.gather(queue.process_evaluator_job(job_id), queue.process_evaluator_job(job_id))

    asyncio.run(run())
    with session_factory() as session:
        assert session.scalar(select(func.count(ScoreResult.id))) == 6
        assert session.get(EvaluatorJob, job_id).completed_items == 6


def test_worker_lock_rejects_duplicate_and_releases(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'worker.db'}"
    with exclusive_worker(database_url):
        with pytest.raises(RuntimeError, match="已有 Worker"):
            with exclusive_worker(database_url):
                pytest.fail("duplicate acquired a live lock")
    with exclusive_worker(database_url):
        pass


def test_duplicate_worker_cannot_recover_the_active_workers_items(tmp_path, monkeypatch):
    from app import worker
    database_url = f"sqlite:///{tmp_path / 'worker.db'}"
    monkeypatch.setattr(worker, "settings", SimpleNamespace(database_url=database_url,
        worker_heartbeat_file=tmp_path / "heartbeat"))
    recoveries = []
    monkeypatch.setattr(worker, "recover_interrupted_jobs", lambda session: recoveries.append(True))
    with exclusive_worker(database_url):
        with pytest.raises(RuntimeError, match="已有 Worker"):
            asyncio.run(worker.run_worker(once=True))
    assert recoveries == []


def test_optional_database_maintenance_cannot_fail_completed_scoring(session_factory, monkeypatch):
    task_id = prepare_task(session_factory)
    with session_factory() as session:
        job_id = session.get(EvaluationTask, task_id).dataset_jobs[0].evaluator_jobs[0].id
    monkeypatch.setattr(queue, "SessionLocal", session_factory)
    monkeypatch.setattr(queue, "optimize_database", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("maintenance unavailable")))
    asyncio.run(queue.process_evaluator_job(job_id))
    with session_factory() as session:
        job = session.get(EvaluatorJob, job_id)
        assert job.status == "completed" and job.completed_items == 6


def test_recovery_requeues_inflight_items(session_factory):
    task_id = prepare_task(session_factory)
    with session_factory() as session:
        job = session.get(EvaluationTask, task_id).dataset_jobs[0].evaluator_jobs[0]
        job.status = "running"
        item = session.scalar(select(EvaluationItem).where(EvaluationItem.evaluator_job_id == job.id))
        item.status = "running"
        session.commit()
        queue.recover_interrupted_jobs(session)
        session.refresh(item)
        assert item.status == "queued" and job.status == "queued"


def test_strict_cache_keeps_revision_and_prompt_separate(session_factory, monkeypatch):
    original = prepare_task(session_factory)

    class LocalJudge(BaseEvaluator):
        evaluator_type = "openai_compatible_llm"
        calls = 0
        @classmethod
        def validate_config(cls, config): return config
        @property
        def model_name(self): return "strict-test"
        async def evaluate_one(self, item):
            self.calls += 1
            return ScoreOutput(9, 0, 10, "point")

    judge = LocalJudge({})
    monkeypatch.setattr(queue, "SessionLocal", session_factory)
    monkeypatch.setattr(queue, "build_evaluator", lambda *a, **kw: judge)
    with session_factory() as session:
        submission = session.get(EvaluationTask, original).submission
        profile = EvaluatorProfile(name="Strict cache test", evaluator_type=judge.evaluator_type, enabled=True)
        session.add(profile); session.flush()
        revision = EvaluatorRevision(profile_id=profile.id, revision=1, default_threshold=8,
            config={"model": judge.model_name, "cache_policy": "strict_revision", "concurrency": 2})
        session.add(revision)
        prompt_a = session.scalar(select(PromptVersion))
        prompt_b = PromptVersion(profile_id=prompt_a.profile_id, version=2, system_template="B", user_template="B", published=True)
        session.add(prompt_b); session.flush()
        job_ids = []
        for prompt in (prompt_a, prompt_b, prompt_b):
            task = queue.create_evaluation_task(session, submission,
                [EvaluatorSelection(evaluator_revision_id=revision.id, prompt_version_id=prompt.id)], False)
            job_ids.append(task.dataset_jobs[0].evaluator_jobs[0].id)
    for job_id in job_ids[:2]:
        asyncio.run(queue.process_evaluator_job(job_id))
    # A malformed newer historical row must not mask the earlier valid score.
    with session_factory() as session:
        old = session.scalar(select(ScoreResult).where(ScoreResult.prompt_version_id == prompt_b.id))
        values = {column.name: getattr(old, column.name) for column in ScoreResult.__table__.columns
                  if column.name not in {"id", "created_at"}}
        values["score"] = 160.69
        session.add(ScoreResult(**values, created_at=datetime.now(UTC) + timedelta(days=1)))
        session.commit()
    asyncio.run(queue.process_evaluator_job(job_ids[2]))
    with session_factory() as session:
        assert [session.get(EvaluatorJob, id).cached_items for id in job_ids] == [0, 0, 6]
    assert judge.calls == 12


def test_only_dispatched_requests_have_running_status(session_factory, monkeypatch):
    task_id = prepare_task(session_factory)
    with session_factory() as session:
        job = session.get(EvaluationTask, task_id).dataset_jobs[0].evaluator_jobs[0]
        job.evaluator_revision.config = {**job.evaluator_revision.config, "concurrency": 2}
        job_id = job.id
        session.commit()
    observations = []
    class ObservedBleu(SacreBleuZhEvaluator):
        async def evaluate_one(self, item):
            with session_factory() as session:
                observations.append(dict(session.execute(select(EvaluationItem.status, func.count(EvaluationItem.id))
                    .where(EvaluationItem.evaluator_job_id == job_id).group_by(EvaluationItem.status)).all()))
            await asyncio.sleep(0.002)
            return await super().evaluate_one(item)
    monkeypatch.setattr(queue, "SessionLocal", session_factory)
    monkeypatch.setattr(queue, "build_evaluator", lambda kind, config, **kw: ObservedBleu(config))
    asyncio.run(queue.process_evaluator_job(job_id))
    assert len(observations) == 6
    assert observations[0] == {"queued": 4, "running": 2}
    assert all(row.get("running", 0) <= 2 for row in observations)


def test_force_reevaluation_still_deduplicates_across_bounded_batches(session_factory, monkeypatch):
    job_id = expanded_task(session_factory, count=600)
    from sqlalchemy import update
    source, reference = "Repeated source", "重复的参考译文。"
    with session_factory() as session:
        job = session.get(EvaluatorJob, job_id)
        dataset = job.dataset_job.submission_dataset
        session.execute(update(DatasetSample).where(DatasetSample.dataset_version_id == dataset.dataset_version_id,
            DatasetSample.sample_id.like("scale-%")).values(source_text=source, reference_zh=reference,
                source_hash=sha256_text(source), reference_hash=sha256_text(reference)))
        session.execute(update(Prediction).where(Prediction.submission_dataset_id == dataset.id,
            Prediction.sample_id.like("scale-%")).values(translation_zh=reference, translation_hash=sha256_text(reference)))
        session.commit()
    monkeypatch.setattr(queue, "SessionLocal", session_factory)
    original_rows = queue._joined_item_rows
    monkeypatch.setattr(queue, "_joined_item_rows", lambda session, job, **kw: original_rows(session, job, limit=64, **kw))
    asyncio.run(queue.process_evaluator_job(job_id))
    with session_factory() as session:
        job = session.get(EvaluatorJob, job_id)
        assert job.status == "completed" and job.completed_items == 600
        assert job.cached_items == 593
        assert session.scalar(select(func.count(ScoreResult.id))) == 7
