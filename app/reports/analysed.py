"""What counts as an analysed page.

`ContentPost.health_score` defaults to 50 and only becomes meaningful once
the scorer has actually run, so "score > 0" is not a test of anything — on
this install it admitted 1,045 pages with no breakdown and no word count,
which both inflated coverage and pinned the reported median to the
placeholder value.

An empty `score_breakdown` means no analysis happened. That is the test.
"""
from sqlalchemy import Text, cast

from app.database.models import ContentPost

ANALYSED = cast(ContentPost.score_breakdown, Text) != "{}"

__all__ = ["ANALYSED"]
