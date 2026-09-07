from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, func, inspect, select, update

from app.models import AggregateScore, DatasetSample, DatasetVersion, EvaluationItem, EvaluatorJob, Prediction, ScoreResult
from app.queries import comparison_checks, threshold_summary
from app.read_model_schema import BUILD_METRIC, SUMMARY_METRIC, drop_read_model_triggers
from app.read_models import begin_summary_build, build_query_summaries, ensure_query_summaries, store_query_summaries
from test_audit_regressions import client, task_job


@pytest.fixture
def completed_pair(session_factory):
    _, first_id = task_job(session_factory)
    with session_factory() as session:
        first = session.get(EvaluatorJob, first_id)
        first.status = "completed"
        first.completed_items = first.total_items
        first.evaluator_revision.default_threshold = 80
        items = list(session.scalars(select(EvaluationItem).where(EvaluationItem.evaluator_job_id == first_id).order_by(EvaluationItem.id)))
        for number, item in enumerate(items):
            prediction = item.prediction
            score = ScoreResult(evaluator_type="sacrebleu_zh", evaluator_revision_id=first.evaluator_revision_id,
                source_language="de", source_hash="s", reference_hash="r", translation_hash=prediction.translation_hash,
                score=60 + 5 * number, score_min=0, score_max=100, unit="BLEU", reason="unused text",
                raw_response={"unused": "large response " * 1000})
            session.add(score)
            session.flush()
            item.score_result_id = score.id
            item.status = "completed"
        second = EvaluatorJob(dataset_job_id=first.dataset_job_id, evaluator_revision_id=first.evaluator_revision_id,
            status="completed", total_items=len(items), completed_items=len(items))
        session.add(second)
        session.flush()
        session.add_all([EvaluationItem(evaluator_job_id=second.id, prediction_id=item.prediction_id,
            score_result_id=item.score_result_id, status="completed", cache_hit=True) for item in items])
        session.commit()
        return first_id, second.id


def test_comparison_excludes_invalid_scores_even_when_both_success_sets_match(client, completed_pair, session_factory):
    body = {"evaluator_job_ids": completed_pair, "threshold": 80}
    initial = client.post("/api/results/compare", json=body).json()
    assert initial["strictly_comparable"]
    with session_factory() as session:
        score = session.scalar(select(ScoreResult).join(EvaluationItem).where(EvaluationItem.evaluator_job_id == completed_pair[0]))
        score.score = 160.69
        session.commit()
        assert session.scalar(select(func.count()).select_from(AggregateScore).where(AggregateScore.metric_name == SUMMARY_METRIC)) == 0
    changed = client.post("/api/results/compare", json=body).json()
    assert changed["successful_sample_sets_match"]
    assert not changed["strictly_comparable"]
    assert all(row["successful"] == 5 and row["unscored"] == 1 for row in changed["items"])


@pytest.mark.parametrize("field,value", [("unit", "point"), ("score_max", 10), ("score_min", -1), ("evaluator_type", "openai_compatible_llm")])
def test_stored_scale_mismatch_is_invalid_everywhere(client, completed_pair, session_factory, field, value):
    with session_factory() as session:
        score = session.scalar(select(ScoreResult).join(EvaluationItem).where(EvaluationItem.evaluator_job_id == completed_pair[0]))
        setattr(score, field, value)
        session.commit()
    summary = client.get(f"/api/evaluator-jobs/{completed_pair[0]}/summary", params={"threshold": 0}).json()
    assert summary["successful"] == 5
    listed = client.get(f"/api/evaluator-jobs/{completed_pair[0]}/items", params={"item_status": "completed"}).json()
    assert listed["total"] == 5
    assert not client.post("/api/results/compare", json={"evaluator_job_ids": completed_pair, "threshold": 0}).json()["strictly_comparable"]


