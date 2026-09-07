"""Real HTTP -> background importer -> separate Worker -> result comparison.

Uses demo text and BLEU only, an isolated migrated SQLite, and a temporary local
port. It never calls an external judge or opens the user's business database.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = Path(__file__).resolve().parents[2]


def main():
    with tempfile.TemporaryDirectory(prefix="translateeval-http-smoke-") as temporary:
        work = Path(temporary)
        env = {**os.environ, "PYTHONPATH": str(ROOT / "backend"),
               "TRANSLATION_EVAL_DATABASE_URL": f"sqlite:///{work / 'test.db'}",
               "TRANSLATION_EVAL_IMPORT_DIR": str(work / "imports"),
               "TRANSLATION_EVAL_WORKER_HEARTBEAT_FILE": str(work / "heartbeat")}
        migrated = subprocess.run([str(ROOT / ".venv/bin/alembic"), "upgrade", "head"],
                                  cwd=ROOT, env=env, capture_output=True, text=True, check=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        evidence = {"migration": migrated.stderr, "backend": "real HTTP and separate worker", "external_model_calls": 0}
        with (work / "api.log").open("w") as log:
            server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                                      cwd=ROOT, env=env, stdout=log, stderr=log)
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=20) as client:
                    for _ in range(100):
                        try:
                            if client.get("/api/health").is_success:
                                break
                        except httpx.ConnectError:
                            pass
                        time.sleep(.1)
                    else:
                        raise RuntimeError((work / "api.log").read_text())
                    def post(path, body=None):
                        response = client.post("/api" + path, json=body or {})
                        response.raise_for_status()
                        return response.json()
                    def get(path, **params):
                        response = client.get("/api" + path, params=params)
                        response.raise_for_status()
                        return response.json()
                    def wait_import(job):
                        phases = []
                        for _ in range(200):
                            current = get(f"/import-commit-jobs/{job['id']}")
                            phases.append(current["phase"])
                            if current["status"] in {"completed", "failed"}:
                                assert current["status"] == "completed", current
                                return current, sorted(set(phases))
                            time.sleep(.05)
                        raise RuntimeError("background import did not finish")
                    def import_source(kind, source, request):
                        draft = post(f"/{kind}-imports/prepare", {"path": str(source)})
                        report = post(f"/import-reports/{draft['id']}/validate", {"manifest": draft["manifest"]})
                        assert report["report"]["valid"], report
                        job = post(f"/import-reports/{report['id']}/commit-job", request)
                        current, phases = wait_import(job)
                        assert post(f"/import-reports/{report['id']}/commit-job", request)["id"] == job["id"]
                        return current, phases
                    dataset, dataset_phases = import_source("dataset", ROOT / "examples/dataset/flores-demo", {})
                    profile = next(item for item in get("/evaluator-profiles") if item["evaluator_type"] == "sacrebleu_zh")
                    request = {"evaluators": [{"evaluator_revision_id": profile["revisions"][0]["id"]}], "force_reevaluate": True}
                    submission, submission_phases = import_source("submission", ROOT / "examples/results/demo-run", request)
                    task_id = submission["result"]["task_id"]
                    subprocess.run([sys.executable, "-m", "app.worker", "--once"], cwd=ROOT, env=env,
                                   capture_output=True, text=True, timeout=60, check=True)
                    task = get(f"/tasks/{task_id}")
                    assert task["status"] == "completed", task
                    job_id = task["dataset_jobs"][0]["evaluator_jobs"][0]["id"]
                    summary = get(f"/evaluator-jobs/{job_id}/summary", threshold=20)
                    assert summary["successful"] == summary["total"] == 6
                    high = get(f"/evaluator-jobs/{job_id}/summary", threshold=80)
                    assert high["passed"] < summary["passed"]
                    second = post(f"/submissions/{submission['result']['submission_id']}/evaluations", request)
                    subprocess.run([sys.executable, "-m", "app.worker", "--once"], cwd=ROOT, env=env,
                                   capture_output=True, text=True, timeout=60, check=True)
                    other = get(f"/tasks/{second['task_id']}")["dataset_jobs"][0]["evaluator_jobs"][0]["id"]
                    comparison = post("/results/compare", {"evaluator_job_ids": [job_id, other], "threshold": 20})
                    assert comparison["strictly_comparable"], comparison
                    page = get(f"/evaluator-jobs/{job_id}/items", language="de", page_size=100)
                    assert page["total"] == 2 and len(page["items"]) == 2
                    evidence.update(dataset_import=dataset, submission_import=submission,
                                    observed_dataset_phases=dataset_phases, observed_submission_phases=submission_phases,
                                    summary=summary, high_threshold_passed=high["passed"], strictly_comparable=True,
                                    language_filtered_rows=page["total"], completed_jobs=len(get("/import-commit-jobs")))
            finally:
                server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
        Path(__file__).with_name("http-smoke-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        print(json.dumps({"status": "passed", "scored_each_run": 6, "runs": 2,
                          "strictly_comparable": True, "temporary_database_removed_on_exit": True}))


if __name__ == "__main__":
    main()
