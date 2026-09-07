from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from app import queue
from app.evaluators.base import PermanentEvaluatorError, RetriableEvaluatorError, ScoreInput
from app.evaluators.bleu import SacreBleuZhEvaluator
from app.evaluators.llm import OpenAICompatibleEvaluator
from app.importers import commit_dataset_import, validate_dataset_import
from app.models import EvaluationItem, EvaluationTask, EvaluatorJob, ImportValidationReport, InferenceSubmission, ModelRun, Prediction, PromptVersion
from app.queries import threshold_summary
from test_audit_regressions import FakeEvaluator, client, task_job  # shared API fixture


ROOT = Path(__file__).resolve().parents[2]


async def wait_progress(factory, job_id, field, minimum):
    for _ in range(1000):
        with factory() as session:
            if getattr(session.get(EvaluatorJob, job_id), field) >= minimum:
                return
        await asyncio.sleep(.001)
    pytest.fail(f"Progress did not reach {field}={minimum}")


@pytest.mark.parametrize("cancel_dataset", [False, True])
def test_retry_preserves_cancelled_items_and_api_counts(client, session_factory, monkeypatch, cancel_dataset):
    task_id, job_id = task_job(session_factory)
    monkeypatch.setattr(queue, "SessionLocal", session_factory)

    class Judge(FakeEvaluator):
        calls = 0

        async def evaluate_one(self, item):
            self.calls += 1
            if self.calls == 1:
                raise PermanentEvaluatorError("first item failed")
            await wait_progress(session_factory, job_id, "failed_items", 1)
            with session_factory() as session:
                if cancel_dataset:
                    queue.cancel_dataset_job(session, session.get(EvaluatorJob, job_id).dataset_job_id)
                else:
                    queue.cancel_task(session, task_id)
            return await super().evaluate_one(item)

    monkeypatch.setattr(queue, "build_evaluator", lambda *a, **kw: Judge({}))
    asyncio.run(queue.process_evaluator_job(job_id))
    assert client.post(f"/api/evaluator-jobs/{job_id}/retry-failed").status_code == 200
    # Cancellation counts must remain accurate while the failed item is queued.
    pending = client.get(f"/api/tasks/{task_id}").json()
    assert pending["cancelled_items"] == 5
    monkeypatch.setattr(queue, "build_evaluator", lambda *a, **kw: FakeEvaluator({}))
    asyncio.run(queue.process_evaluator_job(job_id))
    task = client.get(f"/api/tasks/{task_id}").json()
    detail = client.get(f"/api/evaluator-jobs/{job_id}").json()["evaluator"]
    summary = client.get(f"/api/evaluator-jobs/{job_id}/summary?threshold=8").json()
    assert task["status"] == detail["status"] == "partial_cancelled"
    assert task["cancelled_items"] == detail["cancelled_items"] == summary["cancelled"] == 5
    assert summary["successful"] == 1 and summary["coverage"] == pytest.approx(1 / 6)
    with session_factory() as session:
        assert session.scalar(select(func.count(EvaluationItem.id)).where(EvaluationItem.evaluator_job_id == job_id, EvaluationItem.status == "queued")) == 0


@pytest.mark.parametrize("cancel_before_start", [False, True])
def test_bleu_retry_cancel_rebuilds_aggregates(client, session_factory, monkeypatch, cancel_before_start):
    task_id, job_id = task_job(session_factory)
    monkeypatch.setattr(queue, "SessionLocal", session_factory)

    class PartialBleu(SacreBleuZhEvaluator):
        async def evaluate_one(self, item):
            if item.source_language == "vi":
                raise PermanentEvaluatorError("controlled failure")
            return await super().evaluate_one(item)

    monkeypatch.setattr(queue, "build_evaluator", lambda kind, config, **kw: PartialBleu(config))
    asyncio.run(queue.process_evaluator_job(job_id))
    initial = client.get(f"/api/evaluator-jobs/{job_id}/summary?threshold=20").json()
    assert initial["successful"] == 4 and initial["aggregates"]
    assert client.post(f"/api/evaluator-jobs/{job_id}/retry-failed").status_code == 200
    assert client.get(f"/api/evaluator-jobs/{job_id}/summary?threshold=20").json()["aggregates"] == []

    class RetryBleu(SacreBleuZhEvaluator):
        calls = 0

        async def evaluate_one(self, item):
            self.calls += 1
            if self.calls == 2:
                await wait_progress(session_factory, job_id, "completed_items", 5)
                with session_factory() as session:
                    queue.cancel_task(session, task_id)
            return await super().evaluate_one(item)

    monkeypatch.setattr(queue, "build_evaluator", lambda kind, config, **kw: RetryBleu(config))
    if cancel_before_start:
        assert client.post(f"/api/tasks/{task_id}/cancel").status_code == 200
    asyncio.run(queue.process_evaluator_job(job_id))
    with session_factory() as session:
        summary = threshold_summary(session, job_id, 20)
        job = session.get(EvaluatorJob, job_id)
        assert job.status == "partial_cancelled"
        assert summary["successful"] == (4 if cancel_before_start else 5)
        stored_mean = next(row for row in summary["aggregates"] if row["metric_name"] == "sentence_mean" and not row["source_language"])
        corpus = next(row for row in summary["aggregates"] if row["metric_name"] == "corpus_bleu" and not row["source_language"])
        assert stored_mean["value"] == pytest.approx(summary["micro_mean"])
        assert corpus["sample_count"] == summary["successful"]
        assert corpus["value"] == pytest.approx(53.02521248314443 if cancel_before_start else 40.56319909699935)


