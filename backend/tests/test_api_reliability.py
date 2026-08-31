from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app import api
from app.importers import (
    commit_dataset_import,
    validate_dataset_import,
    validate_submission_import,
)
from app.models import EvaluationTask, EvaluatorProfile, ImportValidationReport, InferenceSubmission
from app.schemas import CommitSubmissionRequest, EvaluatorSelection


ROOT = Path(__file__).resolve().parents[2]


def prepare_submission_report(session):
    dataset_report = validate_dataset_import(
        session, ROOT / "examples" / "dataset" / "flores-demo"
    )
    commit_dataset_import(session, dataset_report.id)
    report = validate_submission_import(
        session, ROOT / "examples" / "results" / "demo-run"
    )
    profile = session.scalar(
        select(EvaluatorProfile).where(EvaluatorProfile.evaluator_type == "sacrebleu_zh")
    )
    assert profile is not None
    return report.id, profile.revisions[0].id


def test_task_events_static_route_precedes_dynamic_task_route() -> None:
    paths = [route.path for route in api.router.routes]
    assert paths.index("/api/tasks/events") < paths.index("/api/tasks/{task_id}")


def test_utc_iso_marks_naive_sqlite_datetime_as_utc() -> None:
    assert api.utc_iso(datetime(2026, 8, 31, 12, 30)) == "2026-08-31T12:30:00Z"


def test_worker_health_uses_recent_heartbeat(tmp_path, monkeypatch) -> None:
    heartbeat = tmp_path / "worker-heartbeat"
    heartbeat.write_text("alive", encoding="utf-8")
    monkeypatch.setattr(api, "settings", SimpleNamespace(worker_heartbeat_file=heartbeat))
    health = api.worker_health()
    assert health["status"] == "connected"
    assert health["last_seen"].endswith("Z")


def test_submission_commit_is_atomic_and_idempotent(session_factory) -> None:
    with session_factory() as session:
        report_id, revision_id = prepare_submission_report(session)
        duplicated = CommitSubmissionRequest(
            evaluators=[
                EvaluatorSelection(evaluator_revision_id=revision_id),
                EvaluatorSelection(evaluator_revision_id=revision_id),
            ]
        )
        with pytest.raises(HTTPException):
            api.commit_submission(report_id, duplicated, session)

        record = session.get(ImportValidationReport, report_id)
        assert record is not None and record.status != "committed"
        assert session.scalar(select(func.count(InferenceSubmission.id))) == 0
        assert session.scalar(select(func.count(EvaluationTask.id))) == 0

        valid = CommitSubmissionRequest(
            evaluators=[EvaluatorSelection(evaluator_revision_id=revision_id)]
        )
        first = api.commit_submission(report_id, valid, session)
        second = api.commit_submission(report_id, valid, session)
        assert second == first
        assert session.scalar(select(func.count(InferenceSubmission.id))) == 1
        assert session.scalar(select(func.count(EvaluationTask.id))) == 1
