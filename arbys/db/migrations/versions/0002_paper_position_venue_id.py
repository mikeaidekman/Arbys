"""add venue_id to paper_position

Revision ID: 0002_paper_position_venue_id
Revises: 0001_initial
Create Date: 2026-08-07

`paper_position` was keyed on (account_id, outcome_id) only, so startup
rehydration had no way to tell which of the per-venue paper brokers owned a
row and fanned every row out to all of them — inflating position qty and
realized PnL by the broker count after any restart.

outcome_id is venue-native, so the owning venue is recoverable by joining
outcome -> market -> venue; that join is the backfill.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_paper_position_venue_id"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("paper_position") as batch:
        batch.add_column(sa.Column("venue_id", sa.String(64), nullable=True))

    # Backfill from the outcome's market. Every paper_position.outcome_id is an
    # FK into outcome, so this resolves for all existing rows.
    op.execute(
        """
        UPDATE paper_position
           SET venue_id = (
               SELECT m.venue_id
                 FROM outcome o
                 JOIN market m ON m.id = o.market_id
                WHERE o.id = paper_position.outcome_id
           )
        """
    )
    # Defensive: an unattributable row cannot be assigned to a broker, and
    # keeping it would block the NOT NULL below.
    op.execute("DELETE FROM paper_position WHERE venue_id IS NULL")

    with op.batch_alter_table("paper_position") as batch:
        batch.alter_column("venue_id", existing_type=sa.String(64), nullable=False)
        batch.drop_constraint("uq_paper_pos_acct_outcome", type_="unique")
        batch.create_unique_constraint(
            "uq_paper_pos_acct_venue_outcome",
            ["account_id", "venue_id", "outcome_id"],
        )
        batch.create_foreign_key(
            "fk_paper_position_venue_id_venue", "venue", ["venue_id"], ["id"]
        )


def downgrade() -> None:
    # Collapsing back to (account_id, outcome_id) can violate the old unique
    # constraint when the same outcome is held on more than one venue, so keep
    # the highest-qty row per (account_id, outcome_id) and drop the rest.
    op.execute(
        """
        DELETE FROM paper_position
         WHERE id NOT IN (
               SELECT MIN(id) FROM paper_position GROUP BY account_id, outcome_id
         )
        """
    )
    with op.batch_alter_table("paper_position") as batch:
        batch.drop_constraint("fk_paper_position_venue_id_venue", type_="foreignkey")
        batch.drop_constraint("uq_paper_pos_acct_venue_outcome", type_="unique")
        batch.create_unique_constraint(
            "uq_paper_pos_acct_outcome", ["account_id", "outcome_id"]
        )
        batch.drop_column("venue_id")
