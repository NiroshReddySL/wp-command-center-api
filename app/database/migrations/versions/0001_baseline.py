"""Baseline — full schema as of adopting Alembic.

Creates every table from the SQLAlchemy models plus the idempotent extras
(legacy column adds, query-path indexes) shared with `init_db`.

For a database that already has the schema (created by init_db on boot),
do NOT run upgrade — mark it as current instead:

    alembic stamp head

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-06

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.database.engine import Base, EXTRA_DDL
from app.database import models  # noqa: F401 — register all models on Base.metadata

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    for stmt in EXTRA_DDL:
        bind.execute(sa.text(stmt))


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
    bind.execute(sa.text("DROP TABLE IF EXISTS agent_job_steps CASCADE"))
    bind.execute(sa.text("DROP TABLE IF EXISTS agent_jobs CASCADE"))
