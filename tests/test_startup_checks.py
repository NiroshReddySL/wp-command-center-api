"""The gate between "runs on a laptop" and "holds someone's credentials".

Every default in app/config.py exists so the project can be cloned and run.
Each of them is a vulnerability in front of real data, and the failure mode is
silent — an app booted with the committed SECRET_KEY works perfectly, right up
until someone notices the key is in a public repository.

So these tests pin two things: that production refuses to start on any of
them, and that development still starts on all of them. A guard that made the
dev experience worse would be routed around, and then it would protect
nothing.
"""
import pytest

from app.config import Settings
from app.security.startup_checks import (
    InsecureConfiguration,
    fatal_problems,
    suggest_secret,
    verify_settings,
    warnings,
)

GOOD_SECRET = "8Jx" + "q" * 60
GOOD_FERNET = "dGVzdC1rZXktdGVzdC1rZXktdGVzdC1rZXktdGVzdC0="


def _settings(**over) -> Settings:
    """A configuration that should pass, minus whatever the test breaks."""
    base = {
        "ENVIRONMENT": "production",
        "SECRET_KEY": GOOD_SECRET,
        "TOKEN_ENCRYPTION_KEY": GOOD_FERNET,
        "DATABASE_URL": "postgresql+asyncpg://wpcc:S3cure-p4ss@db:5432/wpcc",
        "ALLOW_PRIVATE_URLS": False,
        "AUTO_CREATE_SCHEMA": False,
        "CORS_ORIGINS": "https://ops.example.com",
        "FRONTEND_URL": "https://ops.example.com",
        "ADMIN_PASSWORD": "a-long-enough-password",
    }
    base.update(over)
    # _env_file=None: the developer's own .env must not decide whether these
    # tests pass.
    return Settings(_env_file=None, **base)


class TestAValidProductionConfig:
    def test_starts(self) -> None:
        verify_settings(_settings())

    def test_has_no_fatal_problems(self) -> None:
        assert fatal_problems(_settings()) == []


class TestSecretKey:
    @pytest.mark.parametrize(
        "value",
        ["dev-secret-key-change-in-production",
         "change-this-to-a-secure-random-string-in-production",
         "changeme", ""],
    )
    def test_published_placeholders_are_refused(self, value: str) -> None:
        # These appear in the repo, the compose files and .env.example, so a
        # deployment using one has no secret at all: anyone who has read the
        # repository can mint an admin session.
        problems = fatal_problems(_settings(SECRET_KEY=value))
        assert any("SECRET_KEY" in p for p in problems)

    def test_a_short_key_is_refused(self) -> None:
        assert any("SECRET_KEY" in p for p in fatal_problems(_settings(SECRET_KEY="abc123")))

    def test_the_suggested_secret_passes_its_own_check(self) -> None:
        # The docs tell operators to generate one this way; it would be a poor
        # joke if the result were then rejected.
        assert fatal_problems(_settings(SECRET_KEY=suggest_secret())) == []


class TestCredentialsAtRest:
    def test_an_underived_encryption_key_is_required(self) -> None:
        # Deriving it from SECRET_KEY is cryptographically fine and an
        # operational trap: rotating the JWT key would then silently destroy
        # every stored WordPress password and Google refresh token, with no
        # error at the moment of the mistake.
        problems = fatal_problems(_settings(TOKEN_ENCRYPTION_KEY=""))
        assert any("TOKEN_ENCRYPTION_KEY" in p for p in problems)
        assert any("rotating SECRET_KEY" in p for p in problems)


