import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.engine import Base
from app.security.crypto import EncryptedText


def new_uuid() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class User(TimestampMixin, Base):
    """Dashboard login account. Roles: admin (full control) | member (operate)."""
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="member", nullable=False)


class Site(TimestampMixin, Base):
    __tablename__ = "sites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    api_key: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    # Shared secret for verifying inbound webhooks from the WP plugin
    webhook_secret: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    health_score: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    site_context: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    site_context_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Incremental content sync checkpoints — the newest WP `modified_gmt` we
    # have fully accounted for for each collection. A sync only needs to ask
    # WordPress for items newer than this instead of re-crawling everything.
    posts_synced_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pages_synced_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Last time a full crawl + deletion reconciliation ran (incremental syncs
    # alone can't detect posts/pages removed from WordPress).
    last_full_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    alerts: Mapped[list["Alert"]] = relationship("Alert", back_populates="site", cascade="all, delete-orphan")
    content_posts: Mapped[list["ContentPost"]] = relationship("ContentPost", back_populates="site", cascade="all, delete-orphan")
    performance_snapshots: Mapped[list["PerformanceSnapshot"]] = relationship("PerformanceSnapshot", back_populates="site", cascade="all, delete-orphan")
    plugin_audits: Mapped[list["PluginAudit"]] = relationship("PluginAudit", back_populates="site", cascade="all, delete-orphan")
    config: Mapped["SiteConfig | None"] = relationship("SiteConfig", back_populates="site", uselist=False, cascade="all, delete-orphan")
    traffic_snapshots: Mapped[list["TrafficSnapshot"]] = relationship("TrafficSnapshot", back_populates="site", cascade="all, delete-orphan")
    traffic_predictions: Mapped[list["TrafficPrediction"]] = relationship("TrafficPrediction", back_populates="site", cascade="all, delete-orphan")
    watched_urls: Mapped[list["WatchedUrl"]] = relationship("WatchedUrl", back_populates="site", cascade="all, delete-orphan")
    flow_categories: Mapped[list["FlowCategory"]] = relationship("FlowCategory", back_populates="site", cascade="all, delete-orphan")


class Alert(TimestampMixin, Base):
    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    site_id: Mapped[str] = mapped_column(String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    agent: Mapped[str] = mapped_column(String(20), nullable=False)  # watchdog, optimizer, autopilot
    severity: Mapped[str] = mapped_column(String(20), nullable=False)  # critical, warning, info
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    site: Mapped["Site"] = relationship("Site", back_populates="alerts")
    review_items: Mapped[list["ReviewItem"]] = relationship("ReviewItem", back_populates="alert")


class ContentPost(TimestampMixin, Base):
    __tablename__ = "content_posts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    site_id: Mapped[str] = mapped_column(String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    wp_post_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # WP post IDs and page IDs share one global ID sequence in WordPress core
    # (a Page is just a post with post_type='page'), so wp_post_id stays
    # unique across both without a composite key.
    content_type: Mapped[str] = mapped_column(String(10), default="post", nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # WordPress's own modified_gmt for this post/page as of the last full
    # analysis — lets agents skip re-analyzing (and re-crawling / re-asking
    # the AI about) content that hasn't actually changed.
    wp_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    health_score: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    traffic_30d: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    traffic_trend: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    issues: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reading_time_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    score_breakdown: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    ai_recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Structured, research-oriented guidance from the on-demand AI pass:
    # a diagnosis, ready-to-paste title/meta rewrites, and content gaps tied
    # to real search queries. Kept separate from ai_recommendation so the
    # plain-text field stays valid for consumers that render it as prose
    # (e.g. the SEO opportunities list).
    ai_guidance: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Hash of the inputs the current ai_recommendation was generated from —
    # lets the scorer skip paid AI calls when nothing changed.
    ai_rec_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    site: Mapped["Site"] = relationship("Site", back_populates="content_posts")
    variants: Mapped[list["Variant"]] = relationship("Variant", back_populates="content_post", cascade="all, delete-orphan")


class ReviewItem(TimestampMixin, Base):
    __tablename__ = "review_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    alert_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("alerts.id", ondelete="SET NULL"), nullable=True)
    agent: Mapped[str] = mapped_column(String(20), nullable=False)
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    reviewer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    site_id: Mapped[str] = mapped_column(String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    # Two-stage deletion. Retention moves old items here rather than deleting
    # them, because a report someone sent to a client is not something to
    # destroy on a count. NULL means active; a timestamp starts the 30-day
    # window in which it can still be restored.
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Exempt from retention entirely. The whole point of a lock is that an
    # automatic rule cannot override it, so nothing but an explicit unlock
    # removes this protection.
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    alert: Mapped["Alert | None"] = relationship("Alert", back_populates="review_items")
    site: Mapped["Site"] = relationship("Site")


class Variant(TimestampMixin, Base):
    __tablename__ = "variants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    content_post_id: Mapped[str] = mapped_column(String(36), ForeignKey("content_posts.id", ondelete="CASCADE"), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)  # linkedin, twitter, email, ad
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)

    content_post: Mapped["ContentPost"] = relationship("ContentPost", back_populates="variants")


class LinkCheck(Base):
    """When each link was last verified, so coverage rotates across runs.

    A run can only afford a few hundred HTTP checks, and without a memory of
    what was already checked it takes the same alphabetically-first slice every
    time — on a 2,097-link site that meant 76% of links were never verified
    once, and the report said "0 broken links".

    Keyed by a hash of the URL: link targets routinely exceed the ~2,700-byte
    limit for a btree index entry, and a unique index is the point.
    """

    __tablename__ = "link_checks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    site_id: Mapped[str] = mapped_column(String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Last HTTP status; 0 means "could not connect". Kept so a run can report
    # what it knows without re-fetching everything.
    status: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("site_id", "url_hash", name="uq_link_check_site_url"),)


class PerformanceSnapshot(Base):
    __tablename__ = "performance_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    site_id: Mapped[str] = mapped_column(String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    page_url: Mapped[str] = mapped_column(String(512), nullable=False)
    lcp: Mapped[float] = mapped_column(Float, nullable=False)   # ms
    cls: Mapped[float] = mapped_column(Float, nullable=False)   # unitless
    fid: Mapped[float] = mapped_column(Float, nullable=False)   # ms (INP in newer audits)
    ttfb: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)  # ms
    speed_score: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy: Mapped[str] = mapped_column(String(10), default="desktop", nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    site: Mapped["Site"] = relationship("Site", back_populates="performance_snapshots")


class PluginAudit(Base):
    """One auditable site component — a plugin or a theme.

    The table name and the `plugin_*` columns predate theme support; they now
    hold either kind, discriminated by `component_type`. Renaming them would
    touch a lot of code for no behavioural gain, so the meaning is documented
    here instead.
    """

    __tablename__ = "plugin_audits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    site_id: Mapped[str] = mapped_column(String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    plugin_slug: Mapped[str] = mapped_column(String(255), nullable=False)
    plugin_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # "plugin" | "theme" — a plugin and a theme can legitimately share a slug,
    # so this is part of a component's identity, never just a label.
    component_type: Mapped[str] = mapped_column(String(10), default="plugin", nullable=False)
    installed_version: Mapped[str] = mapped_column(String(50), nullable=False)
    latest_version: Mapped[str] = mapped_column(String(50), nullable=False)
    # Where latest_version came from, which decides whether "up to date" is a
    # finding or an absence of one:
    #   "wporg"   — resolved from the WordPress.org directory
    #   "manual"  — the operator supplied it (premium/custom components that
    #               the directory has never heard of: Avada, Swift Performance,
    #               anything built in-house)
    #   "unknown" — not in the directory and nobody has said. latest_version
    #               mirrors installed, so it must NOT be read as current.
    latest_source: Mapped[str] = mapped_column(String(10), default="unknown", nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), default="low", nullable=False)
    vulnerability_details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    # NULL means "not known" rather than "inactive". WordPress reports this;
    # someone entering a component by hand may simply not have said.
    is_active: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # "wordpress" (read from the REST API) | "manual" (entered by a user
    # because the site has no Application Password). A WordPress-sourced run
    # reconciles only its own rows — it must never delete what a person typed.
    source: Mapped[str] = mapped_column(String(20), default="wordpress", nullable=False)
    audited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "site_id", "component_type", "plugin_slug", name="uq_plugin_audits_site_type_slug"
        ),
    )

    site: Mapped["Site"] = relationship("Site", back_populates="plugin_audits")


