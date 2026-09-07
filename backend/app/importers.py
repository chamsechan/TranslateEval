from __future__ import annotations

import json
import shutil
import uuid
import zipfile
from contextlib import ExitStack
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .config import settings
from .import_index import ImportIndex, compare_ids, iter_jsonl
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
from .normalization import sha256_text
from .schemas import DatasetManifest, DatasetSampleInput, PredictionInput, ResultManifest
from .import_options import remember_manifest_options, snapshot_inference_mode


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
            manifest_name = "dataset_info.json" if kind == "dataset" else "result_info.json"
            if len(children) == 1 and children[0].is_dir() and (
                kind == "dataset" or (children[0] / manifest_name).is_file()
                or not (children[0] / "predictions.jsonl").is_file()
            ):
                nested = children[0]
                for child in list(nested.iterdir()):
                    shutil.move(str(child), target / child.name)
                nested.rmdir()
        elif source.suffix.lower() == ".jsonl":
            shutil.copy2(source, target / ("samples.jsonl" if kind == "dataset" else "predictions.jsonl"))
        else:
            raise ImportValidationError("只支持目录、.zip 或 .jsonl 文件")
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
    # Compatibility for small callers; production ingestion uses ImportIndex.
    errors: list[dict[str, Any]] = []
    return list(iter_jsonl(path, model, errors)), errors


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
    return validate_dataset_staged(session, staged)


def _dataset_diff(session: Session, latest: DatasetVersion | None, index: ImportIndex) -> dict[str, Any]:
    counters = {"added": 0, "removed": 0, "source_changed": 0, "reference_changed": 0,
                "both_changed": 0, "unchanged": 0}
    details: list[dict[str, Any]] = []
    old_rows = iter(session.execute(select(
        DatasetSample.sample_id, DatasetSample.source_language,
        DatasetSample.source_hash, DatasetSample.reference_hash,
    ).where(DatasetSample.dataset_version_id == latest.id).order_by(
        DatasetSample.sample_id,
    ).execution_options(yield_per=1_000))) if latest else iter(())
    new_rows = index.dataset_keys()
    old, new = next(old_rows, None), next(new_rows, None)
    while old is not None or new is not None:
        if new is None or (old is not None and old[0] < new[0]):
            sample_id, change = old[0], "removed"
            counters[change] += 1
            old = next(old_rows, None)
        elif old is None or new[0] < old[0]:
            sample_id, change = new[0], "added"
            counters[change] += 1
            new = next(new_rows, None)
        else:
            sample_id = old[0]
            source_changed = old[1] != new[1] or old[2] != new[2]
            reference_changed = old[3] != new[3]
            counters["source_changed"] += int(source_changed)
            counters["reference_changed"] += int(reference_changed)
            if source_changed and reference_changed:
                counters["both_changed"] += 1
                change = "source_and_reference_changed"
            elif source_changed:
                change = "source_changed"
            elif reference_changed:
                change = "reference_changed"
            else:
                counters["unchanged"] += 1
                change = "unchanged"
            old, new = next(old_rows, None), next(new_rows, None)
        if change != "unchanged" and len(details) < 200:
            details.append({"sample_id": sample_id, "change": change})
    return {"base_version_id": latest.id if latest else None,
            "base_version_label": latest.version_label if latest else None,
            **counters, "details": details}


