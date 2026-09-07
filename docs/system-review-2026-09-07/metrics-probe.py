"""Run from any directory with the repository virtualenv; no real LLM calls.

Example: .venv/bin/python docs/system-review-2026-09-07/metrics-probe.py
The only stdout output is the JSON evidence. All DB/import files are temporary.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def probe(temp: Path) -> dict:
    # Set paths before importing app modules or constructing their global engine.
    os.environ["TRANSLATION_EVAL_DATABASE_URL"] = f"sqlite:///{temp / 'eval.db'}"
    os.environ["TRANSLATION_EVAL_IMPORT_DIR"] = str(temp / "imports")
    os.environ["TRANSLATION_EVAL_WORKER_HEARTBEAT_FILE"] = str(temp / "heartbeat")

    import httpx
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app import api, queue
    from app.database import Base, SessionLocal, engine
    from app.evaluators.base import ScoreInput
    from app.evaluators.bleu import SacreBleuZhEvaluator
    from app.evaluators.llm import OpenAICompatibleEvaluator
    from app.importers import (
        commit_dataset_import, commit_submission_import,
        validate_dataset_import, validate_submission_import,
    )
    from app.models import (
        EvaluationItem, EvaluatorJob, EvaluatorRevision, Prediction,
        ScoreResult,
    )
    from app.queries import comparison_checks, threshold_summary
    from app.schemas import EvaluatorSelection
    from app.seed import seed_defaults

    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        seed_defaults(session)
        report = validate_dataset_import(session, ROOT / "examples/dataset/flores-demo")
        commit_dataset_import(session, report.id)
        report = validate_submission_import(session, ROOT / "examples/results/demo-run")
        submission = commit_submission_import(session, report.id)
        submission_id = submission.id

    app = FastAPI()
    app.include_router(api.router)
    result: dict = {}
    with TestClient(app, raise_server_exceptions=False) as client:
        # Valid JSON and the public API, without altering existing revisions.
        response = client.post("/api/evaluator-profiles", json={
            "name": "Audit floor smoothing", "evaluator_type": "sacrebleu_zh",
            "config": {"smooth_method": "floor", "smooth_value": 100},
            "default_threshold": 20,
        })
        result["bleu_profile_create_status"] = response.status_code
        revision_id = response.json()["revisions"][0]["id"]
        bad_type = client.post("/api/evaluator-profiles", json={
            "name": "Audit invalid smoothing type", "evaluator_type": "sacrebleu_zh",
            "config": {"smooth_method": "floor", "smooth_value": "bad"},
            "default_threshold": 20,
        })
        result["invalid_smoothing_type_create_status"] = bad_type.status_code
        invalid_llm = client.post("/api/evaluator-profiles", json={
            "name": "Audit nonfinite temperature", "evaluator_type": "openai_compatible_llm",
            "config": {"base_url": "https://judge.invalid/v1", "model": "audit",
                       "api_key": "dummy", "temperature": "nan"},
            "default_threshold": 8,
        })
        result["nonfinite_llm_config_create_status"] = invalid_llm.status_code
        invalid_revision_id = invalid_llm.json()["revisions"][0]["id"]
        result["blank_llm_fields"] = {}
        for field in ("concurrency", "timeout_seconds", "max_retries"):
            blank = client.post("/api/evaluator-profiles", json={
                "name": f"Audit blank {field}", "evaluator_type": "openai_compatible_llm",
                "config": {"base_url": "https://judge.invalid/v1", "model": "audit",
                           "api_key": "dummy", field: None},
                "default_threshold": 8,
            })
            result["blank_llm_fields"][field] = {"status": blank.status_code, "body": blank.json()}

        ids = []
        for _ in range(2):
            with SessionLocal() as session:
                from app.models import InferenceSubmission
                submission = session.get(InferenceSubmission, submission_id)
                task = queue.create_evaluation_task(
                    session, submission,
                    [EvaluatorSelection(evaluator_revision_id=revision_id)], False,
                )
                ids.append(task.dataset_jobs[0].evaluator_jobs[0].id)
        for job_id in ids:
            asyncio.run(queue.process_evaluator_job(job_id))

        with SessionLocal() as session:
            jobs = [session.get(EvaluatorJob, job_id) for job_id in ids]
            result["bleu_worker_jobs"] = []
            for job in jobs:
                summary = threshold_summary(session, job.id, 20)
                result["bleu_worker_jobs"].append({
                    "status": job.status, "completed_items": job.completed_items,
                    "summary_successful": summary["successful"],
                    "summary_unscored": summary["unscored"],
                    "summary_coverage": summary["coverage"],
                    "summary_aggregates": summary["aggregates"],
                    "stored_scores": list(session.scalars(
                        select(ScoreResult.score)
                        .join(EvaluationItem, EvaluationItem.score_result_id == ScoreResult.id)
                        .where(EvaluationItem.evaluator_job_id == job.id)
                        .order_by(EvaluationItem.id)
                    )),
                })
            result["incomplete_valid_score_comparison"] = comparison_checks(session, jobs)
            config = session.get(EvaluatorRevision, invalid_revision_id).config
            result["stored_temperature_repr"] = repr(config["temperature"])

            job = jobs[0]
            run = job.dataset_job.task.submission.model_run
            run.result_info = {**run.result_info, "inference": {
                **run.result_info["inference"], "detects_language": True,
            }}
            predictions = list(session.scalars(select(Prediction).where(
                Prediction.submission_dataset_id == job.dataset_job.submission_dataset_id,
            ).order_by(Prediction.id)))
            for prediction in predictions:
                prediction.predicted_language = None
            predictions[0].predicted_language = predictions[0].sample_id.split("-")[0]
            session.commit()
            result["one_language_prediction_of_six"] = queue.language_detection_summary(
                session, job.dataset_job_id,
            )

        # Confirm the same result-summary behavior through the public API.
        response = client.get(f"/api/evaluator-jobs/{ids[0]}/summary", params={"threshold": 20})
        result["bleu_summary_api"] = {
            "http_status": response.status_code,
            "successful": response.json()["successful"],
            "unscored": response.json()["unscored"],
            "aggregates": response.json()["aggregates"],
        }

    async def direct_probes() -> dict:
        data: dict = {}
        # MockTransport ensures the .invalid hostname can never be contacted.
        evaluator = OpenAICompatibleEvaluator(config)
        await evaluator.client.aclose()
        requests = []
        async def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"score":9}'}}]})
        evaluator.client = httpx.AsyncClient(
            base_url="https://judge.invalid/v1/", transport=httpx.MockTransport(respond),
        )
        score, error, attempts = await queue._score_with_retry(
            evaluator, ScoreInput("de", "Hallo", "你好", "你好"), 0,
        )
        data["nonfinite_temperature_scoring"] = {
            "score": score.score if score else None, "error": error,
            "attempts": attempts, "mock_requests_sent": len(requests),
        }
        await evaluator.close()

        bleu = SacreBleuZhEvaluator({})
        data["default_bleu_semantic_examples"] = []
        for reference, translation in [
            ("我支持这个决定。", "我不支持这个决定。"),
            ("请关闭窗户。", "请把窗关上。"),
        ]:
            output = await bleu.evaluate_one(ScoreInput("de", "example", reference, translation))
            data["default_bleu_semantic_examples"].append({
                "reference": reference, "translation": translation,
                "score": output.score, "passes_default_20": output.score >= 20,
            })
        invalid = SacreBleuZhEvaluator({"smooth_method": "floor", "smooth_value": "bad"})
        try:
            await invalid.evaluate_one(ScoreInput("de", "a", "你好世界", "你好中国"))
        except Exception as exc:
            data["invalid_smoothing_scoring"] = {"exception": type(exc).__name__, "error": str(exc)}
        return data

    result.update(asyncio.run(direct_probes()))
    engine.dispose()
    return result


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="translation-metrics-review-") as directory:
        print(json.dumps(probe(Path(directory)), ensure_ascii=False, indent=2, allow_nan=False))
