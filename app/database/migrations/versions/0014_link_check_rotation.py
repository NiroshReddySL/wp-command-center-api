"""Remember which links were verified, so coverage rotates.

A run can only afford a few hundred HTTP checks. Without a record of what was
already checked, the checker took the same alphabetically-first slice on every
run — on the install this was found on, 500 of 2,097 links, all of them
internal, forever. Every external link on the site was unverified, and the
dashboard reported zero broken links because nothing had ever looked.

Revision ID: 0014_link_check_rotation
Revises: 0013_report_retention
"""
from alembic import op

revision: str = "0014_link_check_rotation"
down_revision: str | None = "0013_report_retention"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS link_checks (
            id          VARCHAR(36) PRIMARY KEY,
            site_id     VARCHAR(36) NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
            url_hash    VARCHAR(64) NOT NULL,
            url         TEXT NOT NULL,
            is_internal BOOLEAN NOT NULL DEFAULT FALSE,
            status      INTEGER NOT NULL DEFAULT 0,
            checked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )"""
    )
    # Hashed, not the URL itself: link targets routinely exceed the ~2,700-byte
    # limit for a btree index entry, and this index is what makes the upsert work.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_link_check_site_url "
        "ON link_checks (site_id, url_hash)"
    )
    # The planner's query: this site's links, least recently checked first.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_link_checks_rotation "
        "ON link_checks (site_id, checked_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS link_checks")
