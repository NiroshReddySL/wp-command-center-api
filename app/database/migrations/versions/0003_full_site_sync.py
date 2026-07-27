"""Incremental full-site sync — checkpoint columns + page support.

Adds per-site incremental-sync checkpoints (posts/pages "synced through"
watermark + last full reconciliation time) and lets content_posts hold
WordPress Pages alongside Posts (content_type) with their own modified-date
watermark, so agents can skip re-analyzing unchanged content.

Revision ID: 0003_full_site_sync
Revises: 0002_app_settings
"""
from alembic import op

revision: str = "0003_full_site_sync"
down_revision: str | None = "0002_app_settings"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE sites ADD COLUMN IF NOT EXISTS posts_synced_through TIMESTAMPTZ")
    op.execute("ALTER TABLE sites ADD COLUMN IF NOT EXISTS pages_synced_through TIMESTAMPTZ")
    op.execute("ALTER TABLE sites ADD COLUMN IF NOT EXISTS last_full_reconciled_at TIMESTAMPTZ")
    op.execute("ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS content_type VARCHAR(10) NOT NULL DEFAULT 'post'")
    op.execute("ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS wp_modified_at TIMESTAMPTZ")
    op.execute("CREATE INDEX IF NOT EXISTS idx_content_posts_type ON content_posts(site_id, content_type)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_content_posts_type")
    op.execute("ALTER TABLE content_posts DROP COLUMN IF EXISTS wp_modified_at")
    op.execute("ALTER TABLE content_posts DROP COLUMN IF EXISTS content_type")
    op.execute("ALTER TABLE sites DROP COLUMN IF EXISTS last_full_reconciled_at")
    op.execute("ALTER TABLE sites DROP COLUMN IF EXISTS pages_synced_through")
    op.execute("ALTER TABLE sites DROP COLUMN IF EXISTS posts_synced_through")
