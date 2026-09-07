from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .api import report_dict, utc_iso
from .config import settings
from .database import get_session
from .import_options import has_results_expression, option_has_results, remember_option
from .importers import (
    ImportValidationError, _report_record, stage_source,
    validate_dataset_staged, validate_submission_staged,
)
from .import_index import iter_jsonl
from .models import ImportCommitJob, ImportOption, ImportValidationReport
from .schemas import (
    CommitSubmissionRequest, DatasetSampleInput, ImportManifestRequest, ImportOptionCategory,
    ImportOptionCreate, ImportOptionUpdate, PathImportRequest,
)

router = APIRouter(prefix="/api")
ImportKind = Literal["dataset", "submission"]


def import_job_dict(job: ImportCommitJob) -> dict[str, Any]:
    return {
        "id": job.id, "report_id": job.report_id, "kind": job.kind,
        "status": job.status, "phase": job.phase, "error": job.error,
        "result": job.result, "created_at": utc_iso(job.created_at),
        "started_at": utc_iso(job.started_at), "finished_at": utc_iso(job.finished_at),
    }


@router.post("/import-reports/{report_id}/commit-job", status_code=202)
def enqueue_import_commit(report_id: str, body: dict[str, Any] | None = None,
                          session: Session = Depends(get_session)) -> dict[str, Any]:
    report = session.get(ImportValidationReport, report_id)
    if not report:
        raise HTTPException(404, "导入核验记录不存在")
    if not report.report.get("valid"):
        raise HTTPException(400, "核验未通过，不能提交")
    existing = session.scalar(select(ImportCommitJob).where(ImportCommitJob.report_id == report_id))
    if existing and existing.status != "failed":
        return import_job_dict(existing)
    payload = (body or {}) if body else (existing.request if existing else {})
    if report.kind == "submission":
        try:
            payload = CommitSubmissionRequest.model_validate(payload).model_dump(mode="json")
        except ValidationError as exc:
            raise HTTPException(422, "请选择有效的评价器后提交") from exc
    else:
        payload = {}
    if existing:
        from sqlalchemy import update
        session.execute(update(ImportCommitJob).where(
            ImportCommitJob.id == existing.id, ImportCommitJob.status == "failed",
        ).values(status="queued", phase="queued", request=payload, result={}, error=None,
                 started_at=None, finished_at=None))
        session.commit()
        session.refresh(existing)
        return import_job_dict(existing)
    job = ImportCommitJob(report_id=report_id, kind=report.kind, request=payload)
    session.add(job)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        job = session.scalar(select(ImportCommitJob).where(ImportCommitJob.report_id == report_id))
        if job is None:
            raise
    return import_job_dict(job)


