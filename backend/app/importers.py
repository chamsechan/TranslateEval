from __future__ import annotations

import json
import shutil
import uuid
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import (
    Dataset,
    DatasetSample,
    DatasetVersion,
    ImportValidationReport,
    InferenceSubmission,
    Language,
    ModelRun,
    Prediction,
    SubmissionDataset,
)
from .normalization import dataset_content_hash, normalize_text, sample_content_hash, sha256_text
from .schemas import DatasetManifest, DatasetSampleInput, PredictionInput, ResultManifest


T = TypeVar("T", bound=BaseModel)
MAX_REPORTED_ERRORS = 1_000


class ImportValidationError(ValueError):
    pass


def _safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    try:
        with zipfile.ZipFile(archive) as zipped:
            for member in zipped.infolist():
                target = (destination / member.filename).resolve()
                if destination not in target.parents and target != destination:
                    raise ImportValidationError(f"ZIP 包含不安全路径: {member.filename}")
            zipped.extractall(destination)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ImportValidationError("无法读取 ZIP，请确认压缩包完整、未加密且使用受支持的压缩格式") from exc


def stage_source(source_path: str | Path, kind: str) -> Path:
    source = Path(source_path).expanduser().resolve()
    if not source.exists():
        raise ImportValidationError(f"路径不存在: {source}")
    target = settings.import_dir / f"{kind}-{uuid.uuid4()}"
    if target.resolve().is_relative_to(source):
        raise ImportValidationError("导入目录不能包含系统暂存目录，请选择具体的数据集或推理结果目录")
    target.mkdir(parents=True, exist_ok=False)
    try:
        if source.is_dir():
            for child in source.iterdir():
                destination = target / child.name
                if child.is_dir():
                    shutil.copytree(child, destination)
                else:
                    shutil.copy2(child, destination)
        elif source.suffix.lower() == ".zip":
            _safe_extract(source, target)
            children = list(target.iterdir())
            if len(children) == 1 and children[0].is_dir():
                nested = children[0]
                for child in list(nested.iterdir()):
                    shutil.move(str(child), target / child.name)
                nested.rmdir()
        else:
            raise ImportValidationError("只支持目录或 .zip 文件")
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def prediction_content_hash(predictions: list[PredictionInput]) -> str:
    payload = [row.model_dump(mode="json") for row in sorted(predictions, key=lambda row: row.sample_id)]
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _load_json(path: Path, model: type[T]) -> T:
    if not path.is_file():
        raise ImportValidationError(f"缺少文件: {path.name}")
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, ValidationError, json.JSONDecodeError) as exc:
        raise ImportValidationError(f"{path.name} 格式错误: {exc}") from exc


def _load_jsonl(path: Path, model: type[T]) -> tuple[list[T], list[dict[str, Any]]]:
    if not path.is_file():
        return [], [{"file": path.name, "message": "文件不存在"}]
    rows: list[T] = []
    errors: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    rows.append(model.model_validate_json(line))
                except (ValidationError, json.JSONDecodeError) as exc:
                    if len(errors) < MAX_REPORTED_ERRORS:
                        errors.append(
                            {"file": path.name, "line": line_number, "message": str(exc)}
                        )
    except UnicodeDecodeError as exc:
        errors.append({"file": path.name, "message": f"文件不是有效 UTF-8: {exc}"})
    return rows, errors


