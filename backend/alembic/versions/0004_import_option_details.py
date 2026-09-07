"""Add device SDK metadata and persistent option deletion.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("import_options")}
    # Initial migrations use current metadata on fresh installations.
    for column in [
        sa.Column("platform", sa.String(160), nullable=False, server_default=""),
        sa.Column("sdk", sa.String(200), nullable=False, server_default=""),
        sa.Column("sdk_version", sa.String(200), nullable=False, server_default=""),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
    ]:
        if column.name not in columns:
            op.add_column("import_options", column)

    options = sa.table("import_options", sa.column("category"), sa.column("value"),
                       sa.column("label"), sa.column("enabled"))
    # Earlier built-ins encoded language detection as inference modes. Retain
    # their records for compatibility but keep them out of new import choices.
    bind.execute(options.update().where(
        options.c.category == "inference_mode",
        sa.or_(
            sa.and_(options.c.value == "source_language_provided", options.c.label == "已提供源语种"),
            sa.and_(options.c.value == "auto_detect", options.c.label == "自动识别语种"),
        ),
    ).values(enabled=False))


def downgrade() -> None:
    with op.batch_alter_table("import_options") as batch:
        for name in ("deleted", "sdk_version", "sdk", "platform"):
            batch.drop_column(name)
