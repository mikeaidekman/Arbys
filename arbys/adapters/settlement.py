"""What each venue says a finished market settled at.

Both readers answer one question for one ``outcome_id``: the value a contract
on that side paid out, or ``None`` when the venue has not finalised it. They
never infer a result from a price -- a finished market has no price, which is
exactly why ``AutoSettleService`` could not settle it (see
``ingest/venue_settle_service.py``).

Verified 2026-10-03 against Gaubas v Wendelken, 2026-08-27, which both venues
agree Wendelken won:

- **Kalshi** ``GET /markets/{ticker}`` returned ``status: "finalized"``,
  ``result: "no"`` on the Gaubas market. An open market reads ``"active"`` with
  ``result: ""``.
- **Polymarket US** ``GET /v1/markets/{slug}/settlement`` returned
  ``{"settlement": 0}`` -- the value of the **long** side, Gaubas. An unsettled
  market answers 404 ``Settlement not found``. The long side's value is the
  number reported; the short side is its complement, the same inversion the
  quote path uses.

Anything else -- a network error, a 5xx, a 429, a status or result we do not
recognise -- reads as ``None``. Not settling is recoverable on the next pass;
settling on a misread is not.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

import httpx

log = logging.getLogger(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_US_GATEWAY = "https://gateway.polymarket.us"

# `determined` is deliberately absent: the result is known but the venue has
# not paid out, and it moves to `finalized` shortly after. Waiting one more
# pass costs nothing.
_KALSHI_FINAL = frozenset({"finalized", "settled"})


def _split(outcome_id: str) -> tuple[str, str] | None:
    market, sep, side = outcome_id.rpartition(":")
    if not sep or not market:
        return None
    return market, side


async def fetch_kalshi_settlement(
    client: httpx.AsyncClient, outcome_id: str
) -> Decimal | None:
    parts = _split(outcome_id)
    if parts is None or parts[1] not in ("YES", "NO"):
        return None
    ticker, side = parts
    try:
        resp = await client.get(f"{KALSHI_BASE}/markets/{ticker}")
    except httpx.HTTPError as exc:
        log.info("kalshi settlement lookup failed for %s: %s", ticker, exc)
        return None
    if resp.status_code != 200:
        if resp.status_code != 404:
            log.info("kalshi settlement lookup %s -> HTTP %d", ticker, resp.status_code)
        return None
    market = resp.json().get("market") or {}
    if market.get("status") not in _KALSHI_FINAL:
        return None
    result = market.get("result")
    if result not in ("yes", "no"):
        # A void or scalar settlement. Rare enough to leave open and look at
        # by hand rather than guess what the payout was.
        if result:
            log.warning("kalshi %s finalized with unhandled result %r", ticker, result)
        return None
    yes_won = result == "yes"
    return Decimal("1") if yes_won == (side == "YES") else Decimal("0")


async def fetch_polymarket_us_settlement(
    client: httpx.AsyncClient, outcome_id: str
) -> Decimal | None:
    parts = _split(outcome_id)
    if parts is None or parts[1] not in ("LONG", "SHORT"):
        return None
    slug, side = parts
    try:
        resp = await client.get(f"{POLYMARKET_US_GATEWAY}/v1/markets/{slug}/settlement")
    except httpx.HTTPError as exc:
        log.info("polymarket_us settlement lookup failed for %s: %s", slug, exc)
        return None
    if resp.status_code != 200:
        # 404 is the normal "not settled yet" answer.
        if resp.status_code != 404:
            log.info("polymarket_us settlement lookup %s -> HTTP %d", slug, resp.status_code)
        return None
    raw = resp.json().get("settlement")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        long_value = Decimal(str(raw))
    except InvalidOperation:
        return None
    # A walkover settles "to fair market price", so a fraction is legitimate;
    # anything outside [0, 1] is not a payout and is refused.
    if not Decimal("0") <= long_value <= Decimal("1"):
        log.warning("polymarket_us %s settlement %s outside [0, 1]", slug, raw)
        return None
    return long_value if side == "LONG" else Decimal("1") - long_value
