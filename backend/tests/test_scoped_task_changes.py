from app.models import EvaluationTask
from app.queries import task_change_version
from app.queue import cancel_task, create_evaluation_task
from app.schemas import EvaluatorSelection
from test_import_and_queue import prepare_task


def test_result_event_scope_ignores_unrelated_tasks_and_observes_own_cancellation(session_factory):
    task_id = prepare_task(session_factory)
    with session_factory() as session:
        task = session.get(EvaluationTask, task_id)
        job = task.dataset_jobs[0].evaluator_jobs[0]
        before = task_change_version(session, job.id)
        global_before = task_change_version(session)
        other = create_evaluation_task(session, task.submission, [
            EvaluatorSelection(evaluator_revision_id=job.evaluator_revision_id),
        ], True)
        cancel_task(session, other.id)
        assert task_change_version(session) != global_before
        assert task_change_version(session, job.id) == before
        cancel_task(session, task.id)
        assert task_change_version(session, job.id) != before
        assert task_change_version(session, "missing") == "missing:missing"
