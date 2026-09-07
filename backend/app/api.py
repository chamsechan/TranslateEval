from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, load_only, selectinload

from .config import settings
from .database import SessionLocal, get_session
from .evaluators import validate_evaluator_config
from .evaluators.llm import check_openai_compatible_connection
from .importers import (
    ImportValidationError,
    commit_dataset_import,
    commit_submission_import,
    validate_dataset_import,
    validate_submission_import,
)
from .models import (
    Dataset,
    DatasetJob,
    DatasetSample,
    DatasetVersion,
    EvaluationItem,
    EvaluationTask,
    EvaluatorJob,
    EvaluatorProfile,
    EvaluatorRevision,
    ImportValidationReport,
    InferenceSubmission,
    ModelRun,
    Prediction,
    PromptProfile,
    PromptVersion,
    ScoreResult,
    SubmissionDataset,
)
from .validation import validate_threshold
from .read_models import ensure_query_summaries
from .queries import TASK_GROUPS, cancelled_item_counts, comparable_item_score, comparison_checks, dataset_language_pairs, model_search, page_tasks, result_summaries, result_summary_statement, scored_item_condition, status_sort_value, table_order, task_change_version, task_load_options, threshold_summary
from .queue import (
    JobStateConflict,
    cancel_dataset_job,
    cancel_task,
    create_evaluation_task,
    language_detection_summary,
    retry_failed_job,
)
from .schemas import (
    CompareResultsRequest,
    CommitSubmissionRequest,
    EvaluatorProfileCreate,
    EvaluatorProfileUpdate,
    EvaluatorRevisionCreate,
    PathImportRequest,
    PromptProfileCreate,
    PromptVersionCreate,
)


router = APIRouter(prefix="/api")


def utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def worker_health() -> dict[str, str | None]:
    path = settings.worker_heartbeat_file
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except FileNotFoundError:
        return {"status": "disconnected", "last_seen": None}
    age = (datetime.now(UTC) - modified).total_seconds()
    return {
        "status": "connected" if age <= 6 else "disconnected",
        "last_seen": utc_iso(modified),
    }


