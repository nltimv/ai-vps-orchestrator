"""The improved description a creation was made with (sealed like the description itself).

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("prompt_used_enc", sa.LargeBinary(), nullable=True))
    # The user's words used to be copied unencrypted into jobs.resolved -> params; they're rebuilt
    # from the sealed fields now, so drop the copies of finished and waiting jobs alike.
    for key in ("prompt", "negative", "tags", "lyrics"):
        op.execute(
            sa.text(
                "UPDATE jobs SET resolved = jsonb_set(resolved, '{params}', (resolved->'params') - :key) "
                "WHERE jsonb_typeof(resolved->'params') = 'object' AND resolved->'params'->:key IS NOT NULL"
            ).bindparams(key=key)
        )


def downgrade() -> None:
    op.drop_column("jobs", "prompt_used_enc")
