"""Persistent language counts, query invalidation and queue/read indexes.

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# Frozen migration DDL: runtime trigger changes belong in a new migration.
TRIGGER_STATEMENTS = {'te_summary_item_insert': 'CREATE TRIGGER IF NOT EXISTS te_summary_item_insert AFTER INSERT ON '
                           'evaluation_items WHEN EXISTS (SELECT 1 FROM aggregate_scores WHERE '
                           'evaluator_job_id IN (NEW.evaluator_job_id) AND metric_name IN '
                           "('query_summary_v1', 'query_summary_build_v1')) BEGIN DELETE FROM "
                           'aggregate_scores WHERE evaluator_job_id IN (NEW.evaluator_job_id) AND '
                           "metric_name IN ('query_summary_v1', 'query_summary_build_v1'); END",
 'te_summary_item_delete': 'CREATE TRIGGER IF NOT EXISTS te_summary_item_delete AFTER DELETE ON '
                           'evaluation_items WHEN EXISTS (SELECT 1 FROM aggregate_scores WHERE '
                           'evaluator_job_id IN (OLD.evaluator_job_id) AND metric_name IN '
                           "('query_summary_v1', 'query_summary_build_v1')) BEGIN DELETE FROM "
                           'aggregate_scores WHERE evaluator_job_id IN (OLD.evaluator_job_id) AND '
                           "metric_name IN ('query_summary_v1', 'query_summary_build_v1'); END",
 'te_summary_item_update': 'CREATE TRIGGER IF NOT EXISTS te_summary_item_update AFTER UPDATE OF '
                           'evaluator_job_id, prediction_id, score_result_id, status ON evaluation_items '
                           'WHEN EXISTS (SELECT 1 FROM aggregate_scores WHERE evaluator_job_id IN '
                           '(OLD.evaluator_job_id, NEW.evaluator_job_id) AND metric_name IN '
                           "('query_summary_v1', 'query_summary_build_v1')) BEGIN DELETE FROM "
                           'aggregate_scores WHERE evaluator_job_id IN (OLD.evaluator_job_id, '
                           "NEW.evaluator_job_id) AND metric_name IN ('query_summary_v1', "
                           "'query_summary_build_v1'); END",
 'te_summary_score_update': 'CREATE TRIGGER IF NOT EXISTS te_summary_score_update AFTER UPDATE OF score, '
                            'score_min, score_max, unit, evaluator_type, evaluator_revision_id, '
                            'prompt_version_id, evaluator_model, base_url ON score_results WHEN EXISTS '
                            '(SELECT 1 FROM aggregate_scores WHERE evaluator_job_id IN (SELECT '
                            'evaluator_job_id FROM evaluation_items WHERE score_result_id=NEW.id) AND '
                            "metric_name IN ('query_summary_v1', 'query_summary_build_v1')) BEGIN DELETE "
                            'FROM aggregate_scores WHERE evaluator_job_id IN (SELECT evaluator_job_id FROM '
                            'evaluation_items WHERE score_result_id=NEW.id) AND metric_name IN '
                            "('query_summary_v1', 'query_summary_build_v1'); END",
 'te_summary_job_update': 'CREATE TRIGGER IF NOT EXISTS te_summary_job_update AFTER UPDATE OF status, '
                          'evaluator_revision_id, prompt_version_id, updated_at, completed_items, '
                          'failed_items, cached_items, total_items ON evaluator_jobs WHEN EXISTS (SELECT 1 '
                          'FROM aggregate_scores WHERE evaluator_job_id IN (NEW.id) AND metric_name IN '
                          "('query_summary_v1', 'query_summary_build_v1')) BEGIN DELETE FROM "
                          'aggregate_scores WHERE evaluator_job_id IN (NEW.id) AND metric_name IN '
                          "('query_summary_v1', 'query_summary_build_v1'); END",
 'te_summary_revision_update': 'CREATE TRIGGER IF NOT EXISTS te_summary_revision_update AFTER UPDATE OF '
                               'default_threshold, profile_id ON evaluator_revisions WHEN EXISTS (SELECT 1 '
                               'FROM aggregate_scores WHERE evaluator_job_id IN (SELECT id FROM '
                               'evaluator_jobs WHERE evaluator_revision_id=NEW.id) AND metric_name IN '
                               "('query_summary_v1', 'query_summary_build_v1')) BEGIN DELETE FROM "
                               'aggregate_scores WHERE evaluator_job_id IN (SELECT id FROM evaluator_jobs '
                               "WHERE evaluator_revision_id=NEW.id) AND metric_name IN ('query_summary_v1', "
                               "'query_summary_build_v1'); END",
 'te_summary_profile_update': 'CREATE TRIGGER IF NOT EXISTS te_summary_profile_update AFTER UPDATE OF '
                              'evaluator_type ON evaluator_profiles WHEN EXISTS (SELECT 1 FROM '
                              'aggregate_scores WHERE evaluator_job_id IN (SELECT ej.id FROM evaluator_jobs '
                              'ej JOIN evaluator_revisions er ON er.id=ej.evaluator_revision_id WHERE '
                              "er.profile_id=NEW.id) AND metric_name IN ('query_summary_v1', "
                              "'query_summary_build_v1')) BEGIN DELETE FROM aggregate_scores WHERE "
                              'evaluator_job_id IN (SELECT ej.id FROM evaluator_jobs ej JOIN '
                              'evaluator_revisions er ON er.id=ej.evaluator_revision_id WHERE '
                              "er.profile_id=NEW.id) AND metric_name IN ('query_summary_v1', "
                              "'query_summary_build_v1'); END",
 'te_summary_version_update': 'CREATE TRIGGER IF NOT EXISTS te_summary_version_update AFTER UPDATE OF '
                              'sample_count, content_sha256 ON dataset_versions WHEN EXISTS (SELECT 1 FROM '
                              'aggregate_scores WHERE evaluator_job_id IN (SELECT ej.id FROM '
                              'submission_datasets sd JOIN dataset_jobs dj ON dj.submission_dataset_id=sd.id '
                              'JOIN evaluator_jobs ej ON ej.dataset_job_id=dj.id WHERE sd.dataset_version_id '
                              "IN (NEW.id)) AND metric_name IN ('query_summary_v1', "
                              "'query_summary_build_v1')) BEGIN DELETE FROM aggregate_scores WHERE "
                              'evaluator_job_id IN (SELECT ej.id FROM submission_datasets sd JOIN '
                              'dataset_jobs dj ON dj.submission_dataset_id=sd.id JOIN evaluator_jobs ej ON '
                              'ej.dataset_job_id=dj.id WHERE sd.dataset_version_id IN (NEW.id)) AND '
                              "metric_name IN ('query_summary_v1', 'query_summary_build_v1'); END",
 'te_summary_sample_insert': 'CREATE TRIGGER IF NOT EXISTS te_summary_sample_insert AFTER INSERT ON '
                             'dataset_samples WHEN EXISTS (SELECT 1 FROM aggregate_scores WHERE '
                             'evaluator_job_id IN (SELECT ej.id FROM submission_datasets sd JOIN '
                             'dataset_jobs dj ON dj.submission_dataset_id=sd.id JOIN evaluator_jobs ej ON '
                             'ej.dataset_job_id=dj.id WHERE sd.dataset_version_id IN '
                             "(NEW.dataset_version_id)) AND metric_name IN ('query_summary_v1', "
                             "'query_summary_build_v1')) BEGIN DELETE FROM aggregate_scores WHERE "
                             'evaluator_job_id IN (SELECT ej.id FROM submission_datasets sd JOIN '
                             'dataset_jobs dj ON dj.submission_dataset_id=sd.id JOIN evaluator_jobs ej ON '
                             'ej.dataset_job_id=dj.id WHERE sd.dataset_version_id IN '
                             "(NEW.dataset_version_id)) AND metric_name IN ('query_summary_v1', "
                             "'query_summary_build_v1'); END",
 'te_language_sample_insert': 'CREATE TRIGGER IF NOT EXISTS te_language_sample_insert AFTER INSERT ON '
                              'dataset_samples WHEN EXISTS (SELECT 1 FROM dataset_versions WHERE id IN '
                              '(NEW.dataset_version_id) AND language_counts IS NOT NULL) BEGIN UPDATE '
                              'dataset_versions SET language_counts=NULL WHERE id IN '
                              '(NEW.dataset_version_id) AND language_counts IS NOT NULL; END',
 'te_summary_sample_delete': 'CREATE TRIGGER IF NOT EXISTS te_summary_sample_delete AFTER DELETE ON '
                             'dataset_samples WHEN EXISTS (SELECT 1 FROM aggregate_scores WHERE '
                             'evaluator_job_id IN (SELECT ej.id FROM submission_datasets sd JOIN '
                             'dataset_jobs dj ON dj.submission_dataset_id=sd.id JOIN evaluator_jobs ej ON '
                             'ej.dataset_job_id=dj.id WHERE sd.dataset_version_id IN '
                             "(OLD.dataset_version_id)) AND metric_name IN ('query_summary_v1', "
                             "'query_summary_build_v1')) BEGIN DELETE FROM aggregate_scores WHERE "
                             'evaluator_job_id IN (SELECT ej.id FROM submission_datasets sd JOIN '
                             'dataset_jobs dj ON dj.submission_dataset_id=sd.id JOIN evaluator_jobs ej ON '
                             'ej.dataset_job_id=dj.id WHERE sd.dataset_version_id IN '
                             "(OLD.dataset_version_id)) AND metric_name IN ('query_summary_v1', "
                             "'query_summary_build_v1'); END",
 'te_language_sample_delete': 'CREATE TRIGGER IF NOT EXISTS te_language_sample_delete AFTER DELETE ON '
                              'dataset_samples WHEN EXISTS (SELECT 1 FROM dataset_versions WHERE id IN '
                              '(OLD.dataset_version_id) AND language_counts IS NOT NULL) BEGIN UPDATE '
                              'dataset_versions SET language_counts=NULL WHERE id IN '
                              '(OLD.dataset_version_id) AND language_counts IS NOT NULL; END',
 'te_summary_sample_update': 'CREATE TRIGGER IF NOT EXISTS te_summary_sample_update AFTER UPDATE OF '
                             'dataset_version_id, sample_id, source_language ON dataset_samples WHEN EXISTS '
                             '(SELECT 1 FROM aggregate_scores WHERE evaluator_job_id IN (SELECT ej.id FROM '
                             'submission_datasets sd JOIN dataset_jobs dj ON dj.submission_dataset_id=sd.id '
                             'JOIN evaluator_jobs ej ON ej.dataset_job_id=dj.id WHERE sd.dataset_version_id '
                             'IN (OLD.dataset_version_id, NEW.dataset_version_id)) AND metric_name IN '
                             "('query_summary_v1', 'query_summary_build_v1')) BEGIN DELETE FROM "
                             'aggregate_scores WHERE evaluator_job_id IN (SELECT ej.id FROM '
                             'submission_datasets sd JOIN dataset_jobs dj ON dj.submission_dataset_id=sd.id '
                             'JOIN evaluator_jobs ej ON ej.dataset_job_id=dj.id WHERE sd.dataset_version_id '
                             'IN (OLD.dataset_version_id, NEW.dataset_version_id)) AND metric_name IN '
                             "('query_summary_v1', 'query_summary_build_v1'); END",
 'te_language_sample_update': 'CREATE TRIGGER IF NOT EXISTS te_language_sample_update AFTER UPDATE OF '
                              'dataset_version_id, sample_id, source_language ON dataset_samples WHEN EXISTS '
                              '(SELECT 1 FROM dataset_versions WHERE id IN (OLD.dataset_version_id, '
                              'NEW.dataset_version_id) AND language_counts IS NOT NULL) BEGIN UPDATE '
                              'dataset_versions SET language_counts=NULL WHERE id IN '
                              '(OLD.dataset_version_id, NEW.dataset_version_id) AND language_counts IS NOT '
                              'NULL; END',
 'te_summary_prediction_insert': 'CREATE TRIGGER IF NOT EXISTS te_summary_prediction_insert AFTER INSERT ON '
                                 'predictions WHEN EXISTS (SELECT 1 FROM aggregate_scores WHERE '
                                 'evaluator_job_id IN (SELECT ej.id FROM dataset_jobs dj JOIN evaluator_jobs '
                                 'ej ON ej.dataset_job_id=dj.id WHERE dj.submission_dataset_id IN '
                                 "(NEW.submission_dataset_id)) AND metric_name IN ('query_summary_v1', "
                                 "'query_summary_build_v1')) BEGIN DELETE FROM aggregate_scores WHERE "
                                 'evaluator_job_id IN (SELECT ej.id FROM dataset_jobs dj JOIN evaluator_jobs '
                                 'ej ON ej.dataset_job_id=dj.id WHERE dj.submission_dataset_id IN '
                                 "(NEW.submission_dataset_id)) AND metric_name IN ('query_summary_v1', "
                                 "'query_summary_build_v1'); END",
 'te_summary_prediction_delete': 'CREATE TRIGGER IF NOT EXISTS te_summary_prediction_delete AFTER DELETE ON '
                                 'predictions WHEN EXISTS (SELECT 1 FROM aggregate_scores WHERE '
                                 'evaluator_job_id IN (SELECT ej.id FROM dataset_jobs dj JOIN evaluator_jobs '
                                 'ej ON ej.dataset_job_id=dj.id WHERE dj.submission_dataset_id IN '
                                 "(OLD.submission_dataset_id)) AND metric_name IN ('query_summary_v1', "
                                 "'query_summary_build_v1')) BEGIN DELETE FROM aggregate_scores WHERE "
                                 'evaluator_job_id IN (SELECT ej.id FROM dataset_jobs dj JOIN evaluator_jobs '
                                 'ej ON ej.dataset_job_id=dj.id WHERE dj.submission_dataset_id IN '
                                 "(OLD.submission_dataset_id)) AND metric_name IN ('query_summary_v1', "
                                 "'query_summary_build_v1'); END",
 'te_summary_prediction_update': 'CREATE TRIGGER IF NOT EXISTS te_summary_prediction_update AFTER UPDATE OF '
                                 'submission_dataset_id, sample_id ON predictions WHEN EXISTS (SELECT 1 FROM '
                                 'aggregate_scores WHERE evaluator_job_id IN (SELECT ej.id FROM dataset_jobs '
                                 'dj JOIN evaluator_jobs ej ON ej.dataset_job_id=dj.id WHERE '
                                 'dj.submission_dataset_id IN (OLD.submission_dataset_id, '
                                 "NEW.submission_dataset_id)) AND metric_name IN ('query_summary_v1', "
                                 "'query_summary_build_v1')) BEGIN DELETE FROM aggregate_scores WHERE "
                                 'evaluator_job_id IN (SELECT ej.id FROM dataset_jobs dj JOIN evaluator_jobs '
                                 'ej ON ej.dataset_job_id=dj.id WHERE dj.submission_dataset_id IN '
                                 '(OLD.submission_dataset_id, NEW.submission_dataset_id)) AND metric_name IN '
                                 "('query_summary_v1', 'query_summary_build_v1'); END"}


def upgrade():
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if "language_counts" not in {column["name"] for column in inspector.get_columns("dataset_versions")}:
        op.add_column("dataset_versions", sa.Column("language_counts", sa.JSON(), nullable=True))
    indexes = {index["name"] for index in inspector.get_indexes("evaluation_items")}
    for name, columns in [("ix_items_job_status_id", ["evaluator_job_id", "status", "id"]), ("ix_items_score_result", ["score_result_id"])]:
        if name not in indexes:
            op.create_index(name, "evaluation_items", columns)
    versions = sa.table("dataset_versions", sa.column("id", sa.String()), sa.column("language_counts", sa.JSON()))
    samples = sa.table("dataset_samples", sa.column("dataset_version_id", sa.String()), sa.column("source_language", sa.String()), sa.column("id", sa.Integer()))
    # Process one immutable version at a time; do not load sample text or all rows.
    connection.execute(versions.update().where(versions.c.language_counts.is_(None)).values(language_counts={}))
    previous, counts = None, {}
    for version_id, language, count in connection.execute(sa.select(samples.c.dataset_version_id, samples.c.source_language, sa.func.count()).group_by(samples.c.dataset_version_id, samples.c.source_language).order_by(samples.c.dataset_version_id)):
        if previous is not None and previous != version_id:
            connection.execute(versions.update().where(versions.c.id == previous).values(language_counts=counts))
            counts = {}
        previous = version_id
        counts[language] = count
    if previous is not None:
        connection.execute(versions.update().where(versions.c.id == previous).values(language_counts=counts))
    if connection.dialect.name == "sqlite":
        for statement in TRIGGER_STATEMENTS.values():
            connection.exec_driver_sql(statement)
        connection.exec_driver_sql("ANALYZE")


def downgrade():
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        for name in TRIGGER_STATEMENTS:
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
    connection.exec_driver_sql("DELETE FROM aggregate_scores WHERE metric_name IN ('query_summary_v1', 'query_summary_build_v1')")
    for name in ["ix_items_job_status_id", "ix_items_score_result"]:
        if name in {row["name"] for row in sa.inspect(connection).get_indexes("evaluation_items")}:
            op.drop_index(name, table_name="evaluation_items")
    if "language_counts" in {row["name"] for row in sa.inspect(connection).get_columns("dataset_versions")}:
        op.drop_column("dataset_versions", "language_counts")
