# WP Command Center — API

FastAPI backend for **WP Command Center**, an AI-powered WordPress multi-site operations platform. This is the API half of a two-repo split — the frontend lives in the sibling [`wp-command-center-dashboard`](https://github.com/) repo.

## Tech Stack

Python 3.12 + FastAPI + SQLAlchemy 2.0 async + asyncpg + Alembic + OpenAI SDK + APScheduler + httpx, on PostgreSQL 16.

## Getting Started

```bash
cp .env.example .env   # fill in real values — DATABASE_URL, OPENAI_API_KEY, etc.
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload   # http://localhost:8000
```

Requires a reachable PostgreSQL 16 instance matching `DATABASE_URL` in `.env` — e.g. `docker run -p 5432:5432 -e POSTGRES_USER=wpcc -e POSTGRES_PASSWORD=wpcc_secret -e POSTGRES_DB=wp_command_center postgres:16-alpine`.

## Scripts

- `uvicorn app.main:app --reload` — run the dev server
- `alembic upgrade head` — apply migrations
- `alembic revision --autogenerate -m "..."` — generate a new migration
- `python seed.py` — seed script (no dummy data — connect real WordPress sites via the dashboard's Settings page)
- `python -m pytest tests/ -q` — run tests
- `python -m ruff check app/ tests/` — lint

## Project Structure

- `app/api/` — FastAPI routers, one file per resource
- `app/agents/` — Watchdog / Optimizer / Autopilot agents, all inheriting from `BaseAgent`
- `app/connectors/` — external API clients (WordPress, GA4, etc.)
- `app/database/` — SQLAlchemy models + Alembic migrations
- `app/services/` — scheduler, cross-cutting services
- `app/security/` — auth, rate limiting, crypto

See `CLAUDE.md` for detailed conventions.
