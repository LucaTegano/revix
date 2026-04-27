"""v3_kill_db_bloat

Revision ID: 6bb3bb879a21
Revises: acbf6e17a2fe
Create Date: 2026-04-26 15:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "6bb3bb879a21"
down_revision: str | None = "acbf6e17a2fe"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # 1. Ensure heartbeats are UNLOGGED
    op.execute("DROP TABLE IF EXISTS worker_heartbeats")
    op.execute("""
        CREATE UNLOGGED TABLE worker_heartbeats (
            job_id      UUID NOT NULL,
            worker_id   TEXT NOT NULL,
            heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (job_id)
        );
    """)

    # 2. Add Index for partitioning cleanup
    op.create_index("idx_jobs_created_date", "jobs", ["created_date"])


def downgrade() -> None:
    op.drop_index("idx_jobs_created_date", table_name="jobs")
    op.execute("DROP TABLE IF EXISTS worker_heartbeats")
    op.execute("""
        CREATE TABLE worker_heartbeats (
            job_id      UUID NOT NULL,
            worker_id   TEXT NOT NULL,
            heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (job_id)
        );
    """)
