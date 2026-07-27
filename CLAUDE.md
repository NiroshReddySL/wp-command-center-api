# CLAUDE.md — WP Command Center API

## What is this project?
The FastAPI backend for WP Command Center, an AI-powered WordPress multi-site operations platform. It connects to 2-5 WordPress sites and runs three intelligent agents (Watchdog, Optimizer, Autopilot) that detect problems, find opportunities, and automate repetitive marketing engineering tasks. The `wp-command-center-dashboard` repo (sibling) is the frontend that consumes this API. The user is a senior software engineer on a marketing team.

## Tech Stack
Python 3.12 + FastAPI + SQLAlchemy 2.0 async + asyncpg + Alembic + OpenAI SDK + APScheduler + httpx, on PostgreSQL 16.

## Architecture Decisions
- Backend uses async everywhere — no sync database calls
- Pydantic v2 for all request/response schemas
- All agents inherit from a `BaseAgent` class

## Commands
- `uvicorn app.main:app --reload` — run the dev server
- `python seed.py` — seed script
- `alembic upgrade head` — apply migrations
- `python -m pytest tests/ -q` — run tests
- `python -m ruff check app/ tests/` — lint

## Key Files to Understand First
1. `app/database/models.py` — the data model
2. `app/api/router.py` — where every router gets registered
3. `app/agents/base.py` — the BaseAgent all agents inherit from
4. `app/services/scheduler.py` — APScheduler job registration
5. `seed.py` — demo data notes

## When Adding API Endpoints
1. Add a Pydantic schema in the relevant router file
2. All queries use an async session
3. Return proper HTTP status codes (201 for creates, 204 for deletes)
4. Add the route to `app/api/router.py`
5. The `wp-command-center-dashboard` repo will need a corresponding React Query hook — flag this in your response if you're not also updating that repo

## When Adding Agent Features
1. Create a new module in the appropriate agent directory (`app/agents/<watchdog|optimizer|autopilot|...>/`)
2. Inherit from `BaseAgent`
3. Register with the scheduler in `app/services/scheduler.py`
4. Create an API endpoint to surface results
5. The `wp-command-center-dashboard` repo will need a UI component wired to the appropriate page

## Testing Conventions
- pytest + httpx AsyncClient
- Test files live next to the code they test, under `tests/`

## Related Repos
- `wp-command-center-dashboard` — the React frontend this API serves
