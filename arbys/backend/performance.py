"""Server-side aggregation behind the performance dashboard.

Pure functions over rows the repositories already returned -- no I/O, no
session, no FastAPI -- so the arithmetic can be reasoned about and tested on
its own.

**Why this moved off the browser.** The page used to fetch raw tickets and
aggregate them in `frontend/src/lib/performance.ts`, which meant every figure
was bounded by however many rows one request could carry. That cap was 1000,
and at the auto-trader's ~1,500 tickets/day it covered **9h38m** -- so `All`,
`7D`, `30D` and `90D` all rendered the same slice of one morning while
implying otherwise. Aggregating here returns a fixed handful of numbers
whatever the ledger's size, which is the only shape that stays correct as it
grows.

Two conventions carry through from the TypeScript this replaces, and both
matter more than they look:

- **None is unknown, never zero.** A missed ticket has no economics, and a
  zero would read as a free ticket that made nothing. Sums skip None, and a
  total whose every contributor was None stays None.
- **Two independent axes.** `status` says what happened at *submission*
  (filled / rejected / missed / pending); `outcome` says what happened at
  *settlement* (open / won / lost / flat). A rejected ticket never traded, so
  it has no settlement outcome at all -- that is `"none"`, and it is not the
  same thing as breaking even.

Money stays `Decimal` per the project rule. Percentages and ratios are
`float`: they are display-only derived quantities, never a position or a
price.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

# `matcher.py:event_group_id` builds `{sport}-{teamA}-{teamB}-{YYYY-MM-DD}`,
# appending `-{market_type}-{line}` for anything that is not a moneyline. A
# ticket freezes that string and never joins back to `event_group` -- that is
# the point, since discovery retires groups constantly -- so this parse is the
# only route to sport and market type on a historical row.
#
# Anchored on the ISO date rather than on segment counts: team codes vary in
# width (`KU`, `ALTMAIER`) and the date itself contains two hyphens, so
# counting segments misreads both ends.
_GROUP_ID = re.compile(
    r"^([a-z0-9]+)-(.+?)-(\d{4}-\d{2}-\d{2})(?:-([a-z0-9_]+)-(.+))?$"
)

# Captured-edge buckets, in cents per contract pair.
#
# Deliberately sub-cent, unlike whole-cent steps. Both venues charge a fee
# peaking at 1.75c and 1.5c per contract at even money, and measured gross
# divergence between them topped out at 2.75c -- so a real *net* edge lives
# well inside the first cent, and cent-wide buckets would put every row this
# system has ever produced in one bar.
_EDGE_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<0.25¢", 0.0, 0.25),
    # En dashes are deliberate: these strings are rendered as axis labels, and
    # they are the same glyphs the page showed before the aggregation moved.
    ("0.25–0.5¢", 0.25, 0.5),  # noqa: RUF001
    ("0.5–1¢", 0.5, 1.0),  # noqa: RUF001
    ("1–2¢", 1.0, 2.0),  # noqa: RUF001
    ("2¢+", 2.0, float("inf")),
)

_OUTCOME_MIX = (
    ("Settled won", "won"),
    ("Open", "open"),
    ("Settled lost", "lost"),
    ("Flat", "flat"),
    # Kept as its own slice on purpose: a ticket that never traded is the
    # measurement that says whether latency work is worth anything, and
    # folding it in with a loss would hide it.
    ("Never filled", "none"),
)


def parse_group_id(group_id: str) -> tuple[str, str]:
    """(sport, market_type) for a frozen event group id."""
    match = _GROUP_ID.match(group_id)
    if match is None:
        return ("unknown", "unknown")
    # A moneyline group carries no market-type segment at all; that absence is
    # the encoding, not missing data.
    return (match.group(1), match.group(4) or "moneyline")


def _sum_or_none(values: list[Decimal | None]) -> Decimal | None:
    """Sum, skipping None. None when nothing contributed."""
    total = Decimal("0")
    seen = False
    for value in values:
        if value is None:
            continue
        total += value
        seen = True
    return total if seen else None


def _leg_cost(leg: dict) -> Decimal | None:
    """What a leg actually cost: filled size at the filled price, plus fee."""
    if leg["fill_price"] is None:
        return None
    return leg["qty"] * leg["fill_price"] + leg["fee"]


def _leg_returned(leg: dict) -> Decimal | None:
    """What a leg paid back. None while its outcome carries no settlement."""
    if leg["resolved_value"] is None or leg["fill_price"] is None:
        return None
    return leg["qty"] * leg["resolved_value"]


def _pct(numerator: Decimal | float | None, denominator: Decimal | float | None
         ) -> float | None:
    """`numerator / denominator` as a percentage, or None when meaningless."""
    if numerator is None or denominator is None or float(denominator) <= 0:
        return None
    return float(numerator) / float(denominator) * 100.0


def ticket_outcome(status: str, realized_profit: Decimal | None) -> str:
    """Settlement outcome. Orthogonal to `status`, the submission axis."""
    if status in ("rejected", "missed"):
        return "none"
    if realized_profit is None:
        return "open"
    if realized_profit > 0:
        return "won"
    if realized_profit < 0:
        return "lost"
    return "flat"


class _Row:
    """One traded ticket's economics, derived once and reused everywhere."""

    __slots__ = (
        "capital",
        "fees",
        "market_type",
        "net",
        "outcome",
        "qty",
        "returned",
        "settlement_value",
        "sport",
        "status",
        "submitted_at",
    )

    def __init__(self, ticket: dict) -> None:
        legs = ticket["legs"]
        self.sport, self.market_type = parse_group_id(ticket["event_group_id"])
        self.status = ticket["status"]
        self.submitted_at = ticket["submitted_at"]
        self.capital = _sum_or_none([_leg_cost(leg) for leg in legs])
        self.returned = _sum_or_none([_leg_returned(leg) for leg in legs])
        # Realized profit is authoritative where the repo could compute it: it
        # scores the ticket's own fills against settlement. Falling back to
        # returned - capital would silently report a half-settled ticket as a
        # loss the size of its unsettled leg.
        self.net = ticket["realized_profit"]
        self.qty = legs[0]["qty"] if legs else None
        self.fees = sum(
            (leg["fee"] for leg in legs if leg["fill_price"] is not None),
            Decimal("0"),
        )
        self.outcome = ticket_outcome(self.status, self.net)

        # Guaranteed settlement value, but only for a genuinely matched pair.
        # A matched pair pays $1 per contract whoever wins, so its profit is
        # fixed at fill time; an unmatched one is directional and pays 0 or 1
        # depending on the result, and quoting a guaranteed figure for it would
        # be a lie. Every filled leg must therefore carry the same quantity.
        filled_qtys = [leg["qty"] for leg in legs if leg["fill_price"] is not None]
        matched = len(filled_qtys) >= 2 and all(
            q == filled_qtys[0] for q in filled_qtys
        )
        self.settlement_value = (
            filled_qtys[0] - self.capital
            if matched and self.capital is not None
            else None
        )


