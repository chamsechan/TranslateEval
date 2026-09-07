from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .api import report_dict
from .config import settings
from .database import get_session
from .importers import (
    ImportValidationError, _load_jsonl, _report_record, stage_source,
    validate_dataset_staged, validate_submission_staged,
)
from .models import ImportOption, ImportValidationReport, Language
from .schemas import (
    DatasetSampleInput, ImportManifestRequest, ImportOptionCategory,
    ImportOptionCreate, ImportOptionUpdate, PathImportRequest,
)

router = APIRouter(prefix="/api")
ImportKind = Literal["dataset", "submission"]


def check_prefill_structure(manifest: dict[str, Any], kind: ImportKind) -> None:
    """Accept missing fields, but keep structured values out of text form controls."""
    def text_fields(data: dict[str, Any], fields: tuple[str, ...], prefix: str = "") -> None:
        for key in fields:
            if data.get(key) is not None and not isinstance(data[key], str):
                raise ImportValidationError(f"{prefix}{key} 必须是文本")

    if kind == "dataset":
        text_fields(manifest, ("dataset_key", "name", "version_label", "change_note", "description"))
        list_key, fields = "source_languages", ("code", "name_zh")
    else:
        text_fields(manifest, ("run_name", "model_family", "checkpoint_name", "model_version", "model_notes"))
        inference = manifest.get("inference")
        if inference is not None:
            if not isinstance(inference, dict):
                raise ImportValidationError("inference 必须是 JSON 对象")
            text_fields(inference, ("platform", "device", "precision", "mode", "generated_at", "code_revision"), "inference.")
        list_key, fields = "datasets", ("dataset_key", "dataset_content_sha256")
    if list_key in manifest:
        entries = manifest[list_key]
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ImportValidationError(f"{list_key} 必须是 JSON 对象数组")
        for entry in entries:
            text_fields(entry, fields, list_key + ".")


def option_dict(option: ImportOption) -> dict[str, Any]:
    return {key: getattr(option, key) for key in (
        "id", "category", "value", "label", "enabled", "detects_language",
    )}


@router.get("/import-options")
def list_options(category: ImportOptionCategory | None = None,
                 session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    query = select(ImportOption).order_by(ImportOption.category, ImportOption.label)
    if category:
        query = query.where(ImportOption.category == category)
    return [option_dict(option) for option in session.scalars(query)]


@router.post("/import-options", status_code=201)
def create_option(body: ImportOptionCreate, session: Session = Depends(get_session)) -> dict[str, Any]:
    option = ImportOption(**body.model_dump())
    session.add(option)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "该分类中已存在相同选项值") from exc
    return option_dict(option)


@router.patch("/import-options/{option_id}")
def update_option(option_id: str, body: ImportOptionUpdate,
                  session: Session = Depends(get_session)) -> dict[str, Any]:
    option = session.get(ImportOption, option_id)
    if not option:
        raise HTTPException(404, "选项不存在")
    if option.category != "inference_mode" and body.detects_language:
        raise HTTPException(422, "只有推理模式可以设置语种识别统计")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(option, key, value)
    session.commit()
    return option_dict(option)


def prepare_import(session: Session, source: str | Path, kind: ImportKind) -> ImportValidationReport:
    staged = stage_source(source, kind)
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
        samples, errors = _load_jsonl(staged / "samples.jsonl", DatasetSampleInput)
        report["errors"] = errors
        languages = {item.source_language for item in samples}
        names = {item.code: item.name_zh for item in session.scalars(select(Language))}
        # Preserve explicit declarations so undeclared languages still require review.
        if "source_languages" not in manifest:
            manifest["source_languages"] = [{"code": code, "name_zh": names.get(code, code)} for code in sorted(languages)]
        report["detected_languages"] = sorted(languages)
        report["sample_count"] = len(samples)
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
