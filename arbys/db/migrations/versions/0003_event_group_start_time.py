"""add start_time to event_group

Revision ID: 0003_event_group_start_time
Revises: 0002_paper_position_venue_id
Create Date: 2026-08-08

Event groups carried only a date, embedded in the generated id, so the UI had
nothing to show a countdown against and could not order games chronologically
within a day. Both venues report an exact start: Kalshi as
``occurrence_datetime`` on the market, Polymarket as ``gameStartTime``.

Nullable and not backfilled — hand-registered groups have no start, and
discovery repopulates auto-registered ones on its next pass.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_event_group_start_time"
down_revision: str | None = "0002_paper_position_venue_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("event_group") as batch:
        batch.add_column(
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=True)
        )
    op.create_index(
        "ix_event_group_start_time", "event_group", ["start_time"]
    )


def downgrade() -> None:
    op.drop_index("ix_event_group_start_time", table_name="event_group")
    with op.batch_alter_table("event_group") as batch:
        batch.drop_column("start_time")
