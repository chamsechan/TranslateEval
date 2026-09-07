"""Maintain import dropdown options.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
from app.models import ImportOption

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 uses current metadata, so fresh installations already have this table.
    ImportOption.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    ImportOption.__table__.drop(bind=op.get_bind(), checkfirst=True)