class VulnerabilityCache(Base):
    """Cached WPScan answers, keyed by component rather than by version.

    WPScan returns every known vulnerability for a slug, each with the version
    it was `fixed_in`; deciding which ones affect a particular install is local
    arithmetic. So one fetch per slug serves every site and every version, and
    the cache does not have to be invalidated when a component is updated.

    It exists because the free plan allows 25 requests a day against an
    install tracking dozens of components. Without it the first run of the day
    exhausts the quota and every component after that reports "unknown" —
    which is exactly the "no finding means healthy" trap this module spends
    its whole time avoiding.
    """

    __tablename__ = "vulnerability_cache"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    component_type: Mapped[str] = mapped_column(String(10), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    # The raw vulnerability list. An empty list is a real answer — "WPScan
    # knows this component and it has none" — and is not the same as no row.
    vulnerabilities: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    # When an answer was last successfully retrieved. NULL means every attempt
    # so far has failed, so `vulnerabilities` is a placeholder and not an
    # answer — distinct from an empty list, which is a real "none known".
    fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When it was last *tried*, successfully or not. This is what the refresh
    # queue orders by: without it a component that keeps failing stays at the
    # front forever, spending the daily allowance every run and starving
    # everything behind it.
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("component_type", "slug", name="uq_vulnerability_cache_type_slug"),
    )


