"""SQLite invalidation rules for persistent, rebuildable query summaries.

Triggers protect Core writes, retries, external repairs and normal ORM writes.
They only delete an existing cache row; no per-sample counter updates occur.
"""
from sqlalchemy import inspect

SUMMARY_METRIC = "query_summary_v1"
BUILD_METRIC = "query_summary_build_v1"


def trigger_statements():
    summary = f"metric_name IN ('{SUMMARY_METRIC}', '{BUILD_METRIC}')"
    job_cache = lambda ids: f"evaluator_job_id IN ({ids}) AND {summary}"
    dataset_jobs = lambda version: f"SELECT ej.id FROM submission_datasets sd JOIN dataset_jobs dj ON dj.submission_dataset_id=sd.id JOIN evaluator_jobs ej ON ej.dataset_job_id=dj.id WHERE sd.dataset_version_id IN ({version})"
    rules = {}
    def add(name, table, event, ids):
        where = job_cache(ids)
        rules[name] = f"CREATE TRIGGER IF NOT EXISTS {name} AFTER {event} ON {table} WHEN EXISTS (SELECT 1 FROM aggregate_scores WHERE {where}) BEGIN DELETE FROM aggregate_scores WHERE {where}; END"
    for event, refs in [("INSERT", "NEW.evaluator_job_id"), ("DELETE", "OLD.evaluator_job_id"), ("UPDATE OF evaluator_job_id, prediction_id, score_result_id, status", "OLD.evaluator_job_id, NEW.evaluator_job_id")]:
        add("te_summary_item_" + event.split()[0].lower(), "evaluation_items", event, refs)
    add("te_summary_score_update", "score_results", "UPDATE OF score, score_min, score_max, unit, evaluator_type, evaluator_revision_id, prompt_version_id, evaluator_model, base_url", "SELECT evaluator_job_id FROM evaluation_items WHERE score_result_id=NEW.id")
    add("te_summary_job_update", "evaluator_jobs", "UPDATE OF status, evaluator_revision_id, prompt_version_id, updated_at, completed_items, failed_items, cached_items, total_items", "NEW.id")
    add("te_summary_revision_update", "evaluator_revisions", "UPDATE OF default_threshold, profile_id", "SELECT id FROM evaluator_jobs WHERE evaluator_revision_id=NEW.id")
    add("te_summary_profile_update", "evaluator_profiles", "UPDATE OF evaluator_type", "SELECT ej.id FROM evaluator_jobs ej JOIN evaluator_revisions er ON er.id=ej.evaluator_revision_id WHERE er.profile_id=NEW.id")
    add("te_summary_version_update", "dataset_versions", "UPDATE OF sample_count, content_sha256", dataset_jobs("NEW.id"))
    for event, refs in [("INSERT", "NEW.dataset_version_id"), ("DELETE", "OLD.dataset_version_id"), ("UPDATE OF dataset_version_id, sample_id, source_language", "OLD.dataset_version_id, NEW.dataset_version_id")]:
        suffix = event.split()[0].lower()
        add("te_summary_sample_" + suffix, "dataset_samples", event, dataset_jobs(refs))
        rules["te_language_sample_" + suffix] = f"CREATE TRIGGER IF NOT EXISTS te_language_sample_{suffix} AFTER {event} ON dataset_samples WHEN EXISTS (SELECT 1 FROM dataset_versions WHERE id IN ({refs}) AND language_counts IS NOT NULL) BEGIN UPDATE dataset_versions SET language_counts=NULL WHERE id IN ({refs}) AND language_counts IS NOT NULL; END"
    for event, refs in [("INSERT", "NEW.submission_dataset_id"), ("DELETE", "OLD.submission_dataset_id"), ("UPDATE OF submission_dataset_id, sample_id", "OLD.submission_dataset_id, NEW.submission_dataset_id")]:
        add("te_summary_prediction_" + event.split()[0].lower(), "predictions", event, f"SELECT ej.id FROM dataset_jobs dj JOIN evaluator_jobs ej ON ej.dataset_job_id=dj.id WHERE dj.submission_dataset_id IN ({refs})")
    return rules


def install_read_model_triggers(connection):
    if connection.dialect.name != "sqlite":
        return
    columns = {row["name"] for row in inspect(connection).get_columns("dataset_versions")}
    if "language_counts" not in columns:
        return  # An existing pre-migration database is upgraded by Alembic.
    for statement in trigger_statements().values():
        connection.exec_driver_sql(statement)


def drop_read_model_triggers(connection):
    if connection.dialect.name == "sqlite":
        for name in trigger_statements():
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
