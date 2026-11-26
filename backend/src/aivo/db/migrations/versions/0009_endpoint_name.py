"""The name each RunPod endpoint got: RunPod never frees a name, so a re-made endpoint may need another one.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("gpu_endpoints", sa.Column("name", sa.String(80), nullable=True))


def downgrade() -> None:
    op.drop_column("gpu_endpoints", "name")
