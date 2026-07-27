"""Watched URLs — live GA4 active-user tracking for user-curated pages.

Revision ID: 0004_watched_urls
Revises: 0003_full_site_sync
"""
from alembic import op

revision: str = "0004_watched_urls"
down_revision: str | None = "0003_full_site_sync"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS watched_urls (
            id                VARCHAR(36) PRIMARY KEY,
            site_id           VARCHAR(36) NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
            url               VARCHAR(512) NOT NULL,
            path              VARCHAR(512) NOT NULL,
            title             VARCHAR(512),
            title_resolved_at TIMESTAMPTZ,
            source            VARCHAR(10) NOT NULL DEFAULT 'manual',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_watched_urls_site_path UNIQUE (site_id, path)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_watched_urls_site_id ON watched_urls(site_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS watched_urls")
