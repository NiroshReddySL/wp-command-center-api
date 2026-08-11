# Deploying WP Command Center

The API refuses to start in production with any of its development defaults.
That is deliberate: every default in `apps/api/app/config.py` exists so the
project can be cloned and run, and each one is a vulnerability once there is
real data behind it. A deployment that will not start is a problem you have on
day one; one that starts with the committed `SECRET_KEY` is a problem you have
on the day somebody else finds it.

If startup fails, the log names every setting and how to fix it. Fix them all
and restart — the check lists all problems at once rather than one per attempt.

---

## 1. Generate the secrets

```bash
# Signs every session token
python -c "import secrets; print(secrets.token_urlsafe(48))"

# Encrypts WordPress passwords and Google tokens at rest.
# Must be SEPARATE from SECRET_KEY — see "Rotating keys" below.
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Database password
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

## 2. Write `.env` next to `docker-compose.prod.yml`

```ini
POSTGRES_DB=wp_command_center
POSTGRES_USER=wpcc
POSTGRES_PASSWORD=<generated>

SECRET_KEY=<generated>
TOKEN_ENCRYPTION_KEY=<generated Fernet key>

OPENAI_API_KEY=sk-...
FRONTEND_URL=https://ops.yourdomain.com
CORS_ORIGINS=https://ops.yourdomain.com

# Creates the first account on first boot. In production the app will NOT
# generate one, because generating it means logging a live admin credential.
ADMIN_EMAIL=you@yourdomain.com
ADMIN_PASSWORD=<at least 12 characters>

# Google OAuth — the redirect URI must match the Cloud Console entry exactly
GA_CLIENT_ID=
GA_CLIENT_SECRET=
GA_REDIRECT_URI=https://ops.yourdomain.com/api/auth/google/callback
GSC_CLIENT_ID=
GSC_CLIENT_SECRET=

# Optional. Each one that is missing degrades a feature rather than breaking it;
# the startup log says which.
WPSCAN_API_KEY=          # without it, components report vulnerabilities as unknown
PSI_API_KEY=             # without it, PageSpeed is ~25 req/100s and falls back to TTFB
TEAMS_WEBHOOK_URL=

# Bind the HTTP container to localhost and terminate TLS in front of it.
# Set to 0.0.0.0 ONLY if nothing else is fronting this host.
HTTP_BIND=127.0.0.1
HTTP_PORT=80
```

`.env` is gitignored. Keep it out of the image and off the repo.

## 3. Deploy

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs -f api
```

Expect `Production configuration checks passed`. The `migrate` service runs
`alembic upgrade head` to completion before the API starts — deliberately a
separate one-shot service, because two API replicas running migrations
concurrently is how a schema gets half-applied.

## 4. Put TLS in front

The dashboard container serves plain HTTP on `127.0.0.1:80`. Terminate TLS
with whatever you already run (a host nginx, Caddy, a load balancer,
Cloudflare) and proxy to it. Then uncomment the HSTS line at the bottom of
`apps/dashboard/nginx.conf` — not before, because it locks browsers into https
for a year against a host that cannot yet serve it.

## 5. Verify

```bash
curl -fsS https://ops.yourdomain.com/api/../health   # 200
curl -o /dev/null -w '%{http_code}\n' https://ops.yourdomain.com/docs   # 404
```

`/docs`, `/redoc` and `/openapi.json` are closed in production. They enumerate
every route, parameter and schema — a gift to a developer and a map to anyone
probing the deployment.

---

## What is already handled

