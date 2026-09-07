"""Independent publication/recovery checks for the background dispatcher."""
from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import event, func, select

from app import api, import_jobs, importers
from app.models import Dataset, DatasetSample, DatasetVersion, EvaluationTask, EvaluatorProfile, ImportCommitJob, ImportValidationReport, InferenceSubmission
from app.schemas import CommitSubmissionRequest
from test_background_imports import client, dataset_report, DATASET, RESULTS


def test_submission_publication_replay_does_not_create_second_task(client, session_factory):
    with session_factory() as session:
        report = importers.validate_dataset_import(session, DATASET)
        importers.commit_dataset_import(session, report.id)
        report = importers.validate_submission_import(session, RESULTS)
        report_id = report.id
        profile = session.scalar(select(EvaluatorProfile).where(EvaluatorProfile.evaluator_type == "sacrebleu_zh"))
        body = {"evaluators": [{"evaluator_revision_id": profile.revisions[0].id}], "force_reevaluate": True}
    job_id = client.post(f"/api/import-reports/{report_id}/commit-job", json=body).json()["id"]
    with session_factory() as session:
        published = api.commit_submission(report_id, CommitSubmissionRequest.model_validate(body), session)
        job = session.get(ImportCommitJob, job_id)
        job.status, job.phase = "running", "writing"
        session.commit()
        engine = session.get_bind()
    with import_jobs.import_dispatch_lock(engine) as owned:
        assert owned
        assert import_jobs.recover_import_jobs(session_factory) == 1
        assert import_jobs.process_import_job(session_factory, job_id)
    with session_factory() as session:
        job = session.get(ImportCommitJob, job_id)
        assert job.status == "completed" and job.result == published
        assert session.scalar(select(func.count(InferenceSubmission.id))) == 1
        assert session.scalar(select(func.count(EvaluationTask.id))) == 1


def test_interruption_inside_publication_rolls_back_and_replays(client, session_factory):
    report_id = dataset_report(session_factory)
    job_id = client.post(f"/api/import-reports/{report_id}/commit-job", json={}).json()["id"]
    with session_factory() as session:
        engine = session.get_bind()

    class SimulatedPowerLoss(BaseException):
        pass

    def interrupt(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO dataset_samples"):
            raise SimulatedPowerLoss()

    event.listen(engine, "before_cursor_execute", interrupt)
    try:
        with pytest.raises(SimulatedPowerLoss):
            import_jobs.process_import_job(session_factory, job_id)
    finally:
        event.remove(engine, "before_cursor_execute", interrupt)
    with session_factory() as session:
        assert session.scalar(select(func.count(Dataset.id))) == 0
        assert session.scalar(select(func.count(DatasetVersion.id))) == 0
        assert session.get(ImportValidationReport, report_id).status == "validated"
        assert session.get(ImportCommitJob, job_id).status == "running"
    with import_jobs.import_dispatch_lock(engine) as owned:
        assert owned
        assert import_jobs.recover_import_jobs(session_factory) == 1
        assert import_jobs.process_import_job(session_factory, job_id)
    with session_factory() as session:
        assert session.get(ImportCommitJob, job_id).status == "completed"
        assert session.scalar(select(func.count(DatasetVersion.id))) == 1
        assert session.scalar(select(func.count(DatasetSample.id))) == 6


def test_dispatcher_shutdown_keeps_publication_atomic_and_lease_owned(client, session_factory, monkeypatch):
    report_id = dataset_report(session_factory)
    job_id = client.post(f"/api/import-reports/{report_id}/commit-job", json={}).json()["id"]
    with session_factory() as session:
        engine = session.get_bind()
    entered, release = threading.Event(), threading.Event()
    recoveries = []
    original_recover = import_jobs.recover_import_jobs

    def recover(factory):
        recoveries.append(threading.get_ident())
        return original_recover(factory)

    def pause(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO dataset_versions"):
            entered.set()
            assert release.wait(5), "test did not release the publication"

    monkeypatch.setattr(import_jobs, "recover_import_jobs", recover)
    event.listen(engine, "after_cursor_execute", pause)
    first = import_jobs.ImportDispatcher(session_factory).start()
    second = None
    stopper = None
    try:
        assert entered.wait(2)
        second = import_jobs.ImportDispatcher(session_factory).start()
        stopper = threading.Thread(target=first.stop)
        stopper.start()
        assert first.stopping.wait(1)
        time.sleep(.05)
        assert first.thread.is_alive()
        assert len(recoveries) == 1  # The second dispatcher never recovers the live transaction.
        with session_factory() as session:
            assert session.scalar(select(func.count(DatasetVersion.id))) == 0
            assert session.get(ImportCommitJob, job_id).phase == "writing"
        release.set()
        stopper.join(timeout=3)
        assert not first.thread.is_alive()
        with session_factory() as session:
            assert session.get(ImportCommitJob, job_id).status == "completed"
            assert session.scalar(select(func.count(DatasetVersion.id))) == 1
            assert session.scalar(select(func.count(DatasetSample.id))) == 6
    finally:
        release.set()
        first.stop()
        if second:
            second.stop()
        if stopper:
            stopper.join(timeout=2)
        event.remove(engine, "after_cursor_execute", pause)
