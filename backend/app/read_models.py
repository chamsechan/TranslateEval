"""Bounded rebuilds of durable terminal-job summaries and provenance digests.

The cache is derived, never authoritative. SQLite triggers remove it on changes;
an updated_at compare-and-swap also prevents a retry racing a rebuild from
publishing the previous pass. Active jobs continue to use live SQL summaries.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import delete, insert, literal, select
from sqlalchemy.orm import Session

from .models import AggregateScore, DatasetJob, DatasetSample, EvaluationItem, EvaluatorJob, EvaluatorProfile, EvaluatorRevision, Prediction, ScoreResult, SubmissionDataset
from .read_model_schema import BUILD_METRIC, SUMMARY_METRIC
from .score_validity import comparable_item_score, score_maximum, scored_item_condition

TERMINAL_STATUSES = {"completed", "failed", "partial_failed", "partial_cancelled", "cancelled"}


def summary_from_languages(job_id: str, evaluator_type: str, threshold: float, rows: list[dict]) -> dict[str, Any]:
    by_language = []
    for original in rows:
        row = dict(original)
        row["accuracy"] = row["passed"] / row["total"] if row["total"] else 0
        row["coverage"] = row["count"] / row["total"] if row["total"] else 0
        row["unscored"] = row["total"] - row["count"]
        by_language.append(row)
    scored_languages = [row for row in by_language if row["count"]]
    total = sum(row["total"] for row in by_language)
    successful = sum(row["count"] for row in by_language)
    passed = sum(row["passed"] for row in by_language)
    return {
        "evaluator_job_id": job_id, "threshold": threshold, "score_min": 0,
        "score_max": 100 if evaluator_type == "sacrebleu_zh" else 10,
        "unit": "BLEU" if evaluator_type == "sacrebleu_zh" else "point",
        "micro_mean": sum(row["mean"] * row["count"] for row in scored_languages) / successful if successful else None,
        "macro_mean": sum(row["mean"] for row in scored_languages) / len(scored_languages) if scored_languages else None,
        "micro_accuracy": passed / total if total else None,
        "macro_accuracy": sum(row["accuracy"] for row in by_language) / len(by_language) if by_language else None,
        "successful": successful, "total": total, "passed": passed, "unscored": total - successful,
        "failed": sum(row["failed"] for row in by_language),
        "cancelled": sum(row["cancelled"] for row in by_language),
        "coverage": successful / total if total else 0,
        "by_language": by_language,
    }


def _digest_row(digest, value) -> None:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def build_query_summaries(session: Session, job_ids: list[str]) -> dict[str, dict[str, Any]]:
    """One streaming SELECT; memory is bounded by languages and selected jobs.

    No source/translation text, reason, raw response or ScoreResult ORM is loaded.
    Sorting by job and sample ID makes the fingerprints independent of row IDs.
    """
    if not job_ids:
        return {}
    evaluator_type = EvaluatorProfile.evaluator_type
    statement = (
        select(EvaluatorJob.id.label("job_id"), EvaluatorJob.status.label("job_status"), EvaluatorJob.updated_at,
               EvaluatorJob.evaluator_revision_id.label("requested_revision"), EvaluatorJob.prompt_version_id.label("requested_prompt"),
               EvaluatorRevision.default_threshold.label("threshold"), evaluator_type,
               DatasetSample.sample_id, DatasetSample.source_language, EvaluationItem.status,
               comparable_item_score(evaluator_type).label("score"), scored_item_condition(evaluator_type).label("valid"),
               ScoreResult.evaluator_type.label("actual_type"), ScoreResult.evaluator_revision_id.label("actual_revision"),
               ScoreResult.prompt_version_id.label("actual_prompt"), ScoreResult.evaluator_model, ScoreResult.base_url,
               ScoreResult.score_min, ScoreResult.score_max, ScoreResult.unit)
        .select_from(EvaluatorJob)
        .join(EvaluatorRevision, EvaluatorRevision.id == EvaluatorJob.evaluator_revision_id)
        .join(EvaluatorProfile, EvaluatorProfile.id == EvaluatorRevision.profile_id)
        .join(DatasetJob, DatasetJob.id == EvaluatorJob.dataset_job_id)
        .join(SubmissionDataset, SubmissionDataset.id == DatasetJob.submission_dataset_id)
        .outerjoin(DatasetSample, DatasetSample.dataset_version_id == SubmissionDataset.dataset_version_id)
        .outerjoin(Prediction, (Prediction.submission_dataset_id == SubmissionDataset.id) & (Prediction.sample_id == DatasetSample.sample_id))
        .outerjoin(EvaluationItem, (EvaluationItem.evaluator_job_id == EvaluatorJob.id) & (EvaluationItem.prediction_id == Prediction.id))
        .outerjoin(ScoreResult, ScoreResult.id == EvaluationItem.score_result_id)
        .where(EvaluatorJob.id.in_(job_ids))
        .order_by(EvaluatorJob.id, DatasetSample.sample_id)
        .execution_options(yield_per=1000)
    )
    states: dict[str, dict] = {}
    for row in session.execute(statement).mappings():
        state = states.get(row["job_id"])
        if state is None:
            state = states[row["job_id"]] = {
                "languages": {}, "sample_digest": hashlib.sha256(), "source_digest": hashlib.sha256(),
                "matches_requested": True, "evaluator_type": row["evaluator_type"], "threshold": row["threshold"],
                "updated_at": row["updated_at"], "status": row["job_status"],
            }
        if row["sample_id"] is None:
            continue
        language = state["languages"].setdefault(row["source_language"], {
            "source_language": row["source_language"], "total": 0, "count": 0, "sum": 0.0,
            "passed": 0, "failed": 0, "cancelled": 0,
        })
        language["total"] += 1
        language["failed"] += row["status"] == "failed"
        language["cancelled"] += row["status"] == "cancelled"
        if row["valid"]:
            language["count"] += 1
            language["sum"] += row["score"]
            language["passed"] += row["score"] >= row["threshold"]
            _digest_row(state["sample_digest"], row["sample_id"])
            _digest_row(state["source_digest"], [row["sample_id"], row["actual_type"], row["actual_revision"], row["actual_prompt"], row["evaluator_model"], row["base_url"], 0, score_maximum(row["evaluator_type"]), row["unit"]])
            state["matches_requested"] &= row["actual_revision"] == row["requested_revision"] and row["actual_prompt"] == row["requested_prompt"]
    result = {}
    for job_id, state in states.items():
        rows = []
        for language in sorted(state["languages"]):
            row = state["languages"][language]
            score_sum = row.pop("sum")
            row["mean"] = score_sum / row["count"] if row["count"] else None
            rows.append(row)
        result[job_id] = {
            "summary": summary_from_languages(job_id, state["evaluator_type"], state["threshold"], rows),
            "sample_digest": state["sample_digest"].hexdigest(), "source_digest": state["source_digest"].hexdigest(),
            "matches_requested": bool(state["matches_requested"]), "evaluator_type": state["evaluator_type"],
            "status": state["status"], "updated_at": state["updated_at"],
        }
    return result


def begin_summary_build(session: Session, job_ids: list[str]) -> dict[str, str]:
    """A short-lived token is deleted by mutations even if no summary exists."""
    tokens = {job_id: uuid.uuid4().hex for job_id in job_ids}
    if tokens:
        session.execute(delete(AggregateScore).where(AggregateScore.evaluator_job_id.in_(tokens), AggregateScore.metric_name == BUILD_METRIC))
        session.execute(insert(AggregateScore), [dict(evaluator_job_id=job_id, metric_name=BUILD_METRIC, source_language="", value=0,
            sample_count=0, unit="internal", details={"token": token}) for job_id, token in tokens.items()])
    return tokens


def store_query_summaries(session: Session, results: dict[str, dict], tokens: dict[str, str]) -> None:
    """Publish only while the same completed pass is still current; no commit."""
    for job_id, result in results.items():
        if result["status"] not in TERMINAL_STATUSES:
            continue
        details = {key: value for key, value in result.items() if key != "updated_at"}
        details["generation"] = tokens[job_id]
        summary = result["summary"]
        # The write transaction begins before checking the token/deleting a prior
        # derived row. A concurrent retry cannot interleave with this publication.
        marker_table = AggregateScore.__table__.alias("build_marker")
        marker = select(marker_table.c.id).where(marker_table.c.evaluator_job_id == job_id,
            marker_table.c.metric_name == BUILD_METRIC, marker_table.c.details["token"].as_string() == tokens[job_id]).exists()
        session.execute(delete(AggregateScore).where(AggregateScore.evaluator_job_id == job_id, AggregateScore.metric_name == SUMMARY_METRIC, marker).execution_options(synchronize_session=False))
        statement = select(literal(job_id), literal(SUMMARY_METRIC), literal(""), literal(summary["micro_mean"] or 0),
                           literal(summary["successful"]), literal(summary["unit"]), literal(details, type_=AggregateScore.details.type)).select_from(EvaluatorJob).where(
            EvaluatorJob.id == job_id, EvaluatorJob.updated_at == result["updated_at"], EvaluatorJob.status == result["status"],
            marker,
        )
        session.execute(insert(AggregateScore).from_select(
            ["evaluator_job_id", "metric_name", "source_language", "value", "sample_count", "unit", "details"], statement,
        ))
        session.execute(delete(AggregateScore).where(AggregateScore.evaluator_job_id == job_id,
            AggregateScore.metric_name == BUILD_METRIC, AggregateScore.details["token"].as_string() == tokens[job_id]).execution_options(synchronize_session=False))


def refresh_query_summary(session: Session, job: EvaluatorJob) -> dict[str, Any]:
    """Worker hook: call after terminal status/aggregate updates, before commit."""
    session.flush()
    tokens = begin_summary_build(session, [job.id])
    results = build_query_summaries(session, [job.id])
    store_query_summaries(session, results, tokens)
    return results[job.id]["summary"]


def ensure_query_summaries(session: Session, job_ids) -> None:
    """Lazily upgrade historical terminal results once, in bounded batches."""
    cached = select(AggregateScore.evaluator_job_id).where(AggregateScore.metric_name == SUMMARY_METRIC)
    missing = session.scalars(select(EvaluatorJob.id).where(
        EvaluatorJob.id.in_(job_ids), EvaluatorJob.status.in_(TERMINAL_STATUSES), EvaluatorJob.id.not_in(cached),
    )).all()
    for offset in range(0, len(missing), 32):
        batch = missing[offset:offset + 32]
        tokens = begin_summary_build(session, batch)
        session.commit()
        results = build_query_summaries(session, batch)
        store_query_summaries(session, results, tokens)
        session.commit()


def comparison_metadata(session: Session, jobs: list[EvaluatorJob]) -> dict[str, dict]:
    ids = [job.id for job in jobs]
    ensure_query_summaries(session, ids)
    result = dict(session.execute(select(AggregateScore.evaluator_job_id, AggregateScore.details).where(
        AggregateScore.evaluator_job_id.in_(ids), AggregateScore.metric_name == SUMMARY_METRIC,
    )).all())
    missing = [job_id for job_id in ids if job_id not in result]
    result.update(build_query_summaries(session, missing))
    return result


def main() -> None:
    """Optional upgrade maintenance: python -m app.read_models [--rebuild]."""
    import argparse
    import time
    from .database import SessionLocal, optimize_database
    parser = argparse.ArgumentParser(description="预计算历史评测任务摘要，避免首次打开列表/比较时等待。")
    parser.add_argument("--rebuild", action="store_true", help="重新构建已有的派生摘要；不修改原始评分")
    parser.add_argument("--batch-size", type=int, default=4, choices=range(1, 33), metavar="1..32")
    args = parser.parse_args()
    start = time.perf_counter()
    with SessionLocal() as session:
        if args.rebuild:
            session.execute(delete(AggregateScore).where(AggregateScore.metric_name.in_([SUMMARY_METRIC, BUILD_METRIC])))
            session.commit()
        ids = list(session.scalars(select(EvaluatorJob.id).where(EvaluatorJob.status.in_(TERMINAL_STATUSES)).order_by(EvaluatorJob.id)))
        for offset in range(0, len(ids), args.batch_size):
            ensure_query_summaries(session, ids[offset:offset + args.batch_size])
            print(json.dumps({"processed": min(offset + args.batch_size, len(ids)), "total": len(ids), "elapsed_seconds": round(time.perf_counter() - start, 3)}), flush=True)
        optimize_database(session)
        session.commit()


if __name__ == "__main__":
    main()
