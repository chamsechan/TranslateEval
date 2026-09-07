"""Isolated Worker scale verification, no network or product DB access.

24k: real SacreBLEU sentence/corpus computation. 400k: deterministic local judge
with a real production queue/persistence/aggregate path, excluding model latency.
All fixture input is inserted with SQL Core; this does not benchmark import.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
LANGUAGES = "af am ar az be bg bn bs ca cs cy da de el en es et eu fa fi fr ga gl gu he hi hr hu hy id is it ja ka kk km kn ko lo lt".split()


def measure(work: Path, count: int, stage: str):
    os.environ["TRANSLATION_EVAL_DATABASE_URL"] = f"sqlite:///{work / 'test.db'}"
    os.environ["TRANSLATION_EVAL_IMPORT_DIR"] = str(work / "imports")
    os.environ["TRANSLATION_EVAL_WORKER_HEARTBEAT_FILE"] = str(work / "heartbeat")
    sys.path.insert(0, str(ROOT / "backend"))
    from sqlalchemy import event, func, insert, select
    from app import queue
    from app.database import SessionLocal, init_db, engine
    from app.evaluators.base import BaseEvaluator, ScoreOutput
    from app.models import Dataset, DatasetVersion, DatasetSample, Language, ModelRun, InferenceSubmission, SubmissionDataset, Prediction, EvaluatorProfile, EvaluatorRevision, PromptVersion, EvaluationItem, EvaluatorJob, ScoreResult
    from app.normalization import sha256_text, sample_content_hash
    from app.schemas import EvaluatorSelection
    from app.seed import seed_defaults
    state_path = work / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    sql = {"statements": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def record(conn, cursor, statement, parameters, context, executemany):
        sql["statements"] += 1
        verb = statement.lstrip().split(None, 1)[0].lower()
        sql[verb] = sql.get(verb, 0) + 1

    started = time.perf_counter()
    details = {}
    if stage == "fixture":
        init_db()
        with SessionLocal() as session:
            seed_defaults(session)
            session.execute(insert(Language), [dict(code=code, name_zh=code) for code in LANGUAGES])
            dataset = Dataset(key="worker-scale", name="Worker scale fixture")
            session.add(dataset); session.flush()
            version = DatasetVersion(dataset_id=dataset.id, version_label="v1", content_sha256="a" * 64,
                                     sample_count=count, source_languages=LANGUAGES)
            model = ModelRun(run_name="local-scale", model_family="synthetic", checkpoint_name="v1",
                inference_platform="local", inference_mode="default", result_info={})
            session.add_all([version, model]); session.flush()
            submission = InferenceSubmission(model_run_id=model.id, status="ready", manifest={})
            session.add(submission); session.flush()
            mapping = SubmissionDataset(submission_id=submission.id, dataset_version_id=version.id,
                dataset_key=dataset.key, prediction_count=count, dataset_content_sha256=version.content_sha256)
            session.add(mapping); session.flush()
            for offset in range(0, count, 1000):
                samples, predictions = [], []
                for index in range(offset, min(offset + 1000, count)):
                    language, sample_id = LANGUAGES[index % 40], f"sample-{index:08d}"
                    source = f"Record {index}: The regional team reviewed the project requirements on Monday and agreed to publish the revised evaluation results after checking every translation."
                    reference = f"记录{index}：地区团队在星期一审核了项目要求，并同意在检查每条译文之后发布修订后的评测结果。各部门还将核对数据，确保报告准确而且完整。"
                    samples.append(dict(dataset_version_id=version.id, sample_id=sample_id, source_language=language,
                        source_text=source, reference_zh=reference, source_hash=sha256_text(source),
                        reference_hash=sha256_text(reference), content_hash=sample_content_hash(sample_id, language, source, reference)))
                    predictions.append(dict(submission_dataset_id=mapping.id, sample_id=sample_id,
                        translation_zh=reference, translation_hash=sha256_text(reference), predicted_language=language))
                session.execute(insert(DatasetSample), samples)
                session.execute(insert(Prediction), predictions)
            if count == 24000:
                profile = session.scalar(select(EvaluatorProfile).where(EvaluatorProfile.evaluator_type == "sacrebleu_zh"))
                revision = profile.revisions[0]
                prompt_id = None
            else:
                profile = EvaluatorProfile(name="Deterministic local judge", evaluator_type="openai_compatible_llm", enabled=True)
                session.add(profile); session.flush()
                revision = EvaluatorRevision(profile_id=profile.id, revision=1, default_threshold=8,
                    config={"model": "scale-local", "concurrency": 32, "max_retries": 0, "cache_policy": "strict_revision", "base_url": "local-only"})
                session.add(revision)
                prompt_id = session.scalar(select(PromptVersion.id))
            session.commit()
            state.update(submission=submission.id, revision=revision.id, prompt=prompt_id)
    elif stage == "create_task":
        with SessionLocal() as session:
            submission = session.get(InferenceSubmission, state["submission"])
            task = queue.create_evaluation_task(session, submission,
                [EvaluatorSelection(evaluator_revision_id=state["revision"], prompt_version_id=state["prompt"])], True)
            state["job"] = task.dataset_jobs[0].evaluator_jobs[0].id
            details["total_items"] = task.total_items
    elif stage == "first_batch":
        with SessionLocal() as session:
            rows = queue._joined_item_rows(session, session.get(EvaluatorJob, state["job"]))
            details.update(rows=len(rows), identity_map_size=len(session.identity_map))
    elif stage == "complete_scoring":
        class LocalJudge(BaseEvaluator):
            evaluator_type = "openai_compatible_llm"
            calls = 0
            @classmethod
            def validate_config(cls, config): return config
            @property
            def model_name(self): return "scale-local"
            @property
            def base_url(self): return "local-only"
            async def evaluate_one(self, item):
                self.calls += 1
                return ScoreOutput(9, 0, 10, "point", "deterministic local scale fixture", {"local": True})
        judge = LocalJudge({})
        if count != 24000:
            queue.build_evaluator = lambda *a, **kw: judge
        maximum_rows, maximum_identity, refreshes = 0, 0, 0
        original_rows, original_progress = queue._joined_item_rows, queue._refresh_progress
        def bounded_rows(session, job, **kw):
            nonlocal maximum_rows, maximum_identity
            rows = original_rows(session, job, **kw)
            maximum_rows = max(maximum_rows, len(rows))
            maximum_identity = max(maximum_identity, len(session.identity_map))
            return rows
        def counted_progress(session, job, **kw):
            nonlocal refreshes
            if kw.get("delta") is None:
                refreshes += 1
            return original_progress(session, job, **kw)
        queue._joined_item_rows, queue._refresh_progress = bounded_rows, counted_progress
        asyncio.run(queue.process_evaluator_job(state["job"]))
        with SessionLocal() as session:
            job = session.get(EvaluatorJob, state["job"])
            details.update(status=job.status, error=job.error, completed=job.completed_items, failed=job.failed_items,
                score_results=session.scalar(select(func.count(ScoreResult.id))), max_batch_rows=maximum_rows,
                max_identity_map_size=maximum_identity, full_progress_recounts=refreshes,
                evaluator="real_sacrebleu" if count == 24000 else "deterministic_local_judge",
                local_judge_calls=judge.calls, real_model_calls=0)
            assert job.status == "completed" and job.completed_items == count and job.failed_items == 0, details
    else:
        raise ValueError(stage)
    elapsed = time.perf_counter() - started
    engine.dispose()
    state_path.write_text(json.dumps(state))
    return dict(stage=stage, count=count, seconds=elapsed, peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,
                database_bytes=(work / "test.db").stat().st_size, sql=sql, **details)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--child")
    parser.add_argument("--work")
    parser.add_argument("--count", type=int)
    parser.add_argument("--counts", default="24000,400000")
    args = parser.parse_args()
    if args.child:
        print(json.dumps(measure(Path(args.work), args.count, args.child)))
        return
    evidence = []
    for count in map(int, args.counts.split(",")):
        with tempfile.TemporaryDirectory(prefix=f"translateeval-worker-new-{count}-") as temp:
            for stage in ["fixture", "create_task", "first_batch", "complete_scoring"]:
                process = subprocess.run([sys.executable, __file__, "--child", stage, "--work", temp, "--count", str(count)],
                                         capture_output=True, text=True, timeout=900)
                result = json.loads(process.stdout) if process.returncode == 0 else dict(stage=stage, count=count, error=process.stderr)
                evidence.append(result)
                Path(__file__).with_name("worker-scale-evidence.json").write_text(json.dumps(evidence, indent=2))
                print(json.dumps(result), flush=True)
                if process.returncode:
                    break


if __name__ == "__main__":
    main()
