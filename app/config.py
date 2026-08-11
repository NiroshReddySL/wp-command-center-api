from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Which safety rules apply. "production" turns the insecure-default checks
    # in app/security/startup_checks.py from warnings into a refusal to boot:
    # every default in this file is chosen so a developer can clone and run,
    # and every one of those defaults is a vulnerability in front of real data.
    ENVIRONMENT: str = "development"
    DATABASE_URL: str = "postgresql+asyncpg://wpcc:wpcc_secret@localhost:5432/wp_command_center"
    OPENAI_API_KEY: str = "sk-placeholder"
    GA_CLIENT_ID: str = ""
    GA_CLIENT_SECRET: str = ""
    GA_REDIRECT_URI: str = "http://localhost:8000/api/auth/google/callback"
    GSC_CLIENT_ID: str = ""
    GSC_CLIENT_SECRET: str = ""
    WPSCAN_API_KEY: str = ""
    # How long a cached vulnerability answer stays usable. Long by design:
    # the free plan allows 25 requests a day against an install tracking
    # dozens of components, so re-fetching everything daily is not affordable.
    # A week means each component refreshes roughly once per week, spread
    # across runs, which fits comfortably inside the allowance.
    WPSCAN_CACHE_TTL_HOURS: int = 168
    # Most one run may spend. The scheduler sweeps every 6 hours, so without
    # a cap the first run of the day takes the entire allowance and the other
    # three — and any re-run triggered by hand — get nothing. Capping spreads
    # the same total across the day instead of burning it in one burst.
    WPSCAN_MAX_PER_RUN: int = 8
    # Headroom left unspent, so an allowance that is nearly gone still leaves
    # room for the /status checks themselves and for retries. Deliberately
    # small: a large reserve is quota that nothing can ever spend.
    WPSCAN_QUOTA_RESERVE: int = 2
    # Google PageSpeed Insights API key — optional; raises the quota from
    # ~25 req/100s (per IP, keyless) to 25k/day so perf checks stop flaking.
    PSI_API_KEY: str = ""
    # How many pages one performance run measures. Bounded so a run stays
    # short and predictable regardless of library size: coverage comes from
    # rotating across runs, not from doing everything at once.
    PSI_MAX_PAGES_PER_RUN: int = 12
    # Simultaneous PageSpeed calls. Keyless PSI allows roughly 25 requests
    # per 100 seconds per IP, so anything higher just earns 429s; a key
    # raises the ceiling to 25k/day and the concurrency with it.
    PSI_CONCURRENCY: int = 2
    PSI_CONCURRENCY_WITH_KEY: int = 6
    # A page measured within this window is fresh enough to skip, which is
    # what lets the rotation reach pages it has never seen.
    PSI_FRESH_HOURS: int = 72
    # Ceiling on a hand-triggered re-measure. The rotation exists because a
    # full sweep of an enterprise library cannot fit in one run at keyless
    # PSI rates — asking for "everything" has to mean a bounded batch, and
    # the response says how many of the candidates it actually took so a
    # truncated request never passes for a complete one.
    PSI_RESCAN_MAX_PAGES: int = 200

    # ── Watchdog scale limits (enterprise sites) ─────────────────────────────
    # Posts/pages themselves are fetched uncapped (see WordPressConnector) —
    # only the per-run link-verification count is bounded, since HTTP-checking
    # thousands of external URLs every run isn't worth the time even though
    # discovering them all is. Unique URLs verified per run — internal first.
    LINK_CHECK_MAX_URLS: int = 500
    LINK_CHECK_CONCURRENCY: int = 16
    # Cap OpenAI recommendation calls per ContentScorer run — bounds cost and
    # runtime regardless of site size. Posts are prioritized worst-score-first
    # (then by traffic), so the neediest content is always served first; any
    # posts beyond the cap simply carry over to the next run untouched.
    # Whether the nightly ContentScorer generates AI recommendations on its
    # own. OFF by default: every page already gets a full rule-based analysis
    # (app/agents/optimizer/insights.py) for free and instantly, so spending
    # OpenAI budget generating prose for hundreds of pages nobody has opened
    # is waste. Users request AI synthesis per page, on the page. Set true to
    # restore automatic generation, bounded by the budget below.
    CONTENT_AI_AUTO_GENERATE: bool = False
    CONTENT_AI_BUDGET_PER_RUN: int = 200
    CONTENT_AI_CONCURRENCY: int = 5
    # Max posts/pages given full analysis (live-page crawl + scoring + AI
    # candidacy) in a single ContentScorer run — an enterprise site with
    # thousands of never-analyzed items would otherwise try to do it all in
    # one pass and blow the job timeout every time, forever discarding
    # progress. Items beyond the cap are simply the top priority next run.
    CONTENT_ANALYSIS_BATCH_SIZE: int = 150
    # Commit progress after this many fully-processed items so a mid-run
    # timeout or crash only costs the current partial batch, never the
    # whole run.
    CONTENT_COMMIT_EVERY: int = 20
    # Enable ONLY when the API sits behind a reverse proxy that overwrites
    # X-Forwarded-For (nginx, an ALB, Cloudflare). It makes rate limiting
    # key on the real client instead of the proxy's single IP — but since
    # the header is client-settable, enabling it when NOT behind such a
    # proxy would let anyone forge a fresh IP per request and bypass every
    # limit. See app/security/rate_limit.py:_client_ip.
    TRUST_PROXY_HEADERS: bool = False
    FRONTEND_URL: str = "http://localhost:5173"
    SECRET_KEY: str = "dev-secret-key-change-in-production"
    # Fallback MS Teams webhook (Power Automate Workflows URL). The UI-saved
    # value in Settings → Notification Preferences takes precedence.
    TEAMS_WEBHOOK_URL: str = ""
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""

    # ── Auth & security ──────────────────────────────────────────────────────
    JWT_EXPIRY_HOURS: int = 24
    # Initial admin bootstrap — used only when the users table is empty.
    ADMIN_EMAIL: str = ""
    ADMIN_PASSWORD: str = ""
    # Fernet key for secrets-at-rest (base64, 32 bytes). Derived from
    # SECRET_KEY when unset — set explicitly in production.
    TOKEN_ENCRYPTION_KEY: str = ""
    # Comma-separated allowed CORS origins (FRONTEND_URL is always included).
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"
    # Allow sites with private/loopback URLs (local WordPress dev only).
    ALLOW_PRIVATE_URLS: bool = False
    # Run the in-process scheduler. With multiple workers, enable in ONE only.
    ENABLE_SCHEDULER: bool = True
    # Create/patch the schema on boot (dev convenience). In production set
    # false and run `alembic upgrade head` as a deploy step instead.
    AUTO_CREATE_SCHEMA: bool = True
    # WordPress username the Application Passwords belong to.
    WP_API_USERNAME: str = "admin"

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.strip().lower() in {"production", "prod"}

    @property
    def cors_origins_list(self) -> list[str]:
        origins = {o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()}
        origins.add(self.FRONTEND_URL)
        return sorted(origins)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
