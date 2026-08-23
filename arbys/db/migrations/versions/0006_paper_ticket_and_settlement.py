"""add paper_ticket and paper_settlement

Revision ID: 0006_paper_ticket_and_settlement
Revises: 0005_polymarket_us_venue
Create Date: 2026-08-23

Two gaps this closes.

`paper_order` had no ticket identity: `arb_opportunity_id` existed but no
caller ever set it, and it could not be used — opportunities are persisted
deduped by fingerprint while execution re-detects and mints a fresh uuid, so
the executed object's id is routinely absent from the DB.

Settlement wrote no event row at all, so a settled winner looked exactly like
a position sold out at market and no ticket could be scored.

`paper_ticket.event_group_id` is intentionally not a foreign key: discovery
deletes groups, and trade history must outlive them.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_paper_ticket_and_settlement"
down_revision: str | None = "0005_polymarket_us_venue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NUM = sa.Numeric(28, 12)


def upgrade() -> None:
    op.create_table(
        "paper_ticket",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("event_group_id", sa.String(64), nullable=False),
        sa.Column("title_snapshot", sa.String(512), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("rejection_reason", sa.String(256), nullable=True),
        sa.Column("total_stake", NUM, nullable=True),
        sa.Column("expected_profit", NUM, nullable=True),
        sa.Column("expected_edge_bps", NUM, nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["account_id"], ["paper_account.id"],
                                name="fk_paper_ticket_account_id_paper_account"),
    )
    op.create_index(
        "ix_paper_ticket_account_ts", "paper_ticket", ["account_id", "submitted_at"]
    )

    op.create_table(
        "paper_settlement",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("outcome_id", sa.String(64), nullable=False),
        sa.Column("resolved_value", NUM, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("source", sa.String(16), nullable=False, server_default="heuristic"),
        sa.ForeignKeyConstraint(["outcome_id"], ["outcome.id"],
                                name="fk_paper_settlement_outcome_id_outcome"),
    )
    op.create_index(
        "ix_paper_settlement_outcome_ts", "paper_settlement", ["outcome_id", "ts"]
    )

    with op.batch_alter_table("paper_order") as batch:
        batch.add_column(sa.Column("ticket_id", sa.String(64), nullable=True))
        batch.create_foreign_key(
            "fk_paper_order_ticket_id_paper_ticket", "paper_ticket", ["ticket_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("paper_order") as batch:
        batch.drop_constraint("fk_paper_order_ticket_id_paper_ticket", type_="foreignkey")
        batch.drop_column("ticket_id")
    op.drop_index("ix_paper_settlement_outcome_ts", table_name="paper_settlement")
    op.drop_table("paper_settlement")
    op.drop_index("ix_paper_ticket_account_ts", table_name="paper_ticket")
    op.drop_table("paper_ticket")
