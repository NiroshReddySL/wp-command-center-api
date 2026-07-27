# Skill: API Patterns

## FastAPI App Structure

```python
# app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.database.engine import init_db
from app.api.router import api_router
from app.services.scheduler import start_scheduler

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    start_scheduler()
    yield

app = FastAPI(title="WP Command Center API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api")
```

## Database Engine Pattern

```python
# app/database/engine.py
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.config import settings

engine = create_async_engine(settings.DATABASE_URL, echo=False, pool_size=10)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_db():
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

## Model Conventions

```python
# All models inherit from a Base with common fields
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import DateTime, func
from datetime import datetime
import uuid

class Base(DeclarativeBase):
    pass

class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class Site(TimestampMixin, Base):
    __tablename__ = "sites"
    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str]
    url: Mapped[str] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(default="active")  # active, paused, error
    health_score: Mapped[int] = mapped_column(default=100)
    last_synced_at: Mapped[datetime | None]
```

## Router Pattern

```python
# app/api/dashboard.py
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.engine import get_db
from pydantic import BaseModel

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

class DashboardMetrics(BaseModel):
    total_issues: int
    avg_health_score: float
    content_published_this_week: int
    uptime_percentage: float

class PriorityItem(BaseModel):
    id: str
    severity: str  # critical, warning, info
    agent: str     # watchdog, optimizer, autopilot
    title: str
    site_name: str
    created_at: str
    action_type: str

class DashboardResponse(BaseModel):
    metrics: DashboardMetrics
    priority_queue: list[PriorityItem]
    recent_activity: list[dict]

@router.get("", response_model=DashboardResponse)
async def get_dashboard(db: AsyncSession = Depends(get_db)):
    # Aggregate data from all tables
    ...
```

## Error Handling

```python
from fastapi import HTTPException

# Consistent error responses
def not_found(entity: str, id: str):
    raise HTTPException(status_code=404, detail=f"{entity} with id '{id}' not found")

def bad_request(message: str):
    raise HTTPException(status_code=400, detail=message)
```

## Frontend API Client

```typescript
// src/lib/api.ts
import axios from 'axios'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || 'http://localhost:8000/api',
  timeout: 10000,
})

// Response interceptor for error handling
api.interceptors.response.use(
  (response) => response,
  (error) => {
    console.error('API Error:', error.response?.data || error.message)
    return Promise.reject(error)
  }
)

export default api
```

## React Query Hook Pattern

```typescript
// src/hooks/useDashboard.ts
import { useQuery } from '@tanstack/react-query'
import api from '@/lib/api'
import type { DashboardResponse } from '@/types'

export function useDashboard() {
  return useQuery({
    queryKey: ['dashboard'],
    queryFn: async () => {
      const { data } = await api.get('/dashboard')
      return data
    },
    refetchInterval: 30_000, // Refresh every 30s
    staleTime: 10_000,
  })
}

// In component:
// const { data, isLoading, error } = useDashboard()
// if (isLoading) return 
```

## Seed Data Script Pattern

```python
# apps/api/seed.py
import asyncio
from datetime import datetime, timedelta
import random
from app.database.engine import engine, async_session
from app.database.models import Base, Site, Alert, ContentPost, PerformanceSnapshot

async def seed():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    
    async with async_session() as db:
        # Create 3-4 realistic sites
        sites = [
            Site(name="Main Website", url="https://cloudfuze.com", health_score=87),
            Site(name="Product Blog", url="https://blog.cloudfuze.com", health_score=92),
            Site(name="EU Regional", url="https://eu.cloudfuze.com", health_score=74),
            Site(name="Campaign Site", url="https://promo.cloudfuze.com", health_score=68),
        ]
        db.add_all(sites)
        await db.flush()
        
        # Generate 50+ alerts with realistic titles
        alert_templates = [
            ("watchdog", "critical", "Broken form submission on /contact"),
            ("watchdog", "warning", "Plugin 'WP Super Cache' has known vulnerability"),
            ("optimizer", "info", "Page '/cloud-guide' is position #9 for 'cloud migration'"),
            ("autopilot", "info", "Generated LinkedIn post for 'Q3 Results'"),
            # ... many more
        ]
        
        # Generate performance snapshots (7 days of data per page)
        # Generate content posts with health scores
        # Generate review items
        
        await db.commit()
    
    print("Seed data created successfully!")

if __name__ == "__main__":
    asyncio.run(seed())
```

## API Endpoint Checklist
Every endpoint must have:
1. Pydantic response model
2. Proper HTTP status code (200 list, 201 create, 204 delete)
3. Async database session via Depends
4. Error handling for not found / bad input
5. Query parameters for filtering (site_id, severity, agent, date_range)
6. Pagination for list endpoints (limit, offset, total count in response)