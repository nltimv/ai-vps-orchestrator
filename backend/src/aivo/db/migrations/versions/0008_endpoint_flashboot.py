"""Per-endpoint FlashBoot: RunPod plans allow it on a few endpoints only.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("gpu_endpoints", sa.Column("flashboot", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("gpu_endpoints", "flashboot")
