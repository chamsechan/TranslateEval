"""Replay the original isolated import stages against the optimized code.

Run: .venv/bin/python docs/optimization-2026-09-07/import_benchmark.py
The original review evidence is retained unchanged. Temporary databases and
source files are removed, and each phase still uses a separate child process.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "docs/scale-review-2026-09-07/import_worker_probe.py"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--counts", default="24000,400000")
    parser.add_argument("--child")
    parser.add_argument("--work")
    parser.add_argument("--count", type=int)
    parser.add_argument("--cache-mib", type=int, default=0)
    parser.add_argument("--stages", default="generate,dataset_validate,dataset_commit,dataset_diff_validate,dataset_diff_commit,submission_validate,submission_commit")
    parser.add_argument("--output", default=str(Path(__file__).with_name("import-evidence.json")))
    args = parser.parse_args()
    if args.child:
        from sqlalchemy import Engine, event
        sql_seconds = 0.0
        insert_seconds = 0.0
        @event.listens_for(Engine, "connect")
        def configure_cache(connection, _):
            if args.cache_mib:
                connection.execute(f"PRAGMA cache_size={-args.cache_mib * 1024}")
        @event.listens_for(Engine, "before_cursor_execute")
        def before(conn, cursor, statement, parameters, context, executemany):
            context.probe_start = time.perf_counter()
        @event.listens_for(Engine, "after_cursor_execute")
        def after(conn, cursor, statement, parameters, context, executemany):
            nonlocal sql_seconds, insert_seconds
            elapsed = time.perf_counter() - context.probe_start
            sql_seconds += elapsed
            if statement.startswith(("INSERT INTO dataset_samples", "INSERT INTO predictions")):
                insert_seconds += elapsed
        spec = importlib.util.spec_from_file_location("original_probe", PROBE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.child(Path(args.work), args.count, args.child)
        result.update(sql_seconds=sql_seconds, bulk_insert_seconds=insert_seconds, cache_mib=args.cache_mib)
        print(json.dumps(result, ensure_ascii=False))
        return
    output = Path(args.output)
    evidence = {"description": "Original real import helpers; bounded-memory indexes and bulk writes; no model requests.", "measurements": []}
    stages = args.stages.split(",")
    for count in map(int, args.counts.split(",")):
        with tempfile.TemporaryDirectory(prefix="translateeval-optimized-import-") as directory:
            for stage in stages:
                result = subprocess.run([sys.executable, __file__, "--child", stage,
                                         "--work", directory, "--count", str(count), "--cache-mib", str(args.cache_mib)],
                                        capture_output=True, text=True, timeout=180)
                if result.returncode:
                    raise RuntimeError(f"{count}/{stage}: {result.stderr}")
                row = json.loads(result.stdout)
                evidence["measurements"].append(row)
                output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
                print(json.dumps({key: value for key, value in row.items() if key != "report"}), flush=True)


if __name__ == "__main__":
    main()