| | |
|---|---|
| Auth | bcrypt passwords, PyJWT HS256 sessions, every data route behind `require_user`, admin routes behind `require_admin`; expiry, `alg:none`, foreign-key and payload-tamper rejection covered by tests |
| Secrets at rest | WordPress passwords and Google tokens Fernet-encrypted in the database |
| Secrets in transit to the image | `.dockerignore` excludes `.env`; no secret has ever been committed (history scanned) |
| SSRF | user-supplied URLs must resolve to public addresses; performance re-measure only accepts URLs the site already tracks |
| Rate limiting | per-IP sliding window on login and job endpoints; `TRUST_PROXY_HEADERS` on so limits key on the real client, not on nginx |
| Errors | unhandled exceptions return a request id, never the exception text; the detail goes to the log |
| Containers | non-root (uid 10001), read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, memory and CPU limits, no dev tooling or compiler in the runtime image |
| Network | only the dashboard publishes a port, and only on loopback; the API and database are reachable solely inside the compose network |
| Database | SCRAM-SHA-256 auth, 30s `statement_timeout`, 60s idle-in-transaction timeout, slow-query logging |
| Schema | owned by Alembic in a one-shot service; `AUTO_CREATE_SCHEMA` is refused in production |
| Shutdown | bounded graceful shutdown so SSE streams cannot stall a rolling deploy |
| Browser | CSP, `nosniff`, `DENY` framing, referrer and permissions policy, no `server_tokens` |
| Dependencies | `pip-audit` and `npm audit` clean; both wired into CI on push and weekly |

### Verified, not asserted

Everything above was checked against running containers, not read off the
config. Reproduce any of it:

```bash
# The production guard actually refuses insecure settings
docker run --rm -e ENVIRONMENT=production <api-image> \
  timeout 20 uvicorn app.main:app --port 8000     # expect: "Refusing to start"

# No dev tooling, non-root, read-only rootfs
docker run --rm <api-image> sh -c 'command -v pytest ruff gcc; id -u'
docker exec <api-container> touch /app/x         # expect: Read-only file system

# Security headers survive on the document, not just at server level
curl -sI https://ops.yourdomain.com/ | grep -i content-security-policy
```

CI runs the first three on every push, so a regression fails the build rather
than reaching a deploy.

## What is still yours to decide

**Backups.** Nothing here backs up Postgres. The volume is the only copy.

```bash
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > wpcc-$(date +%F).sql.gz
```

Put that on a schedule, off the host, and restore from it once before you rely
on it.

**Scaling past one API process.** The scheduler and the rate limiter are
in-process singletons. Extra replicas need `ENABLE_SCHEDULER=false` (or every
agent runs N times) and rate limiting moved to the edge.

**Sessions cannot be revoked.** JWTs are stateless and valid until they expire
(`JWT_EXPIRY_HOURS`, default 24). Logging out clears the browser's copy; it does
not invalidate the token. If someone leaves or a token leaks, the options today
are to delete the user (the token then fails the user lookup on the next
request) or rotate `SECRET_KEY`, which signs everyone out at once. A revocation
list would need somewhere shared to keep it — the same decision as moving rate
limiting off-process.

**Image update policy.** Base images are pinned to a major (`postgres:16-alpine`,
`nginx:1.27-alpine`), not to a digest. A digest is stricter but goes stale, and
a pinned digest carrying a known CVE is worse than a moving tag nobody watches.
If you mirror into your own registry, pin digests there, where something is
watching for updates.

**Error tracking and metrics.** Logs go to Docker's json-file driver, capped at
10 MB × 5 per service. There is no Sentry, no metrics endpoint, no alerting on
a failed agent run beyond the Teams notification for critical alerts.

**Log shipping.** Nothing ships logs off the host. If you add it, note that the
API logs the initial admin email at INFO on first boot — never the password.

## Rotating keys

`SECRET_KEY` can be rotated freely; it only invalidates live sessions and
everyone logs in again.

`TOKEN_ENCRYPTION_KEY` cannot. It decrypts every stored WordPress application
password and Google refresh token, so changing it makes all of them
unreadable — the sites keep working until a token needs refreshing, then fail
one at a time. If you must rotate it, re-enter the credentials afterwards.

This is exactly why production refuses to start without an explicit
`TOKEN_ENCRYPTION_KEY`: left unset it is derived from `SECRET_KEY`, and then
rotating the session key silently destroys every stored credential, with no
error at the moment of the mistake.
