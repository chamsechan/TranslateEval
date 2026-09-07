from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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
from .queries import comparison_checks, model_search, page_tasks, task_change_version, task_load_options
from .queue import (
    JobStateConflict,
    cancel_dataset_job,
    cancel_task,
    create_evaluation_task,
    language_detection_summary,
    retry_failed_job,
    threshold_summary,
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


def prompt_profile_dict(profile: PromptProfile) -> dict[str, Any]:
    return {
        "id": profile.id,
        "name": profile.name,
        "description": profile.description,
        "created_at": utc_iso(profile.created_at),
        "versions": [
            {
                "id": item.id,
                "version": item.version,
                "system_template": item.system_template,
                "user_template": item.user_template,
                "published": item.published,
                "created_at": utc_iso(item.created_at),
            }
            for item in sorted(profile.versions, key=lambda row: row.version, reverse=True)
        ],
    }


def task_dict(task: EvaluationTask) -> dict[str, Any]:
    model = task.submission.model_run
    dataset_jobs = []
    for dataset_job in sorted(task.dataset_jobs, key=lambda item: item.created_at):
        evaluator_jobs = []
        for evaluator_job in sorted(dataset_job.evaluator_jobs, key=lambda item: item.created_at):
            revision = evaluator_job.evaluator_revision
            cancelled_items = (
                max(
                    evaluator_job.total_items
                    - evaluator_job.completed_items
                    - evaluator_job.failed_items,
                    0,
                )
                if evaluator_job.status in {"cancelled", "partial_cancelled"}
                else 0
            )
            evaluator_jobs.append(
                {
                    "id": evaluator_job.id,
                    "name": revision.profile.name,
                    "evaluator_type": revision.profile.evaluator_type,
                    "revision": revision.revision,
                    "prompt_version_id": evaluator_job.prompt_version_id,
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


@router.get("/datasets")
def list_datasets(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    result = []
    for dataset in session.scalars(select(Dataset).order_by(Dataset.name)):
        versions = sorted(dataset.versions, key=lambda item: item.created_at, reverse=True)
        latest = versions[0] if versions else None
        result.append(
            {
                "id": dataset.id,
                "key": dataset.key,
                "name": dataset.name,
                "description": dataset.description,
                "version_count": len(versions),
                "latest_version": (
                    {
                        "id": latest.id,
                        "version_label": latest.version_label,
                        "sample_count": latest.sample_count,
                        "source_languages": latest.source_languages,
                        "content_sha256": latest.content_sha256,
                        "created_at": utc_iso(latest.created_at),
                    }
                    if latest
                    else None
                ),
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
    return [
        {
            "id": item.id,
            "version_label": item.version_label,
            "change_note": item.change_note,
            "content_sha256": item.content_sha256,
            "sample_count": item.sample_count,
            "source_languages": item.source_languages,
            "created_at": utc_iso(item.created_at),
        }
        for item in sorted(dataset.versions, key=lambda row: row.created_at, reverse=True)
    ]


@router.get("/dataset-versions/{version_id}/samples")
def list_dataset_samples(
    version_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    language: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    query = select(DatasetSample).where(DatasetSample.dataset_version_id == version_id)
    count_query = select(func.count(DatasetSample.id)).where(
        DatasetSample.dataset_version_id == version_id
    )
    if language:
        query = query.where(DatasetSample.source_language == language)
        count_query = count_query.where(DatasetSample.source_language == language)
    rows = session.scalars(
        query.order_by(DatasetSample.id).offset((page - 1) * page_size).limit(page_size)
    )
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


@router.get("/tasks")
def list_tasks(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    query = select(EvaluationTask).options(*task_load_options()).order_by(EvaluationTask.created_at.desc(), EvaluationTask.id.desc()).limit(limit)
    if status:
        query = query.where(EvaluationTask.status == status)
    return [task_dict(item) for item in session.scalars(query)]


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
    return {"items": [task_dict(row) for row in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/tasks/changes")
async def task_changes() -> StreamingResponse:
    def read_version():
        with SessionLocal() as session:
            return task_change_version(session)

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
    async def stream():
        previous = ""
        while True:
            with SessionLocal() as session:
                tasks = list(
                    session.scalars(
                        select(EvaluationTask).options(*task_load_options())
                        .order_by(EvaluationTask.created_at.desc())
                        .limit(50)
                    )
                )
                payload = json.dumps(
                    [task_dict(task) for task in tasks], ensure_ascii=False
                )
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
    return task_dict(task)


@router.post("/tasks/{task_id}/cancel")
def api_cancel_task(task_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        return task_dict(cancel_task(session, task_id))
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
            "run_name": model.run_name,
            "model_family": model.model_family,
            "created_at": utc_iso(task.created_at),
        },
        "dataset": {
            "id": dataset_job.id,
            "dataset_key": dataset_job.submission_dataset.dataset_key,
            "version_label": dataset_job.submission_dataset.dataset_version.version_label,
        },
        "evaluator": {
            "id": job.id,
            "name": revision.profile.name,
            "evaluator_type": revision.profile.evaluator_type,
            "revision": revision.revision,
            "prompt_version_id": job.prompt_version_id,
            "status": job.status,
            "total_items": job.total_items,
            "completed_items": job.completed_items,
            "cached_items": job.cached_items,
            "failed_items": job.failed_items,
            "cancelled_items": (
                max(job.total_items - job.completed_items - job.failed_items, 0)
                if job.status in {"cancelled", "partial_cancelled"}
                else 0
            ),
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
) -> dict[str, Any]:
    statement = select(EvaluatorJob).join(DatasetJob).join(EvaluationTask).join(InferenceSubmission).join(ModelRun)
    if q:
        statement = statement.where(model_search(q))
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    jobs = session.scalars(statement.order_by(EvaluationTask.created_at.desc(), EvaluatorJob.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    # Fetch all parent relationships in batches, shared by the detail serializers below.
    if jobs:
        task_ids = select(DatasetJob.task_id).where(DatasetJob.id.in_({job.dataset_job_id for job in jobs}))
        parents = session.scalars(select(EvaluationTask).where(EvaluationTask.id.in_(task_ids)).options(*task_load_options())).all()
        by_job = {
            evaluator["id"]: {"task": task, "dataset": dataset, "evaluator": evaluator}
            for parent in parents for task in [task_dict(parent)]
            for dataset in task["dataset_jobs"] for evaluator in dataset["evaluator_jobs"]
        }
        items = [by_job[job.id] for job in jobs]
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
    item_status: str | None = None,
    sort: Literal["id", "score"] = "id",
    direction: Literal["asc", "desc"] = "asc",
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    job = session.get(EvaluatorJob, job_id)
    if not job:
        raise fail(404, "评价器任务不存在")
    submission_dataset = job.dataset_job.submission_dataset
    filters = [EvaluationItem.evaluator_job_id == job.id]
    if item_status:
        filters.append(EvaluationItem.status == item_status)
    if language:
        filters.append(DatasetSample.source_language == language)
    base = (
        select(EvaluationItem, Prediction, DatasetSample, ScoreResult)
        .join(Prediction, EvaluationItem.prediction_id == Prediction.id)
        .join(
            DatasetSample,
            (DatasetSample.dataset_version_id == submission_dataset.dataset_version_id)
            & (DatasetSample.sample_id == Prediction.sample_id),
        )
        .outerjoin(ScoreResult, EvaluationItem.score_result_id == ScoreResult.id)
        .where(*filters)
    )
    count_query = (
        select(func.count(EvaluationItem.id))
        .join(Prediction, EvaluationItem.prediction_id == Prediction.id)
        .join(
            DatasetSample,
            (DatasetSample.dataset_version_id == submission_dataset.dataset_version_id)
            & (DatasetSample.sample_id == Prediction.sample_id),
        )
        .where(*filters)
    )
    order = [EvaluationItem.id.asc()]
    if sort == "score":
        order = [ScoreResult.score.is_(None), ScoreResult.score.desc() if direction == "desc" else ScoreResult.score.asc(), EvaluationItem.id.asc()]
    rows = session.execute(
        base.order_by(*order).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {
        "total": session.scalar(count_query) or 0,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": item.id,
                "sample_id": prediction.sample_id,
                "source_language": sample.source_language,
                "source_text": sample.source_text,
                "reference_zh": sample.reference_zh,
                "translation_zh": prediction.translation_zh,
                "predicted_language": prediction.predicted_language,
                "status": item.status,
                "cache_hit": item.cache_hit,
                "attempts": item.attempts,
                "error": item.error,
                "score": score.score if score else None,
                "score_min": score.score_min if score else None,
                "score_max": score.score_max if score else None,
                "unit": score.unit if score else None,
                "reason": score.reason if score else None,
                "actual_prompt_version_id": score.prompt_version_id if score else None,
                "actual_evaluator_model": score.evaluator_model if score else None,
                "actual_base_url": score.base_url if score else None,
            }
            for item, prediction, sample, score in rows
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
        raw_config = dict(body.config)
        if profile.evaluator_type == "openai_compatible_llm":
            supplied_key = str(raw_config.get("api_key", ""))
            if not supplied_key or supplied_key.startswith("••••"):
                latest = max(profile.revisions, key=lambda item: item.revision)
                raw_config["api_key"] = latest.config.get("api_key", "")
        validate_threshold(profile.evaluator_type, body.default_threshold)
        config = validate_evaluator_config(profile.evaluator_type, raw_config)
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
    except ValueError as exc:
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
        prompt_profile_dict(item)
        for item in session.scalars(select(PromptProfile).order_by(PromptProfile.name))
    ]


@router.post("/prompt-profiles")
def create_prompt_profile(
    body: PromptProfileCreate, session: Session = Depends(get_session)
) -> dict[str, Any]:
    try:
        profile = PromptProfile(name=body.name, description=body.description)
        session.add(profile)
        session.flush()
        session.add(
            PromptVersion(
                profile_id=profile.id,
                version=1,
                system_template=body.system_template,
                user_template=body.user_template,
                published=body.published,
            )
        )
        session.commit()
        session.refresh(profile)
        return prompt_profile_dict(profile)
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
    if not profile:
        raise fail(404, "Prompt 配置不存在")
    next_version = max((item.version for item in profile.versions), default=0) + 1
    session.add(
        PromptVersion(
            profile_id=profile.id,
            version=next_version,
            system_template=body.system_template,
            user_template=body.user_template,
            published=body.published,
        )
    )
    session.commit()
    session.refresh(profile)
    return prompt_profile_dict(profile)


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
) -> dict[str, Any]:
    statement = select(ModelRun)
    if q:
        statement = statement.where(model_search(q))
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = session.scalars(statement.order_by(ModelRun.created_at.desc(), ModelRun.id.desc()).offset((page - 1) * page_size).limit(page_size))
    return {"items": [model_run_dict(row) for row in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/model-runs")
def list_model_runs(
    limit: int = Query(100, ge=1, le=500), session: Session = Depends(get_session)
) -> list[dict[str, Any]]:
    return [model_run_dict(row) for row in session.scalars(select(ModelRun).order_by(ModelRun.created_at.desc()).limit(limit))]
