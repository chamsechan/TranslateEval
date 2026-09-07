from __future__ import annotations

import asyncio
import random
import time
import uuid
import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update, insert, literal, union_all, exists
from sqlalchemy.orm import Session, load_only

from .config import settings
from .database import SessionLocal, optimize_database
from .evaluators import build_evaluator
from .evaluators.base import (
    BaseEvaluator,
    PermanentEvaluatorError,
    RetriableEvaluatorError,
    ScoreInput,
    ScoreOutput,
    validate_score_output,
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
ITEM_BATCH_SIZE = 512
logger = logging.getLogger(__name__)


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
    if not selections:
        raise ValueError("至少选择一个评价器")
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
        prediction_count = session.scalar(select(func.count(Prediction.id)).where(
            Prediction.submission_dataset_id == submission_dataset.id)) or 0
        dataset_job.total_items = prediction_count * len(selections)
        total += dataset_job.total_items
        for selection in selections:
            evaluator_job = EvaluatorJob(
                dataset_job_id=dataset_job.id,
                evaluator_revision_id=selection.evaluator_revision_id,
                prompt_version_id=selection.prompt_version_id,
                status="queued",
                total_items=prediction_count,
            )
            session.add(evaluator_job)
            session.flush()
            session.execute(insert(EvaluationItem).from_select(
                ["evaluator_job_id", "prediction_id", "status", "cache_hit", "attempts", "created_at"],
                select(literal(evaluator_job.id), Prediction.id, literal("queued"), literal(False),
                       literal(0), literal(datetime.now(UTC))).where(
                           Prediction.submission_dataset_id == submission_dataset.id),
            ))
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
        session.execute(update(EvaluationItem).where(
            EvaluationItem.evaluator_job_id == job.id, EvaluationItem.status == "running",
        ).values(status="queued", finished_at=None))
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


def _joined_item_rows(session: Session, evaluator_job: EvaluatorJob, *, after_id: int = 0,
                      limit: int = ITEM_BATCH_SIZE) -> list[dict[str, Any]]:
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
            EvaluationItem.id > after_id,
        )
        .order_by(EvaluationItem.id)
        .limit(limit)
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
    session: Session, model_name: str, rows: list[dict[str, Any]], *,
    evaluator_type: str = "openai_compatible_llm", job: EvaluatorJob | None = None,
    current_job_only: bool = False,
) -> dict[tuple[str, ...], ScoreResult]:
    requested = list(
        {
            _cache_key(row, model_name)[:4]
            for row in rows
        }
    )
    cache: dict[tuple[str, ...], ScoreResult] = {}
    maximum, unit = (100, "BLEU") if evaluator_type == "sacrebleu_zh" else (10, "point")
    for offset in range(0, len(requested), 150):
        # Correlated equality probes use every cache-index column even before
        # ANALYZE; fetch one latest valid score per key, never its raw response.
        keys = union_all(*[
            select(*(literal(value).label(name) for name, value in zip(
                ("language", "source", "reference", "translation"), key)))
            for key in requested[offset:offset + 150]
        ]).cte("requested_keys")
        latest = select(ScoreResult.id).where(
            ScoreResult.evaluator_type == evaluator_type, ScoreResult.evaluator_model == model_name,
            ScoreResult.source_language == keys.c.language, ScoreResult.source_hash == keys.c.source,
            ScoreResult.reference_hash == keys.c.reference, ScoreResult.translation_hash == keys.c.translation,
            ScoreResult.score_min == 0, ScoreResult.score_max == maximum, ScoreResult.unit == unit,
            ScoreResult.score.between(0, maximum),
        )
        if job is not None and job.evaluator_revision.config.get("cache_policy") == "strict_revision":
            latest = latest.where(ScoreResult.evaluator_revision_id == job.evaluator_revision_id,
                                  ScoreResult.prompt_version_id == job.prompt_version_id,
                                  ScoreResult.base_url == job.evaluator_revision.config.get("base_url", ""))
        if current_job_only:
            assert job is not None
            latest = latest.where(exists(select(EvaluationItem.id).where(
                EvaluationItem.evaluator_job_id == job.id,
                EvaluationItem.score_result_id == ScoreResult.id,
            )))
        latest = latest.order_by(ScoreResult.created_at.desc(), ScoreResult.id.desc()).limit(1).correlate(keys).scalar_subquery()
        results = session.scalars(
            select(ScoreResult).join(keys, ScoreResult.id == latest).options(load_only(
                ScoreResult.id, ScoreResult.source_language, ScoreResult.source_hash,
                ScoreResult.reference_hash, ScoreResult.translation_hash, ScoreResult.evaluator_model,
            ))
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
            output = validate_score_output(await evaluator.evaluate_one(score_input), evaluator.evaluator_type)
            return output, None, attempts
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
    output = validate_score_output(output, evaluator.evaluator_type)
    sample: DatasetSample = row["sample"]
    prediction: Prediction = row["prediction"]
    result = ScoreResult(
        id=str(uuid.uuid4()),
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
    return result


def _refresh_progress(session: Session, job: EvaluatorJob, *, delta: dict[str, int] | None = None) -> dict[str, int]:
    if delta is not None:
        for target in (job, job.dataset_job, job.dataset_job.task):
            for counter in ("completed", "failed", "cached"):
                field = f"{counter}_items"
                setattr(target, field, getattr(target, field) + delta.get(counter, 0))
        return {}
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
    if counts.get("queued") or counts.get("running"):
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
            EvaluationItem.status.in_(["queued", "running"]),
        )
        .values(status="cancelled", finished_at=now)
    )
    job.status = "partial_cancelled" if job.completed_items or job.failed_items else "cancelled"


