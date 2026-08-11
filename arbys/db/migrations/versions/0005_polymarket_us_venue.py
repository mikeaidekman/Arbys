"""replace the polymarket venue with polymarket_us

Revision ID: 0005_polymarket_us_venue
Revises: 0004_event_group_source
Create Date: 2026-08-11

Polymarket's international book is not tradeable from the US. Polymarket US
is a separate CFTC-regulated exchange with its own order book; shares are not
fungible between the two.

Rows under venue_id='polymarket' are deleted rather than remapped. Their
outcome_id values are international CLOB token ids, which identify nothing on
the US book — there is no correct target to remap them to. This discards
simulated paper history, which is acceptable because it is a paper account.

Deletion order follows the foreign keys inward: fills before orders, legs and
quotes before outcomes, outcomes before markets, markets before the venue row.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_polymarket_us_venue"
down_revision: str | None = "0004_event_group_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "polymarket"
_NEW = "polymarket_us"


def upgrade() -> None:
    # --- paper trading state -------------------------------------------------
    op.execute(
        f"""
        DELETE FROM paper_fill
         WHERE order_id IN (
               SELECT id FROM paper_order WHERE venue_id = '{_OLD}'
         )
        """
    )
    op.execute(f"DELETE FROM paper_order WHERE venue_id = '{_OLD}'")
    op.execute(f"DELETE FROM paper_position WHERE venue_id = '{_OLD}'")
    op.execute(f"DELETE FROM paper_balance WHERE venue_id = '{_OLD}'")

    # --- market reference data ----------------------------------------------
    # event_group_leg and quote both hang off outcome, which hangs off market.
    op.execute(
        f"""
        DELETE FROM event_group_leg
         WHERE outcome_id IN (
               SELECT o.id FROM outcome o
                 JOIN market m ON m.id = o.market_id
                WHERE m.venue_id = '{_OLD}'
         )
        """
    )
    op.execute(
        f"""
        DELETE FROM quote
         WHERE outcome_id IN (
               SELECT o.id FROM outcome o
                 JOIN market m ON m.id = o.market_id
                WHERE m.venue_id = '{_OLD}'
         )
        """
    )
    op.execute(
        f"""
        DELETE FROM outcome
         WHERE market_id IN (SELECT id FROM market WHERE venue_id = '{_OLD}')
        """
    )
    op.execute(f"DELETE FROM market WHERE venue_id = '{_OLD}'")

    # A group left with no legs can no longer produce a cross-venue arb; drop
    # it so discovery re-registers it cleanly against the US book.
    op.execute(
        """
        DELETE FROM event_group
         WHERE id NOT IN (SELECT DISTINCT event_group_id FROM event_group_leg)
        """
    )
    op.execute("DELETE FROM arb_opportunity")

    # --- the venue row itself ------------------------------------------------
    op.execute(f"DELETE FROM venue WHERE id = '{_OLD}'")
    op.execute(
        f"""
        INSERT INTO venue (id, name, kind)
        SELECT '{_NEW}', 'Polymarket US', 'exchange'
         WHERE NOT EXISTS (SELECT 1 FROM venue WHERE id = '{_NEW}')
        """
    )


def downgrade() -> None:
    # The deleted rows are gone for good; this restores only the venue row so
    # the chain stays reversible in shape.
    op.execute(f"DELETE FROM venue WHERE id = '{_NEW}'")
    op.execute(
        f"""
        INSERT INTO venue (id, name, kind)
        SELECT '{_OLD}', 'Polymarket', 'exchange'
         WHERE NOT EXISTS (SELECT 1 FROM venue WHERE id = '{_OLD}')
        """
    )
