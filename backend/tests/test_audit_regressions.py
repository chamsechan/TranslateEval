from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import api, queue
from app.database import get_session
from app.importers import ImportValidationError, commit_submission_import, validate_submission_import
from app.evaluators.base import BaseEvaluator, PermanentEvaluatorError, RetriableEvaluatorError, ScoreOutput
from app.models import EvaluationItem, EvaluationTask, EvaluatorJob, EvaluatorProfile, EvaluatorRevision, PromptVersion, ScoreResult
from app.queries import comparison_checks, task_change_version
from app.schemas import EvaluatorSelection
from test_import_and_queue import prepare_task


@pytest.fixture
def client(session_factory, monkeypatch, tmp_path):
    app = FastAPI()
    app.include_router(api.router)
    def session_dependency():
        with session_factory() as session:
            yield session
    app.dependency_overrides[get_session] = session_dependency
    monkeypatch.setattr(api, "SessionLocal", session_factory)
    monkeypatch.setattr(api, "settings", SimpleNamespace(import_dir=tmp_path / "uploads", worker_heartbeat_file=tmp_path / "heartbeat"))
    with TestClient(app) as client:
        yield client


class FakeEvaluator(BaseEvaluator):
    evaluator_type = "openai_compatible_llm"

    def __init__(self, config):
        super().__init__(config)
        self.evaluator_type = config.get("_evaluator_type", self.evaluator_type)

    @classmethod
    def validate_config(cls, config):
        return config

    @property
    def model_name(self):
        return "audit-judge"

    async def evaluate_one(self, item):
        maximum, unit = (100, "BLEU") if self.evaluator_type == "sacrebleu_zh" else (10, "point")
        return ScoreOutput(9, 0, maximum, unit)


def task_job(session_factory):
    task_id = prepare_task(session_factory)
    with session_factory() as session:
        job = session.get(EvaluationTask, task_id).dataset_jobs[0].evaluator_jobs[0]
        job.evaluator_revision.config = {**job.evaluator_revision.config, "concurrency": 1}
        session.commit()
        return task_id, job.id


def test_running_retry_is_rejected_without_resetting_failed_items(session_factory, monkeypatch):
    task_id, job_id = task_job(session_factory)
    monkeypatch.setattr(queue, "SessionLocal", session_factory)
    calls = []

    class Judge(FakeEvaluator):
        async def evaluate_one(self, item):
            calls.append(item)
            if len(calls) == 1:
                raise PermanentEvaluatorError("first item failed")
            # Wait until the writer has persisted the first response.
            if len(calls) == 2:
                for _ in range(1000):
                    with session_factory() as session:
                        if session.get(EvaluatorJob, job_id).failed_items:
                            with pytest.raises(queue.JobStateConflict):
                                queue.retry_failed_job(session, job_id)
                            break
                    await asyncio.sleep(.001)
                else:
                    pytest.fail("first failed response was not persisted")
            return await super().evaluate_one(item)

    monkeypatch.setattr(queue, "build_evaluator", lambda kind, config, **kw: Judge({"_evaluator_type": kind}))
    asyncio.run(queue.process_evaluator_job(job_id))
    with session_factory() as session:
        job = session.get(EvaluatorJob, job_id)
        assert job.status == "partial_failed"
        assert job.completed_items == 5 and job.failed_items == 1
        assert session.scalar(select(func.count()).select_from(EvaluationItem).where(EvaluationItem.status == "queued")) == 0
        queue.retry_failed_job(session, job_id)
    monkeypatch.setattr(queue, "build_evaluator", lambda kind, config, **kw: FakeEvaluator({"_evaluator_type": kind}))
    asyncio.run(queue.process_evaluator_job(job_id))
    with session_factory() as session:
        assert session.get(EvaluationTask, task_id).status == "completed"
        assert session.get(EvaluatorJob, job_id).completed_items == 6


@pytest.mark.parametrize("cancel_dataset", [False, True])
def test_cancel_stops_waiting_requests_and_preserves_inflight_score(session_factory, monkeypatch, cancel_dataset):
    task_id, job_id = task_job(session_factory)
    monkeypatch.setattr(queue, "SessionLocal", session_factory)
    calls = []

    class Judge(FakeEvaluator):
        async def evaluate_one(self, item):
            calls.append(item)
            with session_factory() as session:
                if cancel_dataset:
                    queue.cancel_dataset_job(session, session.get(EvaluatorJob, job_id).dataset_job_id)
                else:
                    queue.cancel_task(session, task_id)
            return await super().evaluate_one(item)

    monkeypatch.setattr(queue, "build_evaluator", lambda kind, config, **kw: Judge({"_evaluator_type": kind}))
    asyncio.run(queue.process_evaluator_job(job_id))
    assert len(calls) == 1
    with session_factory() as session:
        assert session.get(EvaluatorJob, job_id).status == "cancelled"
        assert session.scalar(select(func.count(ScoreResult.id))) == 1
        assert session.scalar(select(func.count(EvaluationItem.id)).where(EvaluationItem.status == "cancelled")) == 6


