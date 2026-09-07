from __future__ import annotations

from pathlib import Path
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
import pytest

from app import api, import_api, importers
from app.database import get_session
from app.import_jobs import ImportDispatcher, import_dispatch_lock, process_import_job, recover_import_jobs
from app.models import DatasetVersion, EvaluationTask, EvaluatorProfile, ImportCommitJob, InferenceSubmission

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "examples/dataset/flores-demo"
RESULTS = ROOT / "examples/results/demo-run"


@pytest.fixture
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


def dataset_report(session_factory):
    with session_factory() as session:
        return importers.validate_dataset_import(session, DATASET).id


def test_background_dataset_is_persistent_and_duplicate_requests_reuse_job(client, session_factory):
    report_id = dataset_report(session_factory)
    response = client.post(f"/api/import-reports/{report_id}/commit-job", json={})
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "queued"
    assert client.post(f"/api/import-reports/{report_id}/commit-job", json={}).json()["id"] == job["id"]
    assert client.get("/api/import-commit-jobs").json()[0]["id"] == job["id"]
    with session_factory() as session:
        assert session.scalar(select(func.count(DatasetVersion.id))) == 0
    dispatcher = ImportDispatcher(session_factory).start()
    try:
        for _ in range(100):
            latest = client.get(f"/api/import-commit-jobs/{job['id']}").json()
            if latest["status"] in ("completed", "failed"):
                break
            time.sleep(.02)
        assert latest["status"] == "completed", latest
        assert latest["result"]["dataset_version_id"]
        assert latest["started_at"] and latest["finished_at"]
    finally:
        dispatcher.stop()


def test_restart_after_publication_before_job_completion_is_idempotent(client, session_factory):
    report_id = dataset_report(session_factory)
    job = client.post(f"/api/import-reports/{report_id}/commit-job", json={}).json()
    # Simulate a process dying after atomic publication, before recording the
    # asynchronous operation's result. The report already contains its version.
    with session_factory() as session:
        version = importers.commit_dataset_import(session, report_id)
        operation = session.get(ImportCommitJob, job["id"])
        operation.status = "running"
        session.commit()
        version_id = version.id
        engine = session.get_bind()
    with import_dispatch_lock(engine) as owned:
        assert owned
        with import_dispatch_lock(engine) as second:
            assert not second
        assert recover_import_jobs(session_factory) == 1
        assert process_import_job(session_factory, job["id"])
    latest = client.get(f"/api/import-commit-jobs/{job['id']}").json()
    assert latest["status"] == "completed" and latest["result"]["dataset_version_id"] == version_id
    with session_factory() as session:
        assert session.scalar(select(func.count(DatasetVersion.id))) == 1


def test_failed_submission_retries_saved_request_after_page_state_is_lost(client, session_factory):
    with session_factory() as session:
        report = importers.validate_dataset_import(session, DATASET)
        importers.commit_dataset_import(session, report.id)
        report = importers.validate_submission_import(session, RESULTS)
        report_id = report.id
        predictions = Path(report.staged_path) / "flores-demo/predictions.jsonl"
        original = predictions.read_bytes()
        profile = session.scalar(select(EvaluatorProfile).where(EvaluatorProfile.evaluator_type == "sacrebleu_zh"))
        request = {"evaluators": [{"evaluator_revision_id": profile.revisions[0].id}], "force_reevaluate": True}
    job = client.post(f"/api/import-reports/{report_id}/commit-job", json=request).json()
    predictions.write_text('{}\n')
    assert process_import_job(session_factory, job["id"])
    latest = client.get(f"/api/import-commit-jobs/{job['id']}").json()
    assert latest["status"] == "failed" and latest["error"]
    with session_factory() as session:
        assert session.scalar(select(func.count(InferenceSubmission.id))) == 0
    predictions.write_bytes(original)
    retried = client.post(f"/api/import-reports/{report_id}/commit-job", json={}).json()
    assert retried["id"] == job["id"] and retried["status"] == "queued"
    assert process_import_job(session_factory, job["id"])
    latest = client.get(f"/api/import-commit-jobs/{job['id']}").json()
    assert latest["status"] == "completed"
    assert latest["result"]["task_id"] and latest["result"]["submission_id"]
    with session_factory() as session:
        task = session.get(EvaluationTask, latest["result"]["task_id"])
        assert task.force_reevaluate
        assert session.scalar(select(func.count(InferenceSubmission.id))) == 1
        assert session.scalar(select(func.count(EvaluationTask.id))) == 1


def test_invalid_reports_and_requests_cannot_queue_work(client, session_factory):
    report_id = dataset_report(session_factory)
    assert client.post("/api/import-reports/missing/commit-job", json={}).status_code == 404
    with session_factory() as session:
        from app.models import ImportValidationReport
        report = session.get(ImportValidationReport, report_id)
        report.report = {**report.report, "valid": False}
        session.commit()
    assert client.post(f"/api/import-reports/{report_id}/commit-job", json={}).status_code == 400