class TrafficSnapshot(Base):
    """Daily traffic snapshot per site, pulled from GA4 or estimated from post data.

    One row per (site_id, date) — enforced, not just conventional. Without
    it, repeated "Flush & Re-run" clicks (or a backfill re-covering a date
    it already wrote) silently inserted duplicate rows for the same date
    instead of updating the existing one.
    """
    __tablename__ = "traffic_snapshots"
    __table_args__ = (UniqueConstraint("site_id", "date", name="uq_traffic_snapshots_site_date"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    site_id: Mapped[str] = mapped_column(String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    date: Mapped[str] = mapped_column(String(10), nullable=False)          # "2026-04-09" ISO date
    pageviews: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sessions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    users: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bounce_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)  # 0-100
    avg_session_duration: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)  # seconds
    top_pages: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)  # [{url, views}]
    geo_countries: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)  # [{country, country_code, views, sessions}]
    geo_regions: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)   # [{region, views, pct}]
    geo_cities: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)    # [{city, country, views}]
    source: Mapped[str] = mapped_column(String(20), default="ga4", nullable=False)   # "ga4" | "estimated"
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    site: Mapped["Site"] = relationship("Site", back_populates="traffic_snapshots")


class TrafficPrediction(Base):
    """AI-generated traffic forecast per site and horizon, cached for 24 hours."""
    __tablename__ = "traffic_predictions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    site_id: Mapped[str] = mapped_column(String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)  # 7 | 14 | 30
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # [{date, base, optimistic, pessimistic}]
    daily_forecasts: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    # [{date, type, description, severity}]
    anomalies: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    narrative: Mapped[str] = mapped_column(Text, default="", nullable=False)
    model_version: Mapped[str] = mapped_column(String(50), default="gpt-4o", nullable=False)

    site: Mapped["Site"] = relationship("Site", back_populates="traffic_predictions")


class OAuthToken(Base):
    """Stores Google OAuth2 tokens (one row per provider)."""
    __tablename__ = "oauth_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    provider: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)  # "google"
    access_token: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    refresh_token: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    token_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scope: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class SiteConfig(Base):
    """Per-site Google Analytics / Search Console configuration."""
    __tablename__ = "site_configs"

    site_id: Mapped[str] = mapped_column(String(36), ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True)
    ga_property_id: Mapped[str | None] = mapped_column(String(100), nullable=True)   # e.g. "properties/123456789"
    gsc_site_url: Mapped[str | None] = mapped_column(String(512), nullable=True)     # e.g. "https://myblog.com/"
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    site: Mapped["Site"] = relationship("Site", back_populates="config")


