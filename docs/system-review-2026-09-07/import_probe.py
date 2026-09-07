"""Read-only product audit: run with .venv/bin/python; all writes use a temporary DB."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="translation-import-review-") as directory:
        temp = Path(directory)
        os.environ["TRANSLATION_EVAL_DATABASE_URL"] = f"sqlite:///{temp / 'test.db'}"
        os.environ["TRANSLATION_EVAL_IMPORT_DIR"] = str(temp / "imports")
        os.environ["TRANSLATION_EVAL_WORKER_HEARTBEAT_FILE"] = str(temp / "heartbeat")

        from sqlalchemy import func, select
        from app.database import SessionLocal, init_db
        from app.seed import seed_defaults
        from app.importers import (
            commit_dataset_import, validate_dataset_import, validate_submission_import,
        )
        from app.api import commit_submission
        from app.models import EvaluationTask, EvaluatorProfile, ImportValidationReport, Language
        from app.schemas import CommitSubmissionRequest, EvaluatorSelection

        init_db()
        evidence = {}
        with SessionLocal() as session:
            seed_defaults(session)
            source = temp / "invalid-languages"
            source.mkdir()
            manifest = dict(schema_version=1, dataset_key="invalid-languages", name="语种校验探针", version_label="v1")
            (source / "dataset_info.json").write_text(json.dumps(manifest), encoding="utf-8")
            samples = [dict(sample_id=str(i), source_language=language, source_text="hello", reference_zh="你好")
                       for i, language in enumerate(["!!!", "en zh", "123", "中文"])]
            (source / "samples.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in samples), encoding="utf-8")
            report = validate_dataset_import(session, source)
            version = commit_dataset_import(session, report.id)
            evidence["invalid_language_codes"] = dict(valid=report.report["valid"], committed=version.sample_count,
                registered_codes=list(session.scalars(select(Language.code)).all()))

            manifest["version_label"] = "v2"
            (source / "dataset_info.json").write_text(json.dumps(manifest), encoding="utf-8")
            samples[0].update(source_text="changed source", reference_zh="参考译文也变了")
            (source / "samples.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in samples), encoding="utf-8")
            diff_report = validate_dataset_import(session, source)
            evidence["simultaneous_source_reference_change"] = diff_report.report["diff"]

            demo = validate_dataset_import(session, ROOT / "examples/dataset/flores-demo")
            commit_dataset_import(session, demo.id)
            submission_report = validate_submission_import(session, ROOT / "examples/results/demo-run")
            report_id = submission_report.id
            bleu = session.scalar(select(EvaluatorProfile).where(EvaluatorProfile.evaluator_type == "sacrebleu_zh"))
            body = CommitSubmissionRequest(evaluators=[EvaluatorSelection(evaluator_revision_id=bleu.revisions[0].id)])

        # Explicitly interleave the two requests after they have read the same
        # report. This models a concurrent read, without timing-dependent threads.
        with SessionLocal() as first, SessionLocal() as second:
            first_report = first.get(ImportValidationReport, report_id)
            second_report = second.get(ImportValidationReport, report_id)
            assert first_report.status == second_report.status == "validated"
            first_response = commit_submission(report_id, body, first)
            second_response = commit_submission(report_id, body, second)
            evidence["overlapping_commit_reads"] = dict(
                same_report_id=report_id, first=first_response, second=second_response,
                task_count=second.scalar(select(func.count(EvaluationTask.id))),
                distinct_submissions=first_response["submission_id"] != second_response["submission_id"],
                interleaving="Both sessions read validated report; request 1 commits, then request 2 commits its previously read report.",
            )

        print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
