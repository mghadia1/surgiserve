"""Retries and dead letters: an attempts counter and a dead_lettered status.

The status is a VARCHAR with a CHECK constraint rather than a native Postgres
enum (see 0001), so adding a value is a constraint swap.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUSES = "status IN ('queued', 'running', 'succeeded', 'failed')"
NEW_STATUSES = "status IN ('queued', 'running', 'succeeded', 'failed', 'dead_lettered')"


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_jobs_non_negative_attempts"), "jobs", "attempts >= 0"
    )
    op.drop_constraint(op.f("ck_jobs_status"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_status"), "jobs", NEW_STATUSES)


def downgrade() -> None:
    # The old schema has no dead_lettered status; the nearest is failed.
    op.execute("UPDATE jobs SET status = 'failed' WHERE status = 'dead_lettered'")
    op.drop_constraint(op.f("ck_jobs_status"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_status"), "jobs", OLD_STATUSES)
    op.drop_constraint(op.f("ck_jobs_non_negative_attempts"), "jobs", type_="check")
    op.drop_column("jobs", "attempts")
