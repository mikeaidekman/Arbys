"""Void the 2026-09-05 Polymarket outage window without deleting its evidence.

Polymarket ran a system-wide outage on 2026-09-05. Our book held college
football totals frozen near their pre-game prices while Kalshi tracked the live
games, and the derived short leg inherited that staleness inverted, which made
it look extraordinarily cheap. `ncaaf-AUB-BAY` Over 45.5 filled as a pair
costing **36.8c against a $1 payout**, booking $19.59 on $11.41;
`ncaaf-DUKE-TULN` Over 51.5 cost 64.1c and booked $92.35. Return on capital
that day was **10.64% against a 0.66% average**, across 128 settled-won
tickets and 0 lost -- a spread that is not itself a signal, because a phantom
pair is as arithmetically certain as a real one once both legs are bought for
under a dollar. The size of the edge is the discriminator, never the win rate.

Every guard passed because every guard measured *time*, and all of them derive
from the venue's own `transactTime`; a frozen book stamped as current defeats
back-dating, the age limit and the leg-skew check together.
`ARBYS_MAX_PLAUSIBLE_EDGE` now asks the one question a clock cannot.

**Excluding, not deleting.** These rows are the only durable record that the
outage produced fills, and `paper_ticket` is the audit log. `excluded_reason`
withholds them from every aggregate -- `_ticket_filters` carries the clause, so
the ledger, its total and the counters cannot disagree -- while
`count_excluded_tickets` lets the page disclose that it is doing so. A voided
window must never look like a quiet one.

**Why cash has to move too.** In the simulator the profit is real: the broker
filled at the stale price and settlement genuinely paid out $1. Flagging the
rows alone would leave that money sitting as buying power, inflating every
later position and the denominator of every return. So each excluded *filled*
ticket is reversed from its own fills, which is the correct unit because
settlement blends `avg_price` across every ticket on an outcome:

    cash_delta[venue] = +(qty * price + fee)        # un-spend the fill
                        -(resolved_value * qty)     # un-receive the payout

Summed over a matched pair that recovers exactly the ticket's realized profit,
with the winning leg contributing the payout and the losing leg only its cost.

**Positions still open are rebuilt, not adjusted.** Handing back the cost of an
unsettled ticket while leaving its position would pay for those contracts
twice. Nothing in this system ever sells -- positions are built by buy fills
and destroyed by settlement -- so a surviving position is exactly the weighted
average of its non-excluded fills, and one with nothing left behind it is
removed outright.

Reversible by design: the flag records which rows were voided, so the same
arithmetic run backwards restores the account. `downgrade()` does that before
dropping the column.

The window is Eastern 13:00-19:00 on 2026-09-05, which is 17:00-23:00 UTC.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "0011_exclude_outage_window"
down_revision: str | None = "0010_fund_trading_venues"
branch_labels = None
depends_on = None

REASON = "polymarket_outage_2026-09-05"
WINDOW_START = "2026-09-05 17:00:00"
WINDOW_END = "2026-09-05 23:00:00"

# Fills belonging to voided tickets, with the venue holding them. The
# settlement that later paid them out is resolved in Python, because the one
# that matters is the earliest at or after the fill -- the resolution this
# position was still open for.
_VOIDED_FILLS = sa.text(
    """
    SELECT o.account_id, o.venue_id, o.outcome_id,
           f.qty, f.price, f.fee, f.ts
      FROM paper_fill f
      JOIN paper_order o ON o.id = f.order_id
      JOIN paper_ticket t ON t.id = o.ticket_id
     WHERE t.excluded_reason IS NOT NULL
       AND t.status = 'filled'
    """
)

_SETTLEMENTS = sa.text(
    "SELECT outcome_id, resolved_value, ts FROM paper_settlement ORDER BY ts"
)

# Every fill that survives the exclusion, which is the book as it would have
# been. Orders with no ticket at all predate ticket-level history and are kept.
_SURVIVING_FILLS = sa.text(
    """
    SELECT o.account_id, o.venue_id, o.outcome_id, f.qty, f.price
      FROM paper_fill f
      JOIN paper_order o ON o.id = f.order_id
      LEFT JOIN paper_ticket t ON t.id = o.ticket_id
     WHERE t.excluded_reason IS NULL OR t.id IS NULL
    """
)


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _payout_for(settlements, outcome_id, fill_ts):
    """What one contract of this fill was paid, or None if it never settled."""
    for ts, resolved in settlements.get(outcome_id, ()):
        if fill_ts is None or ts is None or ts >= fill_ts:
            return resolved
    return None


def _settlement_map(bind):
    settlements = defaultdict(list)
    for row in bind.execute(_SETTLEMENTS).mappings():
        settlements[row["outcome_id"]].append((row["ts"], row["resolved_value"]))
    return settlements


def _cash_deltas(bind, settlements) -> dict[tuple[str, str], Decimal]:
    deltas: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for row in bind.execute(_VOIDED_FILLS).mappings():
        qty, price, fee = _dec(row["qty"]), _dec(row["price"]), _dec(row["fee"])
        delta = qty * price + fee
        payout = _payout_for(settlements, row["outcome_id"], row["ts"])
        if payout is not None:
            delta -= _dec(payout) * qty
        deltas[(row["account_id"], row["venue_id"])] += delta
    return deltas


def _apply_cash(bind, deltas, *, sign: int) -> None:
    for (account_id, venue_id), delta in deltas.items():
        bind.execute(
            sa.text(
                "UPDATE paper_balance SET amount = amount + :d "
                "WHERE account_id = :a AND venue_id = :v"
            ),
            {"d": str(sign * delta), "a": account_id, "v": venue_id},
        )


def _rebuild_open_positions(bind) -> None:
    """Recompute every still-open position from the fills that survive."""
    surviving: dict[tuple[str, str, str], list[tuple[Decimal, Decimal]]] = defaultdict(list)
    for row in bind.execute(_SURVIVING_FILLS).mappings():
        surviving[(row["account_id"], row["venue_id"], row["outcome_id"])].append(
            (_dec(row["qty"]), _dec(row["price"]))
        )

    open_positions = (
        bind.execute(
            sa.text(
                "SELECT account_id, venue_id, outcome_id FROM paper_position "
                "WHERE qty <> 0"
            )
        )
        .mappings()
        .all()
    )

    for pos in open_positions:
        key = (pos["account_id"], pos["venue_id"], pos["outcome_id"])
        fills = surviving.get(key, [])
        total_qty = sum((q for q, _ in fills), Decimal("0"))
        if total_qty <= 0:
            # Settlement is what normally zeroes a position. This one was
            # bought entirely by voided tickets, so it should not exist.
            bind.execute(
                sa.text(
                    "DELETE FROM paper_position WHERE account_id = :a "
                    "AND venue_id = :v AND outcome_id = :o"
                ),
                {"a": key[0], "v": key[1], "o": key[2]},
            )
            continue
        cost = sum((q * p for q, p in fills), Decimal("0"))
        bind.execute(
            sa.text(
                "UPDATE paper_position SET qty = :q, avg_price = :p "
                "WHERE account_id = :a AND venue_id = :v AND outcome_id = :o"
            ),
            {
                "q": str(total_qty),
                "p": str(cost / total_qty),
                "a": key[0],
                "v": key[1],
                "o": key[2],
            },
        )


def upgrade() -> None:
    op.add_column(
        "paper_ticket", sa.Column("excluded_reason", sa.String(128), nullable=True)
    )
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE paper_ticket SET excluded_reason = :r "
            "WHERE submitted_at >= :s AND submitted_at < :e"
        ),
        {"r": REASON, "s": WINDOW_START, "e": WINDOW_END},
    )
    settlements = _settlement_map(bind)
    _apply_cash(bind, _cash_deltas(bind, settlements), sign=1)
    _rebuild_open_positions(bind)
    # The equity curve through the window is drawn from these and every point
    # carries the phantom gains. They regenerate every 30 seconds.
    bind.execute(
        sa.text("DELETE FROM paper_pnl_snapshot WHERE ts >= :s"),
        {"s": WINDOW_START},
    )


def downgrade() -> None:
    bind = op.get_bind()
    settlements = _settlement_map(bind)
    # Put the money back while the flag that identifies it still exists.
    _apply_cash(bind, _cash_deltas(bind, settlements), sign=-1)
    bind.execute(sa.text("UPDATE paper_ticket SET excluded_reason = NULL"))
    _rebuild_open_positions(bind)
    op.drop_column("paper_ticket", "excluded_reason")
