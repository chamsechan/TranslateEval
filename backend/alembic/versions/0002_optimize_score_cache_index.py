"""Put evaluator model first in the LLM cache lookup index.

Revision ID: 0002
Revises: 0001
"""
from alembic import op


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_score_cache_lookup", table_name="score_results")
    op.create_index(
        "ix_score_cache_lookup",
        "score_results",
        [
            "evaluator_model",
            "source_language",
            "source_hash",
            "reference_hash",
            "translation_hash",
        ],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_score_cache_lookup", table_name="score_results")
    op.create_index(
        "ix_score_cache_lookup",
        "score_results",
        [
            "source_language",
            "source_hash",
            "reference_hash",
            "translation_hash",
            "evaluator_model",
        ],
        unique=False,
    )

