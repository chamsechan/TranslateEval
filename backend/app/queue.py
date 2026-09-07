from __future__ import annotations

import asyncio
import random
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal
from .evaluators import build_evaluator
from .evaluators.base import (
    BaseEvaluator,
    PermanentEvaluatorError,
    RetriableEvaluatorError,
    ScoreInput,
    ScoreOutput,
)
from .models import (
    AggregateScore,
    DatasetJob,
    DatasetSample,
    EvaluationItem,
    EvaluationTask,
    EvaluatorJob,
    EvaluatorRevision,
    InferenceSubmission,
    Prediction,
    PromptVersion,
    ScoreResult,
    SubmissionDataset,
)
from .queries import threshold_summary
from .schemas import EvaluatorSelection


TERMINAL = {"completed", "partial_failed", "partial_cancelled", "failed", "cancelled"}


class JobStateConflict(ValueError):
    pass


def create_evaluation_task(
    session: Session,
    submission: InferenceSubmission,
    selections: list[EvaluatorSelection],
    force_reevaluate: bool,
    *,
    commit: bool = True,
) -> EvaluationTask:
    selection_keys = [
        (selection.evaluator_revision_id, selection.prompt_version_id)
        for selection in selections
    ]
    if len(selection_keys) != len(set(selection_keys)):
        raise ValueError("同一个评测器与 Prompt 版本不能重复选择")
    for selection in selections:
        revision = session.get(EvaluatorRevision, selection.evaluator_revision_id)
        if not revision or not revision.profile.enabled:
            raise ValueError(f"评价器修订不可用: {selection.evaluator_revision_id}")
        if revision.profile.evaluator_type == "openai_compatible_llm":
            if not selection.prompt_version_id:
                raise ValueError("LLM 评价器必须选择 Prompt 版本")
            prompt = session.get(PromptVersion, selection.prompt_version_id)
            if not prompt or not prompt.published:
                raise ValueError("Prompt 版本不存在或未发布")

    task = EvaluationTask(
        submission_id=submission.id,
        status="queued",
        force_reevaluate=force_reevaluate,
    )
    session.add(task)
    session.flush()
    total = 0
    submission_datasets = list(
        session.scalars(
            select(SubmissionDataset).where(SubmissionDataset.submission_id == submission.id)
        )
    )
    for submission_dataset in submission_datasets:
        dataset_job = DatasetJob(
            task_id=task.id,
            submission_dataset_id=submission_dataset.id,
            status="queued",
        )
        session.add(dataset_job)
        session.flush()
        prediction_ids = list(
            session.scalars(
                select(Prediction.id).where(
                    Prediction.submission_dataset_id == submission_dataset.id
                )
            )
        )
        dataset_job.total_items = len(prediction_ids) * len(selections)
        total += dataset_job.total_items
        for selection in selections:
            evaluator_job = EvaluatorJob(
                dataset_job_id=dataset_job.id,
                evaluator_revision_id=selection.evaluator_revision_id,
                prompt_version_id=selection.prompt_version_id,
                status="queued",
                total_items=len(prediction_ids),
            )
            session.add(evaluator_job)
            session.flush()
            for offset in range(0, len(prediction_ids), 1_000):
                session.add_all(
                    [
                        EvaluationItem(
                            evaluator_job_id=evaluator_job.id,
                            prediction_id=prediction_id,
                            status="queued",
                        )
                        for prediction_id in prediction_ids[offset : offset + 1_000]
                    ]
                )
                session.flush()
    task.total_items = total
    submission.status = "queued"
    if commit:
        session.commit()
        session.refresh(task)
    else:
        session.flush()
    return task


def recover_interrupted_jobs(session: Session) -> int:
    jobs = list(
        session.scalars(
            select(EvaluatorJob).where(EvaluatorJob.status.in_(["preprocessing", "running"]))
        )
    )
    if not jobs:
        return 0

    dataset_job_ids: set[str] = set()
    task_ids: set[str] = set()
    for job in jobs:
        job.status = "queued"
        dataset_job_ids.add(job.dataset_job_id)

    for dataset_job in session.scalars(
        select(DatasetJob).where(DatasetJob.id.in_(dataset_job_ids))
    ):
        dataset_job.status = "cancelling" if dataset_job.cancel_requested else "queued"
        task_ids.add(dataset_job.task_id)

    for task in session.scalars(select(EvaluationTask).where(EvaluationTask.id.in_(task_ids))):
        task.status = "cancelling" if task.cancel_requested else "queued"
        task.finished_at = None
        task.submission.status = task.status

    session.commit()
    return len(jobs)


