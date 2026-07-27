from fastapi import APIRouter, Depends

from app.api import (
    admin,
    agents,
    app_settings,
    auth,
    auth_users,
    autopilot,
    dashboard,
    flows,
    notifications,
    optimizer,
    review,
    search,
    sites,
    traffic,
    watchdog,
    watched_urls,
    webhook,
)
from app.security.auth import require_admin, require_user

router = APIRouter()

# Auth endpoints: login is public (rate-limited); the rest guard themselves
# per-endpoint. The Google OAuth callback must stay public — Google's browser
# redirect carries no bearer token — and is protected by the signed `state`.
router.include_router(auth_users.router, prefix="/auth", tags=["auth"])
router.include_router(auth.router, prefix="/auth", tags=["auth"])

# WP plugin webhook — public by design; authenticated via per-site HMAC signature.
router.include_router(webhook.router, prefix="/webhook", tags=["webhook"])

# All data/ops routes require a signed-in user.
protected = [Depends(require_user)]
router.include_router(sites.router, prefix="/sites", tags=["sites"], dependencies=protected)
router.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"], dependencies=protected)
router.include_router(watchdog.router, prefix="/watchdog", tags=["watchdog"], dependencies=protected)
router.include_router(optimizer.router, prefix="/optimizer", tags=["optimizer"], dependencies=protected)
router.include_router(autopilot.router, prefix="/autopilot", tags=["autopilot"], dependencies=protected)
router.include_router(review.router, prefix="/review", tags=["review"], dependencies=protected)
router.include_router(agents.router, prefix="/agents", tags=["agents"], dependencies=protected)
router.include_router(notifications.router, prefix="/notifications", tags=["notifications"], dependencies=protected)
router.include_router(search.router, prefix="/search", tags=["search"], dependencies=protected)
router.include_router(traffic.router, prefix="/traffic", tags=["traffic"], dependencies=protected)
router.include_router(app_settings.router, prefix="/settings", tags=["settings"], dependencies=protected)
router.include_router(watched_urls.router, prefix="/watched-urls", tags=["watched-urls"], dependencies=protected)
router.include_router(flows.router, prefix="/flows", tags=["flows"], dependencies=protected)

# Destructive administration requires the admin role.
router.include_router(admin.router, prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])