async def response_evaluator(handler):
    evaluator = OpenAICompatibleEvaluator({"base_url": "https://judge.invalid/v1", "model": "judge", "api_key": "test-only"})
    await evaluator.client.aclose()
    evaluator.client = httpx.AsyncClient(base_url="https://judge.invalid/v1/", transport=httpx.MockTransport(handler))
    return evaluator


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [True, False, None, [], {}, "8", float("nan"), float("inf"), -1, 11])
async def test_non_numeric_and_invalid_scores_are_retriable(score):
    evaluator = await response_evaluator(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"score": score})}}]}))
    try:
        with pytest.raises(RetriableEvaluatorError):
            await evaluator.evaluate_one(ScoreInput("de", "hello", "你好", "你好"))
    finally:
        await evaluator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [0, 8.25, 10])
async def test_numeric_scores_keep_their_original_value(score):
    evaluator = await response_evaluator(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"score": score})}}]}))
    try:
        assert (await evaluator.evaluate_one(ScoreInput("de", "hello", "你好", "你好"))).score == score
    finally:
        await evaluator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"choices": []}, {"choices": [{"message": {"content": None}}]}, {"choices": [None]}, {"choices": [{"message": []}]}])
async def test_invalid_response_is_retried_before_a_valid_score(payload):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload if len(calls) == 1 else {"choices": [{"message": {"content": '{"score":9}'}}]})

    evaluator = await response_evaluator(handler)
    try:
        score, error, attempts = await queue._score_with_retry(evaluator, ScoreInput("de", "hello", "你好", "你好"), 1)
        assert score.score == 9 and error is None and attempts == len(calls) == 2
    finally:
        await evaluator.close()


@pytest.mark.asyncio
async def test_explicit_refusal_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "refusal": "refused"}}]})

    evaluator = await response_evaluator(handler)
    try:
        score, error, attempts = await queue._score_with_retry(evaluator, ScoreInput("de", "hello", "你好", "你好"), 2)
        assert score is None and "拒绝" in error and attempts == len(calls) == 1
    finally:
        await evaluator.close()


def test_blank_language_is_rejected_during_validation(client, session_factory, tmp_path):
    source = tmp_path / "blank-language"
    source.mkdir()
    manifest = {"schema_version": 1, "dataset_key": "blank", "name": "Blank", "version_label": "v1"}
    (source / "dataset_info.json").write_text(json.dumps(manifest))
    (source / "samples.jsonl").write_text(json.dumps({"sample_id": "s1", "source_language": "   ", "source_text": "hello", "reference_zh": "你好"}))
    report = client.post("/api/dataset-imports/validate", json={"path": str(source)}).json()
    assert not report["report"]["valid"]
    assert any(error.get("file") == "samples.jsonl" and "source_language" in error["message"] for error in report["report"]["errors"])
    # Legacy reports created before the fix also receive a readable 400.
    with session_factory() as session:
        legacy = session.get(ImportValidationReport, report["id"])
        legacy.report = {"valid": True}
        legacy.manifest = {**manifest, "source_languages": [{"code": "", "name_zh": "无效"}]}
        session.commit()
    assert client.post(f"/api/dataset-imports/{report['id']}/commit").status_code == 400


def test_dataset_duplicates_check_all_versions(session_factory, tmp_path):
    source = tmp_path / "versions"
    shutil.copytree(ROOT / "examples/dataset/flores-demo", source)
    with session_factory() as session:
        first = validate_dataset_import(session, source)
        first_version = commit_dataset_import(session, first.id)
        manifest = json.loads((source / "dataset_info.json").read_text())
        manifest["version_label"] = "v2"
        (source / "dataset_info.json").write_text(json.dumps(manifest))
        samples = [json.loads(line) for line in (source / "samples.jsonl").read_text().splitlines() if line.strip()]
        samples[0]["reference_zh"] += "变化"
        (source / "samples.jsonl").write_text("\n".join(json.dumps(row) for row in samples))
        second = validate_dataset_import(session, source)
        commit_dataset_import(session, second.id)
        duplicate = validate_dataset_import(session, ROOT / "examples/dataset/flores-demo")
        assert not duplicate.report["valid"]
        assert any(error.get("existing_version_id") == first_version.id for error in duplicate.report["errors"])


