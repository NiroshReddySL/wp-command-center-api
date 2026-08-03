"""Which sites the agents monitor — one definition, shared.

`Site.status` conflates two unrelated things: whether the operator wants a
site managed at all, and whether the last content sync happened to succeed.
A single failed sync sets it to "error" (see `content_sync`), and only a
later *successful* sync sets it back. So selecting `status == "active"`
silently drops a site out of monitoring for precisely the reason it most
needs watching — and it stays dropped for as long as the sync keeps failing,
which for a bad application password is forever.

Monitoring scope is therefore "everything not explicitly deactivated".

This lives in one place because the two callers had already diverged: the
scheduled run used `== "active"` while the manual re-run used
`!= "inactive"`, so a site could look completely dead on the schedule and
then spring to life the moment someone pressed Re-run.
"""
from sqlalchemy import ColumnElement, Select, select

from app.database.models import Site

INACTIVE = "inactive"


def monitored() -> ColumnElement[bool]:
    """The predicate: a site is monitored unless explicitly deactivated."""
    return Site.status != INACTIVE


def select_monitored_sites() -> Select[tuple[Site]]:
    return select(Site).where(monitored())


def select_monitored_site_ids() -> Select[tuple[str]]:
    return select(Site.id).where(monitored())


__all__ = ["INACTIVE", "monitored", "select_monitored_site_ids", "select_monitored_sites"]
