# Production deployment and recovery

The code now addresses the concrete audit defects. Deploying it still requires the
anonymous abuse controls, credentials, TLS, disk capacity, backups and monitoring described
here. The local demo remains available and is not a public account system.

## Upgrade an existing installation

This release introduces schema revision 2. Do not launch the new API, dispatcher or
worker against an unmigrated database. `/health` returns 503 for a mismatched revision.

1. Take a database backup and verify a restore into a disposable database. Preserve
   object storage and signing configuration as part of the recovery plan.
2. Stop admission, drain/cancel active work, then stop the old API, workers and dispatcher.
   Do not run old and new cleanup/admission implementations simultaneously: the old
   implementation does not maintain the new deletion tombstones.
3. With deployment credentials and the intended `DATABASE_URL` configured, run from `BE`:

   ```sh
   .venv/bin/python -m app.distributed.migrate
   ```

   In Docker, the setup service runs this upgrade before application services start.
   Run setup again when changing retention so its object lifecycle policy is updated.
4. Confirm revision 2, start the new services, verify `/health`, and run an approved
   short conversion and private signed-download check.
5. If validation fails, keep admission stopped. Prefer a forward repair. For rollback,
   restore the pre-upgrade database into a separate target and switch the application
   back with its matching configuration. Reconcile results created after the backup
   before resuming. There is deliberately no destructive automatic downgrade command.

`app/distributed/migrations/001_initial.sql` freezes the original schema.
`002_reliable_cleanup.sql` adds cleanup scheduling and admission tombstones. Existing
unversioned installations are adopted only when expected columns, types, nullability
and primary keys match. Unknown/partial schemas and future revisions fail closed.
The runner uses one transaction and a PostgreSQL advisory lock; SQLite uses
`BEGIN IMMEDIATE` for tests. Failed revisions roll back. Never edit an applied SQL
revision: add the next numbered file and advance `LATEST_REVISION`, with populated-data
upgrade tests. The SQL runner supports simple semicolon-separated statements, not
procedural SQL bodies containing embedded semicolons.

This small forward-only migration runner adds no dependency. If migrations later need
branches or procedural operations, migrate the recorded revision history into a fuller
migration tool explicitly. Do not replace it with `create_all()`.

## Public anonymous access (first release)

Use these API settings for the public downloader:

```text
APP_ENV=production
AUTH_MODE=anonymous
DEV_ALLOW_ANONYMOUS=false
SIGNING_SECRET=<stable random secret of at least 32 characters>
ANONYMOUS_JOBS_PER_HOUR=5
ANONYMOUS_MAX_ACTIVE_JOBS=2
```

No login backend, issuer secret or client API key is required. The existing browser
flow automatically POSTs `{}` to `/api/v1/auth`, obtains a 15-minute session, initializes
a conversion and submits its 120-second grant. Sessions and conversion grants are bound
to the client's network. If that network changes, start a new session. Existing signed
job links continue working on another network until the job expires or is deleted.

Limits are shared by IPv4 address or IPv6 /64, with IPv4-mapped IPv6 normalized. The
subject is a keyed digest; raw IPs are not stored in job records or session claims.
Shared mobile/Wi-Fi networks share these allowances. Rotating a signing secret changes
quota subjects and invalidates capabilities; do not rotate it to reset quotas.

The hourly quota is a fixed 3600-second window starting with the first submission.
Queued and running jobs count toward the active limit; cancellation of a running job
releases its slot only after the worker records a terminal state. PostgreSQL serializes
admission across replicas. Retries of an accepted grant consume no additional quota.
New sessions and deleted results do not reset the hourly allowance. A database failure
after the Redis increment may conservatively consume one allowance. Redis failure
blocks new sessions/submissions with 503 rather than bypassing limits.

The existing 30/minute session-issuance and `JOBS_PER_MINUTE` init/submission limits,
global stored-job capacity, conversion limits and signed job access remain enabled.
These controls do not establish human identity or prevent distributed abuse across
many networks. Add edge limits/challenges as traffic warrants; no Cloudflare account
settings or challenge integration are provisioned by this code change.

The API uses only the client address supplied by the trusted ASGI proxy layer. Never
read arbitrary `CF-Connecting-IP` or leftmost `X-Forwarded-For` values in application
code. The supplied Compose Nginx edge remains supported. For Cloudflare → Heroku,
validate the full forwarding chain and restrict origin bypass before public launch;
the Compose static proxy address is not a Heroku configuration. See edge guidance below.

