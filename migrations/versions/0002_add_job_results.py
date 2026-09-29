"""Add what the worker records: model, result, error and timings.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("model_name", sa.String(length=64), nullable=True))
    op.add_column(
        "jobs", sa.Column("result_path", sa.String(length=255), nullable=True)
    )
    op.add_column("jobs", sa.Column("error", sa.Text(), nullable=True))
    op.add_column(
        "jobs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "jobs", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("jobs", "finished_at")
    op.drop_column("jobs", "started_at")
    op.drop_column("jobs", "error")
    op.drop_column("jobs", "result_path")
    op.drop_column("jobs", "model_name")