def test_comparison_builds_narrow_persistent_metadata_and_hides_internal_rows(client, completed_pair, session_factory):
    engine = session_factory.kw["bind"]
    statements = []
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        body = {"evaluator_job_ids": completed_pair, "threshold": 80}
        assert client.post("/api/results/compare", json=body).json()["strictly_comparable"]
        assert not any("score_results.raw_response" in sql or "score_results.reason" in sql for sql in statements)
        statements.clear()
        result = client.post("/api/results/compare", json=body).json()
        assert result["strictly_comparable"]
        assert not any("score_results" in sql for sql in statements)
        assert all(not any(row["metric_name"].startswith("query_summary_") for row in item["aggregates"]) for item in result["items"])
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_dynamic_threshold_cache_is_exact_bounded_and_invalidates_on_retry(client, completed_pair, session_factory):
    job_id = completed_pair[0]
    endpoint = f"/api/evaluator-jobs/{job_id}/summary"
    assert client.get(endpoint, params={"threshold": 80}).json()["passed"] == 2
    for threshold, expected in [(0, 6), (65, 5), (65.0001, 4), (85, 1), (100, 0)]:
        for _ in range(2):
            assert client.get(endpoint, params={"threshold": threshold}).json()["passed"] == expected
    for threshold in range(10, 21):
        assert client.get(endpoint, params={"threshold": threshold}).status_code == 200
    with session_factory() as session:
        payload = session.scalar(select(AggregateScore.details).where(AggregateScore.evaluator_job_id == job_id, AggregateScore.metric_name == SUMMARY_METRIC))
        assert len(payload["thresholds"]) <= 8
        job = session.get(EvaluatorJob, job_id)
        job.status = "queued"
        for item in session.scalars(select(EvaluationItem).where(EvaluationItem.evaluator_job_id == job_id)):
            item.status = "queued"
            item.score_result_id = None
        session.commit()
    result = client.get(endpoint, params={"threshold": 0}).json()
    assert result["successful"] == result["passed"] == 0
    assert result["unscored"] == 6 and result["aggregates"] == []


def test_retry_during_new_threshold_count_never_mixes_two_passes(client, completed_pair, session_factory):
    job_id = completed_pair[0]
    endpoint = f"/api/evaluator-jobs/{job_id}/summary"
    assert client.get(endpoint, params={"threshold": 80}).json()["successful"] == 6
    armed = True
    engine = session_factory.kw["bind"]
    def retry_before_count(_connection, _cursor, statement, _parameters, _context, _many):
        nonlocal armed
        if armed and statement.startswith("SELECT dataset_samples.source_language, count("):
            armed = False
            with session_factory() as retry:
                retry.execute(update(EvaluationItem).where(EvaluationItem.evaluator_job_id == job_id).values(status="queued", score_result_id=None))
                retry.execute(update(EvaluatorJob).where(EvaluatorJob.id == job_id).values(status="queued"))
                retry.commit()
    event.listen(engine, "before_cursor_execute", retry_before_count)
    try:
        result = client.get(endpoint, params={"threshold": 82.5}).json()
    finally:
        event.remove(engine, "before_cursor_execute", retry_before_count)
    assert not armed
    assert result["successful"] == result["passed"] == result["coverage"] == 0
    assert result["total"] == result["unscored"] == 6
    assert result["micro_mean"] is None


def test_llm_boundary_and_scale_use_the_same_strict_contract_as_writer(client, completed_pair, session_factory):
    with session_factory() as session:
        job = session.get(EvaluatorJob, completed_pair[0])
        job.evaluator_revision.profile.evaluator_type = "openai_compatible_llm"
        job.evaluator_revision.default_threshold = 8
        scores = list(session.scalars(select(ScoreResult).join(EvaluationItem).where(EvaluationItem.evaluator_job_id == job.id)))
        for score in scores:
            score.evaluator_type, score.unit, score.score_max, score.score = "openai_compatible_llm", "point", 10, 8
        scores[0].score = 10 + 1e-10
        scores[1].score_max = 10 + 1e-10
        session.commit()
    result = client.get(f"/api/evaluator-jobs/{completed_pair[0]}/summary", params={"threshold": 8}).json()
    assert result["successful"] == result["passed"] == 4