## Optional user identity integration

For a future login-required deployment, use these alternative API settings:

```text
APP_ENV=production
AUTH_MODE=identity
DEV_ALLOW_ANONYMOUS=false
SIGNING_SECRET=<stable random secret of at least 32 characters>
SESSION_ISSUER_SECRET=<different random secret of at least 32 characters>
```

The trusted application backend must authenticate the user through your login system,
then issue an assertion using the following contract. Never accept the user ID from
an unverified browser field. Keep the issuer secret exclusively in trusted backends;
it authorizes assertions for every user. No identity provider is provisioned here.

```python
# Runs only in the trusted application backend, after user authentication.
import hashlib
import os
import uuid
from itsdangerous import URLSafeTimedSerializer

issuer = URLSafeTimedSerializer(
    os.environ['SESSION_ISSUER_SECRET'],
    signer_kwargs={'digest_method': hashlib.sha256},
)
assertion = issuer.dumps(
    {'sub': authenticated_user_id, 'nonce': str(uuid.uuid4())},
    salt='identity-session',
)
```

`sub` must be a stable, nonempty string of at most 128 characters. Namespace it by
issuer/tenant if IDs are not globally unique. Use synchronized server clocks.
Exchange within 60 seconds via `POST /api/v1/auth` with `{"assertion":"..."}`.
Each assertion is single-use; if a response is lost, request a new assertion. The API
returns the existing 15-minute session-key contract. Initialization/submission quotas
use the authenticated user identity. The old `api_key`/anonymous paths are disabled
in identity mode. The bundled HTML page remains an internal test client; production
clients must use their application's authenticated flow.

To revoke a user's current sessions and unused conversion grants, issue a fresh
assertion with the same `sub`, a new nonce, and purpose `identity-revoke`; POST it to
`/api/v1/auth/revoke`. This endpoint accepts issuer-signed assertions only. Future
login exchanges are allowed; suspend users at the identity provider to prohibit new
logins. Redis stores session generations for 24 hours, extended on exchange; losing
that state invalidates existing identity sessions. Keep Redis durable and private.

Job URLs are separate bearer capabilities. Their validity is checked against the
job's database existence/expiry on every request, so extended recovery cannot make a
retained result inaccessible through an unrelated token timeout. Session revocation
does not revoke already-issued job URLs; cancel and delete the job to revoke those
capabilities. Existing S3 presigned URLs remain usable until their short TTL or object
deletion. Keep signing secrets stable across replicas. Rotating `SIGNING_SECRET`
invalidates all existing capabilities; rotating the issuer secret affects new assertion
exchange. Plan coordinated rotation and user reauthentication.

## Edge and network trust

The supplied Compose edge network assigns Nginx `172.29.247.2`; the API trusts only
that address for forwarded identity. Nginx overwrites `X-Forwarded-For` with its peer
address. API ports are not published. A forged forwarding header sent by a visitor
cannot select their quota identity through this edge.

