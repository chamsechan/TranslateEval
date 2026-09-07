"""Read models for paginated lists, task notifications and result provenance."""
from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from .validation import validate_threshold
from .models import (
    AggregateScore, DatasetJob, DatasetSample, EvaluationItem, EvaluationTask, EvaluatorJob, EvaluatorRevision,
    InferenceSubmission, ModelRun, Prediction, ScoreResult, SubmissionDataset,
)


TASK_GROUPS = {
    "active": {"queued", "preprocessing", "running", "cancelling"},
    "completed": {"completed"},
    "exception": {"failed", "partial_failed", "partial_cancelled", "cancelled"},
}


def task_load_options():
    datasets = selectinload(EvaluationTask.dataset_jobs)
    return (
        selectinload(EvaluationTask.submission).selectinload(InferenceSubmission.model_run),
        datasets.selectinload(DatasetJob.submission_dataset).selectinload(SubmissionDataset.dataset_version),
        datasets.selectinload(DatasetJob.evaluator_jobs).selectinload(EvaluatorJob.evaluator_revision).selectinload(EvaluatorRevision.profile),
    )


def model_search(query: str):
    return or_(*(column.icontains(query, autoescape=True) for column in (
        ModelRun.run_name, ModelRun.model_family, ModelRun.checkpoint_name, ModelRun.inference_platform,
    )))


def cancelled_item_counts(session: Session, job_ids: list[str]) -> dict[str, int]:
    counts = {}
    for offset in range(0, len(job_ids), 500):
        counts.update(session.execute(
            select(EvaluationItem.evaluator_job_id, func.count(EvaluationItem.id))
            .where(EvaluationItem.evaluator_job_id.in_(job_ids[offset:offset + 500]), EvaluationItem.status == "cancelled")
            .group_by(EvaluationItem.evaluator_job_id)
        ).all())
    return counts