class AppSetting(Base):
    """Application-wide settings (agent toggles, notification preferences) as key/value JSON."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class AgentJob(Base):
    """Tracks a single on-demand agent run triggered from the UI."""
    __tablename__ = "agent_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    site_id: Mapped[str] = mapped_column(String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    # pending | running | done | error | stopped
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    steps: Mapped[list["AgentJobStep"]] = relationship(
        "AgentJobStep", back_populates="job",
        cascade="all, delete-orphan",
        order_by="AgentJobStep.step_index",
    )


class AgentJobStep(Base):
    """One agent step within an AgentJob."""
    __tablename__ = "agent_job_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_jobs.id", ondelete="CASCADE"), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_name: Mapped[str] = mapped_column(String(100), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    # pending | running | done | error
    alerts_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped["AgentJob"] = relationship("AgentJob", back_populates="steps")


class WatchedUrl(Base):
    """A specific URL a user wants live GA4 active-user tracking for.

    `path` (e.g. "/pricing/") is the normalized internal identity used for
    dedup and analytics matching — GA4's Realtime API has no page-path
    dimension, only page title, so `title` is resolved separately (from an
    already-crawled ContentPost when available, else a live page fetch) and
    used to join against Realtime API rows. `url` is the full canonical
    link kept purely for display, regardless of whether the user originally
    pasted a full URL or a bare path/slug.
    """
    __tablename__ = "watched_urls"
    __table_args__ = (UniqueConstraint("site_id", "path", name="uq_watched_urls_site_path"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    site_id: Mapped[str] = mapped_column(String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(10), default="manual", nullable=False)  # "manual" | "csv"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    site: Mapped["Site"] = relationship("Site", back_populates="watched_urls")


class FlowCategory(TimestampMixin, Base):
    """A marketer-defined named journey ("A", "Signup Flow", ...) — an
    ordered set of page-pattern steps classified via GA4's Funnel Reports
    API (session-scoped path reconstruction isn't possible through GA4's
    standard Data API; only BigQuery Export exposes raw per-session event
    order — see FlowCategorySnapshot for what this can and can't show)."""
    __tablename__ = "flow_categories"
    __table_args__ = (UniqueConstraint("site_id", "name", name="uq_flow_categories_site_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    site_id: Mapped[str] = mapped_column(String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)  # dashboard chart color token
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    site: Mapped["Site"] = relationship("Site", back_populates="flow_categories")
    steps: Mapped[list["FlowCategoryStep"]] = relationship(
        "FlowCategoryStep", back_populates="flow_category",
        cascade="all, delete-orphan", order_by="FlowCategoryStep.step_index",
    )
    snapshots: Mapped[list["FlowCategorySnapshot"]] = relationship(
        "FlowCategorySnapshot", back_populates="flow_category", cascade="all, delete-orphan",
    )


class FlowCategoryStep(Base):
    """One ordered step in a flow category — a page-pattern condition
    matched against GA4's `page_location` event parameter. Maps directly
    onto a GA4 FunnelStep: `is_directly_followed`/`within_seconds` become
    `isDirectlyFollowedBy`/`withinDurationFromPriorStep`."""
    __tablename__ = "flow_category_steps"
    __table_args__ = (
        UniqueConstraint("flow_category_id", "step_index", name="uq_flow_category_steps_order"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    flow_category_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("flow_categories.id", ondelete="CASCADE"), nullable=False
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    # "contains" (recommended default — robust to query strings/domain
    # prefix) | "exact" (matches the full page_location exactly) | "regex"
    match_type: Mapped[str] = mapped_column(String(20), default="contains", nullable=False)
    pattern: Mapped[str] = mapped_column(String(512), nullable=False)
    is_directly_followed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    within_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Marks this step as the flow's conversion event (e.g. a "thank you"
    # page) — lets the dashboard report a real, explicitly-labeled "leads"
    # count instead of assuming whichever step is last is the goal.
    is_goal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    flow_category: Mapped["FlowCategory"] = relationship("FlowCategory", back_populates="steps")


class FlowCategorySnapshot(Base):
    """One GA4 funnel query's result for a flow category + date range,
    stored durably so a trend chart doesn't need to re-query GA4 for
    history and so a category's past results survive later step edits.

    This is aggregate, user-scoped data straight from GA4's Funnel Reports
    API — NOT a per-session classification. GA4 never exposes individual
    sessions/users outside BigQuery Export, so there is no list of "the
    sessions in this category" to drill into; the closest honest
    equivalent is `breakdown` (one GA4 dimension, e.g. device category or
    channel, sliced across each step).
    """
    __tablename__ = "flow_category_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    flow_category_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("flow_categories.id", ondelete="CASCADE"), nullable=False
    )
    site_id: Mapped[str] = mapped_column(String(36), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    range_start: Mapped[str] = mapped_column(String(10), nullable=False)  # ISO date
    range_end: Mapped[str] = mapped_column(String(10), nullable=False)
    # [{step_index, label, active_users, completion_rate, abandonments, abandonment_rate}]
    step_results: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    total_entered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    conversion_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)  # 0-1
    # Which step (if any) was marked is_goal at the time this snapshot was
    # computed, and that step's active_users — stored rather than
    # re-derived from the category's current steps, so a snapshot's meaning
    # of "leads" survives later step edits.
    goal_step_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    leads: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lead_rate: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0-1, leads/total_entered
    breakdown_dimension: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # [{value, step_index, active_users}] — present only if a breakdown was requested
    breakdown: Mapped[list[dict]] = mapped_column(JSON, default=list, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    flow_category: Mapped["FlowCategory"] = relationship("FlowCategory", back_populates="snapshots")
