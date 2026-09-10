"""initial_schema

Revision ID: 0001_initial_schema
Revises: None
Create Date: 2026-09-07 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # 1. Jobs Table (Queue Engine)
    op.create_table(
        "jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("commit_sha", sa.Text(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("repo_full_name", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "scheduled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("otel_context", postgresql.JSONB(), nullable=True),
        sa.Column("fence_token", sa.Integer(), server_default="0", nullable=False),
        sa.Column("github_check_run_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_date", sa.Date(), server_default=sa.text("CURRENT_DATE"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("commit_sha"),
    )
    op.create_index(
        "idx_jobs_queue",
        "jobs",
        ["scheduled_at", sa.text("priority DESC")],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "idx_jobs_heartbeat",
        "jobs",
        ["heartbeat_at"],
        postgresql_where=sa.text("status = 'processing'"),
    )
    op.create_index("idx_jobs_created_date", "jobs", ["created_date"])

    # 2. Worker Heartbeats (UNLOGGED table for zero WAL overhead on liveness signals)
    op.execute("""
        CREATE UNLOGGED TABLE worker_heartbeats (
            job_id       UUID NOT NULL,
            worker_id    TEXT NOT NULL,
            heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (job_id)
        );
    """)

    # 3. Review Records (System of Record)
    op.create_table(
        "review_records",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("commit_sha", sa.Text(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("repo_full_name", sa.Text(), nullable=False),
        sa.Column("feedback", postgresql.JSONB(), nullable=False),
        sa.Column("model_map", sa.Text(), nullable=False),
        sa.Column("model_reduce", sa.Text(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("commit_sha", name="unique_review_commit"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_review_records_commit", "review_records", ["commit_sha"])
    op.create_index("idx_review_records_pr", "review_records", ["repo_full_name", "pr_number"])

    # 4. Installation Tokens Cache
    op.create_table(
        "installation_tokens",
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("installation_id"),
    )

    # 5. Repo Symbols (Call Graph Analysis)
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

    # 6. Repo References (Call Graph References)
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


def downgrade() -> None:
    op.drop_index("idx_references_repo_symbol", table_name="repo_references")
    op.drop_table("repo_references")
    op.drop_index("idx_symbols_repo_name", table_name="repo_symbols")
    op.drop_table("repo_symbols")
    op.drop_table("installation_tokens")
    op.drop_index("idx_review_records_pr", table_name="review_records")
    op.drop_index("idx_review_records_commit", table_name="review_records")
    op.drop_table("review_records")
    op.execute("DROP TABLE IF EXISTS worker_heartbeats")
    op.drop_index("idx_jobs_created_date", table_name="jobs")
    op.drop_index("idx_jobs_heartbeat", table_name="jobs")
    op.drop_index("idx_jobs_queue", table_name="jobs")
    op.drop_table("jobs")