def page_tasks(session: Session, page: int, page_size: int, group: str, query: str, task_id: str | None):
    statement = select(EvaluationTask).join(InferenceSubmission).join(ModelRun)
    if group in TASK_GROUPS:
        statement = statement.where(EvaluationTask.status.in_(TASK_GROUPS[group]))
    if query:
        statement = statement.where(model_search(query))
    if task_id:
        statement = statement.where(EvaluationTask.id == task_id)
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = session.scalars(
        statement.options(*task_load_options())
        .order_by(EvaluationTask.created_at.desc(), EvaluationTask.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    return rows, total


def task_change_version(session: Session) -> str:
    # Small aggregate queries cover changes outside the currently visible page.
    return "|".join(
        str(session.execute(select(func.count(model.id), func.max(model.updated_at))).one())
        for model in (EvaluationTask, DatasetJob, EvaluatorJob)
    )


def comparison_checks(session: Session, jobs: list[EvaluatorJob]) -> dict[str, Any]:
    requested = {
        (job.dataset_job.submission_dataset.dataset_version_id, job.evaluator_revision_id, job.prompt_version_id)
        for job in jobs
    }
    sources: list[dict[str, tuple]] = []
    matches_requested = True
    complete = True
    for job in jobs:
        rows = session.execute(
            select(Prediction.sample_id, ScoreResult)
            .join(EvaluationItem, EvaluationItem.score_result_id == ScoreResult.id)
            .join(Prediction, Prediction.id == EvaluationItem.prediction_id)
            .where(EvaluationItem.evaluator_job_id == job.id, EvaluationItem.status == "completed")
        ).all()
        actual = {}
        for sample_id, score in rows:
            actual[sample_id] = (
                score.evaluator_type, score.evaluator_revision_id, score.prompt_version_id,
                score.evaluator_model, score.base_url, score.score_min, score.score_max, score.unit,
            )
            matches_requested &= (
                score.evaluator_revision_id == job.evaluator_revision_id
                and score.prompt_version_id == job.prompt_version_id
            )
        sources.append(actual)
        expected = job.dataset_job.submission_dataset.dataset_version.sample_count
        complete &= job.status == "completed" and len(actual) == expected and expected > 0
    same_samples = bool(sources and sources[0]) and all(set(row) == set(sources[0]) for row in sources)
    same_sources = same_samples and all(row == sources[0] for row in sources)
    warnings = []
    if len(requested) != 1:
        warnings.append("数据集版本、评价器修订或所选 Prompt 版本不一致")
    if not matches_requested:
        warnings.append("部分缓存评分的实际修订或 Prompt 与本次选择不同")
    if not same_samples:
        warnings.append("成功评分的样本集合不一致或为空")
    elif not same_sources:
        warnings.append("相同样本的实际评分来源或分值尺度不一致")
    if not complete:
        warnings.append("存在未结束任务或评分覆盖不完整，均值仅基于各自成功样本")
    return {
        "requested_config_consistent": len(requested) == 1,
        "actual_sources_consistent": same_sources,
        "actual_sources_match_requested": matches_requested,
        "successful_sample_sets_match": same_samples,
        "strictly_comparable": not warnings,
        "warning": "；".join(warnings) or None,
    }


def threshold_summary(session: Session, job_id: str, threshold: float) -> dict[str, Any]:
    job = session.get(EvaluatorJob, job_id)
    if not job:
        raise ValueError("评价器任务不存在")
    evaluator_type = job.evaluator_revision.profile.evaluator_type
    validate_threshold(evaluator_type, threshold)
    dataset = job.dataset_job.submission_dataset
    successful = (EvaluationItem.status == "completed") & ScoreResult.id.is_not(None)
    rows = session.execute(
        select(
            DatasetSample.source_language,
            func.count(DatasetSample.id).label("total"),
            func.sum(case((successful, 1), else_=0)).label("count"),
            func.avg(case((successful, ScoreResult.score), else_=None)).label("mean"),
            func.sum(case((successful & (ScoreResult.score >= threshold), 1), else_=0)).label("passed"),
            func.sum(case((EvaluationItem.status == "failed", 1), else_=0)).label("failed"),
            func.sum(case((EvaluationItem.status == "cancelled", 1), else_=0)).label("cancelled"),
        )
        .select_from(DatasetSample)
        .outerjoin(Prediction, (Prediction.submission_dataset_id == dataset.id) & (Prediction.sample_id == DatasetSample.sample_id))
        .outerjoin(EvaluationItem, (EvaluationItem.prediction_id == Prediction.id) & (EvaluationItem.evaluator_job_id == job.id))
        .outerjoin(ScoreResult, ScoreResult.id == EvaluationItem.score_result_id)
        .where(DatasetSample.dataset_version_id == dataset.dataset_version_id)
        .group_by(DatasetSample.source_language).order_by(DatasetSample.source_language)
    ).mappings().all()
    by_language = [
        {**row, "accuracy": row["passed"] / row["count"] if row["count"] else None, "coverage": row["count"] / row["total"]}
        for row in rows
    ]
    scored_languages = [row for row in by_language if row["count"]]
    successful_count = sum(row["count"] for row in by_language)
    total = sum(row["total"] for row in by_language)
    aggregates = [
        {"metric_name": row.metric_name, "source_language": row.source_language, "value": row.value,
         "sample_count": row.sample_count, "unit": row.unit, "details": row.details}
        for row in session.scalars(select(AggregateScore).where(AggregateScore.evaluator_job_id == job.id))
    ]
    # Aggregate rows describe a completed scoring pass. Never expose an older
    # pass while retrying, or legacy aggregates with a different sample count.
    aggregate_total = next((row["sample_count"] for row in aggregates
                            if row["metric_name"] == "sentence_mean" and not row["source_language"]), None)
    if job.status in TASK_GROUPS["active"] or aggregate_total != successful_count:
        aggregates = []
    return {
        "evaluator_job_id": job.id, "threshold": threshold, "score_min": 0,
        "score_max": 100 if evaluator_type == "sacrebleu_zh" else 10,
        "unit": "BLEU" if evaluator_type == "sacrebleu_zh" else "point",
        "micro_mean": sum(row["mean"] * row["count"] for row in scored_languages) / successful_count if successful_count else None,
        "macro_mean": sum(row["mean"] for row in scored_languages) / len(scored_languages) if scored_languages else None,
        "micro_accuracy": sum(row["passed"] for row in by_language) / successful_count if successful_count else None,
        "macro_accuracy": sum(row["accuracy"] for row in scored_languages) / len(scored_languages) if scored_languages else None,
        "successful": successful_count, "total": total,
        "failed": sum(row["failed"] for row in by_language),
        "cancelled": sum(row["cancelled"] for row in by_language),
        "coverage": successful_count / total if total else 0,
        "by_language": by_language, "aggregates": aggregates,
    }