def _activity(scalars: list[dict]) -> dict[str, Any]:
    """Submission-side tallies over every ticket in the window.

    This is the answer to "what has the bot been doing" for the ~83% of rows
    that never traded. Rejections dominate the ledger -- 5,609 of 7,407 on
    2026-09-02, almost all of them the paper account being out of money -- and
    they are a *rate* signal, not entries anybody scrolls, so they are counted
    here and never hydrated.
    """
    by_status: dict[str, int] = {}
    reasons: dict[str, int] = {}
    first = last = None
    for s in scalars:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
        reason = s["rejection_reason"]
        if reason is not None:
            reasons[reason] = reasons.get(reason, 0) + 1
        ts = s["submitted_at"]
        if first is None or ts < first:
            first = ts
        if last is None or ts > last:
            last = ts
    return {
        "by_status": by_status,
        "rejection_reasons": _top_reasons(reasons),
        "first_submitted_at": first,
        "last_submitted_at": last,
    }


# How many distinct rejection reasons to name before rolling the rest up.
#
# The long tail is not a long tail of *causes*: `edge_no_longer_available`
# carries the event group id, and `stale_leg_skew` carries each leg's age, so
# every one of those rows is its own unique string. Measured 2026-09-02, 6,169
# rejections spanned 1,011 distinct reasons of which **852 occurred exactly
# once** -- 141KB of payload, more than the rest of the response put together,
# to say nothing a reader could act on. The top ten cover 72% of rejections and
# are the only ones that name a recurring cause.
_MAX_REASONS = 12


def _top_reasons(reasons: dict[str, int]) -> list[dict]:
    ranked = sorted(reasons.items(), key=lambda kv: -kv[1])
    head = [{"reason": r, "count": c} for r, c in ranked[:_MAX_REASONS]]
    tail = ranked[_MAX_REASONS:]
    if tail:
        # Rolled up rather than dropped: the count still has to reconcile
        # against the rejected total, or the panel silently loses rows.
        head.append(
            {
                "reason": f"other ({len(tail)} distinct)",
                "count": sum(c for _, c in tail),
            }
        )
    return head


