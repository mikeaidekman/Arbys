"""How far into the future discovery looks.

Discovery had no time bound: it registered every game both venues listed, and
Kalshi lists NFL a week or more ahead. On 2026-09-03 the local database held
174 upcoming groups of which 127 started more than 7 days out — past the fill
rule (``ARBYS_MAX_DAYS_TO_START``), so they could never trade and were pure
subscription cost. Spreads triple the group count, which makes that waste the
dominant load.

Nearly all the trading value sits inside a short window. Local ledger, 1,248
fills over 2026-08-28..09-03: 1,177 on game day, 21 one day out, 32 at two to
three days, 16 at four to seven, 2 beyond. A 3-day horizon forgoes 1.4% of
fills and 0.4% of expected profit.

The rule is judged on ``game_date``, which both venues carry as an **Eastern
calendar date** (Kalshi from its ticker, Polymarket US via ``_eastern_date``).
Using the date rather than an exact start means the two venues always agree
on whether a game is in, so a game never has one leg inside the window and
one outside. Day granularity: "3 days" admits a game up to the end of the
third calendar day after today.

The horizon bounds the *future* only. Past games pass, because retirement —
a complete pass no longer finding a group — is what removes finished games,
and a game can only move toward the window, never out of it.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:  # avoid a runtime cycle: kalshi_sports imports this module
    from .kalshi_sports import VenueGame

log = logging.getLogger(__name__)

DEFAULT_HORIZON_DAYS = 3


def discovery_horizon_days() -> int:
    """``ARBYS_DISCOVERY_HORIZON_DAYS``. ``0`` disables the bound."""
    raw = os.environ.get("ARBYS_DISCOVERY_HORIZON_DAYS")
    if raw is None:
        return DEFAULT_HORIZON_DAYS
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning(
            "ARBYS_DISCOVERY_HORIZON_DAYS=%r is not an integer; using %d",
            raw,
            DEFAULT_HORIZON_DAYS,
        )
        return DEFAULT_HORIZON_DAYS


def eastern_today() -> date:
    """Today in Eastern time — the calendar both venues' ``game_date`` uses."""
    now = datetime.now(UTC)
    try:
        return now.astimezone(ZoneInfo("America/New_York")).date()
    except (ValueError, ZoneInfoNotFoundError):  # pragma: no cover - no tzdata
        return now.date()


def within_horizon(game_date: date, *, days: int, today: date | None = None) -> bool:
    """Is the game on or before ``today + days``? ``days <= 0`` admits all."""
    if days <= 0:
        return True
    today = today or eastern_today()
    return game_date <= today + timedelta(days=days)


def filter_horizon(
    games: list[VenueGame], *, days: int, today: date | None = None
) -> list[VenueGame]:
    """The games inside the horizon, in their original order."""
    if days <= 0:
        return games
    today = today or eastern_today()
    return [g for g in games if within_horizon(g.game_date, days=days, today=today)]
