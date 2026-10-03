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

**Polymarket's gateway throttles hard.** Measured 2026-10-03, its Cloudflare
front answered 429 with ``Retry-After: 10`` after about five requests, and
still refused 6 of 40 at one request a second. A pass that did not wait out
``Retry-After`` resolved 29 of 570 settled markets and left the rest open,
which is how the first production pass ended with 76 unresolved. ``_get``
waits it out and retries.

Kalshi settles a tie (an NFL preseason game, 2026-08-28) as ``result:
"scalar"`` with ``settlement_value_dollars: "0.5000"``, the YES payout.

Anything else -- a network error, a 5xx, a 429 that outlasts the retries, a status or result we do not
recognise -- reads as ``None``. Not settling is recoverable on the next pass;
settling on a misread is not.
"""

from __future__ import annotations

import asyncio
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


# Indirected so tests can wait out a 429 without waiting.
_sleep = asyncio.sleep

MAX_429_RETRIES = 4
MAX_RETRY_AFTER_S = 30.0


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    """GET, waiting out 429s. None on a network error or persistent throttle."""
    for attempt in range(MAX_429_RETRIES + 1):
        try:
            resp = await client.get(url)
        except httpx.HTTPError as exc:
            log.info("settlement lookup failed for %s: %s", url, exc)
            return None
        if resp.status_code != 429:
            return resp
        if attempt == MAX_429_RETRIES:
            break
        try:
            wait = float(resp.headers.get("Retry-After", "10"))
        except ValueError:
            wait = 10.0
        await _sleep(min(max(wait, 1.0), MAX_RETRY_AFTER_S))
    log.info("settlement lookup throttled past %d retries: %s", MAX_429_RETRIES, url)
    return None


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
    resp = await _get(client, f"{KALSHI_BASE}/markets/{ticker}")
    if resp is None:
        return None
    if resp.status_code != 200:
        if resp.status_code != 404:
            log.info("kalshi settlement lookup %s -> HTTP %d", ticker, resp.status_code)
        return None
    market = resp.json().get("market") or {}
    if market.get("status") not in _KALSHI_FINAL:
        return None
    result = market.get("result")
    if result in ("yes", "no"):
        yes_value = Decimal("1") if result == "yes" else Decimal("0")
    elif result == "scalar":
        try:
            yes_value = Decimal(str(market.get("settlement_value_dollars")))
        except InvalidOperation:
            yes_value = Decimal("-1")
        if not Decimal("0") <= yes_value <= Decimal("1"):
            log.warning("kalshi %s scalar settlement unreadable: %r", ticker, market)
            return None
    else:
        # A void, or a result shape not seen yet. Left open to look at by hand
        # rather than guess what the payout was.
        if result:
            log.warning("kalshi %s finalized with unhandled result %r", ticker, result)
        return None
    return yes_value if side == "YES" else Decimal("1") - yes_value


async def fetch_polymarket_us_settlement(
    client: httpx.AsyncClient, outcome_id: str
) -> Decimal | None:
    parts = _split(outcome_id)
    if parts is None or parts[1] not in ("LONG", "SHORT"):
        return None
    slug, side = parts
    resp = await _get(client, f"{POLYMARKET_US_GATEWAY}/v1/markets/{slug}/settlement")
    if resp is None:
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