def summarize(*, scalars: list[dict], traded: list[dict]) -> dict[str, Any]:
    """The dashboard figures.

    `scalars` is every ticket in the window carrying only its non-leg columns;
    `traded` is the filled and pending subset, hydrated with legs. The split is
    deliberate and is what makes an unbounded window affordable -- league,
    market-type and captured-edge distributions genuinely range over *all*
    tickets (a rejected ticket still records the edge the engine thought it
    saw) but none of them touch a fill.
    """
    rows = [_Row(t) for t in traded]
    settled = [r for r in rows if r.outcome in ("won", "lost", "flat")]
    open_rows = [r for r in rows if r.outcome == "open"]
    filled_rows = [r for r in rows if r.status == "filled"]

    net_profit = _sum_or_none([r.net for r in settled])
    # Settled scope, matching net_profit. Fees on open tickets are real money
    # already spent, but pairing them with a profit that has not happened yet
    # would produce a drag figure that reconciles against nothing.
    fees_paid = sum((r.fees for r in settled), Decimal("0"))
    gross_profit = None if net_profit is None else net_profit + fees_paid
    settled_capital = _sum_or_none([r.capital for r in settled])
    won_count = sum(1 for r in settled if r.outcome == "won")

    activity = _activity(scalars)
    by_status: dict[str, int] = activity["by_status"]
    attempted = sum(by_status.values())

    return {
        "attempted": attempted,
        "filled": by_status.get("filled", 0),
        "by_status": by_status,
        "rejection_reasons": activity["rejection_reasons"],
        "first_submitted_at": activity["first_submitted_at"],
        "last_submitted_at": activity["last_submitted_at"],
        "net_profit": net_profit,
        "gross_profit": gross_profit,
        "fees_paid": fees_paid,
        # Fees as a share of gross. Measured at 62% on 2026-08-29 -- the
        # dominant fact about this strategy.
        "fee_drag_pct": _pct(fees_paid, gross_profit),
        "capital_deployed": _sum_or_none([r.capital for r in rows]),
        "capital_returned": _sum_or_none([r.returned for r in settled]),
        "return_on_capital_pct": _pct(net_profit, settled_capital),
        "hit_rate_pct": (
            None if not settled else won_count / len(settled) * 100.0
        ),
        "settled_count": len(settled),
        "won_count": won_count,
        "open_exposure": sum(
            (r.capital or Decimal("0") for r in open_rows), Decimal("0")
        ),
        "open_count": len(open_rows),
        "both_legs_filled_pct": _pct(
            sum(1 for r in filled_rows if r.capital is not None and r.qty is not None),
            len(filled_rows),
        ),
        "median_slippage_cents": _median_slippage(traded),
        "accrual_curve": _accrual_curve(rows),
        # What every matched pair in the window settles for, guaranteed. The
        # header for the accrual chart, so it must agree with the series' last
        # point rather than being summed over a different population.
        "accrual_total": sum(
            (r.settlement_value or Decimal("0") for r in rows), Decimal("0")
        ),
        **_edge_stats(scalars),
        "by_league": _group_by(scalars, rows, lambda s: s[0].upper(), lambda r: r.sport.upper()),
        "by_market_type": _group_by(scalars, rows, lambda s: s[1], lambda r: r.market_type),
        "by_venue": _by_venue(traded),
        "outcome_mix": _outcome_mix(rows, attempted, by_status),
    }


# Most points the accrual series will carry. The chart is an 800px-wide SVG,
# so more than this is sub-pixel detail nobody can see -- and shipping one
# point per ticket is how the client ended up holding the raw ledger in the
# first place.
_MAX_CURVE_POINTS = 400

# Cent granularity for plot coordinates. Never used on a stored figure.
_CENTS = Decimal("0.01")