def _report_record(
    session: Session,
    *,
    kind: str,
    staged_path: Path,
    manifest: dict[str, Any],
    report: dict[str, Any],
) -> ImportValidationReport:
    record = ImportValidationReport(
        kind=kind,
        status="validated" if report.get("valid") else "invalid",
        staged_path=str(staged_path),
        manifest=manifest,
        report=report,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


def validate_dataset_import(session: Session, source_path: str | Path) -> ImportValidationReport:
    staged = stage_source(source_path, "dataset")
    errors: list[dict[str, Any]] = []
    try:
        manifest = _load_json(staged / "dataset_info.json", DatasetManifest)
    except ImportValidationError as exc:
        return _report_record(
            session,
            kind="dataset",
            staged_path=staged,
            manifest={},
            report={"valid": False, "errors": [{"message": str(exc)}]},
        )

    sample_models, row_errors = _load_jsonl(staged / "samples.jsonl", DatasetSampleInput)
    errors.extend(row_errors)
    rows = [item.model_dump() for item in sample_models]
    declared_languages = {item.code for item in manifest.source_languages}
    seen: set[str] = set()
    duplicate_ids: list[str] = []
    undeclared_languages: set[str] = set()
    for row in rows:
        if row["sample_id"] in seen:
            duplicate_ids.append(row["sample_id"])
        seen.add(row["sample_id"])
        if row["source_language"] not in declared_languages:
            undeclared_languages.add(row["source_language"])
    if duplicate_ids:
        errors.append(
            {"message": "存在重复 sample_id", "sample_ids": sorted(set(duplicate_ids))[:100]}
        )
    if undeclared_languages:
        errors.append(
            {"message": "样本使用了未在清单声明的语种", "languages": sorted(undeclared_languages)}
        )
    if not rows:
        errors.append({"message": "数据集必须至少包含一个样本"})

    content_hash = dataset_content_hash(rows) if rows and not errors else ""
    dataset = session.scalar(select(Dataset).where(Dataset.key == manifest.dataset_key))
    latest: DatasetVersion | None = None
    diff: dict[str, Any] = {
        "base_version_id": None,
        "base_version_label": None,
        "added": len(rows),
        "removed": 0,
        "source_changed": 0,
        "reference_changed": 0,
        "unchanged": 0,
        "details": [],
    }
    if dataset:
        latest = session.scalar(
            select(DatasetVersion)
            .where(DatasetVersion.dataset_id == dataset.id)
            .order_by(DatasetVersion.created_at.desc())
            .limit(1)
        )
        if latest:
            if latest.version_label == manifest.version_label:
                errors.append({"message": f"版本标签已存在: {manifest.version_label}"})
            if latest.content_sha256 == content_hash and content_hash:
                errors.append(
                    {"message": "数据内容与已有版本完全相同", "existing_version_id": latest.id}
                )
            old_rows = {
                item.sample_id: item
                for item in session.scalars(
                    select(DatasetSample).where(DatasetSample.dataset_version_id == latest.id)
                )
            }
            new_rows = {row["sample_id"]: row for row in rows}
            details: list[dict[str, Any]] = []
            counters = {"added": 0, "removed": 0, "source_changed": 0, "reference_changed": 0, "unchanged": 0}
            for sample_id in sorted(set(old_rows) | set(new_rows)):
                old = old_rows.get(sample_id)
                new = new_rows.get(sample_id)
                if old is None:
                    change = "added"
                elif new is None:
                    change = "removed"
                else:
                    source_changed = (
                        old.source_language != new["source_language"]
                        or old.source_hash != sha256_text(new["source_text"])
                    )
                    reference_changed = old.reference_hash != sha256_text(new["reference_zh"])
                    if source_changed:
                        change = "source_changed"
                    elif reference_changed:
                        change = "reference_changed"
                    else:
                        change = "unchanged"
                counters[change] += 1
                if change != "unchanged" and len(details) < 200:
                    details.append({"sample_id": sample_id, "change": change})
            diff = {
                "base_version_id": latest.id,
                "base_version_label": latest.version_label,
                **counters,
                "details": details,
            }

    report = {
        "valid": not errors,
        "errors": errors,
        "summary": {
            "dataset_key": manifest.dataset_key,
            "name": manifest.name,
            "version_label": manifest.version_label,
            "sample_count": len(rows),
            "languages": sorted({row["source_language"] for row in rows}),
            "content_sha256": content_hash,
            "is_new_dataset": dataset is None,
        },
        "diff": diff,
    }
    return _report_record(
        session,
        kind="dataset",
        staged_path=staged,
        manifest=manifest.model_dump(mode="json"),
        report=report,
    )


def commit_dataset_import(session: Session, report_id: str) -> DatasetVersion:
    record = session.get(ImportValidationReport, report_id)
    if not record or record.kind != "dataset":
        raise ImportValidationError("找不到数据集校验记录")
    if record.status == "committed":
        version_id = record.report.get("committed_version_id")
        existing = session.get(DatasetVersion, version_id)
        if existing:
            return existing
    if not record.report.get("valid"):
        raise ImportValidationError("校验未通过，不能导入")

    manifest = DatasetManifest.model_validate(record.manifest)
    sample_models, errors = _load_jsonl(Path(record.staged_path) / "samples.jsonl", DatasetSampleInput)
    if errors:
        raise ImportValidationError("暂存文件在提交前发生变化，请重新校验")
    rows = [item.model_dump() for item in sample_models]
    current_hash = dataset_content_hash(rows)
    if current_hash != record.report["summary"]["content_sha256"]:
        raise ImportValidationError("暂存数据内容已变化，请重新校验")

    dataset = session.scalar(select(Dataset).where(Dataset.key == manifest.dataset_key))
    if not dataset:
        dataset = Dataset(
            key=manifest.dataset_key,
            name=manifest.name,
            description=manifest.description,
        )
        session.add(dataset)
        session.flush()
    for language in manifest.source_languages:
        if not session.get(Language, language.code):
            session.add(Language(code=language.code, name_zh=language.name_zh))
    session.flush()
    version = DatasetVersion(
        dataset_id=dataset.id,
        version_label=manifest.version_label,
        change_note=manifest.change_note,
        content_sha256=current_hash,
        sample_count=len(rows),
        source_languages=sorted({row["source_language"] for row in rows}),
    )
    session.add(version)
    session.flush()
    for offset in range(0, len(rows), 1_000):
        session.add_all(
            [
                DatasetSample(
                    dataset_version_id=version.id,
                    sample_id=row["sample_id"],
                    source_language=row["source_language"],
                    source_text=normalize_text(row["source_text"]),
                    reference_zh=normalize_text(row["reference_zh"]),
                    source_hash=sha256_text(row["source_text"]),
                    reference_hash=sha256_text(row["reference_zh"]),
                    content_hash=sample_content_hash(
                        row["sample_id"],
                        row["source_language"],
                        row["source_text"],
                        row["reference_zh"],
                    ),
                )
                for row in rows[offset : offset + 1_000]
            ]
        )
        session.flush()
    record.status = "committed"
    record.report = {**record.report, "committed_version_id": version.id}
    session.commit()
    session.refresh(version)
    return version


def validate_submission_import(
    session: Session,
    source_path: str | Path,
    version_overrides: dict[str, str] | None = None,
) -> ImportValidationReport:
    staged = stage_source(source_path, "submission")
    overrides = version_overrides or {}
    try:
        manifest = _load_json(staged / "result_info.json", ResultManifest)
    except ImportValidationError as exc:
        return _report_record(
            session,
            kind="submission",
            staged_path=staged,
            manifest={},
            report={"valid": False, "errors": [{"message": str(exc)}]},
        )

    errors: list[dict[str, Any]] = []
    dataset_reports: list[dict[str, Any]] = []
    for dataset_entry in manifest.datasets:
        dataset = session.scalar(select(Dataset).where(Dataset.key == dataset_entry.dataset_key))
        version: DatasetVersion | None = None
        override = overrides.get(dataset_entry.dataset_key)
        if override:
            version = session.get(DatasetVersion, override)
            if not version or not dataset or version.dataset_id != dataset.id:
                errors.append(
                    {"dataset_key": dataset_entry.dataset_key, "message": "指定的数据集版本无效"}
                )
                version = None
        elif dataset:
            version = session.scalar(
                select(DatasetVersion)
                .where(
                    DatasetVersion.dataset_id == dataset.id,
                    DatasetVersion.content_sha256 == dataset_entry.dataset_content_sha256.lower(),
                )
                .order_by(DatasetVersion.created_at.desc())
                .limit(1)
            )
        if not dataset:
            errors.append({"dataset_key": dataset_entry.dataset_key, "message": "数据集不存在"})
        elif not version:
            errors.append(
                {"dataset_key": dataset_entry.dataset_key, "message": "没有匹配内容哈希的数据集版本"}
            )
        elif version.content_sha256 != dataset_entry.dataset_content_sha256.lower():
            errors.append(
                {"dataset_key": dataset_entry.dataset_key, "message": "所选版本内容哈希不匹配"}
            )

        predictions, row_errors = _load_jsonl(
            staged / dataset_entry.dataset_key / "predictions.jsonl", PredictionInput
        )
        for error in row_errors:
            error["dataset_key"] = dataset_entry.dataset_key
        errors.extend(row_errors)
        ids = [row.sample_id for row in predictions]
        duplicate_ids = sorted(sample_id for sample_id, count in Counter(ids).items() if count > 1)
        if duplicate_ids:
            errors.append(
                {
                    "dataset_key": dataset_entry.dataset_key,
                    "message": "预测中存在重复 sample_id",
                    "sample_ids": duplicate_ids[:100],
                }
            )
        missing: list[str] = []
        unknown: list[str] = []
        if version:
            expected = set(
                session.scalars(
                    select(DatasetSample.sample_id).where(
                        DatasetSample.dataset_version_id == version.id
                    )
                )
            )
            actual = set(ids)
            missing = sorted(expected - actual)
            unknown = sorted(actual - expected)
            if missing:
                errors.append(
                    {
                        "dataset_key": dataset_entry.dataset_key,
                        "message": f"缺失 {len(missing)} 条预测",
                        "sample_ids": missing[:100],
                    }
                )
            if unknown:
                errors.append(
                    {
                        "dataset_key": dataset_entry.dataset_key,
                        "message": f"存在 {len(unknown)} 个未知 ID",
                        "sample_ids": unknown[:100],
                    }
                )
        dataset_reports.append(
            {
                "dataset_key": dataset_entry.dataset_key,
                "dataset_version_id": version.id if version else None,
                "version_label": version.version_label if version else None,
                "prediction_count": len(predictions),
                "predictions_sha256": prediction_content_hash(predictions),
                "missing_count": len(missing),
                "unknown_count": len(unknown),
                "content_sha256": dataset_entry.dataset_content_sha256.lower(),
            }
        )
    report = {
        "valid": not errors,
        "errors": errors[:MAX_REPORTED_ERRORS],
        "summary": {
            "run_name": manifest.run_name,
            "model_family": manifest.model_family,
            "platform": manifest.inference.platform,
            "dataset_count": len(manifest.datasets),
            "prediction_count": sum(item["prediction_count"] for item in dataset_reports),
        },
        "datasets": dataset_reports,
    }
    return _report_record(
        session,
        kind="submission",
        staged_path=staged,
        manifest=manifest.model_dump(mode="json"),
        report=report,
    )


def commit_submission_import(
    session: Session,
    report_id: str,
    *,
    commit: bool = True,
) -> InferenceSubmission:
    record = session.get(ImportValidationReport, report_id)
    if not record or record.kind != "submission":
        raise ImportValidationError("找不到推理结果校验记录")
    if record.status == "committed":
        existing = session.get(InferenceSubmission, record.report.get("submission_id"))
        if existing:
            return existing
    if not record.report.get("valid"):
        raise ImportValidationError("校验未通过，不能提交")
    manifest = ResultManifest.model_validate(record.manifest)
    verified_predictions: dict[str, list[PredictionInput]] = {}
    report_by_key = {item["dataset_key"]: item for item in record.report["datasets"]}
    for dataset_entry in manifest.datasets:
        mapping = report_by_key[dataset_entry.dataset_key]
        predictions, errors = _load_jsonl(
            Path(record.staged_path) / dataset_entry.dataset_key / "predictions.jsonl", PredictionInput,
        )
        if errors or mapping.get("predictions_sha256") != prediction_content_hash(predictions):
            raise ImportValidationError("暂存结果内容已变化或核验记录需要升级，请重新核验")
        expected = set(session.scalars(select(DatasetSample.sample_id).where(DatasetSample.dataset_version_id == mapping["dataset_version_id"])))
        if len(predictions) != len(expected) or {row.sample_id for row in predictions} != expected:
            raise ImportValidationError("预测 ID 与数据集版本不完整匹配，请重新核验")
        verified_predictions[dataset_entry.dataset_key] = predictions
    model_run = ModelRun(
        run_name=manifest.run_name,
        model_family=manifest.model_family,
        checkpoint_name=manifest.checkpoint_name,
        model_version=manifest.model_version,
        notes=manifest.model_notes,
        inference_platform=manifest.inference.platform,
        inference_mode=manifest.inference.mode,
        result_info=manifest.model_dump(mode="json"),
    )
    session.add(model_run)
    session.flush()
    submission = InferenceSubmission(
        model_run_id=model_run.id,
        status="ready",
        manifest=manifest.model_dump(mode="json"),
    )
    session.add(submission)
    session.flush()
    for dataset_entry in manifest.datasets:
        mapping = report_by_key[dataset_entry.dataset_key]
        predictions = verified_predictions[dataset_entry.dataset_key]
        submission_dataset = SubmissionDataset(
            submission_id=submission.id,
            dataset_version_id=mapping["dataset_version_id"],
            dataset_key=dataset_entry.dataset_key,
            prediction_count=len(predictions),
            dataset_content_sha256=dataset_entry.dataset_content_sha256.lower(),
        )
        session.add(submission_dataset)
        session.flush()
        for offset in range(0, len(predictions), 1_000):
            session.add_all(
                [
                    Prediction(
                        submission_dataset_id=submission_dataset.id,
                        sample_id=item.sample_id,
                        translation_zh=normalize_text(item.translation_zh),
                        predicted_language=item.predicted_language,
                        translation_hash=sha256_text(item.translation_zh),
                    )
                    for item in predictions[offset : offset + 1_000]
                ]
            )
            session.flush()
    record.status = "committed"
    record.report = {**record.report, "submission_id": submission.id}
    if commit:
        session.commit()
        session.refresh(submission)
    else:
        session.flush()
    return submission
