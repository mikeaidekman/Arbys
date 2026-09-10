"""paper_transfer — cash levelled between the per-venue paper books.

An arb ticket buys one leg on each venue and the legs almost never cost the
same: a pair is a heavy favourite against a longshot, so one venue is asked
for ~95% of the ticket's capital and the other for ~5%. Measured over the
1,248 filled tickets in the local ledger, the Kalshi share of a ticket's cost
runs p10 0.054 to p90 0.947 — and which venue needs the big half is a coin
flip (mean share 0.508, Kalshi dearer on 52.4%).

Fixed per-venue funding is therefore wrong however it is split. Of 6,316
rejected tickets, **2,835 (44.9%) had one venue out of cash while the other
leg previewed clean** — 2.3x the entire filled book, refused because the money
was in the wrong place rather than absent. `CashSweepService` levels it; this
table is what it wrote.

**A transfer is not a deposit**, which is the reason for a separate table
rather than a `paper_balance` history. Migration 0010 added capital and every
return figure had to move with it. A sweep adds nothing: `account_equity` sums
cash across brokers, so equity is unchanged by construction. Anything that
ever reads this table must preserve that distinction.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012_paper_transfer"
down_revision: str | None = "0011_exclude_outage_window"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_transfer",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "account_id", sa.String(64), sa.ForeignKey("paper_account.id"), nullable=False
        ),
        sa.Column(
            "from_venue_id", sa.String(64), sa.ForeignKey("venue.id"), nullable=False
        ),
        sa.Column(
            "to_venue_id", sa.String(64), sa.ForeignKey("venue.id"), nullable=False
        ),
        sa.Column("amount", sa.Numeric(28, 12), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="sweep"),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_paper_transfer_ts", "paper_transfer", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_paper_transfer_ts", table_name="paper_transfer")
    op.drop_table("paper_transfer")
