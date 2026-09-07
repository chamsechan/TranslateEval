"""Isolated queue review. Run with .venv/bin/python docs/system-review-2026-09-07/queue_repro.py.

Only temporary databases/staging directories are used; evaluators never use the network.
Add --benchmark for local SQLite timing, statement counts, and heartbeat observations.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import event, func, insert, select
from sqlalchemy.orm import sessionmaker
from app import api, importers, queue, worker
from app.database import Base, create_db_engine
from app.evaluators.base import BaseEvaluator, ScoreOutput
from app.importers import commit_dataset_import, commit_submission_import, validate_dataset_import, validate_submission_import
from app.models import DatasetSample, EvaluationItem, EvaluationTask, EvaluatorJob, EvaluatorProfile, EvaluatorRevision, Prediction, PromptVersion, ScoreResult
from app.schemas import EvaluatorSelection
from app.seed import seed_defaults


class Judge(BaseEvaluator):
    evaluator_type = "openai_compatible_llm"

    @classmethod
    def validate_config(cls, config):
        return config

    @property
    def model_name(self):
        return "offline-audit-judge"

    async def evaluate_one(self, item):
        return ScoreOutput(9, 0, 10, "point")


@contextmanager
def isolated_case(count=6, concurrency=8):
    with tempfile.TemporaryDirectory(prefix="queue-system-review-") as directory:
        path = Path(directory)
        original = (importers.settings, queue.SessionLocal, queue.build_evaluator, worker.settings)
        importers.settings = SimpleNamespace(import_dir=path / "imports")
        worker.settings = SimpleNamespace(worker_heartbeat_file=path / "heartbeat")
        engine = create_db_engine(f"sqlite:///{path / 'review.db'}")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        queue.SessionLocal = factory
        queue.build_evaluator = lambda *args, **kwargs: Judge({})
        try:
            with factory() as session:
                seed_defaults(session)
                report = validate_dataset_import(session, ROOT / "examples/dataset/flores-demo")
                version = commit_dataset_import(session, report.id)
                report = validate_submission_import(session, ROOT / "examples/results/demo-run")
                submission = commit_submission_import(session, report.id)
                if count > 6:
                    dataset = submission.datasets[0]
                    session.execute(insert(DatasetSample), [dict(dataset_version_id=version.id,
                        sample_id=f"audit-{i}", source_language="de", source_text=f"source {i}", reference_zh="参考",
                        source_hash=f"{i:064x}", reference_hash="0" * 64, content_hash=f"{i:064x}")
                        for i in range(6, count)])
                    session.execute(insert(Prediction), [dict(submission_dataset_id=dataset.id,
                        sample_id=f"audit-{i}", translation_zh="译文", translation_hash="1" * 64)
                        for i in range(6, count)])
                    version.sample_count = dataset.prediction_count = count
                profile = EvaluatorProfile(name="Offline audit", evaluator_type="openai_compatible_llm", enabled=True)
                session.add(profile)
                session.flush()
                revision = EvaluatorRevision(profile_id=profile.id, revision=1,
                    config={"concurrency": concurrency, "max_retries": 0}, default_threshold=8)
                session.add(revision)
                session.flush()
                prompt = session.scalar(select(PromptVersion))
                selection = EvaluatorSelection(evaluator_revision_id=revision.id, prompt_version_id=prompt.id)
                task = queue.create_evaluation_task(session, submission, [selection], True)
                task_id, job_id = task.id, task.dataset_jobs[0].evaluator_jobs[0].id
            yield factory, engine, task_id, job_id, path
        finally:
            importers.settings, queue.SessionLocal, queue.build_evaluator, worker.settings = original
            engine.dispose()


async def duplicate_worker(factory, task_id, job_id):
    calls = 0
    first_started, both_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class BlockingJudge(Judge):
        async def evaluate_one(self, item):
            nonlocal calls
            calls += 1
            first_started.set()
            if calls >= 12:
                both_started.set()
            await release.wait()
            return await super().evaluate_one(item)

    queue.build_evaluator = lambda *args, **kwargs: BlockingJudge({})
    first = asyncio.create_task(queue.process_evaluator_job(job_id))
    await first_started.wait()
    with factory() as session:
        task = session.get(EvaluationTask, task_id)
        during = {"task": task.status, "submission": task.submission.status,
                  "item_statuses": dict(session.execute(select(EvaluationItem.status, func.count()).group_by(EvaluationItem.status)).all()),
                  "running_filter_total": api.evaluator_job_items(job_id, page=1, page_size=50, item_status="running", session=session)["total"]}
        recovered = queue.recover_interrupted_jobs(session)
    second = asyncio.create_task(queue.process_evaluator_job(job_id))
    await asyncio.wait_for(both_started.wait(), 3)
    release.set()
    await asyncio.gather(first, second)
    with factory() as session:
        return {"during_first_worker": during, "live_job_reclaimed": recovered,
                "calls_for_6_items": calls, "stored_scores": session.scalar(select(func.count(ScoreResult.id))),
                "completed_items": session.get(EvaluatorJob, job_id).completed_items,
                "final_status": session.get(EvaluatorJob, job_id).status}


async def interrupted_cancel(factory, task_id, job_id):
    first_persisted = asyncio.Event()
    calls = 0

    class PartialJudge(Judge):
        async def evaluate_one(self, item):
            nonlocal calls
            calls += 1
            if calls > 1:
                while True:
                    with factory() as session:
                        if session.get(EvaluatorJob, job_id).completed_items == 1:
                            first_persisted.set()
                    await asyncio.sleep(.001)
            return await super().evaluate_one(item)

    queue.build_evaluator = lambda *args, **kwargs: PartialJudge({})
    running = asyncio.create_task(queue.process_evaluator_job(job_id))
    await asyncio.wait_for(first_persisted.wait(), 3)
    with factory() as session:
        queue.cancel_task(session, task_id)
    running.cancel()
    with suppress(asyncio.CancelledError):
        await running
    with factory() as session:
        before = session.get(EvaluatorJob, job_id).status
        recovered = queue.recover_interrupted_jobs(session)
    queue.build_evaluator = lambda *args, **kwargs: Judge({})
    await queue.process_evaluator_job(job_id)
    with factory() as session:
        job = session.get(EvaluatorJob, job_id)
        return {"before_recovery": before, "recovered": recovered, "task_status": job.dataset_job.task.status,
                "item_statuses": dict(session.execute(select(EvaluationItem.status, func.count()).group_by(EvaluationItem.status)).all()),
                "scores_retained": session.scalar(select(func.count(ScoreResult.id)))}


async def benchmark(factory, engine, job_id, path, count):
    statements, grouped_scans = 0, 0

    def count_sql(connection, cursor, statement, parameters, context, executemany):
        nonlocal statements, grouped_scans
        statements += 1
        if "GROUP BY evaluation_items.status" in statement:
            grouped_scans += 1

    event.listen(engine, "before_cursor_execute", count_sql)
    heartbeat = asyncio.create_task(worker.heartbeat_loop())
    await asyncio.sleep(.001)
    stopped = threading.Event()
    ages = []

    def observe():
        while not stopped.wait(.01):
            ages.append(time.time() - (path / "heartbeat").stat().st_mtime)

    observer = threading.Thread(target=observe)
    observer.start()
    start = time.perf_counter()
    try:
        await queue.process_evaluator_job(job_id)
    finally:
        elapsed = time.perf_counter() - start
        stopped.set()
        observer.join()
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        event.remove(engine, "before_cursor_execute", count_sql)
    return {"items": count, "seconds": round(elapsed, 3), "sql_statements": statements,
            "full_job_status_counts": grouped_scans,
            "max_observed_heartbeat_age_seconds": round(max(ages, default=0), 3),
            "heartbeat_disconnected_threshold_seconds": 6}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = {"timestamp_utc": datetime.now(UTC).isoformat()}
    with isolated_case() as (factory, engine, task_id, job_id, path):
        results["duplicate_worker_and_running_filter"] = asyncio.run(duplicate_worker(factory, task_id, job_id))
    with isolated_case(concurrency=1) as (factory, engine, task_id, job_id, path):
        results["cancel_then_worker_interruption_and_recovery"] = asyncio.run(interrupted_cancel(factory, task_id, job_id))
    if args.benchmark:
        results["benchmarks"] = []
        for count in (200, 1000, 3000):
            with isolated_case(count=count) as (factory, engine, task_id, job_id, path):
                result = asyncio.run(benchmark(factory, engine, job_id, path, count))
                results["benchmarks"].append(result)
                print(json.dumps({"benchmark_progress": result}), file=sys.stderr, flush=True)
    rendered = json.dumps(results, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
