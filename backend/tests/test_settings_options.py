from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import api, import_api
from app.database import get_session
from app.import_options import remember_manifest_options, remember_option, snapshot_inference_mode
from app.importers import commit_dataset_import, commit_submission_import, validate_dataset_import
from app.models import ImportOption, ModelRun
from app.schemas import ImportManifestRequest, ResultManifest
from app.seed import seed_defaults

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def client(session_factory):
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(import_api.router)

    def sessions():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = sessions
    with TestClient(app) as client:
        yield client


def manifest(**inference):
    return ResultManifest.model_validate({
        "schema_version": 1, "run_name": "SDK run", "model_family": "test-model",
        "checkpoint_name": "checkpoint", "inference": {
            "platform": "test-platform", "device": "test-device", "precision": "int4",
            "mode": "thinking", **inference,
        },
        "datasets": [{"dataset_key": "demo", "dataset_content_sha256": "a" * 64}],
    })


def add_run(session, metadata):
    run = ModelRun(
        run_name=metadata.run_name, model_family=metadata.model_family,
        checkpoint_name=metadata.checkpoint_name, inference_platform=metadata.inference.platform,
        inference_mode=metadata.inference.mode, result_info=metadata.model_dump(mode="json"),
    )
    session.add(run)
    session.commit()
    return run


@pytest.mark.parametrize("category,value", [
    ("model", "test-model"), ("platform", "test-platform"), ("device", "test-device"),
    ("precision", "int4"), ("inference_mode", "thinking"),
])
def test_used_options_require_confirmation_and_keep_results(client, session_factory, category, value):
    with session_factory() as session:
        remember_option(session, category, value)
        run = add_run(session, manifest())
        original = run.result_info
        run_id = run.id

    option = next(row for row in client.get("/api/import-options").json()
                  if row["category"] == category and row["value"] == value)
    assert option["has_results"] is True
    url = f'/api/import-options/{option["id"]}'
    response = client.delete(url)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "confirmation_required"
    assert any(row["id"] == option["id"] for row in client.get("/api/import-options").json())
    assert client.delete(url + "?confirm=true").status_code == 200
    assert client.delete(url).status_code == 404
    assert client.patch(url, json={"label": "deleted"}).status_code == 404

    with session_factory() as session:
        # Neither startup's historical backfill nor another import restores a
        # deleted choice; immutable results still carry its original metadata.
        seed_defaults(session)
        remember_manifest_options(session, manifest())
        session.commit()
        assert session.get(ImportOption, option["id"]).deleted is True
        assert session.get(ModelRun, run_id).result_info == original
    assert all(row["id"] != option["id"] for row in client.get("/api/import-options").json())


@pytest.mark.parametrize("category", ["model", "device", "precision", "inference_mode", "platform"])
def test_unused_options_delete_directly(client, category):
    created = client.post("/api/import-options", json={
        "category": category, "value": "unused", "label": "未使用",
    }).json()
    assert created["has_results"] is False
    assert client.delete(f'/api/import-options/{created["id"]}').status_code == 200


def test_deleted_seeded_option_stays_deleted_and_explicit_create_restores(client, session_factory):
    original = next(row for row in client.get("/api/import-options?category=model").json()
                    if row["value"] == "qwen3-0.6b")
    assert client.delete(f'/api/import-options/{original["id"]}').status_code == 200
    with session_factory() as session:
        seed_defaults(session)
    assert original["id"] not in {row["id"] for row in client.get("/api/import-options").json()}
    response = client.post("/api/import-options", json={
        "category": "model", "value": "qwen3-0.6b", "label": "重新添加",
    })
    assert response.status_code == 201
    restored = response.json()
    assert restored["id"] == original["id"]
    assert restored["enabled"] is True and restored["label"] == "重新添加"


def test_result_usage_is_matched_by_category(client, session_factory):
    with session_factory() as session:
        add_run(session, manifest())
    response = client.post("/api/import-options", json={
        "category": "precision", "value": "test-device", "label": "同值不同分类",
    })
    assert response.status_code == 201 and response.json()["has_results"] is False
    assert client.delete(f'/api/import-options/{response.json()["id"]}').status_code == 200