def fail(status_code: int, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


def report_dict(record: ImportValidationReport) -> dict[str, Any]:
    return {
        "id": record.id,
        "kind": record.kind,
        "status": record.status,
        "manifest": record.manifest,
        "report": record.report,
        "created_at": utc_iso(record.created_at),
    }


def mask_config(config: dict[str, Any]) -> dict[str, Any]:
    masked = dict(config)
    if "api_key" in masked:
        value = str(masked["api_key"])
        masked["api_key"] = "••••••••" + (value[-4:] if len(value) >= 4 else "")
        masked["has_api_key"] = bool(value)
    return masked


def evaluator_profile_dict(profile: EvaluatorProfile) -> dict[str, Any]:
    revisions = sorted(profile.revisions, key=lambda item: item.revision, reverse=True)
    return {
        "id": profile.id,
        "name": profile.name,
        "evaluator_type": profile.evaluator_type,
        "enabled": profile.enabled,
        "created_at": utc_iso(profile.created_at),
        "revisions": [
            {
                "id": item.id,
                "revision": item.revision,
                "config": mask_config(item.config),
                "default_threshold": item.default_threshold,
                "created_at": utc_iso(item.created_at),
            }
            for item in revisions
        ],
    }


def prompt_deletion_status(
    session: Session, version_ids: list[str]
) -> dict[str, dict[str, Any]]:
    result_ids = set(
        session.scalars(
            select(ScoreResult.prompt_version_id)
            .where(ScoreResult.prompt_version_id.in_(version_ids))
            .union(
                select(EvaluatorJob.prompt_version_id)
                .join(EvaluationItem, EvaluationItem.evaluator_job_id == EvaluatorJob.id)
                .where(
                    EvaluatorJob.prompt_version_id.in_(version_ids),
                    EvaluationItem.score_result_id.is_not(None),
                )
            )
        )
    )
    task_ids = set(
        session.scalars(
            select(EvaluatorJob.prompt_version_id).where(
                EvaluatorJob.prompt_version_id.in_(version_ids)
            )
        )
    )
    return {
        version_id: {
            "has_results": version_id in result_ids,
            "can_delete": version_id not in result_ids | task_ids,
            "delete_block_reason": (
                "已有评测结果，无法删除"
                if version_id in result_ids
                else "已有评测任务引用，无法删除"
                if version_id in task_ids
                else None
            ),
        }
        for version_id in version_ids
    }


def next_prompt_version_label(profile: PromptProfile, requested: str = "") -> str:
    if requested.strip():
        return requested.strip()
    date_label = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    sequence = 0
    for version in profile.versions:
        prefix, _, suffix = version.version_label.rpartition(".")
        if prefix == date_label and suffix.isascii() and suffix.isdigit():
            sequence = max(sequence, int(suffix))
    return f"{date_label}.{sequence + 1}"


def prompt_profile_dict(profile: PromptProfile, session: Session) -> dict[str, Any]:
    statuses = prompt_deletion_status(session, [item.id for item in profile.versions])
    has_results = any(status["has_results"] for status in statuses.values())
    blocked = [status["delete_block_reason"] for status in statuses.values() if not status["can_delete"]]
    return {
        "id": profile.id,
        "name": profile.name,
        "description": profile.description,
        "created_at": utc_iso(profile.created_at),
        "has_results": has_results,
        "can_delete": not blocked,
        "delete_block_reason": "已有评测结果，无法删除" if has_results else next(iter(blocked), None),
        "versions": [
            {
                "id": item.id,
                "version": item.version,
                "version_label": item.version_label,
                "system_template": item.system_template,
                "user_template": item.user_template,
                "published": item.published,
                "created_at": utc_iso(item.created_at),
                **statuses[item.id],
            }
            for item in sorted(profile.versions, key=lambda row: row.version, reverse=True)
        ],
    }


def task_dict(task: EvaluationTask, cancelled_counts: dict[str, int]) -> dict[str, Any]:
    model = task.submission.model_run
    dataset_jobs = []
    for dataset_job in sorted(task.dataset_jobs, key=lambda item: item.created_at):
        evaluator_jobs = []
        for evaluator_job in sorted(dataset_job.evaluator_jobs, key=lambda item: item.created_at):
            revision = evaluator_job.evaluator_revision
            cancelled_items = cancelled_counts.get(evaluator_job.id, 0)
            evaluator_jobs.append(
                {
                    "id": evaluator_job.id,
                    "name": revision.profile.name,
                    "evaluator_type": revision.profile.evaluator_type,
                    "revision": revision.revision,
                    "prompt_version_id": evaluator_job.prompt_version_id,
                    "prompt_version_label": evaluator_job.prompt_version.version_label if evaluator_job.prompt_version else None,
                    "status": evaluator_job.status,
                    "total_items": evaluator_job.total_items,
                    "completed_items": evaluator_job.completed_items,
                    "cached_items": evaluator_job.cached_items,
                    "failed_items": evaluator_job.failed_items,
                    "cancelled_items": cancelled_items,
                    "default_threshold": revision.default_threshold,
                    "error": evaluator_job.error,
                }
            )
        dataset_jobs.append(
            {
                "id": dataset_job.id,
                "dataset_key": dataset_job.submission_dataset.dataset_key,
                "dataset_id": dataset_job.submission_dataset.dataset_version.dataset_id,
                "dataset_version_id": dataset_job.submission_dataset.dataset_version_id,
                "dataset_name": dataset_job.submission_dataset.dataset_version.dataset.name,
                "version_label": dataset_job.submission_dataset.dataset_version.version_label,
                "status": dataset_job.status,
                "total_items": dataset_job.total_items,
                "completed_items": dataset_job.completed_items,
                "cached_items": dataset_job.cached_items,
                "failed_items": dataset_job.failed_items,
                "cancelled_items": sum(item["cancelled_items"] for item in evaluator_jobs),
                "cancel_requested": dataset_job.cancel_requested,
                "evaluator_jobs": evaluator_jobs,
            }
        )
    return {
        "id": task.id,
        "submission_id": task.submission_id,
        "run_name": model.run_name,
        "model_family": model.model_family,
        "checkpoint_name": model.checkpoint_name,
        "platform": model.inference_platform,
        "status": task.status,
        "force_reevaluate": task.force_reevaluate,
        "total_items": task.total_items,
        "completed_items": task.completed_items,
        "cached_items": task.cached_items,
        "failed_items": task.failed_items,
        "cancel_requested": task.cancel_requested,
        "cancelled_items": sum(item["cancelled_items"] for item in dataset_jobs),
        "created_at": utc_iso(task.created_at),
        "started_at": utc_iso(task.started_at),
        "finished_at": utc_iso(task.finished_at),
        "dataset_jobs": dataset_jobs,
    }


def task_dicts(session: Session, tasks: list[EvaluationTask]) -> list[dict[str, Any]]:
    job_ids = [job.id for task in tasks for dataset in task.dataset_jobs for job in dataset.evaluator_jobs]
    counts = cancelled_item_counts(session, job_ids)
    return [task_dict(task, counts) for task in tasks]


@router.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "worker": worker_health()}


@router.get("/dashboard")
def dashboard(session: Session = Depends(get_session)) -> dict[str, Any]:
    active_statuses = ["queued", "preprocessing", "running", "cancelling"]
    return {
        "datasets": session.scalar(select(func.count(Dataset.id))) or 0,
        "dataset_versions": session.scalar(select(func.count(DatasetVersion.id))) or 0,
        "model_runs": session.scalar(select(func.count(ModelRun.id))) or 0,
        "active_tasks": session.scalar(
            select(func.count(EvaluationTask.id)).where(EvaluationTask.status.in_(active_statuses))
        )
        or 0,
        "completed_tasks": session.scalar(
            select(func.count(EvaluationTask.id)).where(EvaluationTask.status == "completed")
        )
        or 0,
    }


@router.post("/dataset-imports/validate")
def validate_dataset(
    body: PathImportRequest, session: Session = Depends(get_session)
) -> dict[str, Any]:
    try:
        return report_dict(validate_dataset_import(session, body.path))
    except ImportValidationError as exc:
        raise fail(400, str(exc)) from exc


@router.post("/dataset-imports/validate-upload")
def validate_dataset_upload(
    file: UploadFile = File(...), session: Session = Depends(get_session)
) -> dict[str, Any]:
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise fail(400, "请上传 .zip 文件")
    settings.import_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".zip", dir=settings.import_dir, delete=False) as handle:
        shutil.copyfileobj(file.file, handle)
        temp_path = Path(handle.name)
    try:
        return report_dict(validate_dataset_import(session, temp_path))
    except ImportValidationError as exc:
        raise fail(400, str(exc)) from exc
    finally:
        temp_path.unlink(missing_ok=True)


