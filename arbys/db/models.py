"""SQLAlchemy ORM models.

Design principles:
* Every table has a string `id` primary key (uuid4 as string) so the shared
  domain types can reference them without coupling to a specific DB dialect.
* `quote` is append-only time series; index tuned for "latest quote per
  outcome" lookups.
* Paper-trading tables live alongside the venue-data tables so the same
  Postgres instance serves both scanning and simulation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


NUM = Numeric(28, 12)  # generous precision for prices, qtys, PnL


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Reference / market-data tables
# ---------------------------------------------------------------------------

class Venue(Base):
    __tablename__ = "venue"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # exchange|sportsbook
    fee_model_ref: Mapped[str | None] = mapped_column(String(128))

    markets: Mapped[list[Market]] = relationship(back_populates="venue")


class Market(Base):
    __tablename__ = "market"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    venue_id: Mapped[str] = mapped_column(ForeignKey("venue.id"), nullable=False)
    venue_market_id: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # binary|categorical|scalar
    close_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_source: Mapped[str | None] = mapped_column(String(256))
    raw_metadata: Mapped[dict | None] = mapped_column("raw_metadata_json", JSON)

    venue: Mapped[Venue] = relationship(back_populates="markets")
    outcomes: Mapped[list[Outcome]] = relationship(back_populates="market")

    __table_args__ = (
        UniqueConstraint("venue_id", "venue_market_id", name="uq_market_venue_native_id"),
    )


class Outcome(Base):
    __tablename__ = "outcome"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    market_id: Mapped[str] = mapped_column(ForeignKey("market.id"), nullable=False)
    label: Mapped[str] = mapped_column(String(256), nullable=False)
    side: Mapped[str | None] = mapped_column(String(16))  # YES|NO|null for categorical

    market: Mapped[Market] = relationship(back_populates="outcomes")

    __table_args__ = (
        UniqueConstraint("market_id", "label", name="uq_outcome_market_label"),
    )


class EventGroup(Base):
    """Ties same-real-world-event outcomes across venues (curated allowlist)."""

    __tablename__ = "event_group"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Scheduled start of the underlying real-world event, UTC. Nullable:
    # hand-registered groups and venues that report no time have none.
    start_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # "discovery" | "manual". Only discovery-sourced groups are retired when a
    # pass stops finding them; hand-registered ones persist.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")

    legs: Mapped[list[EventGroupLeg]] = relationship(back_populates="event_group")


class EventGroupLeg(Base):
    __tablename__ = "event_group_leg"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    event_group_id: Mapped[str] = mapped_column(ForeignKey("event_group.id"), nullable=False)
    outcome_id: Mapped[str] = mapped_column(ForeignKey("outcome.id"), nullable=False)
    is_yes_side: Mapped[bool] = mapped_column(Boolean, nullable=False)
    resolution_source: Mapped[str | None] = mapped_column(String(256))

    event_group: Mapped[EventGroup] = relationship(back_populates="legs")

    __table_args__ = (
        UniqueConstraint("event_group_id", "outcome_id", name="uq_egleg_group_outcome"),
    )


class Quote(Base):
    """Append-only top-of-book quote time series."""

    __tablename__ = "quote"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    outcome_id: Mapped[str] = mapped_column(ForeignKey("outcome.id"), nullable=False)
    bid: Mapped[Decimal] = mapped_column(NUM, nullable=False)
    ask: Mapped[Decimal] = mapped_column(NUM, nullable=False)
    bid_size: Mapped[Decimal | None] = mapped_column(NUM)
    ask_size: Mapped[Decimal | None] = mapped_column(NUM)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    __table_args__ = (
        Index("ix_quote_outcome_ts", "outcome_id", "ts"),
    )


# ---------------------------------------------------------------------------
# Arb opportunities
# ---------------------------------------------------------------------------

class ArbOpportunity(Base):
    """One published opportunity, kept as a tape.

    `event_group_id` is deliberately **not** a ForeignKey, for the same reason
    `PaperTicket.event_group_id` is not: discovery retires a group on nearly
    every pass, and this table is the durable record of what the engine
    published — the only place suppressed auto-trade attempts remain countable.
    Cascading the delete would erase the history of exactly the games that
    finished, and keeping the constraint instead makes retirement fail, which is
    what it did: on Postgres `delete_event_group` raised on any group that had
    ever published, so nothing was ever retired. SQLite never enforced the
    constraint, so dev saw neither outcome.
    """

    __tablename__ = "arb_opportunity"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    event_group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    legs: Mapped[list] = mapped_column(JSON, nullable=False)  # serialized ArbLeg list
    total_stake: Mapped[Decimal] = mapped_column(NUM, nullable=False)
    guaranteed_profit: Mapped[Decimal] = mapped_column(NUM, nullable=False)
    guaranteed_profit_bps: Mapped[Decimal] = mapped_column(NUM, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")

    __table_args__ = (
        Index("ix_arb_opp_event_group_detected", "event_group_id", "detected_at"),
    )


# ---------------------------------------------------------------------------
# Paper trading
# ---------------------------------------------------------------------------

class PaperAccount(Base):
    __tablename__ = "paper_account"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    base_currency: Mapped[str] = mapped_column(String(16), nullable=False, default="USD")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PaperBalance(Base):
    __tablename__ = "paper_balance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("paper_account.id"), nullable=False)
    venue_id: Mapped[str] = mapped_column(ForeignKey("venue.id"), nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False, default="USD")
    amount: Mapped[Decimal] = mapped_column(NUM, nullable=False, default=Decimal("0"))

    __table_args__ = (
        UniqueConstraint(
            "account_id", "venue_id", "currency", name="uq_paper_balance_acct_venue_ccy"
        ),
    )


class PaperOrder(Base):
    __tablename__ = "paper_order"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("paper_account.id"), nullable=False)
    venue_id: Mapped[str] = mapped_column(ForeignKey("venue.id"), nullable=False)
    outcome_id: Mapped[str] = mapped_column(ForeignKey("outcome.id"), nullable=False)
    is_buy: Mapped[bool] = mapped_column(Boolean, nullable=False)
    qty: Mapped[Decimal] = mapped_column(NUM, nullable=False)
    limit_price: Mapped[Decimal] = mapped_column(NUM, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    arb_opportunity_id: Mapped[str | None] = mapped_column(ForeignKey("arb_opportunity.id"))
    ticket_id: Mapped[str | None] = mapped_column(ForeignKey("paper_ticket.id"))
    rejection_reason: Mapped[str | None] = mapped_column(String(256))

    __table_args__ = (
        Index("ix_paper_order_account", "account_id", "submitted_at"),
    )


class PaperTicket(Base):
    """One submitted arb ticket: filled, rejected, or missed.

    `event_group_id` is deliberately **not** a ForeignKey. Discovery retires
    groups when they stop matching and `delete_event_group` takes the legs with
    it, so a live join would blank the name of every finished game — exactly
    the rows worth auditing. `title_snapshot` is frozen at submit time for the
    same reason and is the only naming the UI renders. `starts_at` is snapshotted
    for the third time on the same reasoning: it is when the underlying game
    begins, and so roughly when the ticket pays out, and a live join would fail
    precisely when it matters -- discovery retires a *finished* game while its
    ticket is still awaiting settlement, which is exactly the row a "what
    settles when" view needs to place.

    The three economic columns are nullable because a `missed` ticket has no
    economics: a manual click passes only an event group and outcome ids, so if
    the re-detect comes up empty there is no stake or expected profit to write.
    Zero would read as a free ticket that made nothing.
    """

    __tablename__ = "paper_ticket"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("paper_account.id"), nullable=False)
    event_group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title_snapshot: Mapped[str] = mapped_column(String(512), nullable=False)
    # Nullable: rows written before this column existed have none, and a group
    # that reports no start time (a Kalshi ticker without HHMM, say) legitimately
    # has none either. Null means unknown, never "settles now".
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(String(256))
    total_stake: Mapped[Decimal | None] = mapped_column(NUM)
    expected_profit: Mapped[Decimal | None] = mapped_column(NUM)
    expected_edge_bps: Mapped[Decimal | None] = mapped_column(NUM)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_paper_ticket_account_ts", "account_id", "submitted_at"),
    )


class PaperSettlement(Base):
    """A resolution event for one outcome.

    Settlement previously left no trace: `settle_outcome_async` zeroed the
    position and credited cash, making a settled winner indistinguishable from
    a position sold out at market. Without this row a ticket cannot be scored.
    """

    __tablename__ = "paper_settlement"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    outcome_id: Mapped[str] = mapped_column(ForeignKey("outcome.id"), nullable=False)
    resolved_value: Mapped[Decimal] = mapped_column(NUM, nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="heuristic")

    __table_args__ = (
        Index("ix_paper_settlement_outcome_ts", "outcome_id", "ts"),
    )


class PaperFill(Base):
    __tablename__ = "paper_fill"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("paper_order.id"), nullable=False)
    qty: Mapped[Decimal] = mapped_column(NUM, nullable=False)
    price: Mapped[Decimal] = mapped_column(NUM, nullable=False)
    fee: Mapped[Decimal] = mapped_column(NUM, nullable=False, default=Decimal("0"))
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PaperPosition(Base):
    __tablename__ = "paper_position"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("paper_account.id"), nullable=False)
    venue_id: Mapped[str] = mapped_column(ForeignKey("venue.id"), nullable=False)
    outcome_id: Mapped[str] = mapped_column(ForeignKey("outcome.id"), nullable=False)
    qty: Mapped[Decimal] = mapped_column(NUM, nullable=False, default=Decimal("0"))
    avg_price: Mapped[Decimal] = mapped_column(NUM, nullable=False, default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(NUM, nullable=False, default=Decimal("0"))

    # venue_id is part of the key: outcome_id is venue-native, but one broker
    # exists per venue and each must hydrate only its own rows on restart.
    __table_args__ = (
        UniqueConstraint(
            "account_id", "venue_id", "outcome_id", name="uq_paper_pos_acct_venue_outcome"
        ),
    )


class PaperPnlSnapshot(Base):
    __tablename__ = "paper_pnl_snapshot"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    account_id: Mapped[str] = mapped_column(ForeignKey("paper_account.id"), nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    cash: Mapped[Decimal] = mapped_column(NUM, nullable=False)
    mtm_positions: Mapped[Decimal] = mapped_column(NUM, nullable=False)
    total_equity: Mapped[Decimal] = mapped_column(NUM, nullable=False)
