"""Two-stage deletion for stored reports.

Only the newest few reports per site are worth keeping on screen, but a report
is a document someone may already have sent to a client — deleting one because
a count was exceeded is not a decision an automatic rule should make on its
own. So retention moves items to a trash with a 30-day window, and `locked`
exempts an item from retention entirely.

`locked` is deliberately not derivable from anything else. A lock whose
protection an automatic rule could override is not a lock.

Revision ID: 0013_report_retention
Revises: 0012_scan_rotation
"""
from alembic import op

revision: str = "0013_report_retention"
down_revision: str | None = "0012_scan_rotation"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE review_items ADD COLUMN IF NOT EXISTS trashed_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE review_items ADD COLUMN IF NOT EXISTS locked BOOLEAN "
        "NOT NULL DEFAULT FALSE"
    )
    # The list query is always "this site's reports, in this state, newest
    # first" — retention runs it on every generate.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_review_items_lifecycle "
        "ON review_items (action_type, site_id, trashed_at, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_review_items_lifecycle")
    op.execute("ALTER TABLE review_items DROP COLUMN IF EXISTS locked")
    op.execute("ALTER TABLE review_items DROP COLUMN IF EXISTS trashed_at")
