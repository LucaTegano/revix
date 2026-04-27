"""add_unique_constraint_review_records

Revision ID: 7a30a3bbe76b
Revises: 6bb3bb879a21
Create Date: 2026-04-26 18:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "7a30a3bbe76b"
down_revision: str | None = "6bb3bb879a21"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # Add unique constraint to review_records for ON CONFLICT logic
    op.create_unique_constraint("unique_review_commit", "review_records", ["commit_sha"])


def downgrade() -> None:
    op.drop_constraint("unique_review_commit", "review_records", type_="unique")