class TestOtherFatalDefaults:
    def test_the_development_database_password_is_refused(self) -> None:
        bad = "postgresql+asyncpg://wpcc:wpcc_secret@postgres:5432/wp_command_center"
        assert any("DATABASE_URL" in p for p in fatal_problems(_settings(DATABASE_URL=bad)))

    def test_the_ssrf_guard_may_not_be_disabled(self) -> None:
        # ALLOW_PRIVATE_URLS lets a site URL point at internal services or the
        # cloud metadata endpoint. It exists for local WordPress only.
        problems = fatal_problems(_settings(ALLOW_PRIVATE_URLS=True))
        assert any("ALLOW_PRIVATE_URLS" in p for p in problems)

    def test_schema_creation_on_boot_is_refused(self) -> None:
        # In production the schema belongs to `alembic upgrade head`, run once
        # as a deploy step — not to whichever process happens to boot first.
        problems = fatal_problems(_settings(AUTO_CREATE_SCHEMA=True))
        assert any("AUTO_CREATE_SCHEMA" in p for p in problems)

    def test_a_cors_wildcard_is_refused(self) -> None:
        assert any("CORS" in p for p in fatal_problems(_settings(CORS_ORIGINS="*")))

    def test_a_short_admin_password_is_refused(self) -> None:
        assert any("ADMIN_PASSWORD" in p for p in fatal_problems(_settings(ADMIN_PASSWORD="admin")))

    def test_an_unset_admin_password_is_not_fatal(self) -> None:
        # Legitimate once the first account exists — it is only needed to
        # bootstrap one.
        assert fatal_problems(_settings(ADMIN_PASSWORD="")) == []


class TestRefusalBehaviour:
    def test_production_refuses_to_start(self) -> None:
        with pytest.raises(InsecureConfiguration) as exc:
            verify_settings(_settings(SECRET_KEY="dev-secret-key-change-in-production"))
        message = str(exc.value)
        # A refusal that does not say what to do is just an outage.
        assert "SECRET_KEY" in message
        assert "secrets.token_urlsafe" in message
        assert "ENVIRONMENT=development" in message

    def test_every_problem_is_listed_not_just_the_first(self) -> None:
        # Fixing them one restart at a time is a miserable way to deploy.
        with pytest.raises(InsecureConfiguration) as exc:
            verify_settings(_settings(SECRET_KEY="changeme", ALLOW_PRIVATE_URLS=True,
                                      AUTO_CREATE_SCHEMA=True))
        message = str(exc.value)
        assert "3 setting(s)" in message
        assert all(k in message for k in ("SECRET_KEY", "ALLOW_PRIVATE_URLS", "AUTO_CREATE_SCHEMA"))

    def test_development_still_starts_on_every_default(self) -> None:
        # The whole point of the defaults. A guard that broke `make dev` would
        # be worked around, and then it would protect nothing.
        verify_settings(Settings(_env_file=None, ENVIRONMENT="development"))

    @pytest.mark.parametrize("value", ["production", "PRODUCTION", "prod"])
    def test_production_is_recognised_however_it_is_spelled(self, value: str) -> None:
        assert Settings(_env_file=None, ENVIRONMENT=value).is_production

    @pytest.mark.parametrize("value", ["development", "dev", "staging", ""])
    def test_anything_else_is_not_production(self, value: str) -> None:
        assert not Settings(_env_file=None, ENVIRONMENT=value).is_production


class TestWarnings:
    def test_a_proxied_deployment_without_proxy_headers_is_flagged(self) -> None:
        # Behind nginx every request appears to come from the proxy, so per-IP
        # limits become one shared bucket and the first busy user locks out
        # the team. Not fatal: a direct-to-internet deployment is legitimate.
        notes = warnings(_settings(TRUST_PROXY_HEADERS=False))
        assert any("TRUST_PROXY_HEADERS" in n for n in notes)

    def test_plain_http_frontend_is_flagged(self) -> None:
        assert any("https" in n for n in warnings(_settings(FRONTEND_URL="http://example.com")))

    def test_missing_optional_keys_are_flagged_but_not_fatal(self) -> None:
        s = _settings(WPSCAN_API_KEY="", PSI_API_KEY="")
        assert fatal_problems(s) == []
        notes = " ".join(warnings(s))
        assert "WPSCAN_API_KEY" in notes and "PSI_API_KEY" in notes

    def test_a_healthy_config_still_warns_about_nothing_important(self) -> None:
        s = _settings(TRUST_PROXY_HEADERS=True, WPSCAN_API_KEY="k", PSI_API_KEY="k",
                      OPENAI_API_KEY="sk-real")
        assert warnings(s) == []
