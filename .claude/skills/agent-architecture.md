# Skill: Agent Architecture

## Base Agent Pattern

All three agents (Watchdog, Optimizer, Autopilot) inherit from a common base that handles scheduling, logging, alert creation, and AI calls.

```python
# app/agents/base.py
from abc import ABC, abstractmethod
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models import Alert
from app.ai.engine import AIEngine

class BaseAgent(ABC):
    """Base class for all WP Command Center agents."""
    
    agent_name: str  # 'watchdog', 'optimizer', 'autopilot'
    
    def __init__(self, db: AsyncSession, ai: AIEngine):
        self.db = db
        self.ai = ai
    
    async def create_alert(
        self,
        site_id: str,
        severity: str,
        alert_type: str,
        title: str,
        description: str,
        metadata: dict | None = None,
    ) -> Alert:
        alert = Alert(
            site_id=site_id,
            agent=self.agent_name,
            severity=severity,
            type=alert_type,
            title=title,
            description=description,
            metadata=metadata or {},
        )
        self.db.add(alert)
        await self.db.flush()
        return alert
    
    @abstractmethod
    async def run(self, site_id: str) -> list[Alert]:
        """Execute this agent's checks for a given site."""
        ...

    async def run_all_sites(self, site_ids: list[str]) -> list[Alert]:
        """Run across all connected sites."""
        all_alerts = []
        for site_id in site_ids:
            alerts = await self.run(site_id)
            all_alerts.extend(alerts)
        return all_alerts
```

## Agent 1: Watchdog

Detects problems. Runs on schedule and on webhooks.

```
watchdog/
├── link_checker.py      # Crawls pages, finds broken internal/external links
├── performance.py       # Runs Lighthouse, detects CWV regressions
├── plugin_audit.py      # Checks plugins against WPScan vulnerability DB
├── config_drift.py      # Compares site configs against baseline
└── form_validator.py    # Synthetic form submissions to verify funnels
```

### Link Checker Logic
1. Fetch all pages via WP REST API (sitemap or posts endpoint)
2. For each page, parse HTML for all <a href> tags
3. HEAD request each URL, record status code
4. Broken = 4xx or 5xx status
5. Create alert for each broken link with: source page, broken URL, status code
6. For broken internal links: suggest redirect target using content similarity

### Performance Monitor Logic
1. For each key page (homepage, top 10 traffic pages), call PageSpeed Insights API
2. Record LCP, CLS, FID/INP, Speed Score
3. Compare against last snapshot
4. If any metric regressed > 10%, create warning alert
5. If any metric regressed > 25%, create critical alert
6. AI analyzes the resource waterfall to identify likely cause

### Plugin Audit Logic
1. Fetch installed plugins via WP REST API
2. Cross-reference each plugin slug + version against WPScan API
3. Score risk: critical (actively exploited), high (known vuln, no patch), medium (known vuln, patch available), low (outdated but no known vuln)
4. Create alert per vulnerable plugin with remediation steps

### Config Drift Logic
1. Define a baseline config per site (plugins list, WP version, theme, key options)
2. Fetch current config via WP REST API
3. Diff against baseline
4. Flag deviations: missing plugin, version mismatch, changed option
5. Cross-site comparison: find inconsistencies between sites that should match

## Agent 2: Optimizer

Finds opportunities. Runs daily.

```
optimizer/
├── seo_analyzer.py       # Search Console data → ranking opportunities
├── content_scorer.py     # Health score for every post
├── internal_linker.py    # Semantic link suggestions
└── cross_site_intel.py   # Cross-site content gap analysis
```

### SEO Analyzer Logic
1. Pull Search Console data: queries, pages, positions, clicks, impressions
2. Identify "striking distance" pages: position 8-20 with decent impressions
3. For each, fetch the page content and top-ranking competitor content
4. AI compares: what's missing? (sections, depth, freshness, media)
5. Generate specific recommendations with estimated traffic impact

### Content Health Scorer
Score = weighted average of:
- Traffic trend (30%): 30-day trend vs 90-day average
- Freshness (25%): days since last update, outdated references
- Link equity (20%): internal links pointing to/from this post
- Competitive position (15%): average ranking position for target keywords  
- Technical quality (10%): page speed, mobile-friendly, schema markup

### Internal Linker Logic
1. Build vector embeddings of all posts (title + first 500 chars) using Anthropic embeddings or a local model
2. For each post, find top 5 semantically similar posts
3. Filter out already-linked posts
4. AI generates natural anchor text and suggests placement paragraph
5. Queue as review items

## Agent 3: Autopilot

Automates work. Runs on triggers + schedule.

```
autopilot/
├── repurposer.py      # Blog → LinkedIn, Twitter, Email, Ad copy
├── reporter.py        # Weekly/monthly performance reports
└── ab_tester.py       # Generate variants, deploy, measure
```

