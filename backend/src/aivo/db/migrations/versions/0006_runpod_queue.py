"""RunPod Serverless queue endpoints: one endpoint per GPU type, tickets for workers, worker sessions;
DigitalOcean's prepared images, model disks and background runs are gone

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gpu_endpoints",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("gpu_type", sa.String(64), nullable=False, unique=True),
        sa.Column("gpu_name", sa.String(64), nullable=False),
        sa.Column("gpu_class", sa.String(32), nullable=True),
        sa.Column("vram_gb", sa.Float(), nullable=False, server_default="0"),
        sa.Column("price_hourly", sa.Numeric(10, 5), nullable=False, server_default="0"),
        sa.Column("runpod_id", sa.String(64), nullable=True, unique=True),
        sa.Column("config_hash", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="creating"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("keep_warm_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    # jobs: the queued RunPod job and the worker's one-time ticket
    op.add_column("jobs", sa.Column("endpoint_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_jobs_endpoint", "jobs", "gpu_endpoints", ["endpoint_id"], ["id"], ondelete="SET NULL")
    op.add_column("jobs", sa.Column("gpu_type", sa.String(64), nullable=True))
    op.add_column("jobs", sa.Column("runpod_job_id", sa.String(80), nullable=True))
    op.add_column("jobs", sa.Column("endpoint_runpod_id", sa.String(64), nullable=True))
    op.add_column("jobs", sa.Column("ticket_hash", sa.String(64), nullable=True))
    op.add_column("jobs", sa.Column("candidates", JSONB(), nullable=False, server_default="[]"))
    op.add_column("jobs", sa.Column("reserved_usd", sa.Numeric(10, 4), nullable=False, server_default="0"))
    op.add_column("jobs", sa.Column("log", JSONB(), nullable=False, server_default="[]"))
    op.add_column("jobs", sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("worker_contact_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_jobs_ticket", "jobs", ["ticket_hash"])
    # instances are now RunPod worker sessions (billing: from the worker's start until it scales down)
    op.drop_constraint("uq_instances_provider_id", "instances", type_="unique")
    op.create_index(
        "uq_instances_live_worker",
        "instances",
        ["provider", "provider_instance_id"],
        unique=True,
        postgresql_where=sa.text("state <> 'destroyed'"),
    )
    op.add_column("instances", sa.Column("endpoint_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_instances_endpoint", "instances", "gpu_endpoints", ["endpoint_id"], ["id"], ondelete="SET NULL"
    )
    op.drop_column("instances", "endpoint_url")
    op.drop_column("instances", "worker_ref")
    op.drop_column("instances", "volume_resource_id")
    op.drop_table("cache_resources")
    op.drop_table("background_runs")


def downgrade() -> None:
    raise NotImplementedError("0006 removes the DigitalOcean tables; restore a backup to go back")
