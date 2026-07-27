from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://wpcc:wpcc_secret@localhost:5432/wp_command_center"
    OPENAI_API_KEY: str = "sk-placeholder"
    GA_CLIENT_ID: str = ""
    GA_CLIENT_SECRET: str = ""
    GA_REDIRECT_URI: str = "http://localhost:8000/api/auth/google/callback"
    GSC_CLIENT_ID: str = ""
    GSC_CLIENT_SECRET: str = ""
    WPSCAN_API_KEY: str = ""
    # Google PageSpeed Insights API key — optional; raises the quota from
    # ~25 req/100s (per IP, keyless) to 25k/day so perf checks stop flaking.
    PSI_API_KEY: str = ""

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
    def cors_origins_list(self) -> list[str]:
        origins = {o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()}
        origins.add(self.FRONTEND_URL)
        return sorted(origins)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
