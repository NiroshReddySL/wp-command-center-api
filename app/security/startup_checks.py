"""Refuse to serve real data with development defaults.

Every default in `app/config.py` exists so the project can be cloned and run
without ceremony. Each of those same defaults is a vulnerability once there is
real data behind it, and the failure mode is silent: an app booted with the
shipped SECRET_KEY works perfectly, and stays working right up until someone
notices that the key is in a public repository.

So the checks below run at startup and, when ENVIRONMENT says production,
stop the process rather than log a warning nobody reads. A deployment that
will not start is a problem you have on day one; a deployment that starts
insecurely is a problem you have on the day someone else finds it.

Each message names the variable and how to produce a good value, because a
refusal that does not say what to do is just an outage.
"""
import logging
import secrets

from app.config import Settings

logger = logging.getLogger(__name__)

# Values shipped in the repo, the compose files and .env.example. Anyone can
# read these, so a deployment using one has no secret at all.
KNOWN_PLACEHOLDER_SECRETS = frozenset({
    "",
    "dev-secret-key-change-in-production",
    "change-this-to-a-secure-random-string-in-production",
    "changeme",
    "secret",
})
# Below this, a signing key is guessable by brute force rather than by reading
# the repo. 32 bytes of urlsafe base64 is the documented way to make one.
MIN_SECRET_LENGTH = 32
MIN_ADMIN_PASSWORD_LENGTH = 12
# The password in docker-compose.yml — fine for a local database with seed
# data, not for one holding customer credentials.
DEV_DB_PASSWORD = "wpcc_secret"


def _generate_hint() -> str:
    return (
        'python -c "import secrets; print(secrets.token_urlsafe(48))"'
    )


def fatal_problems(s: Settings) -> list[str]:
    """Configuration that must be corrected before serving production traffic."""
    problems: list[str] = []

    if s.SECRET_KEY in KNOWN_PLACEHOLDER_SECRETS:
        problems.append(
            "SECRET_KEY is a published placeholder. It signs every session token, so "
            "anyone who has read this repository can mint an admin session. "
            f"Generate one with: {_generate_hint()}"
        )
    elif len(s.SECRET_KEY) < MIN_SECRET_LENGTH:
        problems.append(
            f"SECRET_KEY is only {len(s.SECRET_KEY)} characters; use at least "
            f"{MIN_SECRET_LENGTH}. Generate one with: {_generate_hint()}"
        )

    if not s.TOKEN_ENCRYPTION_KEY:
        # Deriving it from SECRET_KEY is cryptographically fine and operationally
        # a trap: rotating the JWT signing key would then silently make every
        # stored WordPress password and Google refresh token undecryptable, with
        # no error at the moment of the mistake.
        problems.append(
            "TOKEN_ENCRYPTION_KEY is unset, so credentials at rest are encrypted with a "
            "key derived from SECRET_KEY — rotating SECRET_KEY would permanently destroy "
            "every stored WordPress password and Google token. Generate a separate key: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )

    if DEV_DB_PASSWORD in s.DATABASE_URL:
        problems.append(
            "DATABASE_URL still contains the development password from docker-compose.yml."
        )

    if s.ALLOW_PRIVATE_URLS:
        problems.append(
            "ALLOW_PRIVATE_URLS is on, which disables the SSRF guard: a site URL could "
            "point at internal services or the cloud metadata endpoint. It exists for "
            "local WordPress development only."
        )

    if s.AUTO_CREATE_SCHEMA:
        problems.append(
            "AUTO_CREATE_SCHEMA is on, which lets any booting process alter the schema. "
            "In production the schema belongs to `alembic upgrade head`, run once as a "
            "deploy step."
        )

    if "*" in s.CORS_ORIGINS or s.FRONTEND_URL.strip() == "*":
        problems.append("CORS_ORIGINS contains a wildcard; list the exact origins instead.")

    if s.ADMIN_PASSWORD and len(s.ADMIN_PASSWORD) < MIN_ADMIN_PASSWORD_LENGTH:
        problems.append(
            f"ADMIN_PASSWORD is shorter than {MIN_ADMIN_PASSWORD_LENGTH} characters."
        )

    return problems


def warnings(s: Settings) -> list[str]:
    """Things that are defensible but usually mistakes. Logged, never fatal —
    each has a legitimate configuration in which it is the right answer."""
    notes: list[str] = []

    if not s.TRUST_PROXY_HEADERS:
        notes.append(
            "TRUST_PROXY_HEADERS is off. If the API sits behind nginx or a load balancer, "
            "every request appears to come from the proxy's address, so rate limits apply "
            "to all users as one — the first busy user locks out everyone. Turn it on ONLY "
            "when the proxy overwrites X-Forwarded-For."
        )
    if not s.FRONTEND_URL.startswith("https://"):
        notes.append(f"FRONTEND_URL is not https ({s.FRONTEND_URL}).")
    if s.OPENAI_API_KEY.startswith("sk-placeholder"):
        notes.append("OPENAI_API_KEY is a placeholder; AI recommendations will fail.")
    if not s.WPSCAN_API_KEY:
        notes.append("WPSCAN_API_KEY is unset; components will report vulnerabilities as unknown.")
    if not s.PSI_API_KEY:
        notes.append(
            "PSI_API_KEY is unset; PageSpeed is limited to ~25 requests/100s per IP and "
            "scores will fall back to TTFB estimates."
        )
    if s.JWT_EXPIRY_HOURS > 24 * 7:
        notes.append(f"JWT_EXPIRY_HOURS is {s.JWT_EXPIRY_HOURS}; sessions live over a week.")
    if not s.ADMIN_PASSWORD:
        notes.append(
            "ADMIN_PASSWORD is unset. If no user exists yet the first-run account cannot "
            "be created and the deployment will have no way to log in."
        )
    return notes


class InsecureConfiguration(RuntimeError):
    """Raised instead of booting with settings that would expose real data."""


def verify_settings(s: Settings) -> None:
    """Called at startup. Fatal in production, advisory everywhere else."""
    problems, notes = fatal_problems(s), warnings(s)

    if not s.is_production:
        if problems:
            logger.warning(
                "%d setting(s) would be refused in production: %s",
                len(problems), " | ".join(problems),
            )
        return

    for note in notes:
        logger.warning("Production check: %s", note)

    if problems:
        listed = "\n".join(f"  {i}. {p}" for i, p in enumerate(problems, 1))
        raise InsecureConfiguration(
            f"Refusing to start: {len(problems)} setting(s) would expose production data.\n"
            f"{listed}\n"
            "Set ENVIRONMENT=development to run with these defaults locally."
        )

    logger.info("Production configuration checks passed")


def suggest_secret() -> str:
    """A value suitable for SECRET_KEY — used by the deploy docs and tests."""
    return secrets.token_urlsafe(48)


__all__ = [
    "InsecureConfiguration",
    "fatal_problems",
    "suggest_secret",
    "verify_settings",
    "warnings",
]
