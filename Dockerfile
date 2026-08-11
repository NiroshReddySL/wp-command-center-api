# Two stages so the runtime image carries neither a compiler nor the test
# suite. The previous single-stage build installed ".[dev]" — pytest, ruff and
# mypy shipped to production — and left gcc and libpq-dev in the final image,
# which is a toolchain waiting for anyone who gets a shell in the container.

# ── Stage 1: build wheels ─────────────────────────────────────────────────────
FROM python:3.12-slim AS build

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app

# Runtime dependencies only. Installed into a prefix that is copied wholesale
# into the runtime stage, so nothing from this stage's toolchain follows it.
RUN pip install --no-cache-dir --prefix=/install .

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# curl is kept deliberately — the container HEALTHCHECK below uses it.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

COPY --from=build /install /usr/local
COPY --chown=appuser:appuser . .

# Read-only to the process that serves traffic: the app never writes to its
# own source tree, so a bug that tries to is a bug worth failing on.
RUN chown -R root:root /app && chmod -R a-w /app

USER appuser

EXPOSE 8000

# Hits /ready, which round-trips the database — a process that is up but
# cannot reach Postgres is not ready to take traffic.
# 127.0.0.1 rather than localhost: in some base images localhost resolves to
# ::1 only, and the check then fails against a container that is serving fine.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/ready || exit 1

# Bounded graceful shutdown: SSE streams stay open indefinitely by design, so
# without this a rolling deploy waits on them rather than draining.
# The dev compose overrides this with --reload.
#
# Deliberately NOT --proxy-headers: the only thing in this app that reads the
# caller's address is the rate limiter, and it already has an explicit switch
# (TRUST_PROXY_HEADERS) whose comment states the exact condition under which
# trusting X-Forwarded-For is safe. Two mechanisms that silently do the same
# job drift, and the uvicorn one would need --forwarded-allow-ips '*' to work
# here, which trusts the header from any peer that can open a socket.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "30"]
