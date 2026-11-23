"""Adult content: per-account permission and a mark on adult creations

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("adult_allowed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("jobs", sa.Column("adult", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("jobs", "adult")
    op.drop_column("users", "adult_allowed")