def validate_dataset_staged(session: Session, staged: Path,
                            manifest_data: dict[str, Any] | None = None) -> ImportValidationReport:
    try:
        manifest = (DatasetManifest.model_validate(manifest_data) if manifest_data is not None
                    else _load_json(staged / "dataset_info.json", DatasetManifest))
    except (ImportValidationError, ValidationError) as exc:
        return _report_record(session, kind="dataset", staged_path=staged,
                              manifest=manifest_data or {},
                              report={"valid": False, "errors": [{"message": str(exc)}]})

    with ImportIndex(staged / "samples.jsonl", DatasetSampleInput, dataset=True) as index:
        errors = list(index.errors)
        duplicates = index.duplicate_ids()
        if duplicates:
            errors.append({"message": "存在重复 sample_id", "sample_ids": duplicates})
        if not index.count:
            errors.append({"message": "数据集必须至少包含一个样本"})
        content_hash = index.content_hash() if index.count and not errors else ""
        dataset = session.scalar(select(Dataset).where(Dataset.key == manifest.dataset_key))
        latest = None
        if dataset:
            existing_label = session.scalar(select(DatasetVersion).where(
                DatasetVersion.dataset_id == dataset.id,
                DatasetVersion.version_label == manifest.version_label,
            ))
            if existing_label:
                errors.append({"message": f"版本标签已存在: {manifest.version_label}", "existing_version_id": existing_label.id})
            existing_content = session.scalar(select(DatasetVersion).where(
                DatasetVersion.dataset_id == dataset.id,
                DatasetVersion.content_sha256 == content_hash,
            )) if content_hash else None
            if existing_content:
                errors.append({"message": "数据内容与已有版本完全相同", "existing_version_id": existing_content.id})
            latest = session.scalar(select(DatasetVersion).where(
                DatasetVersion.dataset_id == dataset.id,
            ).order_by(DatasetVersion.created_at.desc(), DatasetVersion.id.desc()).limit(1))
        report = {
            "valid": not errors, "errors": errors[:MAX_REPORTED_ERRORS],
            "summary": {"dataset_key": manifest.dataset_key, "name": manifest.name,
                        "version_label": manifest.version_label, "sample_count": index.count,
                        "languages": sorted(index.language_counts), "content_sha256": content_hash,
                        "is_new_dataset": dataset is None},
            "diff": _dataset_diff(session, latest, index),
        }
    return _report_record(session, kind="dataset", staged_path=staged,
                          manifest=manifest.model_dump(mode="json"), report=report)


def _claim_import(session: Session, record: ImportValidationReport) -> bool:
    """Own publication atomically, even with a previously loaded ORM snapshot.

    Claim and publication share a transaction; failed commits release the claim.
    File review and disk indexing happen before taking this database write lock.
    """
    if callback := session.info.get("import_writing"):
        callback()
    claimed = session.execute(update(ImportValidationReport).where(
        ImportValidationReport.id == record.id,
        ImportValidationReport.status == "validated",
    ).values(status="committing").execution_options(synchronize_session=False)).rowcount
    session.refresh(record)
    if claimed:
        return True
    if record.status == "committed":
        return False
    raise ImportValidationError("该导入正在提交或状态已变化，请刷新后重试")


def commit_dataset_import(session: Session, report_id: str) -> DatasetVersion:
    record = session.get(ImportValidationReport, report_id)
    if not record or record.kind != "dataset":
        raise ImportValidationError("找不到数据集校验记录")
    session.refresh(record)
    if record.status == "committed":
        existing = session.get(DatasetVersion, record.report.get("committed_version_id"))
        if existing:
            return existing
    if not record.report.get("valid"):
        raise ImportValidationError("校验未通过，不能导入")
    try:
        manifest = DatasetManifest.model_validate(record.manifest)
    except ValidationError as exc:
        raise ImportValidationError("数据集清单无效，请修改后重新核验") from exc
    with ImportIndex(Path(record.staged_path) / "samples.jsonl", DatasetSampleInput, dataset=True) as index:
        if index.errors or index.duplicate_ids() or not index.count:
            raise ImportValidationError("暂存文件在提交前发生变化，请重新校验")
        current_hash = index.content_hash()
        if current_hash != record.report["summary"]["content_sha256"]:
            raise ImportValidationError("暂存数据内容已变化，请重新校验")
        if not _claim_import(session, record):
            existing = session.get(DatasetVersion, record.report.get("committed_version_id"))
            if existing:
                return existing
            raise ImportValidationError("已提交版本不存在，请重新导入")
        dataset = session.scalar(select(Dataset).where(Dataset.key == manifest.dataset_key))
        if not dataset:
            dataset = Dataset(key=manifest.dataset_key, name=manifest.name, description=manifest.description)
            session.add(dataset)
            session.flush()
        languages = sorted(index.language_counts)
        existing_languages = set(session.scalars(select(Language.code).where(Language.code.in_(languages))))
        session.add_all(Language(code=code, name_zh=code) for code in languages if code not in existing_languages)
        session.flush()
        version = DatasetVersion(dataset_id=dataset.id, version_label=manifest.version_label,
                                 change_note=manifest.change_note, content_sha256=current_hash,
                                 sample_count=index.count, source_languages=languages)
        session.add(version)
        session.flush()
        for batch in index.batches():
            session.execute(DatasetSample.__table__.insert(), [
                {"dataset_version_id": version.id, **row} for row in batch
            ])
        version.language_counts = dict(index.language_counts)
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
    return validate_submission_staged(session, staged, version_overrides=version_overrides)


