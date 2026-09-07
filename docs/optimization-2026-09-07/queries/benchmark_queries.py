#!/usr/bin/env python3
"""Isolated, repeatable synthetic API benchmarks; never opens the application DB.

Usage (from repository root):
  .venv/bin/python docs/optimization-2026-09-07/queries/benchmark_queries.py seed /tmp/te-scale-24000.db --samples 24000
  .venv/bin/python docs/optimization-2026-09-07/queries/benchmark_queries.py run /tmp/te-scale-24000.db
  .venv/bin/python docs/optimization-2026-09-07/queries/benchmark_queries.py history /tmp/te-scale-24000.db --jobs 6
  .venv/bin/python docs/optimization-2026-09-07/queries/benchmark_queries.py run /tmp/te-scale-24000.db --cases results_one results_mean_one results_default

Each query case runs in a fresh subprocess with a 90 second timeout. The fixture
uses Core batches, bypasses the importer/worker, and is NOT an import or scoring
throughput benchmark. Two immutable full dataset versions, 40 languages, two
model submissions with complete independent synthetic 0..10 scores. Additional
history jobs reuse the first model's score records, like successful cache hits.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
from datetime import UTC, datetime

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

LANGS = "en fr de es pt it nl sv da no fi pl cs sk hu ro bg el ru uk tr ar he fa hi bn ta te ur id ms vi th ko ja sw am ha yo zu".split()
NOW = datetime(2026, 9, 7, tzinfo=UTC)


def uid(kind, number=0):
    return hashlib.md5(f"scale:{kind}:{number}".encode()).hexdigest()


def digest(kind, number):
    return hashlib.sha256(f"{kind}:{number}".encode()).hexdigest()


def rss_mb():
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return None


def connect(path):
    # Set before importing modules: even their unused global engine is isolated.
    os.environ["TRANSLATION_EVAL_DATABASE_URL"] = f"sqlite:///{path}"
    from app.database import create_db_engine
    return create_db_engine(f"sqlite:///{path}")


def seed(path, samples):
    if path.exists():
        raise SystemExit(f"Refusing to replace existing fixture: {path}")
    if samples % 40:
        raise SystemExit("samples must be divisible by 40")
    from app import models as m
    from app.database import Base
    engine = connect(path)
    Base.metadata.create_all(engine)
    start = time.perf_counter()
    with engine.begin() as c:
        c.execute(m.Language.__table__.insert(), [dict(code=x, name_zh=x) for x in LANGS])
        c.execute(m.Dataset.__table__.insert(), dict(id=uid("dataset"), key="scale", name="Scale dataset", description="synthetic"))
        c.execute(m.DatasetVersion.__table__.insert(), [dict(id=uid("version", v), dataset_id=uid("dataset"), version_label=f"v{v}", content_sha256=digest("version", v), sample_count=samples, source_languages=LANGS, created_at=NOW) for v in [1, 2]])
        c.execute(m.EvaluatorProfile.__table__.insert(), dict(id=uid("profile"), name="Synthetic judge", evaluator_type="openai_compatible_llm"))
        c.execute(m.EvaluatorRevision.__table__.insert(), dict(id=uid("revision"), profile_id=uid("profile"), revision=1, config={"model": "synthetic"}, default_threshold=7))
        for job in [1, 2]:
            c.execute(m.ModelRun.__table__.insert(), dict(id=uid("model", job), run_name=f"Model {job}", model_family="synthetic", checkpoint_name=f"ckpt-{job}", inference_platform="synthetic", inference_mode="source_specified", result_info={}))
            c.execute(m.InferenceSubmission.__table__.insert(), dict(id=uid("submission", job), model_run_id=uid("model", job), manifest={}))
            c.execute(m.SubmissionDataset.__table__.insert(), dict(id=uid("submission_dataset", job), submission_id=uid("submission", job), dataset_version_id=uid("version", 2), dataset_key="scale", prediction_count=samples, dataset_content_sha256=digest("version", 2)))
            c.execute(m.EvaluationTask.__table__.insert(), dict(id=uid("task", job), submission_id=uid("submission", job), status="completed", total_items=samples, completed_items=samples, started_at=NOW, finished_at=NOW))
            c.execute(m.DatasetJob.__table__.insert(), dict(id=uid("dataset_job", job), task_id=uid("task", job), submission_dataset_id=uid("submission_dataset", job), status="completed", total_items=samples, completed_items=samples))
            c.execute(m.EvaluatorJob.__table__.insert(), dict(id=uid("job", job), dataset_job_id=uid("dataset_job", job), evaluator_revision_id=uid("revision"), status="completed", total_items=samples, completed_items=samples))
    for offset in range(0, samples, 4000):
        base = []
        for i in range(offset, min(offset + 4000, samples)):
            lang = LANGS[i // (samples // 40)]
            base.append(dict(sample_id=f"{lang}-{i:08d}", source_language=lang, source_text=(f"Synthetic source {i}. " + "A multilingual benchmark sentence. " * 4), reference_zh=(f"第{i}条参考译文。" + "这是一条用于评测容量和查询性能的合成参考译文。" * 3), source_hash=digest("source", i), reference_hash=digest("reference", i), content_hash=digest("content", i)))
        with engine.begin() as c:
            for v in [1, 2]:
                c.execute(m.DatasetSample.__table__.insert(), [{**x, "id": (v - 1) * samples + offset + j + 1, "dataset_version_id": uid("version", v)} for j, x in enumerate(base)])
            for job in [1, 2]:
                predictions, scores, items = [], [], []
                for j, sample in enumerate(base):
                    i = offset + j
                    translation = f"模型{job}第{i}条译文。" + "这是一条用于评测容量和查询性能的合成译文。" * 3
                    pid = (job - 1) * samples + i + 1
                    sid = uid(f"score-{job}", i)
                    reason = "术语准确，译文基本保留源文含义，语序自然；这是用于性能测试的合成评语。" * 2
                    score = float((i + job) % 11)
                    predictions.append(dict(id=pid, submission_dataset_id=uid("submission_dataset", job), sample_id=sample["sample_id"], translation_zh=translation, predicted_language=sample["source_language"], translation_hash=digest(f"translation-{job}", i)))
                    scores.append(dict(id=sid, evaluator_type="openai_compatible_llm", evaluator_revision_id=uid("revision"), source_language=sample["source_language"], source_hash=sample["source_hash"], reference_hash=sample["reference_hash"], translation_hash=digest(f"translation-{job}", i), evaluator_model="synthetic", base_url="http://synthetic.invalid/v1", score=score, score_min=0.0, score_max=10.0, unit="point", reason=reason, raw_response={"score": score, "reason": reason}, created_at=NOW))
                    items.append(dict(id=pid, evaluator_job_id=uid("job", job), prediction_id=pid, score_result_id=sid, status="completed", attempts=1, finished_at=NOW))
                c.execute(m.Prediction.__table__.insert(), predictions)
                c.execute(m.ScoreResult.__table__.insert(), scores)
                c.execute(m.EvaluationItem.__table__.insert(), items)
    with engine.connect() as c:
        c.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    engine.dispose()
    print(json.dumps(dict(phase="seed", samples_per_version=samples, versions=2, languages=40, jobs=2, score_results=2*samples, seconds=time.perf_counter()-start, db_mb=path.stat().st_size/1024**2, peak_rss_mb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024)))


def history(path, count):
    engine = connect(path)
    from app import models as m
    from sqlalchemy import select, func
    start = time.perf_counter()
    with engine.begin() as c:
        samples = c.scalar(select(m.DatasetVersion.sample_count).where(m.DatasetVersion.id == uid("version", 2)))
        existing = c.scalar(select(func.count()).select_from(m.EvaluatorJob))
        for job in range(existing + 1, count + 1):
            c.execute(m.EvaluationTask.__table__.insert(), dict(id=uid("task", job), submission_id=uid("submission", 1), status="completed", total_items=samples, completed_items=samples, cached_items=samples, started_at=NOW, finished_at=NOW))
            c.execute(m.DatasetJob.__table__.insert(), dict(id=uid("dataset_job", job), task_id=uid("task", job), submission_dataset_id=uid("submission_dataset", 1), status="completed", total_items=samples, completed_items=samples, cached_items=samples))
            c.execute(m.EvaluatorJob.__table__.insert(), dict(id=uid("job", job), dataset_job_id=uid("dataset_job", job), evaluator_revision_id=uid("revision"), status="completed", total_items=samples, completed_items=samples, cached_items=samples))
            c.exec_driver_sql("""INSERT INTO evaluation_items
                (evaluator_job_id,prediction_id,score_result_id,status,cache_hit,attempts,created_at,finished_at)
                SELECT ?,prediction_id,score_result_id,status,1,0,created_at,finished_at
                FROM evaluation_items WHERE evaluator_job_id=?""", (uid("job", job), uid("job", 1)))
    with engine.connect() as c:
        c.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    engine.dispose()
    print(json.dumps(dict(phase="history", jobs=count, seconds=time.perf_counter()-start, db_mb=path.stat().st_size/1024**2)))


CASES = ["datasets", "versions", "samples_first", "samples_deep", "summary", "summary_dynamic", "items_first", "items_deep", "items_score", "items_language", "items_exact", "results_one", "results_default", "results_mean_one", "compare_two"]


def one(path, case):
    engine = connect(path)
    from app.api import router
    from app.database import get_session
    from app import models as m
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import select, event, func
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        samples = s.scalar(select(m.DatasetVersion.sample_count).where(m.DatasetVersion.id == uid("version", 2)))
        jobs = s.scalar(select(func.count()).select_from(m.EvaluatorJob))
    app = FastAPI()
    app.include_router(router)
    def session_override():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[get_session] = session_override
    client = TestClient(app)
    j = uid("job", 1)
    v = uid("version", 2)
    d = uid("dataset")
    paths = {
        "datasets": "/api/datasets", "versions": f"/api/datasets/{d}/versions",
        "samples_first": f"/api/dataset-versions/{v}/samples?page=1&page_size=50",
        "samples_deep": f"/api/dataset-versions/{v}/samples?page={samples//50}&page_size=50",
        "summary": f"/api/evaluator-jobs/{j}/summary?threshold=7",
        "summary_dynamic": f"/api/evaluator-jobs/{j}/summary?threshold=7.5",
        "items_first": f"/api/evaluator-jobs/{j}/items?page=1&page_size=50",
        "items_deep": f"/api/evaluator-jobs/{j}/items?page={samples//50}&page_size=50",
        "items_score": f"/api/evaluator-jobs/{j}/items?sort=score&direction=asc&page_size=50",
        "items_language": f"/api/evaluator-jobs/{j}/items?language=en&page_size=50",
        "items_exact": f"/api/evaluator-jobs/{j}/items?sample_id=zu-{samples-1:08d}",
        "results_one": "/api/results/page?page_size=1",
        "results_default": "/api/results/page?page_size=20",
        "results_mean_one": "/api/results/page?page_size=1&sort=micro_mean&direction=desc",
    }
    sql = []
    @event.listens_for(engine, "before_cursor_execute")
    def before(conn, cursor, statement, parameters, context, executemany):
        context.scale_start = time.perf_counter()
    @event.listens_for(engine, "after_cursor_execute")
    def after(conn, cursor, statement, parameters, context, executemany):
        sql.append(dict(sql=statement, seconds=time.perf_counter()-context.scale_start, params=parameters))
    baseline = rss_mb()
    start = time.perf_counter()
    if case == "compare_two":
        response = client.post("/api/results/compare", json={"evaluator_job_ids": [uid("job", 1), uid("job", 2)], "threshold": 7})
    else:
        response = client.get(paths[case])
    elapsed = time.perf_counter()-start
    payload = response.json()
    assert response.status_code == 200, payload
    if case == "compare_two":
        assert payload["strictly_comparable"] and len(payload["items"]) == 2
    elif case in {"summary", "summary_dynamic"}:
        assert payload["total"] == samples and payload["successful"] == samples and len(payload["by_language"]) == 40
    elif case.startswith("items_"):
        expected = 1 if case == "items_exact" else samples // 40 if case == "items_language" else samples
        assert payload["total"] == expected, (case, payload["total"], expected)
    elif case.startswith("results_"):
        assert payload["total"] == jobs
    result = dict(case=case, samples_per_version=samples, jobs=jobs, http_status=response.status_code, seconds=elapsed, sql_count=len(sql), baseline_rss_mb=baseline, peak_rss_mb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024, response_bytes=len(response.content))
    # This measures cursor execute time, not ORM decoding/fetch/JSON time.
    result["slowest_cursor_execute_seconds"] = max(x["seconds"] for x in sql)
    print(json.dumps(result), flush=True)
    if case in {"items_first", "items_score", "results_one", "results_mean_one", "compare_two"}:
        trace = path.parent / f"{path.stem}-{jobs}jobs-{case}-sql.json"
        trace.write_text(json.dumps(sql, indent=2, ensure_ascii=False, default=str))


def run(path, cases):
    for case in cases:
        start = time.perf_counter()
        try:
            p = subprocess.run([sys.executable, __file__, "one", str(path), "--case", case], capture_output=True, text=True, timeout=90)
            print(p.stdout.strip() if p.returncode == 0 else json.dumps(dict(case=case, returncode=p.returncode, error=p.stderr[-3000:])), flush=True)
        except subprocess.TimeoutExpired:
            print(json.dumps(dict(case=case, status="timeout", seconds=time.perf_counter()-start)), flush=True)


def analyze(path):
    engine = connect(path)
    start = time.perf_counter()
    with engine.begin() as c:
        c.exec_driver_sql("ANALYZE")
    print(json.dumps(dict(phase="analyze", seconds=time.perf_counter()-start)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["seed", "run", "one", "history", "analyze"])
    parser.add_argument("db", type=Path)
    parser.add_argument("--samples", type=int, default=24000)
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--cases", nargs="+", choices=CASES, default=CASES)
    parser.add_argument("--case", choices=CASES)
    args = parser.parse_args()
    path = args.db.resolve()
    if ROOT in path.parents:
        raise SystemExit("Fixture DB must be outside the repository")
    os.environ["TRANSLATION_EVAL_DATABASE_URL"] = f"sqlite:///{path}"
    if args.mode == "seed":
        seed(path, args.samples)
    elif args.mode == "run":
        run(path, args.cases)
    elif args.mode == "history":
        history(path, args.jobs)
    elif args.mode == "analyze":
        analyze(path)
    else:
        one(path, args.case)
