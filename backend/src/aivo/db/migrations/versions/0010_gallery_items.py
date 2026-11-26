"""Each output is a creation of its own in its owner's gallery: favorites and collections.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("favorite_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_assets_owner_role", "assets", ["owner_user_id", "role"])
    op.create_table(
        "collections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_collections_user_id", "collections", ["user_id"])
    op.create_table(
        "collection_items",
        sa.Column(
            "collection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("collections.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "asset_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_collection_items_asset_id", "collection_items", ["asset_id"])


def downgrade() -> None:
    op.drop_index("ix_collection_items_asset_id", table_name="collection_items")
    op.drop_table("collection_items")
    op.drop_index("ix_collections_user_id", table_name="collections")
    op.drop_table("collections")
    op.drop_index("ix_assets_owner_role", table_name="assets")
    op.drop_column("assets", "favorite_at")
