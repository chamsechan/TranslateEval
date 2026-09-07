"""Read-only implementation review; writes only an isolated temporary DB/files."""
from __future__ import annotations

import json
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import import_index, importers
from app.database import Base, create_db_engine
from app.models import DatasetSample, DatasetVersion, InferenceSubmission, Prediction
from app.schemas import DatasetSampleInput
from app.seed import seed_defaults

ROOT = Path(__file__).resolve().parents[3]
DATASET = ROOT / "examples/dataset/flores-demo"
RESULTS = ROOT / "examples/results/demo-run"
output = {"scope": "isolated temporary SQLite DB and fixture files; no application data or source modified"}


def rewrite_first(path, key):
    lines = path.read_text().splitlines()
    first = json.loads(lines[0])
    before = first[key]
    first[key] = "changed after review"
    lines[0] = json.dumps(first, ensure_ascii=False)
    path.write_text("\n".join(lines))
    return first["sample_id"], before


with tempfile.TemporaryDirectory(prefix="translateeval-import-read-review-") as workspace:
    root = Path(workspace)
    engine = create_db_engine(f"sqlite:///{root / 'review.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with patch.object(importers, "settings", SimpleNamespace(import_dir=root / "staging")):
        with factory() as session:
            seed_defaults(session)
            report = importers.validate_dataset_import(session, DATASET)
            rewrite_first(Path(report.staged_path) / "samples.jsonl", "source_text")
            try:
                importers.commit_dataset_import(session, report.id)
                raise AssertionError("Valid dataset text tampering should fail")
            except importers.ImportValidationError:
                assert session.scalar(select(func.count(DatasetVersion.id))) == 0
                output["valid_dataset_tamper_rejected"] = True
                session.rollback()

            report = importers.validate_dataset_import(session, DATASET)
            original_claim = importers._claim_import
            observed = {}
            def mutate_after_dataset_review(session, record):
                observed["sample_id"], observed["source"] = rewrite_first(Path(record.staged_path) / "samples.jsonl", "source_text")
                return original_claim(session, record)
            with patch.object(importers, "_claim_import", mutate_after_dataset_review):
                version = importers.commit_dataset_import(session, report.id)
            stored = session.scalar(select(DatasetSample).where(DatasetSample.dataset_version_id == version.id, DatasetSample.sample_id == observed["sample_id"]))
            assert stored.source_text == observed["source"]
            assert version.content_sha256 == report.report["summary"]["content_sha256"]
            output["dataset_publication_uses_verified_snapshot"] = True

            report = importers.validate_submission_import(session, RESULTS)
            rewrite_first(Path(report.staged_path) / "flores-demo/predictions.jsonl", "translation_zh")
            try:
                importers.commit_submission_import(session, report.id)
                raise AssertionError("Valid prediction text tampering should fail")
            except importers.ImportValidationError:
                assert session.scalar(select(func.count(InferenceSubmission.id))) == 0
                output["valid_prediction_tamper_rejected"] = True
                session.rollback()

            report = importers.validate_submission_import(session, RESULTS)
            def mutate_after_prediction_review(session, record):
                observed["sample_id"], observed["translation"] = rewrite_first(Path(record.staged_path) / "flores-demo/predictions.jsonl", "translation_zh")
                return original_claim(session, record)
            with patch.object(importers, "_claim_import", mutate_after_prediction_review):
                importers.commit_submission_import(session, report.id)
            stored = session.scalar(select(Prediction).where(Prediction.sample_id == observed["sample_id"]))
            assert stored.translation_zh == observed["translation"]
            output["prediction_publication_uses_verified_snapshot"] = True

        # Same-report dataset publication, complementing the checked-in submission concurrency test.
        with factory() as session:
            changed_dir = root / "dataset-v2"
            import shutil
            shutil.copytree(DATASET, changed_dir)
            manifest_path = changed_dir / "dataset_info.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["version_label"] = "v2-review"
            manifest_path.write_text(json.dumps(manifest))
            rewrite_first(changed_dir / "samples.jsonl", "source_text")
            report = importers.validate_dataset_import(session, changed_dir)
            assert report.report["valid"]
            report_id = report.id
        barrier = threading.Barrier(2)
        def simultaneous_claim(session, report):
            barrier.wait(timeout=10)
            return original_claim(session, report)
        def commit():
            with factory() as session:
                return importers.commit_dataset_import(session, report_id).id
        with patch.object(importers, "_claim_import", simultaneous_claim), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(commit) for _ in range(2)]
            versions = [future.result(timeout=20) for future in futures]
        assert versions[0] == versions[1]
        with factory() as session:
            assert session.scalar(select(func.count(DatasetVersion.id))) == 2
        output["concurrent_dataset_commit_returns_one_version"] = True
    engine.dispose()

    # Exception after construction begins must dispose the private index.
    allocated = []
    original_tempdir = import_index.tempfile.TemporaryDirectory
    def tracked_directory(*args, **kwargs):
        directory = original_tempdir(*args, **kwargs)
        allocated.append(Path(directory.name))
        return directory
    def fail_read(*args, **kwargs):
        raise OSError("injected read failure")
        yield
    with patch.object(import_index.tempfile, "TemporaryDirectory", tracked_directory), patch.object(import_index, "iter_jsonl", fail_read):
        try:
            import_index.ImportIndex(DATASET / "samples.jsonl", DatasetSampleInput, dataset=True)
            raise AssertionError("Expected injected failure")
        except OSError:
            pass
    assert allocated and all(not path.exists() for path in allocated)
    output["exception_during_index_read_removes_private_directory"] = True

output["passed"] = True
print(json.dumps(output, ensure_ascii=False, indent=2))
Path(__file__).with_name("import-review-evidence.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
