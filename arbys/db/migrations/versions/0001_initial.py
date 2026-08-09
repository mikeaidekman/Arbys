"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-04

This migration is FROZEN: it spells out the schema as it stood at this
revision, in explicit DDL.

It previously called ``Base.metadata.create_all()``, which reads ``models.py``
as it exists *today* rather than as it existed here. That made the chain
unreplayable — 0001 would create ``paper_position`` already carrying the
``venue_id`` that 0002 then tried to add, so ``alembic upgrade head`` on an
empty database died with "duplicate column name: venue_id". Every later
migration double-applied the same way.

Do not reintroduce metadata-driven DDL in any migration. Each revision must
describe the change it makes, independent of the current models.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NUM = sa.Numeric(28, 12)
# quote/pnl ids are BIGINT on Postgres but SQLite only autoincrements INTEGER.
BIGPK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "venue",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("fee_model_ref", sa.String(128), nullable=True),
    )

    op.create_table(
        "market",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("venue_id", sa.String(64), sa.ForeignKey("venue.id"), nullable=False),
        sa.Column("venue_market_id", sa.String(256), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_source", sa.String(256), nullable=True),
        sa.Column("raw_metadata_json", sa.JSON(), nullable=True),
        sa.UniqueConstraint(
            "venue_id", "venue_market_id", name="uq_market_venue_native_id"
        ),
    )

    op.create_table(
        "outcome",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("market_id", sa.String(64), sa.ForeignKey("market.id"), nullable=False),
        sa.Column("label", sa.String(256), nullable=False),
        sa.Column("side", sa.String(16), nullable=True),
        sa.UniqueConstraint("market_id", "label", name="uq_outcome_market_label"),
    )

    op.create_table(
        "event_group",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "event_group_leg",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "event_group_id", sa.String(64), sa.ForeignKey("event_group.id"), nullable=False
        ),
        sa.Column("outcome_id", sa.String(64), sa.ForeignKey("outcome.id"), nullable=False),
        sa.Column("is_yes_side", sa.Boolean(), nullable=False),
        sa.Column("resolution_source", sa.String(256), nullable=True),
        sa.UniqueConstraint(
            "event_group_id", "outcome_id", name="uq_egleg_group_outcome"
        ),
    )

    op.create_table(
        "quote",
        sa.Column("id", BIGPK, primary_key=True, autoincrement=True),
        sa.Column("outcome_id", sa.String(64), sa.ForeignKey("outcome.id"), nullable=False),
        sa.Column("bid", NUM, nullable=False),
        sa.Column("ask", NUM, nullable=False),
        sa.Column("bid_size", NUM, nullable=True),
        sa.Column("ask_size", NUM, nullable=True),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_quote_ts", "quote", ["ts"])
    op.create_index("ix_quote_outcome_ts", "quote", ["outcome_id", "ts"])

    op.create_table(
        "arb_opportunity",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "event_group_id", sa.String(64), sa.ForeignKey("event_group.id"), nullable=False
        ),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("legs", sa.JSON(), nullable=False),
        sa.Column("total_stake", NUM, nullable=False),
        sa.Column("guaranteed_profit", NUM, nullable=False),
        sa.Column("guaranteed_profit_bps", NUM, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
    )
    op.create_index(
        "ix_arb_opp_event_group_detected", "arb_opportunity", ["event_group_id", "detected_at"]
    )

    op.create_table(
        "paper_account",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("base_currency", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "paper_balance",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "account_id", sa.String(64), sa.ForeignKey("paper_account.id"), nullable=False
        ),
        sa.Column("venue_id", sa.String(64), sa.ForeignKey("venue.id"), nullable=False),
        sa.Column("currency", sa.String(16), nullable=False),
        sa.Column("amount", NUM, nullable=False),
        sa.UniqueConstraint(
            "account_id", "venue_id", "currency", name="uq_paper_balance_acct_venue_ccy"
        ),
    )

    op.create_table(
        "paper_order",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "account_id", sa.String(64), sa.ForeignKey("paper_account.id"), nullable=False
        ),
        sa.Column("venue_id", sa.String(64), sa.ForeignKey("venue.id"), nullable=False),
        sa.Column("outcome_id", sa.String(64), sa.ForeignKey("outcome.id"), nullable=False),
        sa.Column("is_buy", sa.Boolean(), nullable=False),
        sa.Column("qty", NUM, nullable=False),
        sa.Column("limit_price", NUM, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "arb_opportunity_id",
            sa.String(64),
            sa.ForeignKey("arb_opportunity.id"),
            nullable=True,
        ),
        sa.Column("rejection_reason", sa.String(256), nullable=True),
    )
    op.create_index("ix_paper_order_account", "paper_order", ["account_id", "submitted_at"])

    op.create_table(
        "paper_fill",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.String(64), sa.ForeignKey("paper_order.id"), nullable=False),
        sa.Column("qty", NUM, nullable=False),
        sa.Column("price", NUM, nullable=False),
        sa.Column("fee", NUM, nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    # NOTE: no venue_id here — migration 0002 adds it. Keeping this frozen is
    # what makes the chain replayable.
    op.create_table(
        "paper_position",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "account_id", sa.String(64), sa.ForeignKey("paper_account.id"), nullable=False
        ),
        sa.Column("outcome_id", sa.String(64), sa.ForeignKey("outcome.id"), nullable=False),
        sa.Column("qty", NUM, nullable=False),
        sa.Column("avg_price", NUM, nullable=False),
        sa.Column("realized_pnl", NUM, nullable=False),
        sa.UniqueConstraint(
            "account_id", "outcome_id", name="uq_paper_pos_acct_outcome"
        ),
    )

    op.create_table(
        "paper_pnl_snapshot",
        sa.Column("id", BIGPK, primary_key=True, autoincrement=True),
        sa.Column(
            "account_id", sa.String(64), sa.ForeignKey("paper_account.id"), nullable=False
        ),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("cash", NUM, nullable=False),
        sa.Column("mtm_positions", NUM, nullable=False),
        sa.Column("total_equity", NUM, nullable=False),
    )
    op.create_index("ix_paper_pnl_snapshot_ts", "paper_pnl_snapshot", ["ts"])


def downgrade() -> None:
    for table in (
        "paper_pnl_snapshot",
        "paper_position",
        "paper_fill",
        "paper_order",
        "paper_balance",
        "paper_account",
        "arb_opportunity",
        "quote",
        "event_group_leg",
        "event_group",
        "outcome",
        "market",
        "venue",
    ):
        op.drop_table(table)
