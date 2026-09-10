"""paper_transfer.id — BIGINT on Postgres, matching models.py.

`0012` created the column as a plain `sa.Integer()`. `models.PaperTransfer`
declares the house idiom for an autoincrement PK,
`BigInteger().with_variant(Integer(), "sqlite")` — the same
`BIGPK` that `0001_initial` defines and `0006` repeats — so the two agreed on
SQLite, where both collapse to INTEGER, and diverged on Postgres as BIGINT
against INTEGER.

**Nothing local could have caught this**, which is the point worth recording.
`tests/db/test_migrations_match_models.py` replays the chain and diffs it
against `create_all`, and it passed: on SQLite there is one integer type.
`tests/db/test_migrations_postgres.py` runs the identical comparison against a
real Neon branch and failed on the first push. The SQLite-only defect class
CLAUDE.md describes has a mirror image — a *Postgres*-only defect that dev
cannot see — and the Postgres workflow is the only thing standing in front of
it.

Corrected forward rather than by editing `0012`, because `0012` had already
been applied to production by the deploy that carried it. A revision is frozen
at the point in history where it ran; rewriting one leaves the database that
ran the original version permanently adrift from what the file claims.

No-op on SQLite: the variant already resolves to INTEGER there, so there is
nothing to reconcile, and SQLite has no `ALTER COLUMN TYPE` — reconciling it
would mean batch-rebuilding the table to reach the type it already has.

The table is empty everywhere this runs (it was created one deploy ago), so
the alter cannot fail on data. Postgres sequences are int8 regardless, so the
`nextval` default survives the widening untouched.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0013_paper_transfer_bigint_id"
down_revision: str | None = "0012_paper_transfer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.alter_column(
        "paper_transfer",
        "id",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.alter_column(
        "paper_transfer",
        "id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
    )