def cancel_task(session: Session, task_id: str) -> EvaluationTask:
    task = session.get(EvaluationTask, task_id)
    if not task:
        raise ValueError("任务不存在")
    if task.status in TERMINAL:
        return task
    task.cancel_requested = True
    task.status = "cancelling"
    dataset_ids = list(
        session.scalars(select(DatasetJob.id).where(DatasetJob.task_id == task.id))
    )
    if dataset_ids:
        session.execute(
            update(DatasetJob)
            .where(DatasetJob.id.in_(dataset_ids))
            .values(cancel_requested=True)
        )
    session.commit()
    return task


def cancel_dataset_job(session: Session, dataset_job_id: str) -> DatasetJob:
    job = session.get(DatasetJob, dataset_job_id)
    if not job:
        raise ValueError("数据集任务不存在")
    if job.status not in TERMINAL:
        job.cancel_requested = True
        job.status = "cancelling"
        session.commit()
    return job


def retry_failed_job(session: Session, evaluator_job_id: str) -> EvaluatorJob:
    job = session.get(EvaluatorJob, evaluator_job_id)
    if not job:
        raise ValueError("评价器任务不存在")
    if job.status not in TERMINAL or "cancelling" in {job.dataset_job.status, job.dataset_job.task.status}:
        raise JobStateConflict("任务仍在执行或取消中，请等待结束后重试失败项")
    failed_items = session.scalar(
        select(func.count(EvaluationItem.id)).where(
            EvaluationItem.evaluator_job_id == job.id,
            EvaluationItem.status == "failed",
        )
    ) or 0
    if not failed_items:
        raise ValueError("该评价器任务没有可重试的失败项")
    claimed = session.execute(
        update(EvaluatorJob)
        .where(EvaluatorJob.id == job.id, EvaluatorJob.status.in_(TERMINAL))
        .values(status="queued")
    )
    if claimed.rowcount != 1:
        session.rollback()
        raise JobStateConflict("任务状态已变化，请刷新后重试")
    session.execute(
        update(EvaluationItem)
        .where(
            EvaluationItem.evaluator_job_id == job.id,
            EvaluationItem.status == "failed",
        )
        .values(status="queued", error=None, attempts=0, finished_at=None)
    )
    job.status = "queued"
    job.error = None
    job.failed_items = 0
    job.dataset_job.failed_items = max(job.dataset_job.failed_items - failed_items, 0)
    job.dataset_job.task.failed_items = max(
        job.dataset_job.task.failed_items - failed_items, 0
    )
    job.dataset_job.status = "queued"
    job.dataset_job.task.status = "queued"
    job.dataset_job.task.finished_at = None
    job.dataset_job.task.submission.status = "queued"
    job.dataset_job.task.cancel_requested = False
    job.dataset_job.cancel_requested = False
    session.commit()
    return job


def _joined_item_rows(session: Session, evaluator_job: EvaluatorJob) -> list[dict[str, Any]]:
    submission_dataset = evaluator_job.dataset_job.submission_dataset
    rows = session.execute(
        select(EvaluationItem, Prediction, DatasetSample)
        .join(Prediction, EvaluationItem.prediction_id == Prediction.id)
        .join(
            DatasetSample,
            (DatasetSample.dataset_version_id == submission_dataset.dataset_version_id)
            & (DatasetSample.sample_id == Prediction.sample_id),
        )
        .where(
            EvaluationItem.evaluator_job_id == evaluator_job.id,
            EvaluationItem.status == "queued",
        )
        .order_by(EvaluationItem.id)
    ).all()
    return [
        {
            "item": item,
            "prediction": prediction,
            "sample": sample,
            "input": ScoreInput(
                source_language=sample.source_language,
                source_text=sample.source_text,
                reference_zh=sample.reference_zh,
                translation_zh=prediction.translation_zh,
            ),
        }
        for item, prediction, sample in rows
    ]


def _cache_key(row: dict[str, Any], model_name: str) -> tuple[str, str, str, str, str]:
    sample: DatasetSample = row["sample"]
    prediction: Prediction = row["prediction"]
    return (
        sample.source_language,
        sample.source_hash,
        sample.reference_hash,
        prediction.translation_hash,
        model_name,
    )


