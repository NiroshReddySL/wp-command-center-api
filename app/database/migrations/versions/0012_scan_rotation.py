"""Track attempts separately from successes, so the refresh queue rotates.

The daily allowance is spent oldest-first, which is a fair rotation only if
"oldest" reflects when a component was last *tried*. It previously reflected
when one was last successfully fetched, and a failed attempt wrote nothing at
all — so a component that consistently failed stayed permanently at the front
of the queue, consuming budget on every run and starving everything behind it.

`last_attempt_at` records every attempt. `fetched_at` becomes nullable so a
row can exist for a component that has been tried and never answered: its
`vulnerabilities` is then a placeholder, not the real "none known" that an
empty list from a successful lookup represents.

Revision ID: 0012_scan_rotation
Revises: 0011_vulnerability_cache
"""
from alembic import op

revision: str = "0012_scan_rotation"
down_revision: str | None = "0011_vulnerability_cache"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE vulnerability_cache ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ"
    )
    op.execute("ALTER TABLE vulnerability_cache ALTER COLUMN fetched_at DROP NOT NULL")
    op.execute("ALTER TABLE vulnerability_cache ALTER COLUMN fetched_at DROP DEFAULT")
    # Existing rows only exist because a fetch succeeded, so the last attempt
    # was that fetch.
    op.execute(
        "UPDATE vulnerability_cache SET last_attempt_at = fetched_at "
        "WHERE last_attempt_at IS NULL"
    )
    # Ordering the refresh queue is the single hot query here.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_vulnerability_cache_attempt "
        "ON vulnerability_cache(last_attempt_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_vulnerability_cache_attempt")
    op.execute("ALTER TABLE vulnerability_cache DROP COLUMN IF EXISTS last_attempt_at")
    op.execute(
        "UPDATE vulnerability_cache SET fetched_at = NOW() WHERE fetched_at IS NULL"
    )
    op.execute("ALTER TABLE vulnerability_cache ALTER COLUMN fetched_at SET NOT NULL")