@pytest.mark.parametrize("mutation", ["score", "sample", "prediction", "item"])
def test_mutation_during_first_build_prevents_stale_publication(session_factory, completed_pair, mutation):
    job_id = completed_pair[0]
    with session_factory() as building:
        token = begin_summary_build(building, [job_id])
        building.commit()  # Release the writer while the summary is being read.
        snapshot = build_query_summaries(building, [job_id])
        original_updated_at = snapshot[job_id]["updated_at"]
        with session_factory() as repair:
            item = repair.scalar(select(EvaluationItem).where(EvaluationItem.evaluator_job_id == job_id))
            if mutation == "score":
                repair.execute(update(ScoreResult).where(ScoreResult.id == item.score_result_id).values(score=5))
            elif mutation == "sample":
                dataset = repair.get(EvaluatorJob, job_id).dataset_job.submission_dataset
                repair.execute(update(DatasetSample).where(DatasetSample.dataset_version_id == dataset.dataset_version_id,
                    DatasetSample.sample_id == item.prediction.sample_id).values(source_language="th"))
            elif mutation == "prediction":
                repair.execute(update(Prediction).where(Prediction.id == item.prediction_id).values(sample_id="repaired-id"))
            else:
                repair.execute(update(EvaluationItem).where(EvaluationItem.id == item.id).values(status="failed"))
            repair.commit()
            assert repair.get(EvaluatorJob, job_id).updated_at == original_updated_at
            assert repair.scalar(select(func.count()).select_from(AggregateScore).where(AggregateScore.metric_name == BUILD_METRIC)) == 0
        store_query_summaries(building, snapshot, token)
        building.commit()
        assert building.scalar(select(func.count()).select_from(AggregateScore).where(AggregateScore.metric_name == SUMMARY_METRIC)) == 0
    with session_factory() as fresh:
        ensure_query_summaries(fresh, [job_id])
        payload = fresh.scalar(select(AggregateScore.details).where(AggregateScore.evaluator_job_id == job_id, AggregateScore.metric_name == SUMMARY_METRIC))
        assert payload is not None
        assert payload["generation"] != token[job_id]


def test_migration_upgrades_old_schema_and_backfills_language_counts(session_factory, completed_pair):
    migration_path = Path(__file__).resolve().parents[1] / "alembic/versions/0006_query_read_models.py"
    spec = importlib.util.spec_from_file_location("query_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = session_factory.kw["bind"]
    with engine.begin() as connection:
        drop_read_model_triggers(connection)
        connection.exec_driver_sql("DROP INDEX ix_items_job_status_id")
        connection.exec_driver_sql("DROP INDEX ix_items_score_result")
        connection.exec_driver_sql("ALTER TABLE dataset_versions DROP COLUMN language_counts")
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.upgrade()  # 0001 also creates current metadata on new DBs.
        assert {"ix_items_job_status_id", "ix_items_score_result"} <= {row["name"] for row in inspect(connection).get_indexes("evaluation_items")}
    with session_factory() as session:
        version = session.get(EvaluatorJob, completed_pair[0]).dataset_job.submission_dataset.dataset_version
        assert version.language_counts == {"de": 2, "th": 2, "vi": 2}
        sample = session.scalar(select(DatasetSample).where(DatasetSample.dataset_version_id == version.id, DatasetSample.source_language == "de"))
        sample.source_language = "th"
        session.commit()
        session.refresh(version)
        assert version.language_counts is None


def test_migration_downgrade_removes_build_markers_and_internal_summaries(session_factory, completed_pair):
    with session_factory() as session:
        ensure_query_summaries(session, completed_pair)
        begin_summary_build(session, [completed_pair[0]])
        session.commit()
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0006_query_read_models.py"
    spec = importlib.util.spec_from_file_location("query_migration_down", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with session_factory.kw["bind"].begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert connection.exec_driver_sql("SELECT count(*) FROM aggregate_scores WHERE metric_name LIKE 'query_summary_%'").scalar() == 0
        assert "language_counts" not in {row["name"] for row in inspect(connection).get_columns("dataset_versions")}


def test_basic_sample_paging_does_not_sort_or_count_all_joined_scores(client, completed_pair, session_factory):
    statements = []
    engine = session_factory.kw["bind"]
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = client.get(f"/api/evaluator-jobs/{completed_pair[0]}/items", params={"page_size": 2, "page": 3}).json()
        assert result["total"] == 6 and len(result["items"]) == 2
        count = next(sql for sql in statements if "count(" in sql.lower())
        assert "score_results" not in count and "evaluation_items" not in count
        ids = next(sql for sql in statements if "LIMIT" in sql)
        assert "score_results" not in ids and "IS NULL" not in ids
        assert not any("score_results.raw_response" in sql for sql in statements)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
