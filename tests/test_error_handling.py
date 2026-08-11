"""What reaches a client when something breaks.

An unhandled exception's message is written by whichever library failed, and
that text has no obligation to be safe — a driver error carries the connection
string, an ORM error carries the SQL. Returning it is a disclosure decision
nobody made.

The opposite failure matters too: a user reporting "it broke" with nothing to
match against the logs. So the response carries a reference and nothing else,
and the log carries the detail.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.security.errors import REQUEST_ID_HEADER, install_error_handlers


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    async def boom() -> dict:
        # Stands in for the shape of a real leak: a driver error carrying a
        # connection string with credentials in it.
        raise RuntimeError("connection to postgresql://wpcc:hunter2@db:5432 failed")

    @app.get("/fine")
    async def fine() -> dict:
        return {"ok": True}

    return app


@pytest.fixture
def client() -> TestClient:
    # raise_server_exceptions=False so the handler runs instead of the test
    # client re-raising, which is what a real client would see.
    return TestClient(_app(), raise_server_exceptions=False)


class TestRequestIds:
    def test_every_response_carries_one(self, client: TestClient) -> None:
        assert client.get("/fine").headers[REQUEST_ID_HEADER]

    def test_ids_differ_between_requests(self, client: TestClient) -> None:
        a = client.get("/fine").headers[REQUEST_ID_HEADER]
        b = client.get("/fine").headers[REQUEST_ID_HEADER]
        assert a != b

    def test_an_inbound_id_is_kept_so_a_proxy_trace_survives(self, client: TestClient) -> None:
        r = client.get("/fine", headers={REQUEST_ID_HEADER: "edge-abc-123"})
        assert r.headers[REQUEST_ID_HEADER] == "edge-abc-123"

    def test_a_hostile_id_is_stripped(self, client: TestClient) -> None:
        # This value ends up in log lines. A header that reaches the log
        # unfiltered is how a log file gets forged entries.
        r = client.get("/fine", headers={REQUEST_ID_HEADER: "abc\tINFO fake-entry <script>"})
        returned = r.headers[REQUEST_ID_HEADER]
        assert all(c.isalnum() or c in "-_" for c in returned)
        assert "<" not in returned and " " not in returned

    def test_an_overlong_id_is_capped(self, client: TestClient) -> None:
        r = client.get("/fine", headers={REQUEST_ID_HEADER: "a" * 5000})
        assert len(r.headers[REQUEST_ID_HEADER]) <= 64


class TestUnhandledErrors:
    def test_production_returns_a_reference_not_the_exception(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.config.settings.ENVIRONMENT", "production")
        r = client.get("/boom")

        assert r.status_code == 500
        body = r.json()
        # The credential in the exception message must not survive to the client.
        assert "hunter2" not in r.text
        assert "postgresql://" not in r.text
        assert "RuntimeError" not in r.text
        assert body["request_id"] == r.headers[REQUEST_ID_HEADER]

    def test_the_reference_is_returned_so_a_user_can_quote_it(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.config.settings.ENVIRONMENT", "production")
        body = client.get("/boom").json()
        assert body["request_id"]
        assert "reference" in body["detail"].lower()

    def test_development_keeps_the_detail(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A stack trace in the browser is worth more than a reference number
        # when the developer is reading both.
        monkeypatch.setattr("app.config.settings.ENVIRONMENT", "development")
        assert "RuntimeError" in client.get("/boom").json()["detail"]

    def test_the_error_is_logged_with_its_reference(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Suppressing the detail is only acceptable because it is kept here.
        monkeypatch.setattr("app.config.settings.ENVIRONMENT", "production")
        with caplog.at_level("ERROR"):
            request_id = client.get("/boom").json()["request_id"]
        assert request_id in caplog.text
        assert "hunter2" in caplog.text
