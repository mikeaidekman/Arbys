"""Kalshi authenticated WebSocket adapter.

Real-time market data via ``wss://external-api-ws.kalshi.com/trade-api/ws/v2``.

Auth: WS handshake carries three headers signed with an RSA private key using
RSA-PSS-SHA256:

- ``KALSHI-ACCESS-KEY``: the API key ID (a UUID).
- ``KALSHI-ACCESS-TIMESTAMP``: current time in milliseconds.
- ``KALSHI-ACCESS-SIGNATURE``: base64(sign(private_key, f"{ts}GET/trade-api/ws/v2")).

Subscription: ``orderbook_delta`` channel with ``market_tickers`` list. Kalshi
sends one ``orderbook_snapshot`` up front and then incremental
``orderbook_delta`` messages.

Book semantics (Kalshi binary contracts):
- ``yes_dollars_fp`` = **bids** to buy YES at various price levels.
- ``no_dollars_fp`` = bids to buy NO.
- Best YES bid  = max price in yes_dollars_fp.
- Best YES ask  = 1 - max price in no_dollars_fp (selling YES = buying NO).
- Best NO bid   = max price in no_dollars_fp.
- Best NO ask   = 1 - max price in yes_dollars_fp.

For each subscribed ticker we track two per-side price ladders and emit
``Quote`` objects for both ``{ticker}:YES`` and ``{ticker}:NO`` on every book
change.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ..shared.types import Outcome, Quote
from .base import MarketDataAdapter

WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
_SIGNING_PATH = "/trade-api/ws/v2"

log = logging.getLogger(__name__)

ONE = Decimal("1")


def _sign_pss(private_key: rsa.RSAPrivateKey, message: str) -> str:
    sig = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("ascii")


def _auth_headers(
    key_id: str, private_key: rsa.RSAPrivateKey, *, path: str = _SIGNING_PATH
) -> dict[str, str]:
    ts = str(int(time.time() * 1000))
    signature = _sign_pss(private_key, ts + "GET" + path)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": signature,
    }


def load_kalshi_private_key(pem_path: str) -> rsa.RSAPrivateKey:
    """Load the RSA private key from a PEM file on disk."""
    data = Path(pem_path).expanduser().read_bytes()
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError(f"expected RSA private key at {pem_path}, got {type(key).__name__}")
    return key


def kalshi_ws_creds_from_env() -> tuple[str, rsa.RSAPrivateKey] | None:
    """Return ``(key_id, private_key)`` if both env vars are present, else None."""
    key_id = os.environ.get("KALSHI_API_KEY_ID")
    pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not pem_path:
        return None
    try:
        return key_id, load_kalshi_private_key(pem_path)
    except Exception as exc:
        log.error("failed to load Kalshi private key from %s: %s", pem_path, exc)
        return None


class _MarketBook:
    """Two-sided price ladder for one Kalshi market ticker."""

    __slots__ = ("no", "yes")

    def __init__(self) -> None:
        self.yes: dict[str, Decimal] = {}
        self.no: dict[str, Decimal] = {}

    def apply_snapshot(self, msg: dict[str, Any]) -> None:
        self.yes = _levels_to_dict(msg.get("yes_dollars_fp"))
        self.no = _levels_to_dict(msg.get("no_dollars_fp"))

    def apply_delta(self, side: str, price: str, delta_fp: str) -> None:
        book = self.yes if side == "yes" else self.no
        try:
            change = Decimal(delta_fp)
        except (ValueError, TypeError):
            return
        current = book.get(price, Decimal(0))
        new = current + change
        if new <= 0:
            book.pop(price, None)
        else:
            book[price] = new

    def best_yes_bid(self) -> Decimal | None:
        return _max_price(self.yes)

    def best_no_bid(self) -> Decimal | None:
        return _max_price(self.no)

    def best_yes(self) -> tuple[Decimal, Decimal] | None:
        """Best YES bid with the size resting there."""
        return _max_price_and_size(self.yes)

    def best_no(self) -> tuple[Decimal, Decimal] | None:
        return _max_price_and_size(self.no)


def _levels_to_dict(levels: object) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    if not isinstance(levels, list):
        return out
    for lvl in levels:
        try:
            price, size = lvl[0], lvl[1]
        except (IndexError, TypeError):
            continue
        try:
            out[str(price)] = Decimal(str(size))
        except (ValueError, TypeError):
            continue
    return out


def _max_price_and_size(book: dict[str, Decimal]) -> tuple[Decimal, Decimal] | None:
    """Highest price in the ladder and the size resting at it."""
    best: tuple[Decimal, Decimal] | None = None
    for p_str, size in book.items():
        try:
            p = Decimal(p_str)
        except (ArithmeticError, ValueError, TypeError):
            continue
        if best is None or p > best[0]:
            best = (p, size)
    return best


def _max_price(book: dict[str, Decimal]) -> Decimal | None:
    if not book:
        return None
    best: Decimal | None = None
    for p_str in book:
        try:
            p = Decimal(p_str)
        except (ValueError, TypeError):
            continue
        if best is None or p > best:
            best = p
    return best


class KalshiWebSocketAdapter(MarketDataAdapter):
    """Streams Kalshi quotes over an authenticated WebSocket.

    ``outcome_ids`` accepts the same ``{market_ticker}:YES``/``:NO`` format the
    REST adapter emits. On each book change we publish quotes for BOTH the YES
    and NO outcome for a subscribed market ticker.
    """

    venue_id = "kalshi"

    def __init__(
        self,
        *,
        outcome_ids: list[str],
        api_key_id: str,
        private_key: rsa.RSAPrivateKey,
        url: str = WS_URL,
        initial_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
    ) -> None:
        self._url = url
        self._key_id = api_key_id
        self._private_key = private_key
        self._initial_backoff_s = initial_backoff_s
        self._max_backoff_s = max_backoff_s
        # Deduplicate + strip the ":YES"/":NO" suffix — we subscribe per market ticker.
        self._market_tickers: list[str] = sorted({oid.split(":", 1)[0] for oid in outcome_ids})
        self._books: dict[str, _MarketBook] = {t: _MarketBook() for t in self._market_tickers}

    async def close(self) -> None:  # symmetry with REST adapter
        return None

    async def list_markets(self) -> list[Outcome]:
        # Discovery isn't the WS adapter's job; return the outcomes we are asked to stream.
        outcomes: list[Outcome] = []
        for ticker in self._market_tickers:
            for suffix in ("YES", "NO"):
                outcomes.append(
                    Outcome(id=f"{ticker}:{suffix}", venue_id=self.venue_id, market_id=ticker)
                )
        return outcomes

    async def stream_quotes(self) -> AsyncIterator[Quote]:
        if not self._market_tickers:
            log.info("KalshiWebSocketAdapter: no tickers to subscribe to")
            return
        backoff = self._initial_backoff_s
        while True:
            try:
                async for quote in self._connect_and_stream():
                    backoff = self._initial_backoff_s
                    yield quote
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("kalshi WS disconnect: %s; retry in %.1fs", exc, backoff)
            await asyncio.sleep(backoff * (0.5 + random.random()))
            backoff = min(backoff * 2, self._max_backoff_s)

    async def _connect_and_stream(self) -> AsyncIterator[Quote]:
        headers = _auth_headers(self._key_id, self._private_key)
        log.info(
            "kalshi WS connecting: %d ticker(s), first=%s",
            len(self._market_tickers),
            self._market_tickers[0],
        )
        async with websockets.connect(
            self._url,
            additional_headers=headers,
            max_size=2**22,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            sub_cmd = {
                "id": 1,
                "cmd": "subscribe",
                "params": {
                    "channels": ["orderbook_delta"],
                    "market_tickers": self._market_tickers,
                },
            }
            await ws.send(json.dumps(sub_cmd))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                async for q in self._handle_message(msg):
                    yield q

    async def _handle_message(self, msg: dict[str, Any]) -> AsyncIterator[Quote]:
        mtype = msg.get("type")
        body = msg.get("msg") or {}
        ticker = body.get("market_ticker")

        if mtype == "orderbook_snapshot" and ticker in self._books:
            self._books[ticker].apply_snapshot(body)
            for q in self._quotes_for(ticker):
                yield q
        elif mtype == "orderbook_delta" and ticker in self._books:
            side = body.get("side")
            price = body.get("price_dollars")
            delta = body.get("delta_fp")
            if isinstance(side, str) and isinstance(price, str) and isinstance(delta, str):
                self._books[ticker].apply_delta(side, price, delta)
                for q in self._quotes_for(ticker):
                    yield q
        elif mtype == "error":
            log.error("kalshi WS error: %s", msg)
        # Ignore other message types (subscribed ack, ping, etc.).

    def _quotes_for(self, ticker: str) -> list[Quote]:
        book = self._books[ticker]
        # Sizes travel with the price: buying YES matches the resting NO bid,
        # so the YES ask's depth is whatever sits on that NO level.
        yes_best = book.best_yes()  # highest YES bid + its size
        no_best = book.best_no()    # highest NO bid + its size
        zero = Decimal("0")

        def _quote(
            oid: str, bid: Decimal, ask: Decimal, bid_size: Decimal, ask_size: Decimal
        ) -> Quote | None:
            if bid > ask:
                bid = ask
            try:
                return Quote(
                    outcome_id=oid, bid=bid, ask=ask,
                    bid_size=bid_size, ask_size=ask_size,
                )
            except ValueError:
                return None

        yes_bid, yes_bid_size = yes_best if yes_best is not None else (zero, zero)
        no_bid, no_bid_size = no_best if no_best is not None else (zero, zero)
        yes_ask, yes_ask_size = (
            (ONE - no_bid, no_bid_size) if no_best is not None else (ONE, zero)
        )
        no_ask, no_ask_size = (
            (ONE - yes_bid, yes_bid_size) if yes_best is not None else (ONE, zero)
        )

        out: list[Quote] = []
        yes_q = _quote(f"{ticker}:YES", yes_bid, yes_ask, yes_bid_size, yes_ask_size)
        no_q = _quote(f"{ticker}:NO", no_bid, no_ask, no_bid_size, no_ask_size)
        if yes_q is not None:
            out.append(yes_q)
        if no_q is not None:
            out.append(no_q)
        return out


__all__ = [
    "KalshiWebSocketAdapter",
    "kalshi_ws_creds_from_env",
    "load_kalshi_private_key",
]
