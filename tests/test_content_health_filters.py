"""Content Health sort/filter options — the health-status bucket bounds
must stay in sync with the Healthy/Needs work/Poor badge already shown on
the post detail page (health_score >= 70 / >= 40 / else), and the
validation for sort_by/content_type/health_status/analyzed must reject
anything outside the documented allowlist rather than silently no-op.

Specific-issue-category filters are built from score_breakdown (a stable,
already-computed field) rather than the free-text `issues` display strings,
which interpolate live numbers (word counts, day counts, ...) and so can
never be matched reliably as filter keys.
"""
from sqlalchemy.dialects import postgresql

from app.api.optimizer import (
    _ANALYZED_STATES,
    _CONTENT_TYPES,
    _DEFAULT_SORT_DIR,
    _HEALTH_STATUSES,
    _ISSUE_CATEGORIES,
    _SORT_COLUMNS,
    _health_status_bounds,
    _health_status_condition,
    _issue_filter_condition,
)


def _sql(expr) -> str:
    """Compiled SQL with literal values inlined, so the JSON path/key used
    is visible in the assertion rather than hidden behind a bind param."""
    return str(expr.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


class TestHealthStatusBounds:
    def test_healthy_is_70_and_up_with_no_upper_bound(self) -> None:
        assert _health_status_bounds("healthy") == (70, None)

    def test_needs_work_is_40_to_70_exclusive(self) -> None:
        assert _health_status_bounds("needs_work") == (40, 70)

    def test_poor_is_below_40_with_no_lower_bound(self) -> None:
        assert _health_status_bounds("poor") == (None, 40)

    def test_bounds_are_contiguous_and_non_overlapping(self) -> None:
        # Every integer 0-100 must fall into exactly one bucket.
        for score in range(0, 101):
            buckets_matched = []
            for status in ("healthy", "needs_work", "poor"):
                lo, hi = _health_status_bounds(status)
                in_bucket = (lo is None or score >= lo) and (hi is None or score < hi)
                if in_bucket:
                    buckets_matched.append(status)
            assert len(buckets_matched) == 1, f"score {score} matched {buckets_matched}"


class TestSortAndFilterAllowlists:
    def test_every_sort_column_has_a_default_direction(self) -> None:
        assert set(_SORT_COLUMNS) == set(_DEFAULT_SORT_DIR)

    def test_default_directions_are_valid(self) -> None:
        assert all(d in ("asc", "desc") for d in _DEFAULT_SORT_DIR.values())

    def test_content_types_are_post_and_page(self) -> None:
        assert _CONTENT_TYPES == frozenset({"post", "page"})

    def test_health_statuses(self) -> None:
        assert _HEALTH_STATUSES == frozenset({"healthy", "needs_work", "poor"})

    def test_analyzed_states(self) -> None:
        assert _ANALYZED_STATES == frozenset({"analyzed", "never"})

    def test_worst_first_defaults_for_attention_metrics(self) -> None:
        # health_score and word_count default to ascending (worst/thinnest
        # first) — ascending traffic would bury the pages that matter most,
        # so that one stays descending.
        assert _DEFAULT_SORT_DIR["health_score"] == "asc"
        assert _DEFAULT_SORT_DIR["word_count"] == "asc"
        assert _DEFAULT_SORT_DIR["traffic_30d"] == "desc"
        assert _DEFAULT_SORT_DIR["last_analyzed_at"] == "asc"


class TestHealthStatusCondition:
    def test_healthy_is_score_at_least_70_with_no_upper_bound(self) -> None:
        assert _sql(_health_status_condition("healthy")) == "content_posts.health_score >= 70"

    def test_needs_work_is_a_closed_range(self) -> None:
        sql = _sql(_health_status_condition("needs_work"))
        assert "health_score >= 40" in sql
        assert "health_score < 70" in sql
        assert "AND" in sql

    def test_poor_is_score_under_40_with_no_lower_bound(self) -> None:
        assert _sql(_health_status_condition("poor")) == "content_posts.health_score < 40"


class TestIssueCategories:
    def test_exactly_eight_categories(self) -> None:
        assert len(_ISSUE_CATEGORIES) == 8

    def test_every_category_has_a_human_label(self) -> None:
        assert all(isinstance(label, str) and label for label in _ISSUE_CATEGORIES.values())

    def test_every_category_key_produces_a_working_condition(self) -> None:
        # Must not raise for any key in the allowlist — this is the same
        # dict the endpoint validates incoming query params against.
        for key in _ISSUE_CATEGORIES:
            sql = _sql(_issue_filter_condition(key))
            assert "score_breakdown" in sql


class TestIssueFilterCondition:
    def test_thin_content_reads_word_count_status(self) -> None:
        sql = _sql(_issue_filter_condition("thin_content"))
        assert "'word_count'" in sql and "'status'" in sql and "!= 'good'" in sql

    def test_missing_images_reads_images_status(self) -> None:
        sql = _sql(_issue_filter_condition("missing_images"))
        assert "'images'" in sql and "!= 'good'" in sql

    def test_missing_links_reads_links_status(self) -> None:
        sql = _sql(_issue_filter_condition("missing_links"))
        assert "'links'" in sql and "!= 'good'" in sql

    def test_stale_content_reads_freshness_status(self) -> None:
        sql = _sql(_issue_filter_condition("stale_content"))
        assert "'freshness'" in sql and "!= 'good'" in sql

    def test_title_length_reads_title_status(self) -> None:
        sql = _sql(_issue_filter_condition("title_length"))
        assert "'title'" in sql and "!= 'good'" in sql

    def test_heading_structure_matches_warning_only_not_info(self) -> None:
        # "info" means "no headings, but content is short enough that none
        # are expected" — ContentScorer itself doesn't treat that as a
        # problem, so the filter must not either.
        sql = _sql(_issue_filter_condition("heading_structure"))
        assert "'headings'" in sql
        assert "= 'warning'" in sql
        assert "info" not in sql

    def test_missing_meta_description_reads_meta_description_status(self) -> None:
        sql = _sql(_issue_filter_condition("missing_meta_description"))
        assert "'meta_description'" in sql and "!= 'good'" in sql

    def test_missing_faq_schema_reads_schema_markup_faq_recommendation(self) -> None:
        sql = _sql(_issue_filter_condition("missing_faq_schema"))
        assert "'schema_markup'" in sql and "'faq_recommendation'" in sql and "= 'missing'" in sql
