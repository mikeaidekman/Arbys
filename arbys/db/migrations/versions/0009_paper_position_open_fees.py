"""paper_position.open_fees — fees carried against a still-open position.

Realized P&L is now reported net of taker fees, which means the fee has to be
held against the position that incurred it until that position closes, rather
than deducted when it is paid. Cash is debited immediately either way — the
money really has left — but `equity` is `cash + position_value` and does not
include realized, so netting fees into realized cannot double-count.

This column exists so the carried amount survives a restart. Without it a
deploy landing mid-position would drop the fees and let the position settle
gross, and deploys here are frequent.

Defaults to 0, which is correct for rows written before the column existed:
their fees were never carried, so there is nothing to deduct at settlement.
Those positions settle gross, exactly as they would have before — the change is
not retroactive and does not pretend to be.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009_paper_position_open_fees"
down_revision: str | None = "0008_paper_ticket_starts_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "paper_position",
        sa.Column(
            "open_fees",
            sa.Numeric(28, 12),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("paper_position", "open_fees")