def test_cancel_is_checked_before_retry():
    calls = []

    class Judge(FakeEvaluator):
        async def evaluate_one(self, item):
            calls.append(item)
            raise RetriableEvaluatorError("temporary error")

    result = asyncio.run(queue._score_with_retry(Judge({}), None, 3, lambda: bool(calls)))
    assert result == (None, None, 1)
    assert len(calls) == 1


def test_fast_score_is_committed_while_slow_score_is_running(session_factory, monkeypatch):
    _, job_id = task_job(session_factory)
    monkeypatch.setattr(queue, "SessionLocal", session_factory)
    with session_factory() as session:
        job = session.get(EvaluatorJob, job_id)
        job.evaluator_revision.config = {**job.evaluator_revision.config, "concurrency": 2}
        session.commit()
    calls = []

    class Judge(FakeEvaluator):
        async def evaluate_one(self, item):
            calls.append(item)
            if len(calls) == 2:
                for _ in range(100):
                    with session_factory() as session:
                        if session.get(EvaluatorJob, job_id).completed_items:
                            break
                    await asyncio.sleep(.001)
                else:
                    pytest.fail("completed responses were not persisted while another request was pending")
            return await super().evaluate_one(item)

    monkeypatch.setattr(queue, "build_evaluator", lambda kind, config, **kw: Judge({"_evaluator_type": kind}))
    asyncio.run(queue.process_evaluator_job(job_id))


def test_compare_checks_actual_cached_prompt_and_coverage(session_factory, monkeypatch):
    task_id, _ = task_job(session_factory)
    monkeypatch.setattr(queue, "SessionLocal", session_factory)
    monkeypatch.setattr(queue, "build_evaluator", lambda kind, config, **kw: FakeEvaluator({"_evaluator_type": kind}))
    with session_factory() as session:
        profile = EvaluatorProfile(name="Judge", evaluator_type="openai_compatible_llm", enabled=True)
        session.add(profile); session.flush()
        revision = EvaluatorRevision(profile_id=profile.id, revision=1, config={"concurrency": 1}, default_threshold=8)
        session.add(revision)
        prompt_a = session.scalar(select(PromptVersion))
        prompt_b = PromptVersion(profile_id=prompt_a.profile_id, version=2, system_template="B", user_template="B", published=True)
        session.add(prompt_b); session.flush()
        submission = session.get(EvaluationTask, task_id).submission
        ids = []
        for prompt, force in [(prompt_a, True), (prompt_b, False), (prompt_b, True), (prompt_b, True)]:
            task = queue.create_evaluation_task(session, submission, [EvaluatorSelection(evaluator_revision_id=revision.id, prompt_version_id=prompt.id)], force)
            ids.append(task.dataset_jobs[0].evaluator_jobs[0].id)
    for job_id in ids:
        asyncio.run(queue.process_evaluator_job(job_id))
    with session_factory() as session:
        jobs = [session.get(EvaluatorJob, job_id) for job_id in ids]
        assert jobs[1].cached_items == 6
        check = comparison_checks(session, jobs[1:3])
        assert check["requested_config_consistent"]
        assert not check["strictly_comparable"]
        assert not check["actual_sources_match_requested"]
        assert comparison_checks(session, jobs[2:])["strictly_comparable"]
        item = session.scalar(select(EvaluationItem).where(EvaluationItem.evaluator_job_id == ids[3]))
        item.status = "failed"; session.commit()
        assert not comparison_checks(session, jobs[2:])["strictly_comparable"]


def test_old_active_task_remains_searchable_and_paginated(session_factory):
    task_id, _ = task_job(session_factory)
    with session_factory() as session:
        original = session.get(EvaluationTask, task_id)
        revision_id = original.dataset_jobs[0].evaluator_jobs[0].evaluator_revision_id
        for _ in range(55):
            task = queue.create_evaluation_task(session, original.submission, [EvaluatorSelection(evaluator_revision_id=revision_id)], False)
            task.status = "completed"
        session.commit()
        first = api.paginated_tasks(page=1, page_size=20, group="all", q="", task_id=None, session=session)
        last = api.paginated_tasks(page=3, page_size=20, group="all", q="", task_id=None, session=session)
        active = api.paginated_tasks(page=1, page_size=20, group="active", q="", task_id=None, session=session)
        assert first["total"] == 56 and len(first["items"]) == 20
        assert len(last["items"]) == 16 and last["items"][-1]["id"] == task_id
        assert [row["id"] for row in active["items"]] == [task_id]
        version = task_change_version(session)
        original.status = "running"; session.commit()
        assert task_change_version(session) != version


def test_scores_are_sorted_before_pagination(session_factory, monkeypatch):
    _, job_id = task_job(session_factory)
    monkeypatch.setattr(queue, "SessionLocal", session_factory)
    asyncio.run(queue.process_evaluator_job(job_id))
    with session_factory() as session:
        all_rows = api.evaluator_job_items(job_id, page=1, page_size=50, language=None, item_status=None, sort="id", direction="asc", session=session)
        expected = sorted(row["score"] for row in all_rows["items"])
        first = api.evaluator_job_items(job_id, page=1, page_size=2, language=None, item_status=None, sort="score", direction="asc", session=session)
        second = api.evaluator_job_items(job_id, page=2, page_size=2, language=None, item_status=None, sort="score", direction="asc", session=session)
        assert [row["score"] for row in first["items"] + second["items"]] == expected[:4]


