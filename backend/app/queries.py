"""Read models for paginated lists, task notifications and result provenance."""
from __future__ import annotations

from typing import Any

from sqlalchemy import Integer, case, func, or_, select, true, update
from sqlalchemy.orm import Session, selectinload

from .validation import validate_threshold
from .read_model_schema import SUMMARY_METRIC
from .read_models import TERMINAL_STATUSES, comparison_metadata, ensure_query_summaries, summary_from_languages
from .score_validity import comparable_item_score, scored_item_condition
from .models import (
    AggregateScore, DatasetJob, DatasetSample, DatasetVersion, EvaluationItem, EvaluationTask, EvaluatorJob, EvaluatorProfile, EvaluatorRevision,
    InferenceSubmission, Language, ModelRun, Prediction, ScoreResult, SubmissionDataset,
)


TASK_GROUPS = {
    "active": {"queued", "preprocessing", "running", "cancelling"},
    "completed": {"completed"},
    "exception": {"failed", "partial_failed", "partial_cancelled", "cancelled"},
}


def status_sort_value(column):
    """Use the same lifecycle order for job and sample table headers."""
    statuses = ["queued", "preprocessing", "running", "cancelling", "completed",
                "partial_cancelled", "cancelled", "partial_failed", "failed", "unscored"]
    return case({status: rank for rank, status in enumerate(statuses)},
                value=func.coalesce(column, "unscored"), else_=len(statuses))


def table_order(column, direction: str, tie_breaker):
    """Keep missing values last in both directions, and equal values stable."""
    values = (column.desc() if direction == "desc" else column.asc(), tie_breaker.asc())
    return values if getattr(column, "nullable", True) is False else (column.is_(None), *values)