def prediction_path(staged: Path, dataset_key: str, dataset_count: int) -> Path:
    if dataset_count == 1 and (staged / "predictions.jsonl").is_file():
        return staged / "predictions.jsonl"
    return staged / dataset_key / "predictions.jsonl"


def validate_submission_staged(
    session: Session, staged: Path, manifest_data: dict[str, Any] | None = None,
    version_overrides: dict[str, str] | None = None,
) -> ImportValidationReport:
    overrides = version_overrides or {}
    try:
        manifest = (ResultManifest.model_validate(manifest_data) if manifest_data is not None
                    else _load_json(staged / "result_info.json", ResultManifest))
    except (ImportValidationError, ValidationError) as exc:
        return _report_record(
            session,
            kind="submission",
            staged_path=staged,
            manifest=manifest_data or {},
            report={"valid": False, "errors": [{"message": str(exc)}]},
        )

    snapshot_inference_mode(session, manifest)
    errors: list[dict[str, Any]] = []
    file_keys = {p.parent.name for p in staged.glob("*/predictions.jsonl")}
    manifest_keys = {entry.dataset_key for entry in manifest.datasets}
    if file_keys - manifest_keys:
        errors.append({"message": "存在未选择数据集的预测文件", "dataset_keys": sorted(file_keys - manifest_keys)})
    if (staged / "predictions.jsonl").is_file() and (file_keys or len(manifest.datasets) != 1):
        errors.append({"message": "根目录 predictions.jsonl 只能对应一个数据集，不能与子目录预测混用"})
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

        with ImportIndex(prediction_path(staged, dataset_entry.dataset_key, len(manifest.datasets)), PredictionInput) as index:
            errors.extend({**error, "dataset_key": dataset_entry.dataset_key} for error in index.errors)
            duplicates = index.duplicate_ids()
            if duplicates:
                errors.append({"dataset_key": dataset_entry.dataset_key,
                               "message": "预测中存在重复 sample_id", "sample_ids": duplicates})
            missing_count = unknown_count = 0
            if version:
                missing_count, missing, unknown_count, unknown = compare_ids(
                    _expected_ids(session, version.id), index.ids(),
                )
                if missing_count:
                    errors.append({"dataset_key": dataset_entry.dataset_key,
                                   "message": f"缺失 {missing_count} 条预测", "sample_ids": missing})
                if unknown_count:
                    errors.append({"dataset_key": dataset_entry.dataset_key,
                                   "message": f"存在 {unknown_count} 个未知 ID", "sample_ids": unknown})
            dataset_reports.append({
                "dataset_key": dataset_entry.dataset_key,
                "dataset_version_id": version.id if version else None,
                "version_label": version.version_label if version else None,
                "prediction_count": index.count, "predictions_sha256": index.content_hash(),
                "missing_count": missing_count, "unknown_count": unknown_count,
                "content_sha256": dataset_entry.dataset_content_sha256.lower(),
            })
    report = {
        "valid": not errors,
        "errors": errors[:MAX_REPORTED_ERRORS],
        "summary": {
            "run_name": manifest.run_name,
            "model_family": manifest.model_family,
            "checkpoint_name": manifest.checkpoint_name,
            "model_version": manifest.model_version,
            "platform": manifest.inference.platform,
            "device": manifest.inference.device,
            "sdk": manifest.inference.sdk,
            "sdk_version": manifest.inference.sdk_version,
            "precision": manifest.inference.precision,
            "mode": manifest.inference.mode,
            "detects_language": manifest.inference.detects_language,
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


def _expected_ids(session: Session, version_id: str):
    yield from session.scalars(select(DatasetSample.sample_id).where(
        DatasetSample.dataset_version_id == version_id,
    ).order_by(DatasetSample.sample_id).execution_options(yield_per=1_000))


def commit_submission_import(
    session: Session, report_id: str, *, commit: bool = True,
) -> InferenceSubmission:
    record = session.get(ImportValidationReport, report_id)
    if not record or record.kind != "submission":
        raise ImportValidationError("找不到推理结果校验记录")
    session.refresh(record)
    if record.status == "committed":
        existing = session.get(InferenceSubmission, record.report.get("submission_id"))
        if existing:
            return existing
    if not record.report.get("valid"):
        raise ImportValidationError("校验未通过，不能提交")
    manifest = ResultManifest.model_validate(record.manifest)
    report_by_key = {item["dataset_key"]: item for item in record.report["datasets"]}
    # Freeze every file before publication. Only the small index handles remain
    # in memory across datasets; prediction text stays in disposable disk files.
    with ExitStack() as stack:
        verified: dict[str, ImportIndex] = {}
        for entry in manifest.datasets:
            mapping = report_by_key[entry.dataset_key]
            index = stack.enter_context(ImportIndex(
                prediction_path(Path(record.staged_path), entry.dataset_key, len(manifest.datasets)), PredictionInput,
            ))
            if index.errors or index.duplicate_ids() or mapping.get("predictions_sha256") != index.content_hash():
                raise ImportValidationError("暂存结果内容已变化或核验记录需要升级，请重新核验")
            missing, _, unknown, _ = compare_ids(_expected_ids(session, mapping["dataset_version_id"]), index.ids())
            if missing or unknown or index.count != mapping["prediction_count"]:
                raise ImportValidationError("预测 ID 与数据集版本不完整匹配，请重新核验")
            verified[entry.dataset_key] = index
        if not _claim_import(session, record):
            existing = session.get(InferenceSubmission, record.report.get("submission_id"))
            if existing:
                return existing
            raise ImportValidationError("已提交结果不存在，请重新导入")
        remember_manifest_options(session, manifest)
        model_run = ModelRun(
            run_name=manifest.run_name, model_family=manifest.model_family,
            checkpoint_name=manifest.checkpoint_name, model_version=manifest.model_version,
            notes=manifest.model_notes, inference_platform=manifest.inference.platform,
            inference_mode=manifest.inference.mode, result_info=manifest.model_dump(mode="json"),
        )
        session.add(model_run)
        session.flush()
        submission = InferenceSubmission(model_run_id=model_run.id, status="ready", manifest=manifest.model_dump(mode="json"))
        session.add(submission)
        session.flush()
        for entry in manifest.datasets:
            mapping, index = report_by_key[entry.dataset_key], verified[entry.dataset_key]
            submission_dataset = SubmissionDataset(
                submission_id=submission.id, dataset_version_id=mapping["dataset_version_id"],
                dataset_key=entry.dataset_key, prediction_count=index.count,
                dataset_content_sha256=entry.dataset_content_sha256.lower(),
            )
            session.add(submission_dataset)
            session.flush()
            for batch in index.batches():
                session.execute(Prediction.__table__.insert(), [
                    {"submission_dataset_id": submission_dataset.id, **row} for row in batch
                ])
        record.status = "committed"
        record.report = {**record.report, "submission_id": submission.id}
        if commit:
            session.commit()
            session.refresh(submission)
        else:
            session.flush()
        return submission
