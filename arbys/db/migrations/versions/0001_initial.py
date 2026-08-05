"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-04

This first migration uses SQLAlchemy metadata to create all tables. Subsequent
migrations should use explicit op.create_table / op.add_column calls generated
via `alembic revision --autogenerate`.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from arbys.db.models import Base

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
