"""Machines reached through a provider gateway (RunPod Serverless): URL and pinned worker

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("instances", sa.Column("endpoint_url", sa.Text(), nullable=True))
    op.add_column("instances", sa.Column("worker_ref", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("instances", "worker_ref")
    op.drop_column("instances", "endpoint_url")
