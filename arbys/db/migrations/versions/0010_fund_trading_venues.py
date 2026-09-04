"""Fund the two trading venues by $2,000 each and drop the DraftKings balance.

A data migration, not a schema change. The hosted paper account lives in Neon
Postgres, whose connection string exists only in Fly's secret store, and the
deploy's `release_command` (`alembic upgrade head`) is the only path into it.
Bootstrap seeds a balance only for a venue that has never been funded, so
raising `DEFAULT_STARTING_BALANCE` alone would do nothing to a live account.

Why: on 2026-09-03 both venues ran out of buying power and the auto-trader
placed nothing all day. The account started at $2,000 a venue and each venue
now gets $2,000 more, on top of whatever its fills have left -- "add", not
"set". `DEFAULT_STARTING_BALANCE` moves to $4,000 in the same change so a
future reset seeds the same level rather than quietly undoing this.

DraftKings: its paper broker is now built only when `ARBYS_ENABLE_DRAFTKINGS=1`,
the flag that has always gated its adapter. With the flag off nothing hydrates
or reads its balance row, and the $2,000 in it was never tradeable -- it sat
in the headline cash and equity of every account as capacity that did not
exist. Removing the row means re-enabling the flag later seeds the venue fresh
at the then-current default rather than resurrecting a 2026 figure.

On an empty database (a fresh deploy, the CI replay) both statements touch
zero rows and bootstrap seeds the new default afterwards. `downgrade()`
subtracts the deposit again; it does not restore the DraftKings row, because
there is no broker to hydrate it into and nothing that reads it.
"""

from __future__ import annotations

from alembic import op

revision: str = "0010_fund_trading_venues"
down_revision: str | None = "0009_paper_position_open_fees"
branch_labels = None
depends_on = None

_TRADING_VENUES = "('kalshi', 'polymarket_us')"
_DEPOSIT = "2000"


def upgrade() -> None:
    op.execute(
        f"UPDATE paper_balance SET amount = amount + {_DEPOSIT} "
        f"WHERE venue_id IN {_TRADING_VENUES}"
    )
    op.execute("DELETE FROM paper_balance WHERE venue_id = 'draftkings'")


def downgrade() -> None:
    op.execute(
        f"UPDATE paper_balance SET amount = amount - {_DEPOSIT} "
        f"WHERE venue_id IN {_TRADING_VENUES}"
    )