def _accrual_curve(rows: list[_Row]) -> list[dict]:
    """Cumulative guaranteed value over the window, oldest first.

    `total` is every matched pair's locked-in profit; `settled` is the part
    already resolved, so the gap between the two lines is what is still owed.
    Only matched pairs count -- an unmatched fill has no guaranteed payout, and
    a calendar of guaranteed payouts must not contain a forecast.
    """
    pts = sorted(
        (r for r in rows if r.settlement_value is not None),
        key=lambda r: r.submitted_at,
    )
    if len(pts) < 2:
        return []
    step = max(1, len(pts) // _MAX_CURVE_POINTS)
    total = Decimal("0")
    settled = Decimal("0")
    out: list[dict] = []
    for i, r in enumerate(pts):
        total += r.settlement_value or Decimal("0")
        if r.net is not None:
            settled += r.settlement_value or Decimal("0")
        # Downsample, but never drop the last point: it carries the running
        # total the header reports, and losing it would understate the window.
        if i % step == 0 or i == len(pts) - 1:
            # Quantized to cents for the wire. These are plot coordinates on a
            # 230px-tall chart, and the raw Decimals run to 24 fractional
            # digits -- 26 bytes apiece for detail no pixel can show. Each
            # point is the exact running total rounded once, so nothing
            # accumulates; `accrual_total` stays full precision.
            out.append(
                {
                    "ts": r.submitted_at,
                    "total": total.quantize(_CENTS),
                    "settled": settled.quantize(_CENTS),
                }
            )
    return out


def _edge_stats(scalars: list[dict]) -> dict[str, Any]:
    """Captured-edge mean and distribution, in cents per contract pair.

    Ranges over every ticket, rejections included: the edge is what the engine
    believed at detection time, and a rejected ticket is exactly the row where
    that belief went unrealised.
    """
    edges = [
        # Basis points of a $1 payout: 100 bps = 1% = 1c per contract pair.
        float(s["expected_edge_bps"]) / 100.0
        for s in scalars
        if s["expected_edge_bps"] is not None
    ]
    return {
        "mean_edge_cents": (sum(edges) / len(edges)) if edges else None,
        "edge_buckets": [
            {"label": label, "count": sum(1 for e in edges if lo <= e < hi)}
            for label, lo, hi in _EDGE_BUCKETS
        ],
    }


def _median_slippage(traded: list[dict]) -> float | None:
    values: list[float] = []
    for ticket in traded:
        for leg in ticket["legs"]:
            if leg["fill_price"] is None:
                continue
            # Buying above the limit is adverse; selling below it is. The sign
            # convention makes positive mean "worse than asked for" either way.
            delta = float(leg["fill_price"] - leg["limit_price"])
            values.append((delta if leg["is_buy"] else -delta) * 100.0)
    if not values:
        return None
    values.sort()
    mid = len(values) // 2
    if len(values) % 2 == 1:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def _group_by(
    scalars: list[dict],
    rows: list[_Row],
    scalar_key,
    row_key,
) -> list[dict]:
    """Per-dimension ticket counts and economics.

    The **count** comes from `scalars` so it covers every ticket including the
    rejections, while **net and capital** come from the traded rows only.
    That asymmetry is intentional and matches what the page showed before: a
    league's ticket count answers "how often did we try here", its net answers
    "what did that earn", and conflating the two populations would make a
    league that attempted 400 tickets and filled 3 look like a league that
    filled 400.
    """
    counts: dict[str, int] = {}
    for s in scalars:
        name = scalar_key(parse_group_id(s["event_group_id"]))
        counts[name] = counts.get(name, 0) + 1

    nets: dict[str, list[Decimal | None]] = {}
    capitals: dict[str, list[Decimal | None]] = {}
    for r in rows:
        name = row_key(r)
        nets.setdefault(name, []).append(r.net)
        capitals.setdefault(name, []).append(r.capital)

    out = []
    for name, tickets in counts.items():
        net = _sum_or_none(nets.get(name, []))
        capital = _sum_or_none(capitals.get(name, []))
        out.append(
            {
                "name": name,
                "tickets": tickets,
                "net": net,
                "capital": capital,
                "roi_pct": _pct(net, capital),
            }
        )
    # Rows with no settled economics sort last rather than as zero -- unknown
    # is not the same as flat.
    out.sort(key=lambda r: (r["net"] is not None, r["net"] or 0), reverse=True)
    return out


def _by_venue(traded: list[dict]) -> list[dict]:
    venues: dict[str, dict] = {}
    for ticket in traded:
        for leg in ticket["legs"]:
            cost = _leg_cost(leg)
            if cost is None:
                continue
            v = venues.setdefault(
                leg["venue_id"],
                {
                    "name": leg["venue_id"].replace("_", " "),
                    "deployed": Decimal("0"),
                    "returned": Decimal("0"),
                    "net": Decimal("0"),
                    "open": Decimal("0"),
                },
            )
            v["deployed"] += cost
            back = _leg_returned(leg)
            if back is None:
                v["open"] += cost
            else:
                v["returned"] += back
                # Net is returned less the cost of the settled legs alone.
                # Netting against `deployed` would book every open leg as a
                # total loss.
                v["net"] += back - cost
    return sorted(venues.values(), key=lambda v: v["deployed"], reverse=True)


def _outcome_mix(
    rows: list[_Row], attempted: int, by_status: dict[str, int]
) -> list[dict]:
    """Share of tickets by settlement outcome.

    `"none"` is counted from the status tally rather than from `rows`, because
    rows holds only the traded tickets -- the never-traded ones are precisely
    what was left unhydrated, and they are the majority.
    """
    counts = {key: 0 for _, key in _OUTCOME_MIX}
    for r in rows:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    counts["none"] = by_status.get("rejected", 0) + by_status.get("missed", 0)
    return [
        {
            "name": name,
            "count": counts[key],
            "share_pct": (counts[key] / attempted * 100.0) if attempted else 0.0,
        }
        for name, key in _OUTCOME_MIX
    ]
