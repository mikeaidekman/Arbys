"""Server-side dashboard aggregation.

Pure functions over repo rows, so these need no database. The behaviour worth
pinning is the split that makes an unbounded window affordable: counts come
from SQL over every ticket, economics come from the hydrated traded subset,
and the two must not be conflated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from arbys.backend.performance import parse_group_id, summarize

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _leg(
    *, venue="kalshi", qty="10", limit="0.40", fill="0.40", fee="0.10",
    resolved=None, outcome_id="k:YES", is_buy=True,
):
    return {
        "venue_id": venue,
        "outcome_id": outcome_id,
        "is_buy": is_buy,
        "qty": Decimal(qty),
        "limit_price": Decimal(limit),
        "fill_price": None if fill is None else Decimal(fill),
        "fee": Decimal(fee),
        "resolved_value": None if resolved is None else Decimal(resolved),
        "status": "filled",
        "rejection_reason": None,
    }


def _ticket(ticket_id, *, status="filled", group="nfl-ARI-LAC-2026-09-13",
            realized=None, legs=None, edge_bps="40"):
    return {
        "id": ticket_id,
        "event_group_id": group,
        "title_snapshot": "NFL: ARI @ LAC (2026-09-13)",
        "source": "auto",
        "status": status,
        "rejection_reason": None,
        "total_stake": Decimal("4.00"),
        "expected_profit": Decimal("0.10"),
        "expected_edge_bps": None if edge_bps is None else Decimal(edge_bps),
        "submitted_at": NOW,
        "starts_at": None,
        "realized_profit": None if realized is None else Decimal(realized),
        "legs": legs if legs is not None else [],
    }


def _scalar(ticket_id, *, status="filled", group="nfl-ARI-LAC-2026-09-13",
            edge_bps="40", reason=None):
    return {
        "id": ticket_id,
        "event_group_id": group,
        "status": status,
        "rejection_reason": reason,
        "expected_edge_bps": None if edge_bps is None else Decimal(edge_bps),
        "submitted_at": NOW,
    }


def test_attempted_counts_rejections_that_were_never_hydrated():
    """The whole point of the split: 76% of rows never reach `traded`.

    If `attempted` were derived from the hydrated rows it would report 1 here
    and the fill rate would read 100% on a day that filled one ticket in 400.
    """
    d = summarize(
        scalars=[_scalar("t-1")] + [
            _scalar(f"r-{i}", status="rejected") for i in range(399)
        ],
        traded=[_ticket("t-1", realized="1.00", legs=[_leg(resolved="1")])],
    )
    assert d["attempted"] == 400
    assert d["filled"] == 1
    assert d["settled_count"] == 1


def test_never_filled_slice_counts_the_unhydrated_majority():
    d = summarize(
        scalars=[
            _scalar("t-1"),
            _scalar("r-1", status="rejected"),
            _scalar("m-1", status="missed"),
        ],
        traded=[_ticket("t-1", realized="1.00", legs=[_leg(resolved="1")])],
    )
    mix = {s["name"]: s["count"] for s in d["outcome_mix"]}
    assert mix["Never filled"] == 2
    assert mix["Settled won"] == 1
    assert sum(s["count"] for s in d["outcome_mix"]) == d["attempted"]


def test_none_is_unknown_never_zero():
    """A window with nothing settled reports None, not a tidy $0.00."""
    d = summarize(
        scalars=[_scalar("r-1", status="rejected")],
        traded=[],
    )
    assert d["net_profit"] is None
    assert d["gross_profit"] is None
    assert d["capital_deployed"] is None
    assert d["hit_rate_pct"] is None
    assert d["return_on_capital_pct"] is None


def test_open_ticket_is_not_settled_and_not_a_loss():
    """`realized_profit` is None while any leg is unsettled."""
    d = summarize(
        scalars=[_scalar("t-open")],
        traded=[_ticket("t-open", realized=None, legs=[_leg(resolved=None)])],
    )
    assert d["open_count"] == 1
    assert d["settled_count"] == 0
    assert d["net_profit"] is None
    # 10 contracts at 0.40 plus a 0.10 fee.
    assert d["open_exposure"] == Decimal("4.10")


def test_fees_and_gross_reconcile_against_net():
    """gross = net + fees, both scoped to settled tickets only."""
    d = summarize(
        scalars=[_scalar("t-1")],
        traded=[
            _ticket(
                "t-1",
                realized="0.50",
                legs=[
                    _leg(qty="10", fill="0.40", fee="0.10", resolved="1"),
                    _leg(venue="polymarket_us", outcome_id="p:SHORT", qty="10",
                         fill="0.55", fee="0.08", resolved="0"),
                ],
            )
        ],
    )
    assert d["fees_paid"] == Decimal("0.18")
    assert d["net_profit"] == Decimal("0.50")
    assert d["gross_profit"] == Decimal("0.68")
    assert d["fee_drag_pct"] is not None


def test_venue_net_excludes_unsettled_legs_from_the_loss():
    """An open leg is exposure, not a total loss."""
    d = summarize(
        scalars=[_scalar("t-1")],
        traded=[
            _ticket(
                "t-1",
                legs=[
                    _leg(venue="kalshi", qty="10", fill="0.40", fee="0.10",
                         resolved="1"),
                    _leg(venue="polymarket_us", outcome_id="p:SHORT", qty="10",
                         fill="0.55", fee="0.08", resolved=None),
                ],
            )
        ],
    )
    by_venue = {v["name"]: v for v in d["by_venue"]}
    # Kalshi settled: 10 back against 4.10 spent.
    assert by_venue["kalshi"]["net"] == Decimal("5.90")
    assert by_venue["kalshi"]["open"] == Decimal("0")
    # Polymarket is unsettled, so it books exposure and no net at all.
    assert by_venue["polymarket us"]["net"] == Decimal("0")
    assert by_venue["polymarket us"]["open"] == Decimal("5.58")


def test_league_counts_every_attempt_but_nets_only_what_traded():
    """A league that attempted 400 and filled 1 must not look like 400 fills."""
    d = summarize(
        scalars=[_scalar("t-1", group="nfl-ARI-LAC-2026-09-13")] + [
            _scalar(f"r-{i}", status="rejected", group="nfl-ARI-LAC-2026-09-13")
            for i in range(9)
        ] + [_scalar("m-1", status="missed", group="mlb-ATL-LAD-2026-09-13")],
        traded=[_ticket("t-1", realized="1.00", legs=[_leg(resolved="1")])],
    )
    by_league = {r["name"]: r for r in d["by_league"]}
    assert by_league["NFL"]["tickets"] == 10
    assert by_league["NFL"]["net"] == Decimal("1.00")
    # Attempted but never traded: unknown economics, not zero.
    assert by_league["MLB"]["tickets"] == 1
    assert by_league["MLB"]["net"] is None


def test_edge_distribution_includes_rejections():
    """A rejected ticket still records the edge the engine thought it saw."""
    d = summarize(
        scalars=[
            _scalar("t-1", edge_bps="10"),                     # 0.10c
            _scalar("r-1", status="rejected", edge_bps="120"),  # 1.20c
            _scalar("r-2", status="rejected", edge_bps=None),   # no economics
        ],
        traded=[_ticket("t-1", realized="1.00", legs=[_leg(resolved="1")])],
    )
    buckets = {b["label"]: b["count"] for b in d["edge_buckets"]}
    assert buckets["<0.25¢"] == 1
    assert buckets["1–2¢"] == 1  # noqa: RUF001 -- display label, en dash
    assert d["mean_edge_cents"] == 0.65


def test_rejection_reasons_are_tallied_busiest_first():
    """The 76% case, counted rather than scrolled."""
    d = summarize(
        scalars=[
            _scalar("r-1", status="rejected", reason="kalshi:insufficient_funds"),
            _scalar("r-2", status="rejected", reason="kalshi:insufficient_funds"),
            _scalar("r-3", status="rejected", reason="polymarket_us:limit_exceeded"),
            _scalar("t-1"),
        ],
        traded=[_ticket("t-1", realized="1.00", legs=[_leg(resolved="1")])],
    )
    assert d["rejection_reasons"] == [
        {"reason": "kalshi:insufficient_funds", "count": 2},
        {"reason": "polymarket_us:limit_exceeded", "count": 1},
    ]
    assert d["by_status"] == {"rejected": 3, "filled": 1}


def test_window_reports_the_span_it_actually_covers():
    """So the UI can say what it holds instead of implying 90 days of data."""
    early = datetime(2026, 8, 28, 14, 21, tzinfo=UTC)
    d = summarize(
        scalars=[
            {**_scalar("t-1"), "submitted_at": early},
            {**_scalar("t-2"), "submitted_at": NOW},
        ],
        traded=[],
    )
    assert d["first_submitted_at"] == early
    assert d["last_submitted_at"] == NOW


def test_market_type_falls_out_of_the_group_id():
    assert parse_group_id("nfl-ARI-LAC-2026-09-13") == ("nfl", "moneyline")
    assert parse_group_id("nfl-ARI-LAC-2026-09-13-total-44.5") == ("nfl", "total")
    # A synthetic intra-venue id does not parse, and says so rather than
    # guessing a league.
    assert parse_group_id("eg-1:kalshi") == ("unknown", "unknown")
