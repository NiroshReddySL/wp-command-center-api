# WP Command Center — API

The FastAPI backend for **WP Command Center**: an AI-powered operations platform for teams running multiple WordPress marketing sites. It connects to 2–5 WordPress properties, pulls data from Google Analytics 4, Search Console, and WPScan, and runs a set of autonomous agents that detect problems, surface opportunities, and automate repetitive marketing-engineering work.

The companion frontend lives in [`wp-command-center-dashboard`](https://github.com/NiroshReddySL/wp-command-center-dashboard) — the two are designed to run together, but this API is fully usable on its own (interactive docs at `/docs`).

## Contents

- [What it does](#what-it-does)
- [Tech stack](#tech-stack)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [Project structure](#project-structure)
- [The agent system](#the-agent-system)
- [Database & migrations](#database--migrations)
- [Testing & linting](#testing--linting)
- [Deployment](#deployment)
- [Related repos](#related-repos)

## What it does

**Watchdog** — continuously monitors site health:
- Broken link detection (internal + external)
- Plugin vulnerability audits, cross-referenced against the WPScan database
- Page response-time monitoring and regression alerts
- Config drift and form-validation checks

**Optimizer** — finds content and SEO opportunities:
- Per-page content health scoring (word count, images, internal/external links, freshness, title length, heading hierarchy, meta description, schema/FAQ markup) with AI-generated, site-context-aware recommendations
- SEO issue detection and ranking-opportunity discovery from Search Console data
- Internal linking suggestions between related posts
- Scales to enterprise-sized sites: content scoring runs in bounded, resumable batches with incremental commits, so a large backlog converges over successive runs instead of timing out and losing progress

**Autopilot** — automates recurring work:
- AI-assisted content repurposing suggestions
- A/B test tracking
- Automated weekly performance reports (with optional MS Teams digest)

**Traffic** — GA4 + Search Console daily snapshots with AI-generated traffic forecasts and anomaly detection.

**Live Visitors** — tracks real-time and historical GA4 active-user counts for a curated list of pages/URLs, with CSV import/export and day-by-day breakdowns.

**Flow Categories** — lets a marketer define named, ordered page-pattern journeys (e.g. "Pricing → Signup") and classifies GA4 traffic against them using the Funnel Reports API, with conversion-rate trend tracking and drop alerts.

All of the above run on a schedule (via APScheduler) and can also be triggered on demand from the dashboard, with live progress streamed over SSE.

| Agent | Default schedule |
|---|---|
| Broken Link Checker | every 6 hours |
| Plugin Audit | every 6 hours |
| Performance Monitor | every 2 hours |
| SEO Analyzer | daily, 03:00 UTC |
| Content Scorer | daily, 03:00 UTC |
| Traffic Sync | nightly |
| Flow Categories | nightly, 04:00 UTC |
| Automated Reports | Fridays, 06:00 UTC |

Each is individually toggleable from the dashboard's Settings page.

## Tech stack

Python 3.12 · FastAPI · SQLAlchemy 2.0 (async) · asyncpg · Alembic · PostgreSQL 16 · APScheduler · OpenAI SDK · httpx

## Getting started

**Prerequisites:** Python 3.12+, a reachable PostgreSQL 16 instance.

```bash
# 1. Start Postgres (skip if you already have one)
docker run -d --name wpcc-postgres -p 5432:5432 \
  -e POSTGRES_USER=wpcc -e POSTGRES_PASSWORD=wpcc_secret -e POSTGRES_DB=wp_command_center \
  postgres:16-alpine

# 2. Configure
cp .env.example .env
# edit .env — at minimum set DATABASE_URL, SECRET_KEY, TOKEN_ENCRYPTION_KEY

# 3. Install + migrate
pip install -e ".[dev]"
alembic upgrade head

# 4. Run
uvicorn app.main:app --reload
```

The API is now at `http://localhost:8000` — interactive docs at `http://localhost:8000/docs`. On first boot, an admin account is created from `ADMIN_EMAIL`/`ADMIN_PASSWORD` in `.env` (only if the users table is empty). Add real WordPress sites from the dashboard's Settings → Connected Sites; there's no seed/demo data by design (`seed.py` explains why).

## Configuration

All configuration is environment-driven (`app/config.py`, backed by `pydantic-settings`). See `.env.example` for the full list; the ones you'll actually need to touch:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | asyncpg connection string |
| `SECRET_KEY` | JWT signing secret |
| `TOKEN_ENCRYPTION_KEY` | Fernet key encrypting stored WP/Google credentials at rest — generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `OPENAI_API_KEY` | powers AI recommendations, forecasts, and reports |
| `GA_CLIENT_ID` / `GA_CLIENT_SECRET` | Google OAuth app for Analytics + Search Console |
| `WPSCAN_API_KEY` | plugin vulnerability lookups (optional but recommended) |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | bootstrap admin, used once when the users table is empty |
| `CORS_ORIGINS` | comma-separated origins allowed to call this API (set to your dashboard's URL) |

## Project structure

```
app/
├── api/            # FastAPI routers — one file per resource, registered in router.py
├── agents/          # Watchdog / Optimizer / Autopilot / Traffic / Flows agents
│   ├── base.py       #   BaseAgent — every agent inherits this (alert creation, notifications)
│   ├── watchdog/
│   ├── optimizer/
│   ├── autopilot/
│   ├── traffic/
│   └── flows/
├── connectors/      # External API clients: WordPress, GA4, Search Console, WPScan
├── database/
│   ├── models.py     #   SQLAlchemy models — the data model to read first
│   └── migrations/   #   Alembic
├── services/        # Scheduler, content sync, notifications, job executor
├── security/        # Auth, rate limiting, encryption, SSRF guarding
└── ai/              # OpenAI engine wrapper + prompt templates
```

## The agent system

Every agent inherits from `BaseAgent` (`app/agents/base.py`), which standardizes:
- `run(site_id) -> list[Alert]` as the single entry point
- Alert creation, with automatic critical-severity notification dispatch (Teams/email)
- A consistent pattern for querying `Site`/`SiteConfig` and handling a not-connected integration gracefully

Adding a new agent capability means: create a module under the right `agents/<category>/` directory, subclass `BaseAgent`, register it with the scheduler (`app/services/scheduler.py`) and/or the on-demand job pipeline (`app/api/agents.py` + `app/services/job_executor.py`), then expose an endpoint for the dashboard to read the results from.

## Database & migrations

SQLAlchemy 2.0 async models in `app/database/models.py`; schema changes go through Alembic:

```bash
alembic revision --autogenerate -m "add flow categories"
alembic upgrade head
```

`app/database/engine.py` also runs a small set of idempotent `IF NOT EXISTS` DDL statements on boot (`init_db`) as a dev-convenience safety net alongside the real Alembic migration path.

## Testing & linting

```bash
python -m pytest tests/ -q            # 224+ tests, no live DB required — everything's mocked at the HTTP boundary
python -m ruff check app/ tests/
```

Tests favor small, pure functions extracted from the endpoints (date-range math, filter-condition builders, batching/priority logic, ...) so business logic is verified directly, with connectors tested against mocked `httpx` responses rather than real network calls.

## Deployment

A production `Dockerfile` is included (non-root user, health check against `/ready`). Typical flow: build the image, run Alembic migrations against your production database, then start the container — `CORS_ORIGINS` and `FRONTEND_URL` should point at wherever `wp-command-center-dashboard` is deployed.

## Related repos

- [`wp-command-center-dashboard`](https://github.com/NiroshReddySL/wp-command-center-dashboard) — the React frontend this API serves
