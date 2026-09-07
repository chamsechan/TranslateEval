from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select

from app import api, import_api, importers
from app.import_index import ImportIndex
from app.models import DatasetSample, EvaluationTask, EvaluatorProfile, ImportValidationReport, InferenceSubmission, Prediction
from app.normalization import dataset_content_hash
from app.schemas import CommitSubmissionRequest, DatasetSampleInput, EvaluatorSelection, PredictionInput

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "examples/dataset/flores-demo"
RESULTS = ROOT / "examples/results/demo-run"


def write_dataset(path, rows, *, version="v1"):
    path.mkdir()
    (path / "dataset_info.json").write_text(json.dumps({
        "schema_version": 1, "dataset_key": "scale", "name": "Scale", "version_label": version,
    }))
    (path / "samples.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows))
    return path


def test_disk_index_preserves_existing_unicode_and_order_independent_hashes(tmp_path):
    rows = [
        {"sample_id": key, "source_language": "de", "source_text": "cafe\u0301\r\nline\rnext",
         "reference_zh": "你好，世界。"}
        for key in ["z", "a", "中文", "e\u0301", "é"]
    ]
    source = write_dataset(tmp_path / "dataset", rows)
    with ImportIndex(source / "samples.jsonl", DatasetSampleInput, dataset=True) as index:
        assert index.content_hash() == dataset_content_hash(rows)
        assert index.count == 5 and index.language_counts == {"de": 5}
        assert not index.errors and not index.duplicate_ids()
        temp = Path(index.directory.name)
        assert temp.exists()
    assert not temp.exists()
    predictions = [PredictionInput(sample_id=row["sample_id"], translation_zh=row["source_text"]) for row in rows]
    prediction_path = tmp_path / "predictions.jsonl"
    prediction_path.write_text("\n".join(item.model_dump_json() for item in predictions))
    with ImportIndex(prediction_path, PredictionInput) as index:
        assert index.content_hash() == importers.prediction_content_hash(predictions)


@pytest.mark.parametrize("code", ["!!!", "123", "en zh", "中文", "-de", "de-", "de--CH"])
def test_source_and_prediction_languages_share_format_validation(code):
    with pytest.raises(ValidationError):
        DatasetSampleInput(sample_id="s", source_language=code, source_text="hello", reference_zh="你好")
    with pytest.raises(ValidationError):
        PredictionInput(sample_id="s", translation_zh="你好", predicted_language=code)


@pytest.mark.parametrize("code,expected", [(" DE ", "de"), ("zh-Hant-TW", "zh-hant-tw"), ("eng_Latn", "eng_latn"), ("zz", "zz")])
def test_well_formed_unknown_and_script_codes_are_accepted(code, expected):
    assert DatasetSampleInput(sample_id="s", source_language=code, source_text="hello", reference_zh="你好").source_language == expected
    assert PredictionInput(sample_id="s", translation_zh="你好", predicted_language=code).predicted_language == expected


def test_diff_reports_both_text_changes_and_all_membership_changes(session_factory, tmp_path):
    rows = [{"sample_id": key, "source_language": "de", "source_text": "old", "reference_zh": "原文"}
            for key in ["both", "removed", "unchanged"]]
    with session_factory() as session:
        report = importers.validate_dataset_import(session, write_dataset(tmp_path / "v1", rows))
        importers.commit_dataset_import(session, report.id)
        changed = [{**rows[0], "source_text": "new", "reference_zh": "新文"}, rows[2],
                   {**rows[1], "sample_id": "added"}]
        report = importers.validate_dataset_import(session, write_dataset(tmp_path / "v2", changed, version="v2"))
        diff = report.report["diff"]
        assert report.report["valid"]
        assert {k: diff[k] for k in ("added", "removed", "source_changed", "reference_changed", "both_changed", "unchanged")} == {
            "added": 1, "removed": 1, "source_changed": 1, "reference_changed": 1, "both_changed": 1, "unchanged": 1,
        }
        assert {"sample_id": "both", "change": "source_and_reference_changed"} in diff["details"]


def test_sample_commit_uses_batches_and_saves_language_counts(session_factory, tmp_path):
    rows = [{"sample_id": f"s{i}", "source_language": "de" if i % 2 else "th", "source_text": "hello", "reference_zh": "你好"}
            for i in range(2_501)]
    with session_factory() as session:
        report = importers.validate_dataset_import(session, write_dataset(tmp_path / "bulk", rows))
        statements = []
        def observe(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO dataset_samples"):
                statements.append((executemany, len(parameters)))
        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", observe)
        try:
            version = importers.commit_dataset_import(session, report.id)
        finally:
            event.remove(engine, "before_cursor_execute", observe)
        assert statements == [(True, 1000), (True, 1000), (True, 501)]
        assert version.language_counts == {"de": 1250, "th": 1251}
        assert session.scalar(select(func.count(DatasetSample.id))) == 2501


def test_concurrent_submission_commit_creates_one_submission_and_task(session_factory, monkeypatch):
    with session_factory() as session:
        dataset_report = importers.validate_dataset_import(session, DATASET)
        importers.commit_dataset_import(session, dataset_report.id)
        report = importers.validate_submission_import(session, RESULTS)
        profile = session.scalar(select(EvaluatorProfile).where(EvaluatorProfile.evaluator_type == "sacrebleu_zh"))
        request = CommitSubmissionRequest(evaluators=[EvaluatorSelection(evaluator_revision_id=profile.revisions[0].id)])
        report_id = report.id
    barrier = threading.Barrier(2)
    claim = importers._claim_import
    def simultaneous_claim(session, record):
        barrier.wait(timeout=10)
        return claim(session, record)
    monkeypatch.setattr(importers, "_claim_import", simultaneous_claim)
    def commit():
        with session_factory() as session:
            # Both callers may have loaded the report before either commits.
            session.get(ImportValidationReport, report_id)
            return api.commit_submission(report_id, request, session)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(commit) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results[0] == results[1]
    with session_factory() as session:
        assert session.scalar(select(func.count(InferenceSubmission.id))) == 1
        assert session.scalar(select(func.count(EvaluationTask.id))) == 1
        assert session.scalar(select(func.count(Prediction.id))) == 6


def test_prepare_failure_removes_its_staging_directory(session_factory, tmp_path):
    source = tmp_path / "bad"
    source.mkdir()
    (source / "dataset_info.json").write_text("{broken")
    with session_factory() as session:
        with pytest.raises(importers.ImportValidationError):
            import_api.prepare_import(session, source, "dataset")
    assert not list(importers.settings.import_dir.glob("dataset-*"))