def task_load_options():
    datasets = selectinload(EvaluationTask.dataset_jobs)
    return (
        selectinload(EvaluationTask.submission).selectinload(InferenceSubmission.model_run),
        datasets.selectinload(DatasetJob.submission_dataset).selectinload(SubmissionDataset.dataset_version).selectinload(DatasetVersion.dataset),
        datasets.selectinload(DatasetJob.evaluator_jobs).selectinload(EvaluatorJob.evaluator_revision).selectinload(EvaluatorRevision.profile),
        datasets.selectinload(DatasetJob.evaluator_jobs).selectinload(EvaluatorJob.prompt_version),
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


def task_change_version(session: Session, evaluator_job_id: str | None = None) -> str:
    if evaluator_job_id:
        row = session.execute(select(
            EvaluatorJob.updated_at, DatasetJob.updated_at, EvaluationTask.updated_at,
        ).join(DatasetJob, DatasetJob.id == EvaluatorJob.dataset_job_id)
         .join(EvaluationTask, EvaluationTask.id == DatasetJob.task_id)
         .where(EvaluatorJob.id == evaluator_job_id)).one_or_none()
        return str(row) if row else f"missing:{evaluator_job_id}"
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
    metadata = comparison_metadata(session, jobs)
    sources = []
    matches_requested = True
    complete = True
    for job in jobs:
        actual = metadata[job.id]
        matches_requested &= actual["matches_requested"]
        sources.append(actual)
        expected = job.dataset_job.submission_dataset.dataset_version.sample_count
        complete &= job.status == "completed" and actual["summary"]["successful"] == actual["summary"]["total"] == expected and expected > 0
    same_samples = bool(sources and sources[0]["summary"]["successful"]) and all(row["sample_digest"] == sources[0]["sample_digest"] for row in sources)
    same_sources = same_samples and all(row["source_digest"] == sources[0]["source_digest"] for row in sources)
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


def dataset_language_pairs(session: Session, version_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Read immutable counts; unfilled legacy/test versions retain live fallback."""
    result = {version_id: [] for version_id in version_ids}
    for offset in range(0, len(version_ids), 500):
        ids = version_ids[offset:offset + 500]
        statement = (
            select(DatasetSample.dataset_version_id, DatasetSample.source_language, Language.name_zh,
                   func.count(DatasetSample.id))
            .outerjoin(Language, Language.code == DatasetSample.source_language)
            .group_by(DatasetSample.dataset_version_id, DatasetSample.source_language, Language.name_zh)
        )
        if session.get_bind().dialect.name == "sqlite":
            language_counts = func.json_each(DatasetVersion.language_counts).table_valued("key", "value")
            cached = select(DatasetVersion.id, language_counts.c.key, Language.name_zh, language_counts.c.value.cast(Integer)).select_from(DatasetVersion).join(
                language_counts, true()).outerjoin(Language, Language.code == language_counts.c.key).where(DatasetVersion.id.in_(ids), DatasetVersion.language_counts.is_not(None))
            missing = select(DatasetVersion.id).where(DatasetVersion.id.in_(ids), DatasetVersion.language_counts.is_(None))
            statement = cached.union_all(statement.where(DatasetSample.dataset_version_id.in_(missing)))
        else:
            statement = statement.where(DatasetSample.dataset_version_id.in_(ids))
        rows = session.execute(statement)
        for version_id, language, name, count in rows:
            result[version_id].append({
                "source_language": language, "source_name": name or language,
                "target_language": "zh", "target_name": "中文", "sample_count": count,
            })
    for pairs in result.values():
        pairs.sort(key=lambda row: row["source_language"])
    return result


def live_result_summary_statement(job_ids):
    """Shared SQL aggregation for list values and sorting before pagination.

    ``job_ids`` can be a bounded list or the filtered result-list SELECT, so a
    numeric sort never fetches every matching job/sample into Python.
    """
    score_value = comparable_item_score(EvaluatorProfile.evaluator_type)
    successful = scored_item_condition(EvaluatorProfile.evaluator_type)
    return (
        select(
            EvaluatorJob.id, EvaluatorRevision.default_threshold.label("threshold"),
            EvaluatorProfile.evaluator_type,
            func.count(DatasetSample.id).label("total"),
            func.sum(case((successful, 1), else_=0)).label("successful"),
            func.avg(case((successful, score_value), else_=None)).label("micro_mean"),
            func.sum(case((successful & (score_value >= EvaluatorRevision.default_threshold), 1), else_=0)).label("passed"),
        )
        .select_from(EvaluatorJob)
        .join(EvaluatorRevision, EvaluatorRevision.id == EvaluatorJob.evaluator_revision_id)
        .join(EvaluatorProfile, EvaluatorProfile.id == EvaluatorRevision.profile_id)
        .join(DatasetJob, DatasetJob.id == EvaluatorJob.dataset_job_id)
        .join(SubmissionDataset, SubmissionDataset.id == DatasetJob.submission_dataset_id)
        .outerjoin(DatasetSample, DatasetSample.dataset_version_id == SubmissionDataset.dataset_version_id)
        .outerjoin(Prediction, (Prediction.submission_dataset_id == SubmissionDataset.id)
                   & (Prediction.sample_id == DatasetSample.sample_id))
        .outerjoin(EvaluationItem, (EvaluationItem.prediction_id == Prediction.id)
                   & (EvaluationItem.evaluator_job_id == EvaluatorJob.id))
        .outerjoin(ScoreResult, ScoreResult.id == EvaluationItem.score_result_id)
        .where(EvaluatorJob.id.in_(job_ids))
        .group_by(EvaluatorJob.id, EvaluatorRevision.default_threshold, EvaluatorProfile.evaluator_type)
    )


def result_summary_statement(job_ids):
    """Terminal jobs read one durable row; only active/missing jobs scan samples."""
    details = AggregateScore.details["summary"]
    cached_ids = select(AggregateScore.evaluator_job_id).where(AggregateScore.metric_name == SUMMARY_METRIC)
    cached = select(
        AggregateScore.evaluator_job_id.label("id"), details["threshold"].as_float().label("threshold"),
        AggregateScore.details["evaluator_type"].as_string().label("evaluator_type"),
        details["total"].as_integer().label("total"), details["successful"].as_integer().label("successful"),
        details["micro_mean"].as_float().label("micro_mean"), details["passed"].as_integer().label("passed"),
    ).where(AggregateScore.evaluator_job_id.in_(job_ids), AggregateScore.metric_name == SUMMARY_METRIC)
    live = live_result_summary_statement(job_ids).where(EvaluatorJob.id.not_in(cached_ids))
    return cached.union_all(live)


def result_summaries(session: Session, jobs: list[EvaluatorJob]) -> dict[str, dict[str, Any]]:
    """List summaries use each job's threshold and the same complete denominator as detail."""
    summaries = {}
    ensure_query_summaries(session, [job.id for job in jobs])
    rows = session.execute(result_summary_statement([job.id for job in jobs])).mappings()
    for row in rows:
        total, successful_count, passed = row["total"], row["successful"], row["passed"]
        is_bleu = row["evaluator_type"] == "sacrebleu_zh"
        summaries[row["id"]] = {
            "threshold": row["threshold"], "score_max": 100 if is_bleu else 10,
            "unit": "BLEU" if is_bleu else "point",
            "micro_accuracy": passed / total if total else None, "micro_mean": row["micro_mean"],
            "passed": passed, "total": total, "successful": successful_count,
            "unscored": total - successful_count, "coverage": successful_count / total if total else 0,
        }
    return summaries


def threshold_summary(session: Session, job_id: str, threshold: float, *, _allow_cache: bool = True) -> dict[str, Any]:
    job = session.get(EvaluatorJob, job_id)
    if not job:
        raise ValueError("评价器任务不存在")
    evaluator_type = job.evaluator_revision.profile.evaluator_type
    validate_threshold(evaluator_type, threshold)
    dataset = job.dataset_job.submission_dataset
    successful = scored_item_condition(evaluator_type)
    score_value = comparable_item_score(evaluator_type)
    if _allow_cache and job.status in TERMINAL_STATUSES:
        ensure_query_summaries(session, [job.id])
        cached = session.scalar(select(AggregateScore.details).where(
            AggregateScore.evaluator_job_id == job.id, AggregateScore.metric_name == SUMMARY_METRIC,
        ))
        if cached:
            if threshold == cached["summary"]["threshold"]:
                result = dict(cached["summary"])
            else:
                key = str(float(threshold))
                thresholds = dict(cached.get("thresholds", {}))
                passed = thresholds.get(key)
                if passed is None:
                    passed = dict(session.execute(
                        select(DatasetSample.source_language, func.count())
                        .select_from(EvaluationItem)
                        .join(Prediction, Prediction.id == EvaluationItem.prediction_id)
                        .join(DatasetSample, (DatasetSample.dataset_version_id == dataset.dataset_version_id) & (DatasetSample.sample_id == Prediction.sample_id))
                        .join(ScoreResult, ScoreResult.id == EvaluationItem.score_result_id)
                        .where(EvaluationItem.evaluator_job_id == job.id, successful, score_value >= threshold)
                        .group_by(DatasetSample.source_language)
                    ).all())
                    thresholds[key] = passed
                    # Keep the exact default summary plus at most eight dynamic
                    # thresholds; arbitrary real-valued thresholds stay exact.
                    thresholds = dict(list(thresholds.items())[-8:])
                    published = session.execute(update(AggregateScore).where(
                        AggregateScore.evaluator_job_id == job.id, AggregateScore.metric_name == SUMMARY_METRIC,
                        AggregateScore.details["generation"].as_string() == cached["generation"],
                    ).values(details={**cached, "thresholds": thresholds}).execution_options(synchronize_session=False))
                    session.commit()
                    if published.rowcount != 1:
                        # A retry/repair changed the pass while its new threshold
                        # was counted. Recompute every field from one live SQL
                        # snapshot instead of mixing old coverage with new passes.
                        session.expire_all()
                        return threshold_summary(session, job_id, threshold, _allow_cache=False)
                result = summary_from_languages(job.id, evaluator_type, threshold, [
                    {**row, "passed": passed.get(row["source_language"], 0)} for row in cached["summary"]["by_language"]
                ])
            return {**result, "aggregates": public_aggregates(session, job, result["successful"])}
    rows = session.execute(
        select(
            DatasetSample.source_language,
            func.count(DatasetSample.id).label("total"),
            func.sum(case((successful, 1), else_=0)).label("count"),
            func.avg(case((successful, score_value), else_=None)).label("mean"),
            func.sum(case((successful & (score_value >= threshold), 1), else_=0)).label("passed"),
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
    result = summary_from_languages(job.id, evaluator_type, threshold, rows)
    return {**result, "aggregates": public_aggregates(session, job, result["successful"])}


def public_aggregates(session: Session, job: EvaluatorJob, successful_count: int) -> list[dict]:
    aggregates = [
        {"metric_name": row.metric_name, "source_language": row.source_language, "value": row.value,
         "sample_count": row.sample_count, "unit": row.unit, "details": row.details}
        for row in session.scalars(select(AggregateScore).where(AggregateScore.evaluator_job_id == job.id,
            ~AggregateScore.metric_name.startswith("query_summary_")))
    ]
    # Aggregate rows describe a completed scoring pass. Never expose an older
    # pass while retrying, or legacy aggregates with a different sample count.
    aggregate_total = next((row["sample_count"] for row in aggregates
                            if row["metric_name"] == "sentence_mean" and not row["source_language"]), None)
    if job.status in TASK_GROUPS["active"] or aggregate_total != successful_count:
        aggregates = []
    return aggregates
