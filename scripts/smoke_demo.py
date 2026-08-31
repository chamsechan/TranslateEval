from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.importers import (
    commit_dataset_import,
    commit_submission_import,
    validate_dataset_import,
    validate_submission_import,
)
from app.models import EvaluationTask, EvaluatorProfile
from app.queue import create_evaluation_task, threshold_summary, worker_loop
from app.schemas import EvaluatorSelection
from app.seed import seed_defaults


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    init_db()
    with SessionLocal() as session:
        seed_defaults(session)
        dataset_report = validate_dataset_import(session, ROOT / "examples" / "dataset" / "flores-demo")
        assert dataset_report.report["valid"], dataset_report.report["errors"]
        commit_dataset_import(session, dataset_report.id)

        submission_report = validate_submission_import(
            session, ROOT / "examples" / "results" / "demo-run"
        )
        assert submission_report.report["valid"], submission_report.report["errors"]
        submission = commit_submission_import(session, submission_report.id)
        bleu = session.scalar(
            select(EvaluatorProfile).where(EvaluatorProfile.evaluator_type == "sacrebleu_zh")
        )
        assert bleu and bleu.revisions
        task = create_evaluation_task(
            session,
            submission,
            [EvaluatorSelection(evaluator_revision_id=bleu.revisions[-1].id)],
            force_reevaluate=False,
        )
        task_id = task.id

    asyncio.run(worker_loop(once=True))
    with SessionLocal() as session:
        task = session.get(EvaluationTask, task_id)
        assert task is not None
        job = task.dataset_jobs[0].evaluator_jobs[0]
        summary = threshold_summary(session, job.id, 20.0)
        print(
            json.dumps(
                {
                    "task_id": task.id,
                    "status": task.status,
                    "successful": summary["successful"],
                    "micro_mean": summary["micro_mean"],
                    "micro_accuracy_at_20": summary["micro_accuracy"],
                    "aggregates": summary["aggregates"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