@router.post("/dataset-imports/{report_id}/commit")
def commit_dataset(report_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        version = commit_dataset_import(session, report_id)
        return {"dataset_version_id": version.id, "content_sha256": version.content_sha256}
    except (ImportValidationError, IntegrityError) as exc:
        session.rollback()
        raise fail(400, str(exc)) from exc


@router.get("/import-reports/{report_id}")
def get_import_report(report_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    report = session.get(ImportValidationReport, report_id)
    if not report:
        raise fail(404, "校验记录不存在")
    return report_dict(report)


def dataset_version_dict(version: DatasetVersion, language_pairs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": version.id,
        "dataset_id": version.dataset_id,
        "dataset_key": version.dataset.key,
        "dataset_name": version.dataset.name,
        "version_label": version.version_label,
        "change_note": version.change_note,
        "content_sha256": version.content_sha256,
        "sample_count": version.sample_count,
        "source_languages": version.source_languages,
        "language_pairs": language_pairs,
        "created_at": utc_iso(version.created_at),
    }


@router.get("/datasets")
def list_datasets(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    datasets = session.scalars(select(Dataset).options(selectinload(Dataset.versions)).order_by(Dataset.name)).all()
    latest_versions = {
        dataset.id: max(dataset.versions, key=lambda item: (item.created_at, item.id), default=None)
        for dataset in datasets
    }
    pairs = dataset_language_pairs(session, [version.id for version in latest_versions.values() if version])
    result = []
    for dataset in datasets:
        latest = latest_versions[dataset.id]
        result.append(
            {
                "id": dataset.id,
                "key": dataset.key,
                "name": dataset.name,
                "description": dataset.description,
                "version_count": len(dataset.versions),
                "latest_version": dataset_version_dict(latest, pairs[latest.id]) if latest else None,
            }
        )
    return result


@router.get("/datasets/{dataset_id}/versions")
def list_dataset_versions(
    dataset_id: str, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise fail(404, "数据集不存在")
    versions = sorted(dataset.versions, key=lambda row: (row.created_at, row.id), reverse=True)
    pairs = dataset_language_pairs(session, [version.id for version in versions])
    return [dataset_version_dict(version, pairs[version.id]) for version in versions]


@router.get("/dataset-versions/{version_id}")
def dataset_version_detail(version_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    version = session.get(DatasetVersion, version_id)
    if not version:
        raise fail(404, "数据集版本不存在")
    return dataset_version_dict(version, dataset_language_pairs(session, [version_id])[version_id])


@router.get("/dataset-versions/{version_id}/samples")
def list_dataset_samples(
    version_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    language: str | None = None,
    session: Session = Depends(get_session),
    sample_id: str | None = None,
    sort: Literal["id", "sample_id", "source_language", "source_text", "reference_zh"] = "id",
    direction: Literal["asc", "desc"] = "asc",
) -> dict[str, Any]:
    query = select(DatasetSample).where(DatasetSample.dataset_version_id == version_id)
    count_query = select(func.count(DatasetSample.id)).where(
        DatasetSample.dataset_version_id == version_id
    )
    if language:
        query = query.where(DatasetSample.source_language == language)
        count_query = count_query.where(DatasetSample.source_language == language)
    if sample_id is not None:
        query = query.where(DatasetSample.sample_id == sample_id)
        count_query = count_query.where(DatasetSample.sample_id == sample_id)
    sort_columns = {"id": DatasetSample.id, "sample_id": DatasetSample.sample_id,
                    "source_language": DatasetSample.source_language, "source_text": DatasetSample.source_text,
                    "reference_zh": DatasetSample.reference_zh}
    rows = session.scalars(query.order_by(*table_order(sort_columns[sort], direction, DatasetSample.id))
                           .offset((page - 1) * page_size).limit(page_size))
    return {
        "total": session.scalar(count_query) or 0,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "sample_id": item.sample_id,
                "source_language": item.source_language,
                "source_text": item.source_text,
                "reference_zh": item.reference_zh,
            }
            for item in rows
        ],
    }


@router.post("/submission-imports/validate")
def validate_submission(
    body: PathImportRequest, session: Session = Depends(get_session)
) -> dict[str, Any]:
    try:
        return report_dict(
            validate_submission_import(session, body.path, body.version_overrides)
        )
    except ImportValidationError as exc:
        raise fail(400, str(exc)) from exc


@router.post("/submission-imports/validate-upload")
def validate_submission_upload(
    file: UploadFile = File(...), session: Session = Depends(get_session)
) -> dict[str, Any]:
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise fail(400, "请上传 .zip 文件")
    settings.import_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".zip", dir=settings.import_dir, delete=False) as handle:
        shutil.copyfileobj(file.file, handle)
        temp_path = Path(handle.name)
    try:
        return report_dict(validate_submission_import(session, temp_path))
    except ImportValidationError as exc:
        raise fail(400, str(exc)) from exc
    finally:
        temp_path.unlink(missing_ok=True)


@router.post("/submission-imports/{report_id}/commit")
def commit_submission(
    report_id: str,
    body: CommitSubmissionRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        report = session.get(ImportValidationReport, report_id)
        if report and report.kind == "submission":
            existing_task_id = report.report.get("task_id")
            if existing_task_id and session.get(EvaluationTask, existing_task_id):
                return {
                    "submission_id": report.report.get("submission_id"),
                    "task_id": existing_task_id,
                }

        submission = commit_submission_import(session, report_id, commit=False)
        # The atomic import claim may have waited for another request. Its
        # refreshed report is authoritative even if this Session read it earlier.
        report = session.get(ImportValidationReport, report_id)
        if report and report.report.get("task_id"):
            existing_task_id = report.report["task_id"]
            if session.get(EvaluationTask, existing_task_id):
                return {"submission_id": submission.id, "task_id": existing_task_id}
        task = create_evaluation_task(
            session,
            submission,
            body.evaluators,
            body.force_reevaluate,
            commit=False,
        )
        if report is None:
            raise ImportValidationError("找不到推理结果校验记录")
        report.report = {
            **report.report,
            "submission_id": submission.id,
            "task_id": task.id,
        }
        session.commit()
        return {"submission_id": submission.id, "task_id": task.id}
    except (ImportValidationError, ValueError, IntegrityError) as exc:
        session.rollback()
        raise fail(400, str(exc)) from exc


@router.get("/submissions/{submission_id}")
def submission_detail(submission_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    submission = session.get(InferenceSubmission, submission_id)
    if not submission:
        raise fail(404, "推理提交不存在")
    model = submission.model_run
    inference = model.result_info.get("inference", {})
    return {
        "id": submission.id,
        "summary": {
            "run_name": model.run_name, "model_family": model.model_family,
            "device": inference.get("device", ""),
            "sdk": inference.get("sdk", ""), "sdk_version": inference.get("sdk_version", ""),
            "precision": inference.get("precision", ""), "mode": model.inference_mode,
            "platform": model.inference_platform, "dataset_count": len(submission.datasets),
            "prediction_count": sum(dataset.prediction_count for dataset in submission.datasets),
        },
        "datasets": [
            {"dataset_key": dataset.dataset_key, "version_label": dataset.dataset_version.version_label,
             "prediction_count": dataset.prediction_count}
            for dataset in submission.datasets
        ],
    }


@router.post("/submissions/{submission_id}/evaluations")
def evaluate_submission(
    submission_id: str, body: CommitSubmissionRequest, session: Session = Depends(get_session),
) -> dict[str, str]:
    submission = session.get(InferenceSubmission, submission_id)
    if not submission:
        raise fail(404, "推理提交不存在")
    try:
        task = create_evaluation_task(session, submission, body.evaluators, body.force_reevaluate)
        return {"submission_id": submission.id, "task_id": task.id}
    except (ValueError, IntegrityError) as exc:
        session.rollback()
        raise fail(400, str(exc)) from exc


@router.get("/tasks")
def list_tasks(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    query = select(EvaluationTask).options(*task_load_options()).order_by(EvaluationTask.created_at.desc(), EvaluationTask.id.desc()).limit(limit)
    if status:
        query = query.where(EvaluationTask.status == status)
    return task_dicts(session, list(session.scalars(query)))


@router.get("/tasks/page")
def paginated_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    group: Literal["all", "active", "completed", "exception"] = "all",
    q: str = "",
    task_id: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rows, total = page_tasks(session, page, page_size, group, q, task_id)
    return {"items": task_dicts(session, rows), "total": total, "page": page, "page_size": page_size}


@router.get("/tasks/changes")
async def task_changes(evaluator_job_id: str | None = None) -> StreamingResponse:
    def read_version():
        with SessionLocal() as session:
            return task_change_version(session, evaluator_job_id)

    async def stream():
        previous = None
        while True:
            version = await asyncio.to_thread(read_version)
            if version != previous:
                yield f"event: tasks-changed\ndata: {json.dumps({'version': version})}\n\n"
                previous = version
            else:
                yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/tasks/events")
async def task_events() -> StreamingResponse:
    def read_payload():
        with SessionLocal() as session:
            tasks = list(session.scalars(select(EvaluationTask).options(*task_load_options())
                .order_by(EvaluationTask.created_at.desc()).limit(50)))
            return json.dumps(task_dicts(session, tasks), ensure_ascii=False)

    async def stream():
        previous = ""
        while True:
            payload = await asyncio.to_thread(read_payload)
            if payload != previous:
                yield f"event: tasks\ndata: {payload}\n\n"
                previous = payload
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/tasks/{task_id}")
def get_task(task_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    task = session.get(EvaluationTask, task_id)
    if not task:
        raise fail(404, "任务不存在")
    return task_dicts(session, [task])[0]


@router.post("/tasks/{task_id}/cancel")
def api_cancel_task(task_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        return task_dicts(session, [cancel_task(session, task_id)])[0]
    except ValueError as exc:
        raise fail(404, str(exc)) from exc


@router.post("/dataset-jobs/{job_id}/cancel")
def api_cancel_dataset_job(job_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        job = cancel_dataset_job(session, job_id)
        return {"id": job.id, "status": job.status, "cancel_requested": job.cancel_requested}
    except ValueError as exc:
        raise fail(404, str(exc)) from exc


@router.post("/evaluator-jobs/{job_id}/retry-failed")
def api_retry_failed_job(job_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        job = retry_failed_job(session, job_id)
        return {"id": job.id, "status": job.status}
    except JobStateConflict as exc:
        raise fail(409, str(exc)) from exc
    except ValueError as exc:
        raise fail(404 if "不存在" in str(exc) else 400, str(exc)) from exc


@router.get("/evaluator-jobs/{job_id}")
def evaluator_job_detail(
    job_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    job = session.get(EvaluatorJob, job_id)
    if not job:
        raise fail(404, "评价器任务不存在")
    dataset_job = job.dataset_job
    task = dataset_job.task
    model = task.submission.model_run
    revision = job.evaluator_revision
    return {
        "task": {
            "id": task.id,
            "submission_id": task.submission_id,
            "run_name": model.run_name,
            "model_family": model.model_family,
            "checkpoint_name": model.checkpoint_name,
            "created_at": utc_iso(task.created_at),
        },
        "dataset": {
            "id": dataset_job.id,
            "dataset_key": dataset_job.submission_dataset.dataset_key,
            "dataset_id": dataset_job.submission_dataset.dataset_version.dataset_id,
            "dataset_version_id": dataset_job.submission_dataset.dataset_version_id,
            "dataset_name": dataset_job.submission_dataset.dataset_version.dataset.name,
            "version_label": dataset_job.submission_dataset.dataset_version.version_label,
        },
        "evaluator": {
            "id": job.id,
            "name": revision.profile.name,
            "evaluator_type": revision.profile.evaluator_type,
            "revision": revision.revision,
            "prompt_version_id": job.prompt_version_id,
            "prompt_version_label": job.prompt_version.version_label if job.prompt_version else None,
            "status": job.status,
            "total_items": job.total_items,
            "completed_items": job.completed_items,
            "cached_items": job.cached_items,
            "failed_items": job.failed_items,
            "cancelled_items": cancelled_item_counts(session, [job.id]).get(job.id, 0),
            "default_threshold": revision.default_threshold,
            "error": job.error,
        },
    }


@router.get("/evaluator-jobs/{job_id}/summary")
def evaluator_job_summary(
    job_id: str,
    threshold: float,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        return threshold_summary(session, job_id, threshold)
    except ValueError as exc:
        raise fail(404 if "不存在" in str(exc) else 400, str(exc)) from exc


@router.get("/dataset-jobs/{job_id}/language-detection-summary")
def api_language_detection_summary(
    job_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    try:
        return language_detection_summary(session, job_id)
    except ValueError as exc:
        raise fail(404, str(exc)) from exc


@router.get("/results/page")
def paginated_results(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    q: str = "", session: Session = Depends(get_session),
    dataset_version_id: str | None = None,
    status_group: Literal["all", "completed", "active", "exception"] = "all",
    sort: Literal["default", "run_name", "dataset", "micro_accuracy", "micro_mean", "status", "created_at"] = "default",
    direction: Literal["asc", "desc"] = "asc",
) -> dict[str, Any]:
    statement = (
        select(EvaluatorJob).join(DatasetJob).join(EvaluationTask).join(InferenceSubmission).join(ModelRun)
        .join(SubmissionDataset, SubmissionDataset.id == DatasetJob.submission_dataset_id)
        .join(DatasetVersion, DatasetVersion.id == SubmissionDataset.dataset_version_id)
        .join(Dataset, Dataset.id == DatasetVersion.dataset_id)
    )
    if q:
        statement = statement.where(or_(model_search(q), *(
            column.icontains(q, autoescape=True)
            for column in (Dataset.key, Dataset.name, DatasetVersion.version_label)
        )))
    if dataset_version_id:
        statement = statement.where(SubmissionDataset.dataset_version_id == dataset_version_id)
    if status_group in TASK_GROUPS:
        statement = statement.where(EvaluatorJob.status.in_(TASK_GROUPS[status_group]))
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    order = (EvaluationTask.created_at.desc(), EvaluatorJob.id.desc())
    if sort != "default":
        if sort in {"micro_accuracy", "micro_mean"}:
            ensure_query_summaries(session, statement.with_only_columns(EvaluatorJob.id))
            summary = result_summary_statement(statement.with_only_columns(EvaluatorJob.id)).subquery()
            statement = statement.join(summary, summary.c.id == EvaluatorJob.id)
            sort_column = (summary.c.passed * 1.0 / func.nullif(summary.c.total, 0)
                           if sort == "micro_accuracy" else summary.c.micro_mean)
        else:
            sort_column = {"run_name": ModelRun.run_name, "dataset": Dataset.key,
                           "status": status_sort_value(EvaluatorJob.status),
                           "created_at": EvaluationTask.created_at}[sort]
        order = table_order(sort_column, direction, EvaluatorJob.id)
        if sort == "dataset":
            order = (*table_order(Dataset.key, direction, EvaluatorJob.id)[:-1],
                     *table_order(DatasetVersion.version_label, direction, EvaluatorJob.id))
    jobs = session.scalars(statement.order_by(*order).offset((page - 1) * page_size).limit(page_size)).all()
    # Fetch all parent relationships in batches, shared by the detail serializers below.
    if jobs:
        task_ids = select(DatasetJob.task_id).where(DatasetJob.id.in_({job.dataset_job_id for job in jobs}))
        parents = session.scalars(select(EvaluationTask).where(EvaluationTask.id.in_(task_ids)).options(*task_load_options())).all()
        by_job = {
            evaluator["id"]: {"task": task, "dataset": dataset, "evaluator": evaluator}
            for task in task_dicts(session, parents)
            for dataset in task["dataset_jobs"] for evaluator in dataset["evaluator_jobs"]
        }
        summaries = result_summaries(session, jobs)
        items = [{**by_job[job.id], "summary": summaries[job.id]} for job in jobs]
    else:
        items = []
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.post("/results/compare")
def compare_results(
    body: CompareResultsRequest, session: Session = Depends(get_session)
) -> dict[str, Any]:
    rows = []
    jobs = []
    for job_id in body.evaluator_job_ids:
        job = session.get(EvaluatorJob, job_id)
        if not job:
            raise fail(404, f"评价器任务不存在: {job_id}")
        jobs.append(job)
        try:
            summary = threshold_summary(session, job_id, body.threshold)
        except ValueError as exc:
            raise fail(400, str(exc)) from exc
        dataset_job = job.dataset_job
        model = dataset_job.task.submission.model_run
        rows.append(
            {
                "run_name": model.run_name,
                "model_family": model.model_family,
                "dataset_key": dataset_job.submission_dataset.dataset_key,
                "dataset_version_id": dataset_job.submission_dataset.dataset_version_id,
                "version_label": dataset_job.submission_dataset.dataset_version.version_label,
                "evaluator_name": job.evaluator_revision.profile.name,
                "evaluator_revision_id": job.evaluator_revision_id,
                "prompt_version_id": job.prompt_version_id,
                "status": job.status,
                **summary,
            }
        )
    return {"threshold": body.threshold, **comparison_checks(session, jobs), "items": rows}


@router.get("/evaluator-jobs/{job_id}/items")
def evaluator_job_items(
    job_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    language: str | None = None,
    item_status: Literal["completed", "failed", "cancelled", "queued", "running", "unscored"] | None = None,
    sort: Literal["id", "sample_id", "source_language", "translation_zh", "score", "status", "verdict", "cache_hit", "reason"] = "id",
    direction: Literal["asc", "desc"] = "asc",
    score_operator: Literal["eq", "lt", "lte", "gt", "gte"] | None = None,
    score_value: float | None = None,
    session: Session = Depends(get_session),
    sample_id: str | None = None,
    threshold: float | None = None,
) -> dict[str, Any]:
    job = session.get(EvaluatorJob, job_id)
    if not job:
        raise fail(404, "评价器任务不存在")
    submission_dataset = job.dataset_job.submission_dataset
    evaluator_type = job.evaluator_revision.profile.evaluator_type
    if (score_operator is None) != (score_value is None):
        raise fail(400, "得分筛选需同时提供比较方式和得分")
    if score_value is not None:
        try:
            validate_threshold(evaluator_type, score_value)
        except ValueError as exc:
            raise fail(400, str(exc).replace("阈值", "筛选得分")) from exc
    if threshold is not None:
        try:
            validate_threshold(evaluator_type, threshold)
        except ValueError as exc:
            raise fail(400, str(exc)) from exc
    scored = scored_item_condition(evaluator_type)
    current_score = comparable_item_score(evaluator_type)
    filters = [DatasetSample.dataset_version_id == submission_dataset.dataset_version_id]
    if item_status == "unscored":
        filters.append(~scored)
    elif item_status == "completed":
        filters.append(scored)
    elif item_status:
        filters.append(EvaluationItem.status == item_status)
    if language:
        filters.append(DatasetSample.source_language == language)
    if sample_id is not None:
        filters.append(DatasetSample.sample_id == sample_id)
    if score_operator is not None:
        comparisons = {
            "eq": current_score == score_value,
            "lt": current_score < score_value,
            "lte": current_score <= score_value,
            "gt": current_score > score_value,
            "gte": current_score >= score_value,
        }
        filters.extend([scored, comparisons[score_operator]])
    base = (
        select(EvaluationItem, Prediction, DatasetSample, ScoreResult, scored.label("scored"), current_score.label("current_score"))
        .select_from(DatasetSample)
        .outerjoin(
            Prediction,
            (Prediction.submission_dataset_id == submission_dataset.id)
            & (Prediction.sample_id == DatasetSample.sample_id),
        )
        .outerjoin(
            EvaluationItem,
            (EvaluationItem.evaluator_job_id == job.id)
            & (EvaluationItem.prediction_id == Prediction.id),
        )
        .outerjoin(ScoreResult, EvaluationItem.score_result_id == ScoreResult.id)
        .where(*filters)
    )
    # Most browsing does not depend on scoring state. Count/index-page immutable
    # sample IDs first, then load only the visible rows' text and scoring fields.
    sample_filters = [DatasetSample.dataset_version_id == submission_dataset.dataset_version_id]
    if language:
        sample_filters.append(DatasetSample.source_language == language)
    if sample_id is not None:
        sample_filters.append(DatasetSample.sample_id == sample_id)
    sample_only = item_status is None and score_operator is None
    count_query = (select(func.count()).select_from(DatasetSample).where(*sample_filters)
                   if sample_only else select(func.count()).select_from(base.with_only_columns(DatasetSample.id).subquery()))
    verdict_threshold = threshold if threshold is not None else job.evaluator_revision.default_threshold
    visible_reason = case((scored, func.nullif(ScoreResult.reason, "")), else_=None)
    sort_columns = {
        "id": DatasetSample.id, "sample_id": DatasetSample.sample_id,
        "source_language": DatasetSample.source_language,
        "translation_zh": func.nullif(Prediction.translation_zh, ""),
        "score": case((scored, current_score), else_=None),
        "status": status_sort_value(EvaluationItem.status),
        "verdict": case((~scored, 0), (current_score < verdict_threshold, 1), else_=2),
        "cache_hit": case((scored, EvaluationItem.cache_hit), else_=None),
        "reason": func.coalesce(visible_reason, func.nullif(EvaluationItem.error, "")),
    }
    order = table_order(sort_columns[sort], direction, DatasetSample.id)
    ids_query = (select(DatasetSample.id).where(*sample_filters)
                 if sample_only and sort in {"id", "sample_id", "source_language"}
                 else base.with_only_columns(DatasetSample.id))
    visible_ids = list(session.scalars(ids_query.order_by(*order).offset((page - 1) * page_size).limit(page_size)))
    rows = session.execute(base.where(DatasetSample.id.in_(visible_ids)).order_by(*order).options(
        load_only(DatasetSample.sample_id, DatasetSample.source_language, DatasetSample.source_text, DatasetSample.reference_zh),
        load_only(Prediction.translation_zh, Prediction.predicted_language),
        load_only(EvaluationItem.status, EvaluationItem.cache_hit, EvaluationItem.attempts, EvaluationItem.error),
        load_only(ScoreResult.score_min, ScoreResult.score_max, ScoreResult.unit, ScoreResult.reason,
                  ScoreResult.prompt_version_id, ScoreResult.evaluator_model, ScoreResult.base_url),
    )).all() if visible_ids else []
    return {
        "total": session.scalar(count_query) or 0,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": item.id if item else f"sample:{sample.id}",
                "sample_id": sample.sample_id,
                "source_language": sample.source_language,
                "source_text": sample.source_text,
                "reference_zh": sample.reference_zh,
                "translation_zh": prediction.translation_zh if prediction else "",
                "predicted_language": prediction.predicted_language if prediction else None,
                "status": item.status if item else "unscored",
                "scored": bool(is_scored),
                "cache_hit": item.cache_hit if item else False,
                "attempts": item.attempts if item else 0,
                "error": item.error if item else None,
                "score": value if is_scored else None,
                "score_min": score.score_min if is_scored else None,
                "score_max": score.score_max if is_scored else None,
                "unit": score.unit if is_scored else None,
                "reason": score.reason if is_scored else None,
                "actual_prompt_version_id": score.prompt_version_id if is_scored else None,
                "actual_evaluator_model": score.evaluator_model if is_scored else None,
                "actual_base_url": score.base_url if is_scored else None,
            }
            for item, prediction, sample, score, is_scored, value in rows
        ],
    }


@router.get("/evaluator-profiles")
def list_evaluator_profiles(
    enabled_only: bool = False, session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    query = select(EvaluatorProfile).order_by(EvaluatorProfile.name)
    if enabled_only:
        query = query.where(EvaluatorProfile.enabled.is_(True))
    return [evaluator_profile_dict(item) for item in session.scalars(query)]


@router.post("/evaluator-profiles/{profile_id}/connection-test")
async def test_evaluator_connection(
    profile_id: str, session: Session = Depends(get_session)
) -> dict[str, Any]:
    profile = session.get(EvaluatorProfile, profile_id)
    if not profile:
        raise fail(404, "评价器配置不存在")
    if profile.evaluator_type != "openai_compatible_llm":
        return {
            "status": "connected",
            "detail": "本地评价器可用，无需连接外部模型服务",
            "latency_ms": 0,
            "model_available": True,
        }
    if not profile.revisions:
        raise fail(400, "评价器尚无可用修订")
    latest = max(profile.revisions, key=lambda item: item.revision)
    try:
        return await check_openai_compatible_connection(latest.config)
    except ValueError as exc:
        raise fail(400, str(exc)) from exc


@router.post("/evaluator-profiles")
def create_evaluator_profile(
    body: EvaluatorProfileCreate, session: Session = Depends(get_session)
) -> dict[str, Any]:
    try:
        validate_threshold(body.evaluator_type, body.default_threshold)
        config = validate_evaluator_config(body.evaluator_type, body.config)
        profile = EvaluatorProfile(
            name=body.name,
            evaluator_type=body.evaluator_type,
            enabled=body.enabled,
        )
        session.add(profile)
        session.flush()
        session.add(
            EvaluatorRevision(
                profile_id=profile.id,
                revision=1,
                config=config,
                default_threshold=body.default_threshold,
            )
        )
        session.commit()
        session.refresh(profile)
        return evaluator_profile_dict(profile)
    except (ValueError, IntegrityError) as exc:
        session.rollback()
        raise fail(400, str(exc)) from exc


@router.post("/evaluator-profiles/{profile_id}/revisions")
def create_evaluator_revision(
    profile_id: str,
    body: EvaluatorRevisionCreate,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    profile = session.get(EvaluatorProfile, profile_id)
    if not profile:
        raise fail(404, "评价器配置不存在")
    try:
        latest = max(profile.revisions, key=lambda item: item.revision)
        raw_config = {**latest.config, **body.config}
        if profile.evaluator_type == "openai_compatible_llm":
            supplied_key = str(raw_config.get("api_key", ""))
            if not supplied_key or supplied_key.startswith("••••"):
                raw_config["api_key"] = latest.config.get("api_key", "")
        validate_threshold(profile.evaluator_type, body.default_threshold)
        config = validate_evaluator_config(profile.evaluator_type, raw_config)
        if config == latest.config and body.default_threshold == latest.default_threshold:
            return evaluator_profile_dict(profile)
        next_revision = max((item.revision for item in profile.revisions), default=0) + 1
        session.add(
            EvaluatorRevision(
                profile_id=profile.id,
                revision=next_revision,
                config=config,
                default_threshold=body.default_threshold,
            )
        )
        session.commit()
        session.refresh(profile)
        return evaluator_profile_dict(profile)
    except (ValueError, IntegrityError) as exc:
        session.rollback()
        raise fail(400, str(exc)) from exc


@router.patch("/evaluator-profiles/{profile_id}")
def update_evaluator_profile(
    profile_id: str,
    body: EvaluatorProfileUpdate,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    profile = session.get(EvaluatorProfile, profile_id)
    if not profile:
        raise fail(404, "评价器配置不存在")
    profile.enabled = body.enabled
    session.commit()
    return evaluator_profile_dict(profile)


@router.get("/prompt-profiles")
def list_prompt_profiles(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [
        prompt_profile_dict(item, session)
        for item in session.scalars(
            select(PromptProfile).where(PromptProfile.deleted.is_(False)).order_by(PromptProfile.name)
        )
    ]


@router.post("/prompt-profiles")
def create_prompt_profile(
    body: PromptProfileCreate, session: Session = Depends(get_session)
) -> dict[str, Any]:
    try:
        profile = session.scalar(
            select(PromptProfile).where(PromptProfile.name == body.name, PromptProfile.deleted.is_(True))
        )
        if profile:
            profile.deleted = False
            profile.description = body.description
            profile.created_at = datetime.now(UTC)
        else:
            profile = PromptProfile(name=body.name, description=body.description)
        session.add(profile)
        session.flush()
        session.add(
            PromptVersion(
                profile_id=profile.id,
                version=1,
                version_label=next_prompt_version_label(profile, body.version_label),
                system_template=body.system_template,
                user_template=body.user_template,
                published=body.published,
            )
        )
        session.commit()
        session.refresh(profile)
        return prompt_profile_dict(profile, session)
    except IntegrityError as exc:
        session.rollback()
        raise fail(400, "Prompt 名称已存在") from exc


@router.post("/prompt-profiles/{profile_id}/versions")
def create_prompt_version(
    profile_id: str,
    body: PromptVersionCreate,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    profile = session.get(PromptProfile, profile_id)
    if not profile or profile.deleted:
        raise fail(404, "Prompt 配置不存在")
    next_version = max((item.version for item in profile.versions), default=0) + 1
    session.add(
        PromptVersion(
            profile_id=profile.id,
            version=next_version,
            version_label=next_prompt_version_label(profile, body.version_label),
            system_template=body.system_template,
            user_template=body.user_template,
            published=body.published,
        )
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise fail(409, "Prompt 版本标签已存在，请使用其他标签或重试") from exc
    session.refresh(profile)
    return prompt_profile_dict(profile, session)


@router.delete("/prompt-versions/{version_id}")
def delete_prompt_version(
    version_id: str, session: Session = Depends(get_session)
) -> dict[str, bool]:
    version = session.get(PromptVersion, version_id)
    if not version:
        raise fail(404, "Prompt 版本不存在")
    status = prompt_deletion_status(session, [version_id])[version_id]
    if not status["can_delete"]:
        raise fail(409, status["delete_block_reason"])
    profile = version.profile
    session.delete(version)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise fail(409, "此 Prompt 版本已被引用，无法删除") from exc
    session.expire(profile, ["versions"])
    return {"deleted": True}


@router.delete("/prompt-profiles/{profile_id}")
def delete_prompt_profile(
    profile_id: str, session: Session = Depends(get_session)
) -> dict[str, bool]:
    profile = session.get(PromptProfile, profile_id)
    if not profile or profile.deleted:
        raise fail(404, "Prompt 配置不存在")
    status = prompt_profile_dict(profile, session)
    if not status["can_delete"]:
        raise fail(409, status["delete_block_reason"])
    # Retain the name so startup defaults do not recreate a removed configuration.
    profile.versions.clear()
    profile.deleted = True
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise fail(409, "此 Prompt 配置已被引用，无法删除") from exc
    return {"deleted": True}


def model_run_dict(item: ModelRun) -> dict[str, Any]:
    return {
            "id": item.id,
            "run_name": item.run_name,
            "model_family": item.model_family,
            "checkpoint_name": item.checkpoint_name,
            "model_version": item.model_version,
            "notes": item.notes,
            "inference_platform": item.inference_platform,
            "inference_mode": item.inference_mode,
            "created_at": utc_iso(item.created_at),
        }


@router.get("/model-runs/page")
def paginated_model_runs(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    q: str = "", session: Session = Depends(get_session),
    sort: Literal["default", "run_name", "model_family", "model_version", "inference_platform", "inference_mode", "notes", "created_at"] = "default",
    direction: Literal["asc", "desc"] = "asc",
) -> dict[str, Any]:
    statement = select(ModelRun)
    if q:
        statement = statement.where(model_search(q))
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    order = (ModelRun.created_at.desc(), ModelRun.id.desc())
    if sort != "default":
        sort_columns = {"run_name": ModelRun.run_name, "model_family": ModelRun.model_family,
                        "model_version": func.nullif(ModelRun.model_version, ""),
                        "inference_platform": ModelRun.inference_platform, "inference_mode": ModelRun.inference_mode,
                        "notes": func.nullif(ModelRun.notes, ""), "created_at": ModelRun.created_at}
        order = table_order(sort_columns[sort], direction, ModelRun.id)
    rows = session.scalars(statement.order_by(*order).offset((page - 1) * page_size).limit(page_size))
    return {"items": [model_run_dict(row) for row in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/model-runs")
def list_model_runs(
    limit: int = Query(100, ge=1, le=500), session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    return [model_run_dict(row) for row in session.scalars(select(ModelRun).order_by(ModelRun.created_at.desc()).limit(limit))]


@router.get("/model-runs/{model_run_id}")
def model_run_detail(model_run_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    model = session.get(ModelRun, model_run_id)
    if not model:
        raise fail(404, "模型运行不存在")
    submissions = session.scalars(select(InferenceSubmission).where(InferenceSubmission.model_run_id == model.id))
    return {
        **model_run_dict(model), "result_info": model.result_info,
        "submissions": [{"id": item.id, "created_at": utc_iso(item.created_at)} for item in submissions],
    }