@router.get("/import-commit-jobs")
def list_import_commit_jobs(limit: int = Query(20, ge=1, le=100),
                            session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    jobs = session.scalars(select(ImportCommitJob).order_by(
        ImportCommitJob.created_at.desc(), ImportCommitJob.id.desc(),
    ).limit(limit))
    return [import_job_dict(job) for job in jobs]


@router.get("/import-commit-jobs/{job_id}")
def get_import_commit_job(job_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    job = session.get(ImportCommitJob, job_id)
    if not job:
        raise HTTPException(404, "后台导入任务不存在")
    return import_job_dict(job)


def check_prefill_structure(manifest: dict[str, Any], kind: ImportKind) -> None:
    """Accept missing fields, but keep structured values out of text form controls."""
    def text_fields(data: dict[str, Any], fields: tuple[str, ...], prefix: str = "") -> None:
        for key in fields:
            if data.get(key) is not None and not isinstance(data[key], str):
                raise ImportValidationError(f"{prefix}{key} 必须是文本")

    if kind == "dataset":
        text_fields(manifest, ("dataset_key", "name", "version_label", "change_note", "description"))
        return
    else:
        text_fields(manifest, ("run_name", "model_family", "checkpoint_name", "model_version", "model_notes"))
        inference = manifest.get("inference")
        if inference is not None:
            if not isinstance(inference, dict):
                raise ImportValidationError("inference 必须是 JSON 对象")
            text_fields(inference, ("platform", "device", "sdk", "sdk_version", "precision", "mode", "generated_at", "code_revision"), "inference.")
        list_key, fields = "datasets", ("dataset_key", "dataset_content_sha256")
    if list_key in manifest:
        entries = manifest[list_key]
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ImportValidationError(f"{list_key} 必须是 JSON 对象数组")
        for entry in entries:
            text_fields(entry, fields, list_key + ".")


def option_dict(option: ImportOption, has_results: bool = False) -> dict[str, Any]:
    return {**{key: getattr(option, key) for key in (
        "id", "category", "value", "label", "enabled", "detects_language",
        "platform", "sdk", "sdk_version",
    )}, "has_results": has_results}


def validate_device_fields(option: ImportOption) -> None:
    if option.category != "device" and (option.platform or option.sdk or option.sdk_version):
        raise HTTPException(422, "只有设备可以设置平台和 SDK 信息")
    if (option.sdk or option.sdk_version) and not option.platform:
        raise HTTPException(422, "设置 SDK 信息时必须指定所属平台")


@router.get("/import-options")
def list_options(category: ImportOptionCategory | None = None,
                 session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    query = select(ImportOption, has_results_expression()).where(
        ImportOption.deleted.is_(False),
    ).order_by(ImportOption.category, ImportOption.label)
    if category:
        query = query.where(ImportOption.category == category)
    return [option_dict(option, has_results) for option, has_results in session.execute(query)]


@router.post("/import-options", status_code=201)
def create_option(body: ImportOptionCreate, session: Session = Depends(get_session)) -> dict[str, Any]:
    option = session.scalar(select(ImportOption).where(
        ImportOption.category == body.category, ImportOption.value == body.value,
    ))
    if option and not option.deleted:
        raise HTTPException(409, "该分类中已存在相同选项值")
    if option:
        for key, value in body.model_dump().items():
            setattr(option, key, value)
        option.deleted = False
    else:
        option = ImportOption(**body.model_dump())
        session.add(option)
    try:
        if option.category == "device":
            remember_option(session, "platform", option.platform)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "该分类中已存在相同选项值") from exc
    return option_dict(option, option_has_results(session, option))


@router.patch("/import-options/{option_id}")
def update_option(option_id: str, body: ImportOptionUpdate,
                  session: Session = Depends(get_session)) -> dict[str, Any]:
    option = session.get(ImportOption, option_id)
    if not option or option.deleted:
        raise HTTPException(404, "选项不存在")
    if option.category != "inference_mode" and body.detects_language:
        raise HTTPException(422, "只有推理模式可以设置语种识别统计")
    updates = body.model_dump(exclude_unset=True)
    if option.category == "device":
        if "platform" in updates and updates["platform"] != option.platform:
            updates.setdefault("sdk", "")
            updates.setdefault("sdk_version", "")
        if "sdk" in updates and updates["sdk"] != option.sdk:
            updates.setdefault("sdk_version", "")
    for key, value in updates.items():
        setattr(option, key, value)
    validate_device_fields(option)
    if option.category == "device":
        remember_option(session, "platform", option.platform)
    session.commit()
    return option_dict(option, option_has_results(session, option))


@router.delete("/import-options/{option_id}")
def delete_option(option_id: str, confirm: bool = False,
                  session: Session = Depends(get_session)) -> dict[str, bool]:
    option = session.get(ImportOption, option_id)
    if not option or option.deleted:
        raise HTTPException(404, "选项不存在")
    if option_has_results(session, option) and not confirm:
        raise HTTPException(409, {
            "code": "confirmation_required",
            "message": "该选项已有导入结果，确认删除？历史结果将保留。",
        })
    # Tombstones stop startup seeding and future imports from restoring deleted
    # choices. Result snapshots reference values, so history remains intact.
    option.deleted = True
    option.enabled = False
    session.commit()
    return {"deleted": True}


def prepare_import(session: Session, source: str | Path, kind: ImportKind) -> ImportValidationReport:
    staged = stage_source(source, kind)
    try:
        return _prepare_staged(session, staged, kind)
    except Exception:
        session.rollback()
        shutil.rmtree(staged, ignore_errors=True)
        raise


def _prepare_staged(session: Session, staged: Path, kind: ImportKind) -> ImportValidationReport:
    manifest_path = staged / ("dataset_info.json" if kind == "dataset" else "result_info.json")
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImportValidationError(f"{manifest_path.name} 格式错误: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ImportValidationError(f"{manifest_path.name} 必须是 JSON 对象")
    manifest.setdefault("schema_version", 1)
    check_prefill_structure(manifest, kind)
    report: dict[str, Any] = {"valid": False, "errors": [], "has_manifest": manifest_path.is_file()}
    if kind == "dataset":
        manifest.pop("source_languages", None)
        errors: list[dict[str, Any]] = []
        count = 0
        languages: set[str] = set()
        for item in iter_jsonl(staged / "samples.jsonl", DatasetSampleInput, errors):
            languages.add(item.source_language)
            count += 1
        report["errors"] = errors
        report["detected_languages"] = sorted(languages)
        report["sample_count"] = count
    else:
        root_predictions = (staged / "predictions.jsonl").is_file()
        keys = sorted(p.parent.name for p in staged.glob("*/predictions.jsonl"))
        if not root_predictions and not keys:
            report["errors"].append({"message": "未找到 predictions.jsonl"})
        report["root_predictions"] = root_predictions
        report["prediction_directories"] = keys
        entries = manifest.get("datasets", [])
        if isinstance(entries, list):
            present = {entry.get("dataset_key") for entry in entries if isinstance(entry, dict)}
            entries = entries + [{"dataset_key": key} for key in keys if key not in present]
            if root_predictions and not entries:
                entries = [{}]
            manifest["datasets"] = entries
    record = _report_record(session, kind=kind, staged_path=staged, manifest=manifest, report=report)
    record.status = "draft"
    session.commit()
    return record


@router.post("/{kind}-imports/prepare")
def prepare_path(kind: ImportKind, body: PathImportRequest,
                 session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        return report_dict(prepare_import(session, body.path, kind))
    except ImportValidationError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{kind}-imports/prepare-upload")
def prepare_upload(kind: ImportKind, file: UploadFile = File(...),
                   session: Session = Depends(get_session)) -> dict[str, Any]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".zip", ".jsonl"):
        raise HTTPException(400, "请上传 .zip 或 .jsonl 文件")
    settings.import_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=suffix, dir=settings.import_dir, delete=False) as handle:
        shutil.copyfileobj(file.file, handle)
        path = Path(handle.name)
    try:
        return report_dict(prepare_import(session, path, kind))
    except ImportValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        path.unlink(missing_ok=True)


@router.post("/import-reports/{report_id}/validate")
def validate_draft(report_id: str, body: ImportManifestRequest,
                   session: Session = Depends(get_session)) -> dict[str, Any]:
    record = session.get(ImportValidationReport, report_id)
    if not record:
        raise HTTPException(404, "导入草稿不存在")
    if record.status == "committed":
        raise HTTPException(409, "已提交的导入不能修改，请重新导入")
    validate = validate_dataset_staged if record.kind == "dataset" else validate_submission_staged
    result = validate(session, Path(record.staged_path), body.manifest)
    result.report = {**{key: value for key, value in record.report.items() if key in (
        "has_manifest", "detected_languages", "root_predictions", "prediction_directories",
    )}, **result.report}
    session.commit()
    return report_dict(result)
