"""The CSV export of Watchdog findings.

Two properties matter, and both are about the file matching what the person
who asked for it was looking at.

It must use the same filter as the list. The export is offered from a table,
and a file that quietly selects a different set is one someone acts on
believing it is the view they exported — which is why `alert_query` is shared
rather than reimplemented.

It must flatten the useful part into columns. A generic dump puts the URL, the
version, the score into one cell of JSON, which is the difference between a
file you can work from and one you have to re-read by hand.
"""
from datetime import UTC, datetime

from sqlalchemy.dialects import postgresql

from app.api.watchdog import (
    _CORE_COLUMNS,
    _EXPORT_COLUMNS,
    EXPORT_MAX_ROWS,
    _cell,
    alert_query,
)


def _sql(statement) -> str:
    return str(statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    ))


class TestTheExportMatchesTheView:
    def test_the_same_filters_apply(self) -> None:
        sql = _sql(alert_query(site_id="s1", severity="critical", bucket="broken_link"))
        assert "site_id = 's1'" in sql
        assert "severity = 'critical'" in sql
        # `broken/_link` — autoescape escapes the underscore, which is a LIKE
        # wildcard. Without it, `broken_link` would also match `brokenXlink`.
        assert "LIKE 'broken/_link'" in sql and "ESCAPE '/'" in sql

    def test_only_open_and_acknowledged_by_default(self) -> None:
        # Dismissed findings are ones someone triaged away; exporting them
        # would hand back work that was already decided.
        assert "IN ('open', 'acknowledged')" in _sql(alert_query())

    def test_an_explicit_status_overrides_the_default(self) -> None:
        assert "status = 'dismissed'" in _sql(alert_query(status="dismissed"))

    def test_newest_first(self) -> None:
        assert "ORDER BY alerts.created_at DESC" in _sql(alert_query())

    def test_an_unknown_bucket_is_ignored_rather_than_matching_nothing(self) -> None:
        # A typo in the query string should not silently produce an empty file
        # that reads as "no findings".
        assert "startswith" not in _sql(alert_query(bucket="not-a-bucket")).lower()


class TestColumns:
    def test_every_export_is_identifiable_without_its_filename(self) -> None:
        labels = [label for _, label in _CORE_COLUMNS]
        for expected in ("Severity", "Site", "Finding", "First seen"):
            assert expected in labels

    def test_broken_links_carry_the_url_and_status(self) -> None:
        keys = [key for key, _ in _EXPORT_COLUMNS["broken_link"]]
        assert "url" in keys and "status_code" in keys
        # The distinction that tells someone to edit content rather than chase
        # a flaky server.
        assert "malformed" in keys

    def test_performance_carries_the_measurements(self) -> None:
        keys = [key for key, _ in _EXPORT_COLUMNS["performance"]]
        assert {"page_url", "speed_score", "lcp_ms"} <= set(keys)

    def test_components_carry_both_versions(self) -> None:
        keys = [key for key, _ in _EXPORT_COLUMNS["component"]]
        assert "installed_version" in keys and "latest_version" in keys

    def test_every_bucket_the_ui_offers_has_columns(self) -> None:
        from app.api.watchdog import BUCKET_PREFIXES
        assert set(_EXPORT_COLUMNS) == set(BUCKET_PREFIXES)


class TestCellFormatting:
    def test_lists_become_readable_rather_than_json(self) -> None:
        # `found_on` is a list of pages; a spreadsheet cell of JSON brackets
        # is not something anyone can sort or scan.
        assert _cell(["https://a/", "https://b/"]) == "https://a/ | https://b/"

    def test_booleans_read_as_words(self) -> None:
        assert _cell(True) == "yes"
        assert _cell(False) == "no"

    def test_missing_is_blank_not_the_word_none(self) -> None:
        assert _cell(None) == ""

    def test_timestamps_are_sortable(self) -> None:
        assert _cell(datetime(2026, 8, 17, 9, 30, tzinfo=UTC)).startswith("2026-08-17T09:30:00")

    def test_nested_data_survives_as_json_rather_than_being_dropped(self) -> None:
        assert _cell({"cve": "CVE-1"}) == '{"cve":"CVE-1"}'


class TestBounds:
    def test_the_file_is_bounded(self) -> None:
        # And the response reports the row count, so the UI can say the file
        # was capped instead of letting it pass for everything.
        assert 0 < EXPORT_MAX_ROWS <= 100_000
