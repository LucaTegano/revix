"""v2_architectural_improvements

Revision ID: acbf6e17a2fe
Revises: 345e7325ef96
Create Date: 2026-04-26 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "acbf6e17a2fe"
down_revision: str | None = "345e7325ef96"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add fencing token to jobs
    op.add_column(
        "jobs", sa.Column("fence_token", sa.Integer(), server_default="0", nullable=False)
    )

    # 2. Create UNLOGGED heartbeat table
    op.execute("""
        CREATE UNLOGGED TABLE worker_heartbeats (
            job_id      UUID NOT NULL,
            worker_id   TEXT NOT NULL,
            heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (job_id)
        );
    """)

    # 3. Create Repo Graph tables for deterministic call analysis
    op.create_table(
        "repo_graphs",
        sa.Column("repo_full_name", sa.Text(), nullable=False),
        sa.Column("graph_data", postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("repo_full_name"),
    )

    op.create_table(
        "repo_symbols",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("repo_full_name", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("qualified_name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("parent", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_symbols_repo_name", "repo_symbols", ["repo_full_name", "name"])

    op.create_table(
        "repo_references",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("repo_full_name", sa.Text(), nullable=False),
        sa.Column("symbol_name", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("line", sa.Integer(), nullable=False),
        sa.Column("context_line", sa.Text(), nullable=False),
        sa.Column("ref_type", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_references_repo_symbol", "repo_references", ["repo_full_name", "symbol_name"]
    )

    # 4. Partitioning: This is complex in an existing table.
    # For this MVP upgrade, we will add a created_date column and prepare for partitioning.
    op.add_column(
        "jobs",
        sa.Column(
            "created_date", sa.Date(), server_default=sa.text("CURRENT_DATE"), nullable=False
        ),
    )

    # Note: In a real production system, we would migrate 'jobs' to a partitioned table here.
    # For now, we add the column and the infrastructure to support it.


def downgrade() -> None:
    op.drop_index("idx_references_repo_symbol", table_name="repo_references")
    op.drop_table("repo_references")
    op.drop_index("idx_symbols_repo_name", table_name="repo_symbols")
    op.drop_table("repo_symbols")
    op.drop_table("repo_graphs")
    op.execute("DROP TABLE IF EXISTS worker_heartbeats")
    op.drop_column("jobs", "created_date")
    op.drop_column("jobs", "fence_token")