def _preload_llm_cache(
    session: Session, model_name: str, rows: list[dict[str, Any]]
) -> dict[tuple[str, ...], ScoreResult]:
    requested = list(
        {
            _cache_key(row, model_name)[:4]
            for row in rows
        }
    )
    cache: dict[tuple[str, ...], ScoreResult] = {}
    key_columns = tuple_(
        ScoreResult.source_language,
        ScoreResult.source_hash,
        ScoreResult.reference_hash,
        ScoreResult.translation_hash,
    )
    for offset in range(0, len(requested), 200):
        results = session.scalars(
            select(ScoreResult)
            .where(
                ScoreResult.evaluator_type == "openai_compatible_llm",
                ScoreResult.evaluator_model == model_name,
                key_columns.in_(requested[offset : offset + 200]),
            )
            .order_by(ScoreResult.created_at.desc())
        )
        for result in results:
            key = (
                result.source_language,
                result.source_hash,
                result.reference_hash,
                result.translation_hash,
                result.evaluator_model,
            )
            cache.setdefault(key, result)
    return cache


async def _score_with_retry(
    evaluator: BaseEvaluator,
    score_input: ScoreInput,
    retries: int,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[ScoreOutput | None, str | None, int]:
    attempts = 0
    while True:
        if should_cancel and should_cancel():
            return None, None, attempts
        attempts += 1
        try:
            return await evaluator.evaluate_one(score_input), None, attempts
        except PermanentEvaluatorError as exc:
            return None, str(exc), attempts
        except RetriableEvaluatorError as exc:
            if attempts > retries:
                return None, str(exc), attempts
            if should_cancel and should_cancel():
                return None, None, attempts
            delay = min(8.0, (2 ** (attempts - 1)) + random.random())
            await asyncio.sleep(delay)
        except Exception as exc:
            return None, f"未预期的评价器错误: {exc}", attempts


def _persist_score(
    session: Session,
    *,
    evaluator_job: EvaluatorJob,
    evaluator: BaseEvaluator,
    row: dict[str, Any],
    output: ScoreOutput,
) -> ScoreResult:
    sample: DatasetSample = row["sample"]
    prediction: Prediction = row["prediction"]
    result = ScoreResult(
        evaluator_type=evaluator_job.evaluator_revision.profile.evaluator_type,
        evaluator_revision_id=evaluator_job.evaluator_revision_id,
        prompt_version_id=evaluator_job.prompt_version_id,
        source_language=sample.source_language,
        source_hash=sample.source_hash,
        reference_hash=sample.reference_hash,
        translation_hash=prediction.translation_hash,
        evaluator_model=evaluator.model_name,
        base_url=evaluator.base_url,
        score=output.score,
        score_min=output.score_min,
        score_max=output.score_max,
        unit=output.unit,
        reason=output.reason,
        raw_response=output.raw_response,
    )
    session.add(result)
    session.flush()
    return result


def _refresh_progress(session: Session, job: EvaluatorJob) -> dict[str, int]:
    session.flush()
    counts = dict(
        session.execute(
            select(EvaluationItem.status, func.count(EvaluationItem.id))
            .where(EvaluationItem.evaluator_job_id == job.id)
            .group_by(EvaluationItem.status)
        ).all()
    )
    job.completed_items = int(counts.get("completed", 0))
    job.failed_items = int(counts.get("failed", 0))
    job.cached_items = int(
        session.scalar(
            select(func.count(EvaluationItem.id)).where(
                EvaluationItem.evaluator_job_id == job.id,
                EvaluationItem.cache_hit.is_(True),
            )
        )
        or 0
    )
    dataset_job = job.dataset_job
    sibling_jobs = list(
        session.scalars(
            select(EvaluatorJob).where(EvaluatorJob.dataset_job_id == dataset_job.id)
        )
    )
    dataset_job.completed_items = sum(item.completed_items for item in sibling_jobs)
    dataset_job.failed_items = sum(item.failed_items for item in sibling_jobs)
    dataset_job.cached_items = sum(item.cached_items for item in sibling_jobs)
    task = dataset_job.task
    dataset_jobs = list(
        session.scalars(select(DatasetJob).where(DatasetJob.task_id == task.id))
    )
    task.completed_items = sum(item.completed_items for item in dataset_jobs)
    task.failed_items = sum(item.failed_items for item in dataset_jobs)
    task.cached_items = sum(item.cached_items for item in dataset_jobs)
    return counts


def _item_status(counts: dict[str, int]) -> str:
    """Derive the job state from all item outcomes, including prior cancellations."""
    if counts.get("queued"):
        return "queued"
    if counts.get("cancelled"):
        return "partial_cancelled" if counts.get("completed") or counts.get("failed") else "cancelled"
    if counts.get("failed"):
        return "partial_failed" if counts.get("completed") else "failed"
    return "completed"


def _cancel_remaining(session: Session, job: EvaluatorJob) -> None:
    now = datetime.now(UTC)
    session.execute(
        update(EvaluationItem)
        .where(
            EvaluationItem.evaluator_job_id == job.id,
            EvaluationItem.status == "queued",
        )
        .values(status="cancelled", finished_at=now)
    )
    job.status = "partial_cancelled" if job.completed_items or job.failed_items else "cancelled"


def _store_aggregates(session: Session, job: EvaluatorJob, evaluator: BaseEvaluator | None = None) -> None:
    session.execute(delete(AggregateScore).where(AggregateScore.evaluator_job_id == job.id))
    submission_dataset = job.dataset_job.submission_dataset
    rows = session.execute(
        select(ScoreResult, DatasetSample, Prediction)
        .join(EvaluationItem, EvaluationItem.score_result_id == ScoreResult.id)
        .join(Prediction, EvaluationItem.prediction_id == Prediction.id)
        .join(
            DatasetSample,
            (DatasetSample.dataset_version_id == submission_dataset.dataset_version_id)
            & (DatasetSample.sample_id == Prediction.sample_id),
        )
        .where(
            EvaluationItem.evaluator_job_id == job.id,
            EvaluationItem.status == "completed",
        )
    ).all()
    # A cancelled job may never construct its scoring client. BLEU aggregates are
    # local; LLM means below only need the already persisted numeric scores.
    if rows and evaluator is None and job.evaluator_revision.profile.evaluator_type == "sacrebleu_zh":
        evaluator = build_evaluator("sacrebleu_zh", job.evaluator_revision.config)
    grouped: dict[str, list[tuple[ScoreResult, DatasetSample, Prediction]]] = defaultdict(list)
    for score, sample, prediction in rows:
        grouped[sample.source_language].append((score, sample, prediction))
    if rows:
        mean_score = sum(score.score for score, _, _ in rows) / len(rows)
        session.add(
            AggregateScore(
                evaluator_job_id=job.id,
                metric_name="sentence_mean",
                source_language="",
                value=mean_score,
                sample_count=len(rows),
                unit=rows[0][0].unit,
            )
        )
    language_means: list[float] = []
    for language, language_rows in grouped.items():
        mean_score = sum(score.score for score, _, _ in language_rows) / len(language_rows)
        language_means.append(mean_score)
        session.add(
            AggregateScore(
                evaluator_job_id=job.id,
                metric_name="sentence_mean",
                source_language=language,
                value=mean_score,
                sample_count=len(language_rows),
                unit=language_rows[0][0].unit,
            )
        )
        if evaluator is not None and evaluator.evaluator_type == "sacrebleu_zh":
            inputs = [
                ScoreInput(
                    source_language=sample.source_language,
                    source_text=sample.source_text,
                    reference_zh=sample.reference_zh,
                    translation_zh=prediction.translation_zh,
                )
                for _, sample, prediction in language_rows
            ]
            corpus = evaluator.corpus_aggregates(inputs)
            session.add(
                AggregateScore(
                    evaluator_job_id=job.id,
                    metric_name="corpus_bleu",
                    source_language=language,
                    value=float(corpus["corpus_bleu"]),
                    sample_count=len(inputs),
                    unit="BLEU",
                    details={"signature": corpus["signature"]},
                )
            )
    if language_means:
        session.add(
            AggregateScore(
                evaluator_job_id=job.id,
                metric_name="macro_sentence_mean",
                source_language="",
                value=sum(language_means) / len(language_means),
                sample_count=len(language_means),
                unit=rows[0][0].unit,
            )
        )
    if rows and evaluator is not None and evaluator.evaluator_type == "sacrebleu_zh":
        all_inputs = [
            ScoreInput(
                source_language=sample.source_language,
                source_text=sample.source_text,
                reference_zh=sample.reference_zh,
                translation_zh=prediction.translation_zh,
            )
            for _, sample, prediction in rows
        ]
        corpus = evaluator.corpus_aggregates(all_inputs)
        session.add(
            AggregateScore(
                evaluator_job_id=job.id,
                metric_name="corpus_bleu",
                source_language="",
                value=float(corpus["corpus_bleu"]),
                sample_count=len(rows),
                unit="BLEU",
                details={"signature": corpus["signature"]},
            )
        )


def _finish_parent_statuses(session: Session, job: EvaluatorJob) -> None:
    session.flush()
    dataset_job = job.dataset_job
    evaluator_statuses = list(
        session.scalars(
            select(EvaluatorJob.status).where(EvaluatorJob.dataset_job_id == dataset_job.id)
        )
    )
    if all(status in TERMINAL for status in evaluator_statuses):
        if all(status == "cancelled" for status in evaluator_statuses):
            dataset_job.status = "cancelled"
        elif any(status in {"failed", "partial_failed"} for status in evaluator_statuses):
            dataset_job.status = "partial_failed"
        elif any(status in {"cancelled", "partial_cancelled"} for status in evaluator_statuses):
            dataset_job.status = "partial_cancelled"
        else:
            dataset_job.status = "completed"
    session.flush()
    task = dataset_job.task
    dataset_statuses = list(
        session.scalars(select(DatasetJob.status).where(DatasetJob.task_id == task.id))
    )
    if all(status in TERMINAL for status in dataset_statuses):
        if all(status == "cancelled" for status in dataset_statuses):
            task.status = "cancelled"
        elif any(status in {"failed", "partial_failed"} for status in dataset_statuses):
            task.status = "partial_failed"
        elif any(status in {"cancelled", "partial_cancelled"} for status in dataset_statuses):
            task.status = "partial_cancelled"
        else:
            task.status = "completed"
        task.finished_at = datetime.now(UTC)
        task.submission.status = task.status


async def process_evaluator_job(job_id: str) -> None:
    with SessionLocal() as session:
        job = session.get(EvaluatorJob, job_id)
        if not job or job.status != "queued":
            return
        task = job.dataset_job.task
        if task.cancel_requested or job.dataset_job.cancel_requested:
            _cancel_remaining(session, job)
            job.status = _item_status(_refresh_progress(session, job))
            _store_aggregates(session, job)
            _finish_parent_statuses(session, job)
            session.commit()
            return
        now = datetime.now(UTC)
        job.status = "preprocessing"
        job.dataset_job.status = "running"
        task.status = "running"
        if not task.started_at:
            task.started_at = now
        session.commit()

        revision = job.evaluator_revision
        profile = revision.profile
        prompt = job.prompt_version
        evaluator: BaseEvaluator | None = None
        try:
            evaluator = build_evaluator(
                profile.evaluator_type,
                revision.config,
                system_template=prompt.system_template if prompt else None,
                user_template=prompt.user_template if prompt else None,
            )
            rows = _joined_item_rows(session, job)
            cache: dict[tuple[str, ...], ScoreResult] = {}
            if profile.evaluator_type == "openai_compatible_llm" and not task.force_reevaluate:
                cache = _preload_llm_cache(session, evaluator.model_name, rows)
                for row in rows:
                    cached = cache.get(_cache_key(row, evaluator.model_name))
                    if cached:
                        item: EvaluationItem = row["item"]
                        item.score_result_id = cached.id
                        item.status = "completed"
                        item.cache_hit = True
                        item.finished_at = datetime.now(UTC)
                session.commit()
                rows = [row for row in rows if row["item"].status == "queued"]

            job.status = "running"
            session.commit()
            concurrency = min(
                int(revision.config.get("concurrency", 8)), settings.worker_global_concurrency
            )
            retries = int(revision.config.get("max_retries", 0))
            chunk_size = max(100, concurrency * 8)
            current_results: dict[tuple[str, ...], ScoreResult] = {}

            def should_cancel() -> bool:
                # Read fresh flags without sharing the writer's ORM state with coroutines.
                with SessionLocal() as check:
                    flags = check.execute(
                        select(EvaluationTask.cancel_requested, DatasetJob.cancel_requested)
                        .join(DatasetJob, DatasetJob.task_id == EvaluationTask.id)
                        .where(DatasetJob.id == job.dataset_job_id)
                    ).one()
                    return any(flags)

            for offset in range(0, len(rows), chunk_size):
                if should_cancel():
                    _cancel_remaining(session, job)
                    break
                grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
                for row in rows[offset : offset + chunk_size]:
                    grouped[_cache_key(row, evaluator.model_name)].append(row)
                semaphore = asyncio.Semaphore(concurrency)

                async def run_group(key, group):
                    if key in current_results:
                        return key, group, None, None, 0
                    async with semaphore:
                        output, error, attempts = await _score_with_retry(
                            evaluator, group[0]["input"], retries, should_cancel
                        )
                        return key, group, output, error, attempts

                pending = [asyncio.create_task(run_group(key, group)) for key, group in grouped.items()]
                try:
                    # Persist each finished response before waiting for slower requests.
                    for finished in asyncio.as_completed(pending):
                        key, group, output, error, attempts = await finished
                        cancelled = should_cancel()
                        result = current_results.get(key)
                        reused = result is not None
                        if output is not None and result is None:
                            result = _persist_score(
                                session, evaluator_job=job, evaluator=evaluator,
                                row=group[0], output=output,
                            )
                            current_results[key] = result
                        for index, row in enumerate(group):
                            item = row["item"]
                            item.score_result_id = result.id if result else None
                            item.status = "cancelled" if cancelled else "completed" if result else "failed"
                            item.cache_hit = bool(result) and (reused or index > 0)
                            item.error = None if cancelled or result else error
                            item.attempts = attempts
                            item.finished_at = datetime.now(UTC)
                        _refresh_progress(session, job)
                        session.commit()
                finally:
                    for pending_task in pending:
                        if not pending_task.done():
                            pending_task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                if should_cancel():
                    _cancel_remaining(session, job)
                    break

            job.status = _item_status(_refresh_progress(session, job))
            if job.status in TERMINAL:
                _store_aggregates(session, job, evaluator)
            _finish_parent_statuses(session, job)
            session.commit()
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
            session.execute(
                update(EvaluationItem)
                .where(
                    EvaluationItem.evaluator_job_id == job.id,
                    EvaluationItem.status == "queued",
                )
                .values(status="failed", error=f"评价器任务异常: {exc}")
            )
            _refresh_progress(session, job)
            _finish_parent_statuses(session, job)
            session.commit()
        finally:
            if evaluator is not None:
                await evaluator.close()


async def worker_loop(once: bool = False) -> None:
    while True:
        with SessionLocal() as session:
            job_id = session.scalar(
                select(EvaluatorJob.id)
                .where(EvaluatorJob.status == "queued")
                .order_by(EvaluatorJob.created_at, EvaluatorJob.id)
                .limit(1)
            )
        if not job_id:
            if once:
                return
            await asyncio.sleep(settings.worker_poll_seconds)
            continue
        await process_evaluator_job(job_id)
        if once:
            return


def language_detection_summary(session: Session, dataset_job_id: str) -> dict[str, Any]:
    job = session.get(DatasetJob, dataset_job_id)
    if not job:
        raise ValueError("数据集任务不存在")
    model_run = job.task.submission.model_run
    if not model_run.detects_language:
        return {"applicable": False, "reason": "本次推理已提供源语种"}
    submission_dataset = job.submission_dataset
    rows = session.execute(
        select(Prediction, DatasetSample)
        .join(
            DatasetSample,
            (DatasetSample.dataset_version_id == submission_dataset.dataset_version_id)
            & (DatasetSample.sample_id == Prediction.sample_id),
        )
        .where(Prediction.submission_dataset_id == submission_dataset.id)
    ).all()
    total = len(rows)
    covered = [(prediction, sample) for prediction, sample in rows if prediction.predicted_language]
    correct = sum(
        prediction.predicted_language == sample.source_language
        for prediction, sample in covered
    )
    labels = sorted(
        {sample.source_language for _, sample in rows}
        | {prediction.predicted_language for prediction, _ in covered if prediction.predicted_language}
    )
    confusion: dict[str, dict[str, int]] = {
        actual: {predicted: 0 for predicted in labels} for actual in labels
    }
    for prediction, sample in covered:
        confusion[sample.source_language][prediction.predicted_language or ""] += 1
    by_language = []
    for language in labels:
        true_positive = confusion.get(language, {}).get(language, 0)
        actual_count = sum(confusion.get(language, {}).values())
        predicted_count = sum(row.get(language, 0) for row in confusion.values())
        by_language.append(
            {
                "source_language": language,
                "sample_count": sum(sample.source_language == language for _, sample in rows),
                "covered": actual_count,
                "precision": true_positive / predicted_count if predicted_count else None,
                "recall": true_positive / actual_count if actual_count else None,
            }
        )
    return {
        "applicable": True,
        "total": total,
        "covered": len(covered),
        "coverage": len(covered) / total if total else 0,
        "correct": correct,
        "accuracy": correct / len(covered) if covered else None,
        "by_language": by_language,
        "labels": labels,
        "confusion_matrix": [
            [confusion[actual][predicted] for predicted in labels] for actual in labels
        ],
    }