def test_device_sdk_validation_and_platform_changes(client):
    fields = {"category": "device", "value": "board", "label": "设备", "sdk": "SDK A"}
    assert client.post("/api/import-options", json=fields).status_code == 422
    assert client.post("/api/import-options", json={**fields, "category": "model", "platform": "vendor"}).status_code == 422
    created = client.post("/api/import-options", json={
        **fields, "platform": "vendor-a", "sdk_version": "1.2",
    })
    assert created.status_code == 201
    device = created.json()
    assert device["platform"] == "vendor-a" and device["sdk"] == "SDK A" and device["sdk_version"] == "1.2"
    assert "vendor-a" in {row["value"] for row in client.get("/api/import-options?category=platform").json()}
    url = f'/api/import-options/{device["id"]}'
    response = client.patch(url, json={"label": "重命名"})
    assert response.json()["sdk_version"] == "1.2"
    response = client.patch(url, json={"label": "重命名", "sdk": "SDK B"})
    assert response.json()["sdk"] == "SDK B" and response.json()["sdk_version"] == ""
    response = client.patch(url, json={"label": "重命名", "platform": "vendor-b"})
    assert response.json()["platform"] == "vendor-b"
    assert response.json()["sdk"] == "" and response.json()["sdk_version"] == ""
    assert client.patch(url, json={"label": "重命名", "platform": "", "sdk": "invalid"}).status_code == 422
    assert next(row for row in client.get("/api/import-options?category=device").json()
                if row["id"] == device["id"])["platform"] == "vendor-b"


def test_seed_devices_and_thinking_modes(client, session_factory):
    modes = client.get("/api/import-options?category=inference_mode").json()
    assert {(row["value"], row["label"]) for row in modes} == {
        ("default", "默认"), ("thinking", "思考"), ("non_thinking", "不思考"),
    }
    devices = client.get("/api/import-options?category=device").json()
    assert {(row["value"], row["label"], row["platform"]) for row in devices} == {
        ("nvidia-1080ti", "英伟达1080Ti", "nvidia"),
        ("nvidia-2080ti", "英伟达2080Ti", "nvidia"), ("ax650", "爱芯650", "axera"),
    }
    with session_factory() as session:
        for mode in ("default", "thinking", "non_thinking"):
            metadata = manifest(mode=mode)
            snapshot_inference_mode(session, metadata)
            assert metadata.inference.detects_language is False
            metadata = manifest(mode=mode, detects_language=True)
            snapshot_inference_mode(session, metadata)
            assert metadata.inference.detects_language is True
        legacy = manifest(mode="auto_detect")
        snapshot_inference_mode(session, legacy)
        assert legacy.inference.detects_language is True


def test_sdk_manifest_survives_validation_commit_and_option_backfill(client, session_factory):
    with session_factory() as session:
        dataset_report = validate_dataset_import(session, ROOT / "examples/dataset/flores-demo")
        version = commit_dataset_import(session, dataset_report.id)
        draft = import_api.prepare_import(session, ROOT / "examples/results/demo-run", "submission")
        metadata = manifest(platform="nvidia", device="nvidia-1080ti", sdk="CUDA", sdk_version="12.6")
        edited = metadata.model_dump(mode="json")
        edited["datasets"] = [{"dataset_key": "flores-demo", "dataset_content_sha256": version.content_sha256}]
        validated = import_api.validate_draft(draft.id, ImportManifestRequest(manifest=edited), session)
        assert validated["report"]["valid"]
        assert validated["report"]["summary"]["sdk"] == "CUDA"
        assert validated["report"]["summary"]["sdk_version"] == "12.6"
        submission = commit_submission_import(session, validated["id"])
        submission_id = submission.id
        assert submission.manifest["inference"]["sdk"] == "CUDA"
        assert submission.model_run.result_info["inference"]["sdk_version"] == "12.6"
        device = session.scalar(select(ImportOption).where(
            ImportOption.category == "device", ImportOption.value == "nvidia-1080ti",
        ))
        assert (device.platform, device.sdk, device.sdk_version) == ("nvidia", "CUDA", "12.6")
        remember_option(session, "device", "nvidia-1080ti", platform="axera", sdk="AX SDK", sdk_version="9")
        assert (device.platform, device.sdk, device.sdk_version) == ("nvidia", "CUDA", "12.6")
    summary = client.get(f"/api/submissions/{submission_id}").json()["summary"]
    assert summary["sdk"] == "CUDA" and summary["sdk_version"] == "12.6"


@pytest.mark.parametrize("field", ["sdk", "sdk_version"])
def test_prefill_rejects_nontext_sdk_fields(field):
    with pytest.raises(ValueError, match="必须是文本"):
        import_api.check_prefill_structure({"inference": {field: {"bad": "value"}}}, "submission")