If this subnet conflicts with your infrastructure, change the subnet, static edge
address and API `FORWARDED_ALLOW_IPS` together. If deploying behind another load
balancer, explicitly configure its trusted addresses and real-client-IP handling at
Nginx, then test the whole chain. Do not set trust to `*`. Terminate TLS at the public
edge and use explicit CORS origins where needed. On Docker Desktop, host NAT can
collapse addresses; quota tests for distinct internet clients belong at the actual
production edge. See [Uvicorn proxy settings](https://www.uvicorn.org/settings/).

## Credentials and storage

Compose passes signing, issuer and internal-client secrets only to the API. Each
service can override `DATABASE_URL` and S3 credentials using the `API_`, `WORKER_`,
`DISPATCHER_` or `SETUP_` prefix; examples are `API_DATABASE_URL` and
`WORKER_S3_ACCESS_KEY`/`WORKER_S3_SECRET_KEY`. Unprefixed values are local-demo fallbacks,
not proof that production privileges are isolated. Provision roles with only their
required operations:

| Service | Required privileges |
| --- | --- |
| API | Admission/job reads and mutations; S3 signing credential with GetObject |
| Worker | Job claim/heartbeat/completion; S3 upload/multipart and attempt deletion |
| Dispatcher | Job recovery, expiry and admission cleanup; S3 DeleteObject |
| Setup | Schema DDL and bucket creation/lifecycle administration |

Use a dedicated private temporary-media bucket and scoped prefixes. Verify anonymous
GET is denied in your provider; setup does not audit an existing bucket's public policy.
It owns the dedicated bucket lifecycle configuration. Expiration is calculated from
retention plus execution/upload allowance, rounded up with an additional day. Exact
result expiry is enforced by the API and cleanup. Abandoned multipart uploads expire
after one day. Changes to retention require rerunning setup before accepting jobs.

Media intermediates now use disk in the Compose worker's writable container layer,
not a 2 GiB RAM-backed mount under a 1536 MiB memory limit. Container removal clears
that layer; process crashes may leave intermediate directories until container
replacement. Provision a disk quota at the host/runtime, monitor free space, and
replace failed worker containers. Kubernetes uses disk-backed `emptyDir` with a 2 GiB
size limit and explicit ephemeral-storage requests/limits. Measure maximum-size
split-stream conversions before selecting worker concurrency and resource budgets.
A Compose writable layer alone is not a hard per-container disk quota.

## Recovery and operations

- Each accepted conversion has an independent admission tombstone for at least the
  remaining 120-second conversion-authorization window. Deleted-result replays return
  410; tombstones do not count against retained-result capacity.
- Recovery, publishing and cleanup each run even if a preceding pass fails. Cleanup
  reserves an expired job for 300 seconds, commits, then calls storage without holding
  its row lock. Failures back off per job up to 300 seconds; process death leaves a
  retryable reservation. Deletion is idempotent.
- SIGTERM/SIGINT stops new worker claims and cancels active work through the existing
  subprocess/upload cleanup path. An interrupted job remains leased for recovery;
  it is not reported as a successful conversion or a user cancellation. Compose and
  Kubernetes allow 150 seconds for cleanup; forced kills still rely on leases and S3
  lifecycle. Test your storage outage behavior against this grace period.
- Database connections have 5-second connect/lock timeouts, 15-second statement timeout
  and 10-second pool checkout timeout. Verify these budgets under load before changing
  them. Migrations touching large tables require a planned timeout/maintenance strategy.
- `/live` checks process responsiveness. `/health` checks the schema revision and Redis;
  it is wired as Compose API readiness. It does not establish worker/storage health.
- Alert on oldest queued job age, expired running leases, cleanup backlog/retries,
  worker exits/OOM, low disk, DB pool pressure, Redis/storage failures and admission 429s.
  Scrape infrastructure metrics with your monitoring system. Alert routing is an
  operator-owned deployment task, not configured by this repository.
- Schedule PostgreSQL backups/PITR and rehearse restores into a separate disposable
  target. Record recovery time/data-loss objectives. Back up roles/configuration too;
  a persistent Docker volume is not a backup. Never test restore against the active DB.

## Verification and CI

The GitHub workflow at `../.github/workflows/backend.yml` runs offline media tests
and isolated PostgreSQL/Redis/SeaweedFS integration tests on Python 3.14. It uses only
throwaway service credentials. This workflow must be installed/enabled in the actual
Git repository to provide a merge gate.

For an explicitly disposable local test environment, configure:

```text
ALLOW_DISPOSABLE_POSTGRES_TESTS=1
TEST_POSTGRES_URL=<disposable PostgreSQL SQLAlchemy URL>
ALLOW_DISPOSABLE_SERVICE_TESTS=1
TEST_REDIS_URL=<disposable Redis URL>
TEST_S3_ENDPOINT=<disposable S3 endpoint>
TEST_S3_ACCESS_KEY=<test credential>
TEST_S3_SECRET_KEY=<test credential>
```

Then run `.venv/bin/python -m pytest -q` from `BE`. Tests create randomly named schemas,
Redis keys and buckets and remove only those artifacts. Never reuse production targets.
Without these variables, real-service tests skip while the offline suite runs normally.
Real-service checks cover concurrent migration/admission/claims, fencing, cleanup lock
release, atomic quotas and private signed GET/HEAD/range downloads. They do not simulate
all network partitions, host crashes, failover, maximum-size loads or real YouTube
availability; those remain deployment acceptance tests in the audit.
