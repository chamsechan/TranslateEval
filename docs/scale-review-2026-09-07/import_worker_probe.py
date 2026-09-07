"""Isolated scale probe; uses real import/queue helpers and never calls a judge.

Run from repository root:
  .venv/bin/python docs/scale-review-2026-09-07/import_worker_probe.py

Each operation runs in a fresh process, so peak RSS is per operation. Temporary
databases/data are removed after the run; JSON evidence is retained beside this
script. Synthetic language labels exercise grouping, not translation quality.
"""
from __future__ import annotations

import argparse
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


def disk_sizes(work: Path) -> dict:
    return {
        "db_bytes": (work / "test.db").stat().st_size,
        "wal_bytes": (work / "test.db-wal").stat().st_size if (work / "test.db-wal").exists() else 0,
        "total_bytes": sum(p.stat().st_size for p in work.rglob("*") if p.is_file()),
    }


def child(work: Path, count: int, stage: str) -> dict:
    os.environ["TRANSLATION_EVAL_DATABASE_URL"] = f"sqlite:///{work / 'test.db'}"
    os.environ["TRANSLATION_EVAL_IMPORT_DIR"] = str(work / "imports")
    os.environ["TRANSLATION_EVAL_WORKER_HEARTBEAT_FILE"] = str(work / "heartbeat")
    sys.path.insert(0, str(ROOT / "backend"))
    from sqlalchemy import event, select
    from app.database import SessionLocal, engine, init_db
    from app.importers import (
        validate_dataset_import, commit_dataset_import,
        validate_submission_import, commit_submission_import,
    )
    from app.models import DatasetVersion, InferenceSubmission, EvaluatorProfile, EvaluatorJob
    from app.queue import create_evaluation_task, _joined_item_rows, _preload_llm_cache, _refresh_progress
    from app.schemas import EvaluatorSelection
    from app.seed import seed_defaults

    state_path = work / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    sql = {"statements": 0, "selects": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def count_sql(conn, cursor, statement, parameters, context, executemany):
        sql["statements"] += 1
        if statement.lstrip().upper().startswith("SELECT"):
            sql["selects"] += 1

    baseline = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    started = time.perf_counter()
    result = {}
    with SessionLocal() as session:
        if stage == "generate":
            init_db()
            seed_defaults(session)
            for folder in ["source-v1", "source-v2", "predictions"]:
                (work / folder).mkdir()
            manifest = dict(schema_version=1, dataset_key="scale-test", name="Scale test", version_label="v1")
            (work / "source-v1/dataset_info.json").write_text(json.dumps(manifest))
            manifest["version_label"] = "v2"
            (work / "source-v2/dataset_info.json").write_text(json.dumps(manifest))
            total_source_bytes = total_reference_bytes = 0
            with (work / "source-v1/samples.jsonl").open("w") as v1, (work / "source-v2/samples.jsonl").open("w") as v2, (work / "predictions/predictions.jsonl").open("w") as pred:
                for index in range(count):
                    language = LANGUAGES[index % len(LANGUAGES)]
                    sample_id = f"{language}-{index:08d}"
                    source_text = f"Record {index}: The regional team reviewed the project requirements on Monday and agreed to publish the revised evaluation results after checking every translation."
                    reference = f"记录{index}：地区团队在星期一审核了项目要求，并同意在检查每条译文之后发布修订后的评测结果。各部门还将核对数据，确保报告准确而且完整。"
                    sample = dict(sample_id=sample_id, source_language=language, source_text=source_text, reference_zh=reference)
                    v1.write(json.dumps(sample, ensure_ascii=False) + "\n")
                    if index % 100 == 0:
                        sample["reference_zh"] += "本条参考译文经过复核。"
                    v2.write(json.dumps(sample, ensure_ascii=False) + "\n")
                    pred.write(json.dumps(dict(sample_id=sample_id, translation_zh=reference, predicted_language=language), ensure_ascii=False) + "\n")
                    total_source_bytes += len(source_text.encode())
                    total_reference_bytes += len(reference.encode())
            result.update(languages=len(LANGUAGES), samples_per_language=count // len(LANGUAGES), source_mean_bytes=total_source_bytes/count, reference_mean_bytes=total_reference_bytes/count,
                          samples_jsonl_bytes=(work / "source-v1/samples.jsonl").stat().st_size,
                          predictions_jsonl_bytes=(work / "predictions/predictions.jsonl").stat().st_size)
        elif stage in {"dataset_validate", "dataset_diff_validate"}:
            version = "v2" if stage == "dataset_diff_validate" else "v1"
            report = validate_dataset_import(session, work / f"source-{version}")
            state[f"report_{version}"] = report.id
            result.update(report=report.report)
            assert report.report["valid"], report.report
        elif stage in {"dataset_commit", "dataset_diff_commit"}:
            label = "v2" if stage == "dataset_diff_commit" else "v1"
            version = commit_dataset_import(session, state[f"report_{label}"])
            state[f"version_{label}"] = version.id
            result.update(sample_count=version.sample_count)
        elif stage == "submission_validate":
            version = session.get(DatasetVersion, state["version_v1"])
            manifest = dict(schema_version=1, run_name="scale-predictions", model_family="synthetic", checkpoint_name="synthetic-v1", inference=dict(platform="synthetic", mode="default", detects_language=True), datasets=[dict(dataset_key="scale-test", dataset_content_sha256=version.content_sha256)])
            (work / "predictions/result_info.json").write_text(json.dumps(manifest))
            report = validate_submission_import(session, work / "predictions")
            state["submission_report"] = report.id
            result.update(report=report.report)
            assert report.report["valid"], report.report
        elif stage == "submission_commit":
            submission = commit_submission_import(session, state["submission_report"])
            state["submission"] = submission.id
            result.update(submission_id=submission.id)
        elif stage == "create_job":
            submission = session.get(InferenceSubmission, state["submission"])
            profile = session.scalar(select(EvaluatorProfile).where(EvaluatorProfile.evaluator_type == "sacrebleu_zh"))
            task = create_evaluation_task(session, submission, [EvaluatorSelection(evaluator_revision_id=profile.revisions[0].id)], False)
            state["job"] = session.scalar(select(EvaluatorJob.id))
            result.update(total_items=task.total_items)
        elif stage == "joined_rows_empty_cache":
            job = session.get(EvaluatorJob, state["job"])
            join_started = time.perf_counter()
            rows = _joined_item_rows(session, job)
            result.update(joined_rows_seconds=time.perf_counter()-join_started, row_count=len(rows), joined_rows_peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024)
            cache_started = time.perf_counter()
            cache_rows = rows if count <= 24_000 else rows[:200]
            cache = _preload_llm_cache(session, "synthetic-no-score-results", cache_rows)
            result.update(empty_cache_seconds=time.perf_counter()-cache_started, cache_input_rows=len(cache_rows), cache_entries=len(cache), identity_map_size=len(session.identity_map))
        elif stage == "refresh_progress":
            job = session.get(EvaluatorJob, state["job"])
            timings = []
            for _ in range(3):
                progress_started = time.perf_counter()
                counts = _refresh_progress(session, job)
                session.commit()
                timings.append(time.perf_counter()-progress_started)
            result.update(refresh_and_commit_seconds=timings, counts=counts)
        else:
            raise ValueError(stage)
    elapsed = time.perf_counter() - started
    engine.dispose()
    state_path.write_text(json.dumps(state))
    return dict(stage=stage, count=count, seconds=elapsed, peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024, baseline_rss_mib=baseline, sql=sql, disk=disk_sizes(work), **result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--child")
    parser.add_argument("--work")
    parser.add_argument("--count", type=int)
    parser.add_argument("--counts", default="24000,400000")
    parser.add_argument("--output", default=str(Path(__file__).with_name("import-worker-evidence.json")))
    args = parser.parse_args()
    if args.child:
        print(json.dumps(child(Path(args.work), args.count, args.child), ensure_ascii=False))
        return
    evidence = {"description": "Real helper calls; independent child per stage; no judge/API calls; no scoring loop.", "measurements": []}
    stages = ["generate", "dataset_validate", "dataset_commit", "dataset_diff_validate", "dataset_diff_commit", "submission_validate", "submission_commit", "create_job", "joined_rows_empty_cache", "refresh_progress"]
    for count in map(int, args.counts.split(",")):
        with tempfile.TemporaryDirectory(prefix=f"translateeval-scale-{count}-") as temp:
            for stage in stages:
                try:
                    process = subprocess.run([sys.executable, __file__, "--child", stage, "--work", temp, "--count", str(count)], capture_output=True, text=True, timeout=180)
                    if process.returncode:
                        measurement = dict(stage=stage, count=count, error=process.stderr, exit_code=process.returncode)
                    else:
                        measurement = json.loads(process.stdout)
                except subprocess.TimeoutExpired:
                    measurement = dict(stage=stage, count=count, error="Child exceeded 180-second limit")
                evidence["measurements"].append(measurement)
                Path(args.output).write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
                print(json.dumps({k: v for k, v in measurement.items() if k != "report"}, ensure_ascii=False), flush=True)
                if "error" in measurement:
                    break


if __name__ == "__main__":
    main()
