"""initial_schema

Revision ID: 345e7325ef96
Revises:
Create Date: 2026-04-24 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "345e7325ef96"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # Queue Table
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

    # Review Records (System of Record)
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
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_review_records_commit", "review_records", ["commit_sha"])
    op.create_index("idx_review_records_pr", "review_records", ["repo_full_name", "pr_number"])

    # Installation Tokens Cache
    op.create_table(
        "installation_tokens",
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("installation_id"),
    )


def downgrade() -> None:
    op.drop_table("installation_tokens")
    op.drop_table("jobs")
    op.drop_table("review_records")
