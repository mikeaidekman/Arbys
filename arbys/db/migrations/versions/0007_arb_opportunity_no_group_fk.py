"""arb_opportunity.event_group_id stops being a foreign key.

Discovery retires a group on nearly every pass and `delete_event_group` removes
the row. `arb_opportunity` is the durable tape of what the engine published --
per CLAUDE.md it is where suppressed auto-trade attempts stay countable -- so it
has to outlive the group it describes. That is the same decision already taken
for `paper_ticket.event_group_id`, and for the same reason: cascading the delete
would erase the history of exactly the games that finished.

Keeping the constraint instead made retirement fail. On Postgres,
`delete_event_group` raised on any group that had ever published an
opportunity, `run_write` swallowed and counted it, and nothing was ever retired.
SQLite does not enforce foreign keys unless asked, so dev saw neither the
failure nor an orphan and the drift was invisible for the life of the project.

Dropped on both dialects deliberately. Leaving it on SQLite alone would just
invert the asymmetry now that dev enforces foreign keys: local writes would fail
where production succeeds.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007_arb_opportunity_no_group_fk"
down_revision: str | None = "0006_paper_ticket_and_settlement"
branch_labels = None
depends_on = None

# SQLite reflects the constraint without a name, and `drop_constraint` needs
# one. A naming convention supplies a deterministic one for the duration of the
# batch operation; Postgres uses its own real name, looked up from the catalog
# rather than assumed, since 0001 never named it explicitly.
NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
SQLITE_FK_NAME = "fk_arb_opportunity_event_group_id_event_group"

_FIND_PG_FK = sa.text(
    """
    SELECT tc.constraint_name
    FROM information_schema.table_constraints AS tc
    JOIN information_schema.key_column_usage AS kcu
      ON tc.constraint_name = kcu.constraint_name
     AND tc.table_schema = kcu.table_schema
    WHERE tc.table_name = 'arb_opportunity'
      AND tc.constraint_type = 'FOREIGN KEY'
      AND kcu.column_name = 'event_group_id'
    """
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for (name,) in bind.execute(_FIND_PG_FK).fetchall():
            op.drop_constraint(name, "arb_opportunity", type_="foreignkey")
    else:
        with op.batch_alter_table("arb_opportunity", naming_convention=NAMING) as batch:
            batch.drop_constraint(SQLITE_FK_NAME, type_="foreignkey")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_foreign_key(
            "arb_opportunity_event_group_id_fkey",
            "arb_opportunity",
            "event_group",
            ["event_group_id"],
            ["id"],
        )
    else:
        with op.batch_alter_table("arb_opportunity", naming_convention=NAMING) as batch:
            batch.create_foreign_key(
                SQLITE_FK_NAME, "event_group", ["event_group_id"], ["id"]
            )
