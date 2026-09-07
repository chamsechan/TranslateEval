"""Persistent background import publication jobs.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    # 0001 historically uses current metadata; also support that fresh-db path.
    if "import_commit_jobs" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "import_commit_jobs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("report_id", sa.String(36), sa.ForeignKey("import_validation_reports.id"), nullable=False, unique=True),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("phase", sa.String(32), nullable=False),
            sa.Column("request", sa.JSON(), nullable=False),
            sa.Column("result", sa.JSON(), nullable=False),
            sa.Column("error", sa.Text()),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_import_commit_jobs_status", "import_commit_jobs", ["status"])


def downgrade():
    op.drop_table("import_commit_jobs")