def test_config_revision_preserves_unsupplied_fields_and_skips_noop(client):
    config = {"base_url": "https://judge.invalid/v1", "model": "judge", "api_key": "test-key", "temperature": .7, "max_tokens": 1024, "max_retries": 0}
    created = client.post("/api/evaluator-profiles", json={"name": "Preserve parameters", "evaluator_type": "openai_compatible_llm", "config": config, "default_threshold": 8}).json()
    endpoint = f"/api/evaluator-profiles/{created['id']}/revisions"
    response = client.post(endpoint, json={"config": {"concurrency": 2, "api_key": ""}, "default_threshold": 8})
    assert response.status_code == 200
    revised = response.json()
    current = revised["revisions"][0]["config"]
    assert current["temperature"] == .7 and current["max_tokens"] == 1024 and current["max_retries"] == 0
    assert current["concurrency"] == 2 and current["has_api_key"]
    same = client.post(endpoint, json={"config": {"concurrency": 2}, "default_threshold": 8}).json()
    assert len(same["revisions"]) == 2


def test_existing_submission_can_use_new_credentials_without_reimport(client, session_factory, monkeypatch):
    original_task_id, _ = task_job(session_factory)
    original = client.get(f"/api/tasks/{original_task_id}").json()
    submission_id = original["submission_id"]
    config = {"base_url": "https://judge.invalid/v1", "model": "judge", "api_key": "old-key", "concurrency": 1, "max_retries": 0}
    profile = client.post("/api/evaluator-profiles", json={"name": "Rotate key", "evaluator_type": "openai_compatible_llm", "config": config, "default_threshold": 8}).json()
    with session_factory() as session:
        prompt_id = session.scalar(select(PromptVersion)).id
        before = [session.scalar(select(func.count(model.id))) for model in (ModelRun, InferenceSubmission, Prediction)]

    monkeypatch.setattr(queue, "SessionLocal", session_factory)

    class CredentialJudge(FakeEvaluator):
        async def evaluate_one(self, item):
            if self.config["api_key"] != "new-key":
                raise PermanentEvaluatorError("expired credentials")
            return await super().evaluate_one(item)

    monkeypatch.setattr(queue, "build_evaluator", lambda kind, config, **kw: CredentialJudge(config))

    def submit(revision_id):
        response = client.post(f"/api/submissions/{submission_id}/evaluations", json={"evaluators": [{"evaluator_revision_id": revision_id, "prompt_version_id": prompt_id}], "force_reevaluate": True})
        assert response.status_code == 200
        task = client.get(f"/api/tasks/{response.json()['task_id']}").json()
        return task["id"], task["dataset_jobs"][0]["evaluator_jobs"][0]["id"]

    old_task, old_job = submit(profile["revisions"][0]["id"])
    asyncio.run(queue.process_evaluator_job(old_job))
    revised = client.post(f"/api/evaluator-profiles/{profile['id']}/revisions", json={"config": {"api_key": "new-key"}, "default_threshold": 8}).json()
    new_task, new_job = submit(revised["revisions"][0]["id"])
    asyncio.run(queue.process_evaluator_job(new_job))
    assert client.get(f"/api/evaluator-jobs/{old_job}").json()["evaluator"]["failed_items"] == 6
    assert client.get(f"/api/evaluator-jobs/{new_job}").json()["evaluator"]["completed_items"] == 6
    assert old_task != new_task
    assert client.get(f"/api/submissions/{submission_id}").json()["summary"]["prediction_count"] == 6
    with session_factory() as session:
        assert [session.scalar(select(func.count(model.id))) for model in (ModelRun, InferenceSubmission, Prediction)] == before
        model_id = session.get(InferenceSubmission, submission_id).model_run_id
    detail = client.get(f"/api/model-runs/{model_id}").json()
    assert "decoding" in detail["result_info"]["inference"]
    assert detail["submissions"][0]["id"] == submission_id
    invalid = client.post(f"/api/submissions/{submission_id}/evaluations", json={"evaluators": [{"evaluator_revision_id": "missing"}]})
    assert invalid.status_code == 400
    with session_factory() as session:
        assert session.scalar(select(func.count(EvaluationTask.id))) == 3
