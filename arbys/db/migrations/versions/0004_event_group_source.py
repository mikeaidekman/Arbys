"""add source to event_group

Revision ID: 0004_event_group_source
Revises: 0003_event_group_start_time
Create Date: 2026-08-09

Discovery upserted the groups it found but never removed ones that stopped
matching, so a group whose underlying markets had gone away lived on forever
via restart hydration — still displayed, still priced off its last quotes.

Retiring them needs a way to tell a discovery-registered group from a
hand-registered one, or a cleanup pass would delete the latter too.

Existing rows are backfilled to 'discovery' when their id matches the shape
discovery generates (``<sport>-<TEAM>-<TEAM>-<date>``, optionally with a
market suffix), and 'manual' otherwise.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_event_group_source"
down_revision: str | None = "0003_event_group_start_time"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("event_group") as batch:
        batch.add_column(
            sa.Column("source", sa.String(16), nullable=True, server_default="manual")
        )
    # Discovery ids look like "mlb-CIN-WSH-2026-08-09" or
    # "nfl-ARI-LAC-2026-09-13-total-44.5"; anything else was hand-registered.
    op.execute(
        """
        UPDATE event_group
           SET source = 'discovery'
         WHERE id GLOB '*-[A-Z]*-[A-Z]*-[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
        """
    )
    op.execute("UPDATE event_group SET source = 'manual' WHERE source IS NULL")
    with op.batch_alter_table("event_group") as batch:
        batch.alter_column(
            "source", existing_type=sa.String(16), nullable=False,
            server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table("event_group") as batch:
        batch.drop_column("source")