@pytest.mark.parametrize("change", ["remove", "translation", "language"])
def test_changed_staged_predictions_cannot_be_committed(session_factory, change):
    prepare_task(session_factory)
    with session_factory() as session:
        report = validate_submission_import(session, Path(__file__).resolve().parents[2] / "examples/results/demo-run")
        staged = Path(report.staged_path) / "flores-demo/predictions.jsonl"
        rows = [json.loads(line) for line in staged.read_text().splitlines() if line.strip()]
        if change == "remove": rows.pop()
        elif change == "translation": rows[0]["translation_zh"] = "修改后的译文"
        else: rows[0]["predicted_language"] = "changed"
        staged.write_text("\n".join(json.dumps(row) for row in rows))
        with pytest.raises(ImportValidationError, match="重新核验"):
            commit_submission_import(session, report.id)


def test_http_retry_and_paginated_endpoints(client, session_factory):
    task_id, job_id = task_job(session_factory)
    response = client.post(f"/api/evaluator-jobs/{job_id}/retry-failed")
    assert response.status_code == 409
    tasks = client.get("/api/tasks/page?group=active&page_size=1").json()
    assert tasks["total"] == 1 and tasks["items"][0]["id"] == task_id
    results = client.get("/api/results/page?page_size=1").json()
    assert results["total"] == 1 and results["items"][0]["evaluator"]["id"] == job_id
    models = client.get("/api/model-runs/page?q=qwen3").json()
    assert models["total"] == 1
    assert client.get("/api/tasks/page?group=invalid").status_code == 422
    assert client.get(f"/api/evaluator-jobs/{job_id}/items?sort=invalid").status_code == 422


@pytest.mark.parametrize("kind", ["dataset", "submission"])
def test_broken_zip_returns_actionable_client_error(client, kind):
    response = client.post(f"/api/{kind}-imports/validate-upload", files={"file": ("broken.zip", b"not a zip", "application/zip")})
    assert response.status_code == 400
    assert "ZIP" in response.json()["detail"]


def test_prompt_validation_and_evaluator_ranges(client):
    prompt = {"name": "Invalid braces", "system_template": "Score", "user_template": '{translation_zh}\nReturn {"score": 8}'}
    assert client.post("/api/prompt-profiles", json=prompt).status_code == 422
    prompt["user_template"] = '{translation_zh}\nReturn {{"score": 8}}'
    assert client.post("/api/prompt-profiles", json=prompt).status_code == 200
    invalid = {"name": "Invalid tokenizer", "evaluator_type": "sacrebleu_zh", "config": {"tokenize": "invalid"}, "default_threshold": 20}
    assert client.post("/api/evaluator-profiles", json=invalid).status_code == 400
    invalid["config"] = {"tokenize": "zh"}; invalid["default_threshold"] = 1234
    assert client.post("/api/evaluator-profiles", json=invalid).status_code == 422
    llm = {"name": "Zero retries", "evaluator_type": "openai_compatible_llm", "config": {"base_url": "http://127.0.0.1:1/v1", "model": "test", "api_key": "test-only", "max_retries": 0}, "default_threshold": 11}
    assert client.post("/api/evaluator-profiles", json=llm).status_code == 400
    llm["default_threshold"] = 8
    created = client.post("/api/evaluator-profiles", json=llm).json()
    assert created["revisions"][0]["config"]["max_retries"] == 0


def test_language_with_no_successes_remains_in_summary(session_factory, monkeypatch):
    _, job_id = task_job(session_factory)
    monkeypatch.setattr(queue, "SessionLocal", session_factory)

    class Judge(FakeEvaluator):
        async def evaluate_one(self, item):
            if item.source_language == "de":
                raise PermanentEvaluatorError("German samples failed")
            return await super().evaluate_one(item)

    monkeypatch.setattr(queue, "build_evaluator", lambda kind, config, **kw: Judge({"_evaluator_type": kind}))
    asyncio.run(queue.process_evaluator_job(job_id))
    with session_factory() as session:
        summary = queue.threshold_summary(session, job_id, 8)
        german = next(row for row in summary["by_language"] if row["source_language"] == "de")
        assert german["total"] == german["failed"] == 2
        assert german["coverage"] == 0 and german["mean"] is None
        assert summary["successful"] == 4 and summary["total"] == 6


def test_live_change_event_reflects_an_old_task(session_factory, monkeypatch):
    task_id, _ = task_job(session_factory)
    monkeypatch.setattr(api, "SessionLocal", session_factory)

    async def run():
        response = await api.task_changes()
        stream = response.body_iterator
        try:
            first = await anext(stream)
            with session_factory() as session:
                session.get(EvaluationTask, task_id).status = "running"
                session.commit()
            second = await anext(stream)
            assert "event: tasks-changed" in first and "event: tasks-changed" in second
            assert first != second
        finally:
            await stream.aclose()

    asyncio.run(run())
