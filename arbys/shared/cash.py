"""Levelling free cash across the per-venue paper books.

An arb ticket buys one leg on each venue, and the two legs almost never cost
the same: a pair is a heavy favourite against a longshot, so one venue is
asked for ~95% of the ticket's capital and the other for ~5%. Measured over
the 1,248 filled tickets in the local ledger, the Kalshi share of a ticket's
cost has p10 **0.054** and p90 **0.947** -- and *which* venue needs the big
half is a coin flip: mean share 0.508, Kalshi dearer on 52.4% of tickets.

So a fixed per-venue allocation is wrong by construction. Equal funding drains
whichever venue a run of tickets happens to lean on while the other sits idle,
and no smarter *starting* split helps, because the demand is symmetric in
expectation and violently lopsided per trade. Of 6,316 rejected tickets in
that ledger, **2,835 (44.9%) had one venue out of cash and the other leg
previewing clean** -- 2.3x the entire filled book, refused because the money
was in the wrong place rather than absent. (751 more had *both* venues dry;
that is real capital exhaustion and no transfer fixes it.)

Real venue-to-venue movement is a bank round trip, not an internal sweep, so
this models a rail that settles faster than the real one. That is a deliberate
simplification: levelling is what a funded operator does continuously, and the
alternative -- cash in transit, counting toward equity but not buying power --
would have to be threaded through `account_equity`, `PnlSnapshotService` and
`GET /paper/{id}` or the equity curve would dip by the transferred amount for
the duration of every sweep.

Pure: plans from a balance mapping and mutates only in-memory broker state, so
it performs no I/O and is legal in `shared/`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from .paper_broker import PaperExecutionAdapter

CENT = Decimal("0.01")


@dataclass(frozen=True)
class CashTransfer:
    from_venue: str
    to_venue: str
    amount: Decimal


def _floor_cent(amount: Decimal) -> Decimal:
    """Round *down* to the cent, so a plan can never overdraw its source."""
    return amount.quantize(CENT, rounding=ROUND_DOWN)


def plan_transfers(
    balances: Mapping[str, Decimal], *, min_transfer: Decimal
) -> tuple[CashTransfer, ...]:
    """Transfers that would level `balances`, largest surplus paying first.

    The target is an equal share, because the measured demand is symmetric in
    expectation (see the module docstring) -- there is no per-venue bias to
    correct, only a random walk to keep centred.

    `min_transfer` drops individual moves below it. Without a floor the sweep
    writes an audit row every interval to move pennies, which is the same
    bookkeeping-for-a-fraction-of-a-cent problem `ARBYS_MIN_CONTRACT_QTY`
    exists to stop.
    """
    if len(balances) < 2:
        return ()
    total = sum(balances.values(), Decimal("0"))
    if total <= 0:
        return ()
    target = total / len(balances)

    # Ties broken on venue id so a plan is deterministic and testable.
    surplus = sorted(
        ((v, amt - target) for v, amt in balances.items() if amt > target),
        key=lambda p: (-p[1], p[0]),
    )
    deficit = sorted(
        ((v, target - amt) for v, amt in balances.items() if amt < target),
        key=lambda p: (-p[1], p[0]),
    )

    plan: list[CashTransfer] = []
    si = 0
    for to_venue, want in deficit:
        remaining = want
        while remaining > 0 and si < len(surplus):
            from_venue, spare = surplus[si]
            move = min(remaining, spare)
            amount = _floor_cent(move)
            if amount >= min_transfer:
                plan.append(CashTransfer(from_venue, to_venue, amount))
            remaining -= move
            spare -= move
            if spare <= 0:
                si += 1
            else:
                surplus[si] = (from_venue, spare)
    return tuple(plan)


def apply_transfers(
    brokers: Mapping[str, PaperExecutionAdapter],
    account_id: str,
    plan: tuple[CashTransfer, ...],
) -> tuple[CashTransfer, ...]:
    """Move the cash in memory and return what actually moved.

    Synchronous and awaitless on purpose, the same discipline `apply_fill`
    keeps: coroutines only yield at `await`, so a whole plan lands without a
    fill or a settlement interleaving and reading a half-levelled account.

    A leg is skipped rather than raised on if its source cannot cover it --
    the plan is built from the same balances, so that means something moved
    underneath us, and a partial level is harmless where a raise would kill
    the sweep task.
    """
    applied: list[CashTransfer] = []
    for t in plan:
        src = brokers.get(t.from_venue)
        dst = brokers.get(t.to_venue)
        if src is None or dst is None:
            continue
        if not src.withdraw(account_id, t.amount):
            continue
        dst.deposit(account_id, t.amount)
        applied.append(t)
    return tuple(applied)
