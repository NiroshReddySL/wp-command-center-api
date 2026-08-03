from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# Idempotent DDL applied on top of Base.metadata — legacy column adds for
# pre-existing databases plus the query-path indexes. Shared by init_db (dev
# convenience) and the Alembic baseline migration (the real deploy path).
EXTRA_DDL: list[str] = [
            "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS word_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS reading_time_minutes INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS score_breakdown JSONB NOT NULL DEFAULT '{}'",
            "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS ai_recommendation TEXT",
            "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS ai_rec_hash VARCHAR(64)",
            "ALTER TABLE sites ADD COLUMN IF NOT EXISTS site_context JSONB NOT NULL DEFAULT '{}'",
            "ALTER TABLE sites ADD COLUMN IF NOT EXISTS webhook_secret TEXT",
            "ALTER TABLE sites ADD COLUMN IF NOT EXISTS site_context_analyzed_at TIMESTAMPTZ",
            "ALTER TABLE sites ADD COLUMN IF NOT EXISTS posts_synced_through TIMESTAMPTZ",
            "ALTER TABLE sites ADD COLUMN IF NOT EXISTS pages_synced_through TIMESTAMPTZ",
            "ALTER TABLE sites ADD COLUMN IF NOT EXISTS last_full_reconciled_at TIMESTAMPTZ",
            "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS content_type VARCHAR(10) NOT NULL DEFAULT 'post'",
            "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS wp_modified_at TIMESTAMPTZ",
            "ALTER TABLE content_posts ADD COLUMN IF NOT EXISTS ai_guidance JSONB",
            # Agent job tables (idempotent — safe on every boot)
            """CREATE TABLE IF NOT EXISTS agent_jobs (
                id              VARCHAR(36) PRIMARY KEY,
                site_id         VARCHAR(36) NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
                status          VARCHAR(20) NOT NULL DEFAULT 'pending',
                stop_requested  BOOLEAN NOT NULL DEFAULT FALSE,
                error_message   TEXT,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                started_at      TIMESTAMPTZ,
                completed_at    TIMESTAMPTZ
            )""",
            """CREATE TABLE IF NOT EXISTS agent_job_steps (
                id            VARCHAR(36) PRIMARY KEY,
                job_id        VARCHAR(36) NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
                step_index    INTEGER NOT NULL,
                agent_name    VARCHAR(100) NOT NULL,
                label         VARCHAR(255) NOT NULL,
                category      VARCHAR(50) NOT NULL,
                status        VARCHAR(20) NOT NULL DEFAULT 'pending',
                alerts_count  INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                started_at    TIMESTAMPTZ,
                completed_at  TIMESTAMPTZ
            )""",
            "CREATE INDEX IF NOT EXISTS idx_agent_jobs_site_id ON agent_jobs(site_id)",
            "CREATE INDEX IF NOT EXISTS idx_agent_jobs_status ON agent_jobs(status)",
            "CREATE INDEX IF NOT EXISTS idx_agent_job_steps_job_id ON agent_job_steps(job_id)",
            # Query-path indexes — every hot WHERE/ORDER BY in the API routers
            "CREATE INDEX IF NOT EXISTS idx_alerts_site_id ON alerts(site_id)",
            "CREATE INDEX IF NOT EXISTS idx_alerts_agent_status_created ON alerts(agent, status, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_alerts_type ON alerts(type)",
            "CREATE INDEX IF NOT EXISTS idx_content_posts_site_id ON content_posts(site_id)",
            "CREATE INDEX IF NOT EXISTS idx_content_posts_site_wp_post ON content_posts(site_id, wp_post_id)",
            "CREATE INDEX IF NOT EXISTS idx_content_posts_health ON content_posts(health_score)",
            "CREATE INDEX IF NOT EXISTS idx_content_posts_traffic ON content_posts(traffic_30d)",
            "CREATE INDEX IF NOT EXISTS idx_content_posts_type ON content_posts(site_id, content_type)",
            "CREATE INDEX IF NOT EXISTS idx_review_items_site_id ON review_items(site_id)",
            "CREATE INDEX IF NOT EXISTS idx_review_items_agent_status ON review_items(agent, status)",
            "CREATE INDEX IF NOT EXISTS idx_performance_snapshots_site_at ON performance_snapshots(site_id, snapshot_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_variants_content_post_id ON variants(content_post_id)",
            "CREATE INDEX IF NOT EXISTS idx_plugin_audits_site_id ON plugin_audits(site_id)",
            "CREATE INDEX IF NOT EXISTS idx_traffic_predictions_site_horizon ON traffic_predictions(site_id, horizon_days)",
            """CREATE TABLE IF NOT EXISTS watched_urls (
                id                VARCHAR(36) PRIMARY KEY,
                site_id           VARCHAR(36) NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
                url               VARCHAR(512) NOT NULL,
                path              VARCHAR(512) NOT NULL,
                title             VARCHAR(512),
                title_resolved_at TIMESTAMPTZ,
                source            VARCHAR(10) NOT NULL DEFAULT 'manual',
                created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_watched_urls_site_path UNIQUE (site_id, path)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_watched_urls_site_id ON watched_urls(site_id)",
            # Flow categories — marketer-defined ordered page-pattern journeys,
            # classified via GA4's Funnel Reports API.
            """CREATE TABLE IF NOT EXISTS flow_categories (
                id           VARCHAR(36) PRIMARY KEY,
                site_id      VARCHAR(36) NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
                name         VARCHAR(100) NOT NULL,
                description  TEXT,
                color        VARCHAR(20),
                is_active    BOOLEAN NOT NULL DEFAULT TRUE,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_flow_categories_site_name UNIQUE (site_id, name)
            )""",
            """CREATE TABLE IF NOT EXISTS flow_category_steps (
                id                    VARCHAR(36) PRIMARY KEY,
                flow_category_id      VARCHAR(36) NOT NULL REFERENCES flow_categories(id) ON DELETE CASCADE,
                step_index            INTEGER NOT NULL,
                label                 VARCHAR(255) NOT NULL,
                match_type            VARCHAR(20) NOT NULL DEFAULT 'contains',
                pattern               VARCHAR(512) NOT NULL,
                is_directly_followed  BOOLEAN NOT NULL DEFAULT FALSE,
                within_seconds        INTEGER,
                CONSTRAINT uq_flow_category_steps_order UNIQUE (flow_category_id, step_index)
            )""",
            """CREATE TABLE IF NOT EXISTS flow_category_snapshots (
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
            )""",
            "CREATE INDEX IF NOT EXISTS idx_flow_categories_site_id ON flow_categories(site_id)",
            "CREATE INDEX IF NOT EXISTS idx_flow_category_steps_category_id ON flow_category_steps(flow_category_id)",
            "CREATE INDEX IF NOT EXISTS idx_flow_category_snapshots_category_range ON flow_category_snapshots(flow_category_id, range_start, range_end)",
            "CREATE INDEX IF NOT EXISTS idx_flow_category_snapshots_site_id ON flow_category_snapshots(site_id)",
            # Goal steps — mark a step as the conversion event so the
            # dashboard can report a real "leads" count.
            "ALTER TABLE flow_category_steps ADD COLUMN IF NOT EXISTS is_goal BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE flow_category_snapshots ADD COLUMN IF NOT EXISTS goal_step_index INTEGER",
            "ALTER TABLE flow_category_snapshots ADD COLUMN IF NOT EXISTS leads INTEGER",
            "ALTER TABLE flow_category_snapshots ADD COLUMN IF NOT EXISTS lead_rate DOUBLE PRECISION",
            # One row per (site_id, date) — repeated "Flush & Re-run" clicks
            # used to insert duplicate dates since there was no upsert.
            # Dedupe first (keep real GA4 over estimated, then most recent),
            # then guard the constraint add since Postgres has no ADD
            # CONSTRAINT IF NOT EXISTS.
            """
            DELETE FROM traffic_snapshots
            WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY site_id, date
                               ORDER BY (source = 'ga4') DESC, snapshot_at DESC, id DESC
                           ) AS rn
                    FROM traffic_snapshots
                ) ranked
                WHERE rn > 1
            )
            """,
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'uq_traffic_snapshots_site_date'
                ) THEN
                    ALTER TABLE traffic_snapshots
                        ADD CONSTRAINT uq_traffic_snapshots_site_date UNIQUE (site_id, date);
                END IF;
            END $$;
            """,
            "DROP INDEX IF EXISTS idx_traffic_snapshots_site_date",
            # Component audits: plugins AND themes, from WordPress or entered
            # by hand when a site has no Application Password.
            "ALTER TABLE plugin_audits ADD COLUMN IF NOT EXISTS component_type VARCHAR(10) NOT NULL DEFAULT 'plugin'",
            "ALTER TABLE plugin_audits ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'wordpress'",
            "ALTER TABLE plugin_audits ADD COLUMN IF NOT EXISTS is_active BOOLEAN",
            """
            DELETE FROM plugin_audits
            WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY site_id, component_type, plugin_slug
                               ORDER BY audited_at DESC, id DESC
                           ) AS rn
                    FROM plugin_audits
                ) ranked
                WHERE rn > 1
            )
            """,
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'uq_plugin_audits_site_type_slug'
                ) THEN
                    ALTER TABLE plugin_audits
                        ADD CONSTRAINT uq_plugin_audits_site_type_slug
                        UNIQUE (site_id, component_type, plugin_slug);
                END IF;
            END $$;
            """,
            # Where latest_version came from — "up to date" is only meaningful
            # when something actually resolved it.
            "ALTER TABLE plugin_audits ADD COLUMN IF NOT EXISTS latest_source VARCHAR(10) NOT NULL DEFAULT 'unknown'",
]


async def init_db() -> None:
    from sqlalchemy import text

    from app.database import models  # noqa: F401 — ensure models are imported

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for stmt in EXTRA_DDL:
            await conn.execute(text(stmt))