def _store_aggregates(session: Session, job: EvaluatorJob, evaluator: BaseEvaluator | None = None) -> None:
    from .evaluators.bleu import SacreBleuZhEvaluator
    from .score_validity import comparable_item_score, scored_item_condition
    from .read_models import refresh_query_summary

    session.flush()
    session.execute(delete(AggregateScore).where(AggregateScore.evaluator_job_id == job.id))
    dataset = job.dataset_job.submission_dataset
    kind = job.evaluator_revision.profile.evaluator_type
    corpus_text = (Prediction.translation_zh, DatasetSample.reference_zh) if kind == "sacrebleu_zh" else (literal(""), literal(""))
    rows = session.execute(
        select(comparable_item_score(kind), ScoreResult.unit, DatasetSample.source_language,
               *corpus_text)
        .join(EvaluationItem, EvaluationItem.score_result_id == ScoreResult.id)
        .join(Prediction, EvaluationItem.prediction_id == Prediction.id)
        .join(DatasetSample, (DatasetSample.dataset_version_id == dataset.dataset_version_id)
              & (DatasetSample.sample_id == Prediction.sample_id))
        .where(EvaluationItem.evaluator_job_id == job.id, scored_item_condition(kind))
        .execution_options(yield_per=ITEM_BATCH_SIZE)
    )
    # Only numeric sufficient statistics and one small accumulator per language
    # remain resident. Corpus BLEU is computed from summed n-gram counts, not a
    # mean of batch or sentence BLEU values.
    grouped: dict[str, dict[str, Any]] = {}
    overall = {"sum": 0.0, "count": 0, "statistics": [0] * 10}
    bleu = evaluator if isinstance(evaluator, SacreBleuZhEvaluator) else None
    unit = "BLEU" if kind == "sacrebleu_zh" else "point"
    for score, row_unit, language, translation, reference in rows:
        if evaluator is None and kind == "sacrebleu_zh" and bleu is None:
            bleu = SacreBleuZhEvaluator(job.evaluator_revision.config)
        group = grouped.setdefault(language, {"sum": 0.0, "count": 0, "statistics": [0] * 10})
        unit = row_unit
        for accumulator in (group, overall):
            accumulator["sum"] += score
            accumulator["count"] += 1
        if bleu is not None:
            statistics = bleu.segment_statistics(translation, reference)
            for accumulator in (group, overall):
                accumulator["statistics"] = [a + b for a, b in zip(accumulator["statistics"], statistics)]
    if overall["count"]:
        for language, accumulator in [("", overall), *grouped.items()]:
            session.add(AggregateScore(evaluator_job_id=job.id, metric_name="sentence_mean",
                source_language=language, value=accumulator["sum"] / accumulator["count"],
                sample_count=accumulator["count"], unit=unit))
            if bleu is not None:
                corpus = bleu.corpus_from_statistics(accumulator["statistics"])
                session.add(AggregateScore(evaluator_job_id=job.id, metric_name="corpus_bleu",
                    source_language=language, value=corpus["corpus_bleu"], sample_count=accumulator["count"],
                    unit="BLEU", details={"signature": corpus["signature"]}))
        session.add(AggregateScore(evaluator_job_id=job.id, metric_name="macro_sentence_mean",
            source_language="", value=sum(g["sum"] / g["count"] for g in grouped.values()) / len(grouped),
            sample_count=len(grouped), unit=unit))
    session.flush()
    refresh_query_summary(session, job)


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
        # A conditional claim also protects direct helper callers. Only the
        # singleton CLI worker may recover another process's interrupted work.
        claimed = session.execute(update(EvaluatorJob).where(
            EvaluatorJob.id == job_id, EvaluatorJob.status == "queued",
        ).values(status="preprocessing"))
        if claimed.rowcount != 1:
            session.rollback()
            return
        job = session.get(EvaluatorJob, job_id)
        task = job.dataset_job.task
        evaluator: BaseEvaluator | None = None
        try:
            if task.cancel_requested or job.dataset_job.cancel_requested:
                _cancel_remaining(session, job)
                job.status = _item_status(_refresh_progress(session, job))
                _store_aggregates(session, job)
                _finish_parent_statuses(session, job)
                session.commit()
                return
            now = datetime.now(UTC)
            job.dataset_job.status = "running"
            task.status = "running"
            if not task.started_at:
                task.started_at = now
            session.commit()
            revision = job.evaluator_revision
            kind = revision.profile.evaluator_type
            prompt = job.prompt_version
            evaluator = build_evaluator(kind, revision.config,
                system_template=prompt.system_template if prompt else None,
                user_template=prompt.user_template if prompt else None)
            concurrency = min(int(revision.config.get("concurrency", 32 if kind == "sacrebleu_zh" else 8)),
                              max(1, settings.worker_global_concurrency))
            retries = int(revision.config.get("max_retries", 0))
            # Reconcile once on start/recovery; thereafter each completed batch
            # contributes a delta. There is no per-response full-table count.
            _refresh_progress(session, job)
            job.status = "running"
            session.commit()
            cancellation = {"checked": 0.0, "value": False}

            def should_cancel(force: bool = False) -> bool:
                now = time.monotonic()
                if force or now - cancellation["checked"] >= 0.05:
                    with SessionLocal() as check:
                        flags = check.execute(select(EvaluationTask.cancel_requested, DatasetJob.cancel_requested)
                            .join(DatasetJob, DatasetJob.task_id == EvaluationTask.id)
                            .where(DatasetJob.id == job.dataset_job_id)).one()
                    cancellation.update(checked=now, value=any(flags))
                return cancellation["value"]

            after_id = 0
            while not should_cancel(force=True):
                rows = _joined_item_rows(session, job, after_id=after_id)
                if not rows:
                    break
                after_id = rows[-1]["item"].id
                cache = _preload_llm_cache(session, evaluator.model_name, rows,
                    evaluator_type=kind, job=job,
                    current_job_only=bool(task.force_reevaluate or kind != "openai_compatible_llm"))
                delta = {"completed": 0, "cached": 0}
                groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
                for row in rows:
                    key = _cache_key(row, evaluator.model_name)
                    cached = cache.get(key)
                    if cached is not None:
                        item = row["item"]
                        item.score_result_id, item.status, item.cache_hit = cached.id, "completed", True
                        item.finished_at = datetime.now(UTC)
                        delta["completed"] += 1
                        delta["cached"] += 1
                    else:
                        groups[key].append(row)
                if delta["completed"]:
                    _refresh_progress(session, job, delta=delta)
                    session.commit()
                waiting = iter(groups.values())
                pending: dict[asyncio.Task, list[dict[str, Any]]] = {}
                exhausted = False
                try:
                    while pending or not exhausted:
                        cancelled = should_cancel(force=True)
                        starting = []
                        if not cancelled:
                            for _ in range(concurrency - len(pending)):
                                group = next(waiting, None)
                                if group is None:
                                    exhausted = True
                                    break
                                starting.append(group)
                                for row in group:
                                    row["item"].status = "running"
                        else:
                            exhausted = True
                        if starting:
                            # Only dispatched groups are marked running, never
                            # the rest of the fetched batch waiting for a slot.
                            session.commit()
                            for group in starting:
                                pending[asyncio.create_task(_score_with_retry(
                                    evaluator, group[0]["input"], retries, should_cancel))] = group
                        if not pending:
                            break
                        done, _ = await asyncio.wait(pending, timeout=0.05,
                                                     return_when=asyncio.FIRST_COMPLETED)
                        if not done:
                            continue
                        cancelled = should_cancel(force=True)
                        delta = {"completed": 0, "failed": 0, "cached": 0}
                        for finished in done:
                            group = pending.pop(finished)
                            output, error, attempts = finished.result()
                            result = (_persist_score(session, evaluator_job=job, evaluator=evaluator,
                                      row=group[0], output=output) if output is not None else None)
                            for index, row in enumerate(group):
                                item = row["item"]
                                item.score_result_id = result.id if result else None
                                item.status = "cancelled" if cancelled else "completed" if result else "failed"
                                item.cache_hit = bool(result) and index > 0
                                item.error = None if cancelled or result else error
                                item.attempts = attempts
                                item.finished_at = datetime.now(UTC)
                                if item.status in delta:
                                    delta[item.status] += 1
                                delta["cached"] += int(item.cache_hit)
                        _refresh_progress(session, job, delta=delta)
                        session.commit()
                finally:
                    for pending_task in pending:
                        if not pending_task.done():
                            pending_task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                # All large containers above are replaced at the next bounded
                # fetch. ORM weak references release already committed objects.
            if should_cancel(force=True):
                _cancel_remaining(session, job)
            job.status = _item_status(_refresh_progress(session, job))
            if job.status in TERMINAL:
                _store_aggregates(session, job, evaluator)
            _finish_parent_statuses(session, job)
            session.commit()
        except Exception as exc:
            session.rollback()
            job = session.get(EvaluatorJob, job_id)
            job.status, job.error = "failed", str(exc)
            session.execute(update(EvaluationItem).where(
                EvaluationItem.evaluator_job_id == job.id,
                EvaluationItem.status.in_(["queued", "running"]),
            ).values(status="failed", error=f"评价器任务异常: {exc}", finished_at=datetime.now(UTC)))
            _refresh_progress(session, job)
            _store_aggregates(session, job, evaluator)
            _finish_parent_statuses(session, job)
            session.commit()
        finally:
            if evaluator is not None:
                await evaluator.close()
    # Planning maintenance is optional and owns a separate short transaction.
    # Its failure must never turn a committed successful evaluation into failed.
    try:
        with SessionLocal() as maintenance:
            optimize_database(maintenance)
            maintenance.commit()
    except Exception:
        logger.warning("评分已持久化，数据库查询统计维护失败", exc_info=True)


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
    counts = session.execute(
        select(DatasetSample.source_language, Prediction.predicted_language, func.count(Prediction.id))
        .join(DatasetSample, (DatasetSample.dataset_version_id == submission_dataset.dataset_version_id)
              & (DatasetSample.sample_id == Prediction.sample_id))
        .where(Prediction.submission_dataset_id == submission_dataset.id)
        .group_by(DatasetSample.source_language, Prediction.predicted_language)
    ).all()
    total = sum(count for _, _, count in counts)
    covered = sum(count for _, predicted, count in counts if predicted)
    correct = sum(count for actual, predicted, count in counts if actual == predicted)
    labels = sorted({actual for actual, _, _ in counts} | {predicted for _, predicted, _ in counts if predicted})
    confusion = {actual: {predicted: 0 for predicted in labels} for actual in labels}
    sample_counts: dict[str, int] = defaultdict(int)
    for actual, predicted, count in counts:
        sample_counts[actual] += count
        if predicted:
            confusion[actual][predicted] += count
    by_language = []
    for language in labels:
        true_positive = confusion[language][language]
        actual_count = sum(confusion[language].values())
        predicted_count = sum(row[language] for row in confusion.values())
        sample_count = sample_counts[language]
        by_language.append({
            "source_language": language, "sample_count": sample_count, "covered": actual_count,
            "coverage": actual_count / sample_count if sample_count else None,
            "precision": true_positive / predicted_count if predicted_count else None,
            "recall": true_positive / actual_count if actual_count else None,
            "recall_overall": true_positive / sample_count if sample_count else None,
        })
    return {
        "applicable": True, "total": total, "covered": covered,
        "coverage": covered / total if total else 0, "correct": correct,
        "accuracy": correct / covered if covered else None,
        "accuracy_on_covered": correct / covered if covered else None,
        "accuracy_overall": correct / total if total else None,
        "by_language": by_language, "labels": labels,
        "confusion_matrix": [[confusion[actual][predicted] for predicted in labels] for actual in labels],
    }
