"""Flow Categories — marketer-defined ordered page-pattern journeys,
classified via GA4's Funnel Reports API.

Important scope note (see FlowCategorySnapshot's docstring for the full
reasoning): GA4's standard Data API has no session-identifying dimension at
all, so this operates on GA4's Funnel Reports API instead — aggregate and
USER-scoped, not a literal per-session classification. There is no list of
"the sessions in this category" to drill into (GA4 never exposes individual
sessions/users outside BigQuery Export); the closest honest equivalent is
an optional one-dimension breakdown sliced across every step.
"""
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.engine import get_db
from app.database.models import FlowCategory, FlowCategorySnapshot, FlowCategoryStep, Site, SiteConfig
from app.security.rate_limit import ai_limiter, job_limiter

router = APIRouter()

_MATCH_TYPES = {"contains", "exact", "regex"}
# Curated allowlist rather than free text — an invalid GA4 dimension name
# would otherwise surface as a cryptic 400 from Google, not a clear error.
_BREAKDOWN_DIMENSIONS = {
    "deviceCategory", "sessionDefaultChannelGroup", "country", "sessionSource", "browser",
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_STEPS = 20


# ── Schemas ─────────────────────────────────────────────────────────────────────

class FlowStepIn(BaseModel):
    label: str = Field(min_length=1, max_length=255)
    match_type: str = "contains"
    pattern: str = Field(min_length=1, max_length=512)
    is_directly_followed: bool = False
    within_seconds: int | None = Field(default=None, ge=1, le=86400)

    @field_validator("match_type")
    @classmethod
    def _valid_match_type(cls, v: str) -> str:
        if v not in _MATCH_TYPES:
            raise ValueError(f"match_type must be one of {sorted(_MATCH_TYPES)}")
        return v


class FlowStepResponse(BaseModel):
    id: str
    step_index: int
    label: str
    match_type: str
    pattern: str
    is_directly_followed: bool
    within_seconds: int | None

    model_config = {"from_attributes": True}


class FlowCategoryCreate(BaseModel):
    site_id: str
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    color: str | None = None
    steps: list[FlowStepIn] = Field(min_length=1, max_length=_MAX_STEPS)


class FlowCategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    # description/color: a client explicitly sending null (to clear a
    # previously-set value) must be distinguished from the field being
    # omitted entirely — see update_flow_category, which checks
    # model_fields_set rather than "is not None" for these two.
    description: str | None = None
    color: str | None = None
    is_active: bool | None = None
    # Present -> REPLACES every existing step; absent -> steps untouched.
    # min_length matches create — updating a category down to zero steps
    # would otherwise silently break every future run of it.
    steps: list[FlowStepIn] | None = Field(default=None, min_length=1, max_length=_MAX_STEPS)


class FlowCategoryResponse(BaseModel):
    id: str
    site_id: str
    name: str
    description: str | None
    color: str | None
    is_active: bool
    steps: list[FlowStepResponse]


class FlowSnapshotResponse(BaseModel):
    id: str
    range_start: str
    range_end: str
    step_results: list[dict]
    total_entered: int
    total_completed: int
    conversion_rate: float
    breakdown_dimension: str | None
    breakdown: list[dict]

    model_config = {"from_attributes": True}


class RunFlowRequest(BaseModel):
    start_date: str
    end_date: str
    breakdown_dimension: str | None = None


class FlowDashboardItem(BaseModel):
    category: FlowCategoryResponse
    latest: FlowSnapshotResponse | None
    trend: list[FlowSnapshotResponse]  # daily snapshots, chronological


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _serialize_category(c: FlowCategory) -> FlowCategoryResponse:
    return FlowCategoryResponse(
        id=c.id, site_id=c.site_id, name=c.name, description=c.description,
        color=c.color, is_active=c.is_active,
        steps=[FlowStepResponse.model_validate(s) for s in c.steps],
    )


def _build_steps(steps_in: list[FlowStepIn]) -> list[FlowCategoryStep]:
    return [
        FlowCategoryStep(
            step_index=i, label=s.label.strip(), match_type=s.match_type, pattern=s.pattern.strip(),
            is_directly_followed=s.is_directly_followed, within_seconds=s.within_seconds,
        )
        for i, s in enumerate(steps_in)
    ]


def _resolve_update_fields(payload: FlowCategoryUpdate) -> dict[str, Any]:
    """Which plain (non-steps) FlowCategory attributes an update should
    actually write, and what to write them as.

    description/color use "was this key present in the request at all"
    (`model_fields_set`), not "is it non-null" — a client explicitly
    clearing one of these back to empty sends null on purpose, and that
    must actually take effect rather than being silently ignored as if the
    field were never mentioned. name/is_active have no such case (a null
    name or null is_active is never a meaningful request), so those keep
    the simpler "is not None" check.
    """
    fields_set = payload.model_fields_set
    updates: dict[str, Any] = {}
    if payload.name is not None:
        updates["name"] = payload.name.strip()
    if "description" in fields_set:
        updates["description"] = payload.description
    if "color" in fields_set:
        updates["color"] = payload.color
    if payload.is_active is not None:
        updates["is_active"] = payload.is_active
    return updates


async def _get_category_or_404(category_id: str, db: AsyncSession) -> FlowCategory:
    result = await db.execute(
        select(FlowCategory).options(selectinload(FlowCategory.steps)).where(FlowCategory.id == category_id)
    )
    category = result.scalar_one_or_none()
    if not category:
        raise HTTPException(status_code=404, detail="Flow category not found")
    return category


def _name_conflict_detail(name: str | None) -> str:
    return f'A flow category named "{name}" already exists for this site' if name else "A flow category with that name already exists for this site"


def _raise_for_integrity_error(exc: IntegrityError, name: str | None) -> None:
    """Only report a name conflict when that's actually what failed — an
    IntegrityError from a different constraint (e.g. a transient step-index
    collision from replacing the steps collection) must never be
    misreported as "this name already exists", which sent a user chasing a
    rename that was never the real problem."""
    constraint = getattr(getattr(exc, "orig", None), "constraint_name", None)
    if constraint == "uq_flow_categories_site_name":
        raise HTTPException(status_code=409, detail=_name_conflict_detail(name)) from None
    raise HTTPException(status_code=409, detail="Could not save this flow category — please try again.") from None


# ── Routes ──────────────────────────────────────────────────────────────────────

@router.post("/categories", response_model=FlowCategoryResponse, status_code=201, dependencies=[Depends(job_limiter)])
async def create_flow_category(
    payload: FlowCategoryCreate, db: AsyncSession = Depends(get_db),
) -> FlowCategoryResponse:
    site = await db.get(Site, payload.site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    category = FlowCategory(
        site_id=payload.site_id, name=payload.name.strip(),
        description=payload.description, color=payload.color,
    )
    category.steps = _build_steps(payload.steps)
    db.add(category)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        _raise_for_integrity_error(exc, payload.name)
    return _serialize_category(category)


@router.get("/categories", response_model=list[FlowCategoryResponse])
async def list_flow_categories(site_id: str, db: AsyncSession = Depends(get_db)) -> list[FlowCategoryResponse]:
    result = await db.execute(
        select(FlowCategory).options(selectinload(FlowCategory.steps))
        .where(FlowCategory.site_id == site_id).order_by(FlowCategory.created_at)
    )
    return [_serialize_category(c) for c in result.scalars().all()]


@router.get("/categories/{category_id}", response_model=FlowCategoryResponse)
async def get_flow_category(category_id: str, db: AsyncSession = Depends(get_db)) -> FlowCategoryResponse:
    return _serialize_category(await _get_category_or_404(category_id, db))


@router.patch("/categories/{category_id}", response_model=FlowCategoryResponse)
async def update_flow_category(
    category_id: str, payload: FlowCategoryUpdate, db: AsyncSession = Depends(get_db),
) -> FlowCategoryResponse:
    category = await _get_category_or_404(category_id, db)
    for key, value in _resolve_update_fields(payload).items():
        setattr(category, key, value)
    if payload.steps is not None:
        # Delete the old steps and flush BEFORE building the new ones —
        # not optional. The replacement steps reuse the same step_index
        # values (0, 1, 2, ...) as the ones they replace, and SQLAlchemy's
        # unit-of-work otherwise INSERTs the new rows before DELETEing the
        # old ones in the same flush, which trips uq_flow_category_steps_order
        # (flow_category_id, step_index) even though the end state — once
        # both sides of the swap are done — is perfectly valid. This was
        # surfacing as a wildly misleading "name already exists" 409 on
        # every steps edit, regardless of whether the name changed at all.
        for step in list(category.steps):
            await db.delete(step)
        await db.flush()
        category.steps = _build_steps(payload.steps)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        _raise_for_integrity_error(exc, payload.name)
    return _serialize_category(category)


@router.delete("/categories/{category_id}", status_code=204)
async def delete_flow_category(category_id: str, db: AsyncSession = Depends(get_db)) -> None:
    category = await _get_category_or_404(category_id, db)
    await db.delete(category)
    await db.commit()


@router.post(
    "/categories/{category_id}/run", response_model=FlowSnapshotResponse, dependencies=[Depends(ai_limiter)],
)
async def run_flow_category(
    category_id: str, payload: RunFlowRequest, db: AsyncSession = Depends(get_db),
) -> FlowSnapshotResponse:
    if not (_DATE_RE.match(payload.start_date) and _DATE_RE.match(payload.end_date)):
        raise HTTPException(status_code=422, detail="start_date and end_date must be YYYY-MM-DD")
    if payload.start_date > payload.end_date:
        raise HTTPException(status_code=422, detail="start_date must not be after end_date")
    if payload.breakdown_dimension and payload.breakdown_dimension not in _BREAKDOWN_DIMENSIONS:
        raise HTTPException(
            status_code=422, detail=f"breakdown_dimension must be one of {sorted(_BREAKDOWN_DIMENSIONS)}"
        )

    category = await _get_category_or_404(category_id, db)
    if not category.steps:
        raise HTTPException(status_code=422, detail="This flow category has no steps yet")

    cfg_r = await db.execute(select(SiteConfig).where(SiteConfig.site_id == category.site_id))
    cfg = cfg_r.scalar_one_or_none()
    if not cfg or not cfg.ga_property_id:
        raise HTTPException(status_code=400, detail="Connect Google Analytics for this site first")

    from app.api.auth import get_google_token
    from app.connectors.analytics import AnalyticsConnector

    token = await get_google_token(db)
    if not token:
        raise HTTPException(status_code=400, detail="Connect Google Analytics for this site first")

    ga = AnalyticsConnector(token.access_token)
    steps: list[dict[str, Any]] = [
        {
            "label": s.label, "match_type": s.match_type, "pattern": s.pattern,
            "is_directly_followed": s.is_directly_followed, "within_seconds": s.within_seconds,
        }
        for s in category.steps
    ]
    try:
        result = await ga.run_funnel_report(
            cfg.ga_property_id, steps, payload.start_date, payload.end_date,
            breakdown_dimension=payload.breakdown_dimension,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GA4 funnel query failed: {exc}") from exc

    snapshot = FlowCategorySnapshot(
        flow_category_id=category.id, site_id=category.site_id,
        range_start=payload.start_date, range_end=payload.end_date,
        step_results=result["step_results"], total_entered=result["total_entered"],
        total_completed=result["total_completed"], conversion_rate=result["conversion_rate"],
        breakdown_dimension=payload.breakdown_dimension, breakdown=result["breakdown"],
    )
    db.add(snapshot)
    await db.commit()
    return FlowSnapshotResponse.model_validate(snapshot)


@router.get("/categories/{category_id}/snapshots", response_model=list[FlowSnapshotResponse])
async def list_flow_snapshots(
    category_id: str, limit: int = Query(90, ge=1, le=365), db: AsyncSession = Depends(get_db),
) -> list[FlowSnapshotResponse]:
    await _get_category_or_404(category_id, db)
    result = await db.execute(
        select(FlowCategorySnapshot)
        .where(FlowCategorySnapshot.flow_category_id == category_id)
        .order_by(FlowCategorySnapshot.range_start.desc())
        .limit(limit)
    )
    snapshots = list(reversed(result.scalars().all()))  # chronological, for charting
    return [FlowSnapshotResponse.model_validate(s) for s in snapshots]


@router.get("/dashboard", response_model=list[FlowDashboardItem])
async def get_flows_dashboard(
    site_id: str, trend_days: int = Query(30, ge=1, le=180), db: AsyncSession = Depends(get_db),
) -> list[FlowDashboardItem]:
    """One card per flow category: its latest result plus a daily trend
    series. `trend_days` bounds each category to its own single query — the
    number of flow categories on a site is small (low tens at most), so
    this isn't worth collapsing into one mega-join."""
    result = await db.execute(
        select(FlowCategory).options(selectinload(FlowCategory.steps))
        .where(FlowCategory.site_id == site_id).order_by(FlowCategory.created_at)
    )
    categories = result.scalars().all()

    items: list[FlowDashboardItem] = []
    for category in categories:
        snap_r = await db.execute(
            select(FlowCategorySnapshot)
            .where(
                FlowCategorySnapshot.flow_category_id == category.id,
                # Daily granularity only — an on-demand custom-range run
                # (e.g. "last 90 days" as one snapshot) would otherwise
                # distort a day-by-day trend line.
                FlowCategorySnapshot.range_start == FlowCategorySnapshot.range_end,
            )
            .order_by(FlowCategorySnapshot.range_start.desc())
            .limit(trend_days)
        )
        daily = list(reversed(snap_r.scalars().all()))
        latest = daily[-1] if daily else None
        items.append(FlowDashboardItem(
            category=_serialize_category(category),
            latest=FlowSnapshotResponse.model_validate(latest) if latest else None,
            trend=[FlowSnapshotResponse.model_validate(s) for s in daily],
        ))
    return items
