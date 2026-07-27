"""Flow categories — marketer-defined ordered page-pattern journeys,
classified via GA4's Funnel Reports API.

Revision ID: 0005_flow_categories
Revises: 0004_watched_urls
"""
from alembic import op

revision: str = "0005_flow_categories"
down_revision: str | None = "0004_watched_urls"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS flow_categories (
            id           VARCHAR(36) PRIMARY KEY,
            site_id      VARCHAR(36) NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
            name         VARCHAR(100) NOT NULL,
            description  TEXT,
            color        VARCHAR(20),
            is_active    BOOLEAN NOT NULL DEFAULT TRUE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_flow_categories_site_name UNIQUE (site_id, name)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS flow_category_steps (
            id                    VARCHAR(36) PRIMARY KEY,
            flow_category_id      VARCHAR(36) NOT NULL REFERENCES flow_categories(id) ON DELETE CASCADE,
            step_index            INTEGER NOT NULL,
            label                 VARCHAR(255) NOT NULL,
            match_type            VARCHAR(20) NOT NULL DEFAULT 'contains',
            pattern               VARCHAR(512) NOT NULL,
            is_directly_followed  BOOLEAN NOT NULL DEFAULT FALSE,
            within_seconds        INTEGER,
            CONSTRAINT uq_flow_category_steps_order UNIQUE (flow_category_id, step_index)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS flow_category_snapshots (
            id                   VARCHAR(36) PRIMARY KEY,
            flow_category_id     VARCHAR(36) NOT NULL REFERENCES flow_categories(id) ON DELETE CASCADE,
            site_id              VARCHAR(36) NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
            range_start          VARCHAR(10) NOT NULL,
            range_end            VARCHAR(10) NOT NULL,
            step_results         JSONB NOT NULL DEFAULT '[]',
            total_entered        INTEGER NOT NULL DEFAULT 0,
            total_completed      INTEGER NOT NULL DEFAULT 0,
            conversion_rate      DOUBLE PRECISION NOT NULL DEFAULT 0,
            breakdown_dimension  VARCHAR(100),
            breakdown            JSONB NOT NULL DEFAULT '[]',
            computed_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_flow_categories_site_id ON flow_categories(site_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_flow_category_steps_category_id ON flow_category_steps(flow_category_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_flow_category_snapshots_category_range "
        "ON flow_category_snapshots(flow_category_id, range_start, range_end)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_flow_category_snapshots_site_id ON flow_category_snapshots(site_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS flow_category_snapshots")
    op.execute("DROP TABLE IF EXISTS flow_category_steps")
    op.execute("DROP TABLE IF EXISTS flow_categories")
