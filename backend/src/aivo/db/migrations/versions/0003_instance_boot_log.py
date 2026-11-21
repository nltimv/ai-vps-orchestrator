"""Machine start-up step and the saved log of failed machines

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("instances", sa.Column("boot_stage", sa.String(48), nullable=True))
    op.add_column("instances", sa.Column("boot_log", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("instances", "boot_log")
    op.drop_column("instances", "boot_stage")
