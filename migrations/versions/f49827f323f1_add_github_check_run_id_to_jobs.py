"""add_github_check_run_id_to_jobs

Revision ID: f49827f323f1
Revises: 7a30a3bbe76b
Create Date: 2026-04-27 14:02:00.480716

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f49827f323f1"
down_revision: str | Sequence[str] | None = "7a30a3bbe76b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("jobs", sa.Column("github_check_run_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("jobs", "github_check_run_id")
