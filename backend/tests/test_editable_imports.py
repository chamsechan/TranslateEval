from __future__ import annotations

import copy
import json
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import api, import_api
from app.database import get_session
from app.importers import (
    ImportValidationError, commit_dataset_import, commit_submission_import,
    validate_dataset_import,
)
from app.models import DatasetVersion, EvaluatorProfile, ImportOption, Language, ModelRun
from app.queue import create_evaluation_task, language_detection_summary
from app.schemas import EvaluatorSelection, ImportManifestRequest

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "examples/dataset/flores-demo"
RESULTS = ROOT / "examples/results/demo-run"


@pytest.fixture()
def client(session_factory, tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(import_api.router)
    def sessions():
        with session_factory() as session:
            yield session
    app.dependency_overrides[get_session] = sessions
    monkeypatch.setattr(import_api, "settings", SimpleNamespace(import_dir=tmp_path / "uploads"))
    with TestClient(app) as client:
        yield client


def prepare_dataset(session):
    report = validate_dataset_import(session, DATASET)
    return commit_dataset_import(session, report.id)


def test_dataset_without_manifest_can_be_completed_and_committed(session_factory):
    with session_factory() as session:
        draft = import_api.prepare_import(session, DATASET / "samples.jsonl", "dataset")
        assert draft.status == "draft"
        assert not draft.report["has_manifest"]
        assert "source_languages" not in draft.manifest
        assert draft.report["detected_languages"] == ["de", "th", "vi"]
        with pytest.raises(ImportValidationError, match="校验未通过"):
            commit_dataset_import(session, draft.id)
        edited = {**draft.manifest, "dataset_key": "manual", "name": "界面填写", "version_label": "v1"}
        result = import_api.validate_draft(draft.id, ImportManifestRequest(manifest=edited), session)
        assert result["report"]["valid"]
        version = commit_dataset_import(session, result["id"])
        assert version.dataset.name == "界面填写" and version.sample_count == 6


@pytest.mark.parametrize("editable", [False, True], ids=["direct", "editable"])
@pytest.mark.parametrize("legacy_languages", [False, True], ids=["no-languages", "legacy-languages"])
def test_dataset_languages_are_inferred_from_samples(session_factory, tmp_path, editable, legacy_languages):
    source = tmp_path / "inferred-languages"
    source.mkdir()
    manifest = {"schema_version": 1, "dataset_key": "inferred", "name": "自动识别语种", "version_label": "v1"}
    if legacy_languages:
        manifest["source_languages"] = [
            {"code": "de", "name_zh": "过时名称"},
            {"code": "unused", "name_zh": "未使用语种"},
        ]
    (source / "dataset_info.json").write_text(json.dumps(manifest))
    samples = [
        {"sample_id": f"s{i}", "source_language": language, "source_text": "hello", "reference_zh": "你好"}
        for i, language in enumerate([" ZZ ", " DE ", "zz"])
    ]
    (source / "samples.jsonl").write_text("\n".join(json.dumps(row) for row in samples))
    with session_factory() as session:
        session.add(Language(code="de", name_zh="已维护的德语名称"))
        session.commit()
        if editable:
            draft = import_api.prepare_import(session, source, "dataset")
            assert "source_languages" not in draft.manifest
            assert draft.report["detected_languages"] == ["de", "zz"]
            validated = import_api.validate_draft(draft.id, ImportManifestRequest(manifest=draft.manifest), session)
            report_id, report, saved_manifest = validated["id"], validated["report"], validated["manifest"]
        else:
            validated = validate_dataset_import(session, source)
            report_id, report, saved_manifest = validated.id, validated.report, validated.manifest
        assert report["valid"], report["errors"]
        assert report["summary"]["languages"] == ["de", "zz"]
        assert "source_languages" not in saved_manifest
        version = commit_dataset_import(session, report_id)
        assert version.source_languages == ["de", "zz"]
        assert sorted(sample.source_language for sample in version.samples) == ["de", "zz", "zz"]
        assert session.get(Language, "de").name_zh == "已维护的德语名称"
        assert session.get(Language, "zz").name_zh == "zz"
        assert session.get(Language, "unused") is None


def test_manifest_prefill_edits_are_saved_without_modifying_source(session_factory, tmp_path):
    source = tmp_path / "source"
    shutil.copytree(DATASET, source)
    before = (source / "dataset_info.json").read_bytes()
    with session_factory() as session:
        draft = import_api.prepare_import(session, source, "dataset")
        assert draft.report["has_manifest"]
        edited = {**draft.manifest, "name": "编辑后名称", "version_label": "edited", "change_note": "编辑后备注"}
        result = import_api.validate_draft(draft.id, ImportManifestRequest(manifest=edited), session)
        version = commit_dataset_import(session, result["id"])
        assert version.dataset.name == "编辑后名称"
        assert version.version_label == "edited" and version.change_note == "编辑后备注"
    assert (source / "dataset_info.json").read_bytes() == before


def test_result_without_manifest_selects_version_and_preserves_snapshot(session_factory):
    with session_factory() as session:
        version = prepare_dataset(session)
        draft = import_api.prepare_import(session, RESULTS / "flores-demo/predictions.jsonl", "submission")
        assert draft.report["root_predictions"]
        assert "model_family" not in draft.manifest
        manifest = {
            "schema_version": 1, "run_name": "manual", "model_family": "new-model",
            "checkpoint_name": "free text step 1200", "inference": {
                "platform": "new-platform", "device": "NPU", "precision": "int4", "mode": "custom-mode",
            }, "datasets": [{"dataset_key": "flores-demo", "dataset_content_sha256": version.content_sha256}],
        }
        option = ImportOption(category="inference_mode", value="custom-mode", label="自定义识别", detects_language=True)
        session.add(option); session.commit()
        result = import_api.validate_draft(draft.id, ImportManifestRequest(manifest=manifest), session)
        assert result["report"]["valid"] and result["report"]["root_predictions"]
        # Changes to maintained options after validation cannot change the reviewed snapshot.
        option.detects_language = False
        session.commit()
        submission = commit_submission_import(session, result["id"])
        assert submission.model_run.detects_language
        assert submission.model_run.checkpoint_name == "free text step 1200"
        assert submission.model_run.result_info["inference"]["precision"] == "int4"
        saved = session.scalar(select(ImportOption).where(ImportOption.category == "model", ImportOption.value == "new-model"))
        assert saved is not None
        assert len(submission.datasets[0].predictions) == 6
        profile = session.scalar(select(EvaluatorProfile).where(EvaluatorProfile.evaluator_type == "sacrebleu_zh"))
        task = create_evaluation_task(session, submission, [EvaluatorSelection(evaluator_revision_id=profile.revisions[0].id)], False)
        assert language_detection_summary(session, task.dataset_jobs[0].id)["applicable"] is True


def test_result_prefill_can_edit_nested_metadata_and_keep_advanced_fields(session_factory):
    with session_factory() as session:
        prepare_dataset(session)
        draft = import_api.prepare_import(session, RESULTS, "submission")
        edited = copy.deepcopy(draft.manifest)
        edited["model_family"] = "edited-model"
        edited["checkpoint_name"] = "text-checkpoint"
        edited["inference"].update(platform="edited-platform", precision="bf16", mode="auto_detect")
        result = import_api.validate_draft(draft.id, ImportManifestRequest(manifest=edited), session)
        assert result["report"]["valid"]
        submission = commit_submission_import(session, result["id"])
        saved = submission.model_run.result_info
        assert saved["model_family"] == "edited-model"
        assert saved["inference"]["decoding"] == draft.manifest["inference"]["decoding"]
        assert saved["inference"]["code_revision"] == "demo"
        assert submission.model_run.detects_language


def test_result_bad_version_and_missing_predictions_still_block_commit(session_factory, tmp_path):
    source = tmp_path / "results"
    shutil.copytree(RESULTS, source)
    with session_factory() as session:
        prepare_dataset(session)
        draft = import_api.prepare_import(session, source, "submission")
        edited = copy.deepcopy(draft.manifest)
        edited["datasets"][0]["dataset_content_sha256"] = "0" * 64
        result = import_api.validate_draft(draft.id, ImportManifestRequest(manifest=edited), session)
        assert not result["report"]["valid"]
        with pytest.raises(ImportValidationError):
            commit_submission_import(session, result["id"])
        predictions = Path(draft.staged_path) / "flores-demo/predictions.jsonl"
        predictions.write_text(predictions.read_text().splitlines()[0] + "\n")
        result = import_api.validate_draft(draft.id, ImportManifestRequest(manifest=draft.manifest), session)
        assert not result["report"]["valid"]
        assert any("缺失 5 条预测" in item["message"] for item in result["report"]["errors"])
        assert session.scalar(select(func.count(ModelRun.id))) == 0


def test_prepared_samples_are_rechecked_at_commit(session_factory):
    with session_factory() as session:
        draft = import_api.prepare_import(session, DATASET, "dataset")
        result = import_api.validate_draft(draft.id, ImportManifestRequest(manifest=draft.manifest), session)
        (Path(draft.staged_path) / "samples.jsonl").write_text('{}\n')
        with pytest.raises(ImportValidationError):
            commit_dataset_import(session, result["id"])
        assert session.scalar(select(func.count(DatasetVersion.id))) == 0


@pytest.mark.parametrize("wrapped", [False, True])
def test_zip_without_result_info_preserves_dataset_folder(client, tmp_path, wrapped):
    archive = tmp_path / "results.zip"
    name = ("run/" if wrapped else "") + "flores-demo/predictions.jsonl"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.write(RESULTS / "flores-demo/predictions.jsonl", name)
    response = client.post('/api/submission-imports/prepare-upload', files={"file": ("results.zip", archive.read_bytes(), "application/zip")})
    assert response.status_code == 200
    data = response.json()
    assert data["manifest"]["datasets"] == [{"dataset_key": "flores-demo"}]
    assert data["report"]["root_predictions"] is False


def test_jsonl_upload_then_edit_and_validate_over_http(client):
    response = client.post('/api/dataset-imports/prepare-upload', files={"file": ("data.jsonl", (DATASET / "samples.jsonl").read_bytes())})
    assert response.status_code == 200
    draft = response.json()
    response = client.post(f'/api/import-reports/{draft["id"]}/validate', json={"manifest": draft["manifest"]})
    assert response.status_code == 200 and not response.json()["report"]["valid"]
    manifest = {**draft["manifest"], "dataset_key": "upload", "name": "上传", "version_label": "v1"}
    response = client.post(f'/api/import-reports/{draft["id"]}/validate', json={"manifest": manifest})
    report = response.json()
    assert report["report"]["valid"]
    assert client.post(f'/api/dataset-imports/{report["id"]}/commit').status_code == 200
    assert client.post(f'/api/import-reports/{report["id"]}/validate', json={"manifest": manifest}).status_code == 409


def test_option_maintenance_validation_and_disable_persistence(client, session_factory):
    response = client.post('/api/import-options', json={"category": "model", "value": "custom", "label": "自定义模型"})
    assert response.status_code == 201
    option = response.json()
    assert client.post('/api/import-options', json={"category": "model", "value": "custom", "label": "重复"}).status_code == 409
    response = client.patch(f'/api/import-options/{option["id"]}', json={"label": "新显示名称", "enabled": False})
    assert response.status_code == 200 and not response.json()["enabled"]
    assert response.json()["value"] == "custom"
    response = client.patch(f'/api/import-options/{option["id"]}', json={"label": "仅修改名称"})
    assert response.status_code == 200 and not response.json()["enabled"]
    for invalid in [
        {"category": "unknown", "value": "x", "label": "x"},
        {"category": "model", "value": "  ", "label": "x"},
        {"category": "device", "value": "x", "label": "x", "detects_language": True},
        {"category": "inference_mode", "value": "x" * 41, "label": "x"},
    ]:
        assert client.post('/api/import-options', json=invalid).status_code == 422
    assert client.patch(f'/api/import-options/{option["id"]}', json={"label": "x", "detects_language": True}).status_code == 422
    from app.seed import seed_defaults
    with session_factory() as session:
        seed_defaults(session)
        assert session.get(ImportOption, option["id"]).enabled is False


def test_invalid_json_manifest_reports_error(client, tmp_path):
    source = tmp_path / "bad-json"
    source.mkdir()
    (source / "dataset_info.json").write_text('{invalid')
    response = client.post('/api/dataset-imports/prepare', json={"path": str(source)})
    assert response.status_code == 400 and "格式错误" in response.json()["detail"]


@pytest.mark.parametrize("manifest", [
    {"inference": []}, {"model_family": {"bad": "shape"}},
    {"datasets": "bad"}, {"datasets": [{"dataset_content_sha256": 123}]},
])
def test_invalid_prefill_shapes_report_errors_instead_of_breaking_form(client, tmp_path, manifest):
    source = tmp_path / "bad-prefill"
    source.mkdir()
    (source / "result_info.json").write_text(json.dumps(manifest))
    response = client.post('/api/submission-imports/prepare', json={"path": str(source)})
    assert response.status_code == 400
