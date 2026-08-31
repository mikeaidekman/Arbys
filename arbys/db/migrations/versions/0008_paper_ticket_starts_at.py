"""paper_ticket.starts_at — when the game begins, snapshotted at submit time.

The third value frozen onto a ticket rather than joined at read time, for the
same reason as the first two: a ticket outlives its event group. The edge here
is sharper than for `title_snapshot`, though. Discovery retires a group when the
game *finishes*, which is while the ticket is still awaiting settlement — so a
live join to `event_group.start_time` would be empty for exactly the rows a
"what settles when" view has to place.

Nullable, and null means *unknown*. A group that reports no start time has none
to record (an NFL Kalshi ticker carries a date with no HHMM), and rows written
before this column existed have none either. Reading null as "settles now"
would put every historical ticket on today's calendar.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008_paper_ticket_starts_at"
down_revision: str | None = "0007_arb_opportunity_no_group_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "paper_ticket",
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("paper_ticket", "starts_at")
