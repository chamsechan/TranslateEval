"""Add date labels to Prompt versions.

Revision ID: 0005
Revises: 0004
"""
from collections import defaultdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if "deleted" not in {column["name"] for column in sa.inspect(connection).get_columns("prompt_profiles")}:
        op.add_column(
            "prompt_profiles", sa.Column("deleted", sa.Boolean, nullable=False, server_default=sa.false())
        )
    # 0001 creates current metadata on fresh installations.
    if "version_label" in {column["name"] for column in sa.inspect(connection).get_columns("prompt_versions")}:
        return
    op.add_column("prompt_versions", sa.Column("version_label", sa.String(160), nullable=True))
    versions = sa.table(
        "prompt_versions",
        sa.column("id", sa.String),
        sa.column("profile_id", sa.String),
        sa.column("version", sa.Integer),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("version_label", sa.String),
    )
    sequences: dict[tuple[str, str], int] = defaultdict(int)
    for row in connection.execute(
        sa.select(versions).order_by(versions.c.profile_id, versions.c.created_at, versions.c.version)
    ).mappings().all():
        created_at = row["created_at"] or datetime.now(UTC)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        day = created_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
        key = (row["profile_id"], day)
        sequences[key] += 1
        connection.execute(
            versions.update()
            .where(versions.c.id == row["id"])
            .values(version_label=f"{day}.{sequences[key]}")
        )
    with op.batch_alter_table("prompt_versions") as batch:
        batch.alter_column("version_label", existing_type=sa.String(160), nullable=False)
        batch.create_unique_constraint("uq_prompt_version_label", ["profile_id", "version_label"])


def downgrade() -> None:
    connection = op.get_bind()
    if "version_label" in {column["name"] for column in sa.inspect(connection).get_columns("prompt_versions")}:
        with op.batch_alter_table("prompt_versions") as batch:
            batch.drop_constraint("uq_prompt_version_label", type_="unique")
            batch.drop_column("version_label")
    if "deleted" in {column["name"] for column in sa.inspect(connection).get_columns("prompt_profiles")}:
        with op.batch_alter_table("prompt_profiles") as batch:
            batch.drop_column("deleted")