### Repurposer Logic
1. Triggered by WP publish webhook
2. Fetch full post content, clean HTML → markdown
3. Send to AI with brand guide + channel-specific templates
4. Generate variants for each enabled channel
5. Store as Variant records linked to ContentPost
6. Queue for review

### Reporter Logic  
1. Scheduled weekly (Friday) and monthly (1st of month)
2. Pull data from: Analytics API, Search Console, WP REST API, internal alert/variant tables
3. AI generates narrative report with sections:
   - Executive summary (3 sentences)
   - Traffic analysis with comparisons
   - Content performance (top/bottom posts)
   - Agent activity summary (issues found, fixed, content generated)
   - Recommendations for next period
4. Store report, notify team

### A/B Tester Logic
1. User selects a page and element to test (headline, CTA, hero)
2. AI generates 2-3 meaningful variants
3. Deploy via WP REST API (custom meta field) + lightweight JS snippet
4. Track impressions and conversions via Analytics events
5. After statistical significance reached, declare winner
6. Auto-promote winner (with approval) or pause test

## Scheduler Configuration

```python
# app/services/scheduler.py
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

scheduler = AsyncIOScheduler()

def start_scheduler():
    # Watchdog: every 6 hours
    scheduler.add_job(run_watchdog, CronTrigger(hour='*/6'), id='watchdog')
    
    # Optimizer: daily at 3am
    scheduler.add_job(run_optimizer, CronTrigger(hour=3), id='optimizer')
    
    # Reporter: weekly Friday 6am
    scheduler.add_job(run_reporter, CronTrigger(day_of_week='fri', hour=6), id='reporter')
    
    # Performance check: every 2 hours
    scheduler.add_job(run_performance, CronTrigger(hour='*/2'), id='performance')
    
    scheduler.start()
```

## AI Engine Wrapper

```python
# app/ai/engine.py
import anthropic
from app.config import settings

class AIEngine:
    def __init__(self):
        self.client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    
    async def analyze(self, system_prompt: str, user_content: str, max_tokens: int = 2000) -> str:
        response = await self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text
    
    async def generate_json(self, system_prompt: str, user_content: str) -> dict:
        """For structured outputs — instructs model to return JSON only."""
        response = await self.analyze(
            system_prompt + "\n\nRespond ONLY with valid JSON. No markdown, no preamble.",
            user_content,
        )
        import json
        return json.loads(response.strip().removeprefix("```json").removesuffix("```").strip())
```

## WordPress Connector

```python
# app/connectors/wordpress.py
import httpx

class WordPressConnector:
    def __init__(self, site_url: str, api_key: str | None = None):
        self.base_url = f"{site_url.rstrip('/')}/wp-json"
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            timeout=30.0,
        )
    
    async def get_posts(self, per_page: int = 100, page: int = 1) -> list[dict]:
        resp = await self.client.get("/wp/v2/posts", params={"per_page": per_page, "page": page})
        resp.raise_for_status()
        return resp.json()
    
    async def get_plugins(self) -> list[dict]:
        resp = await self.client.get("/wp/v2/plugins")
        resp.raise_for_status()
        return resp.json()
    
    async def update_post_meta(self, post_id: int, meta: dict) -> dict:
        resp = await self.client.post(f"/wp/v2/posts/{post_id}", json={"meta": meta})
        resp.raise_for_status()
        return resp.json()
    
    async def get_site_health(self) -> dict:
        resp = await self.client.get("/wp-site-health/v1/tests")
        resp.raise_for_status()
        return resp.json()
```

## Alert Priority Scoring

Used by the dashboard to sort the priority queue:

```python
def calculate_priority(alert: Alert) -> int:
    """Higher score = higher priority. Max 100."""
    score = 0
    
    # Severity weight (0-50)
    severity_weights = {"critical": 50, "warning": 30, "info": 10}
    score += severity_weights.get(alert.severity, 0)
    
    # Recency weight (0-25): newer = higher priority
    hours_old = (datetime.utcnow() - alert.created_at).total_seconds() / 3600
    if hours_old < 1: score += 25
    elif hours_old < 6: score += 20
    elif hours_old < 24: score += 15
    elif hours_old < 72: score += 10
    else: score += 5
    
    # Agent weight (0-15): watchdog issues > optimizer > autopilot
    agent_weights = {"watchdog": 15, "optimizer": 10, "autopilot": 5}
    score += agent_weights.get(alert.agent, 0)
    
    # Actionability weight (0-10): items with clear actions rank higher
    if alert.metadata.get("auto_fixable"): score += 10
    elif alert.metadata.get("has_suggestion"): score += 5
    
    return min(score, 100)
```