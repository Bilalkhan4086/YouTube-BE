# Backend Code Audit

## Public anonymous release update — 2026-10-06

The first release is public and requires no login. `AUTH_MODE=anonymous` is now an
explicit production mode, independent of the development bypass. Identity exchange
remains optional; earlier login-integration release gates below are superseded for
this product.

- [x] Automatic 15-minute anonymous sessions with signed job capabilities.
- [x] Default five new submissions per hour and two queued/running jobs per IPv4
  address or IPv6 /64; limits persist across session renewal and result deletion.
- [x] Active-job admission serialized under the existing PostgreSQL capacity lock.
  Eight concurrent requests against a one-job network limit admit exactly one job.
- [x] Sessions/conversion grants bind to a keyed network digest; raw IPs are not stored
  in those claims. Job links remain usable after a visitor changes networks.
- [x] Redis outages fail closed; direct forged proxy/Cloudflare headers cannot override
  request identity. Existing trusted-proxy middleware tests remain applicable.
- [x] UI, Compose and production documentation support the mode without client or
  issuer secrets. No schema changes or dependency additions are required.

**Verification:** 95 tests passed, including five real PostgreSQL tests; two optional
Redis/S3 tests were skipped because those disposable services were not started for
this change. One existing TestClient deprecation warning remains. Compose validated
with synthetic anonymous-production settings. The disposable PostgreSQL cluster was
stopped after tests.

Cloudflare/Heroku origin restrictions, trusted forwarding and edge abuse controls
still require deployment configuration and validation. Shared networks share quotas;
IP limits do not stop attackers with many networks. No deployment or secret changes
were performed. See [production operations](docs/production.md).

## Remediation review — 2026-10-06

**Outcome:** All six confirmed findings have code-level remediations. Production identity
still requires the application's trusted login backend; operational release gates below
remain open. Existing service databases, media and secrets were not accessed or migrated.

| Finding | Implemented change | Verification |
| --- | --- | --- |
| Cleanup blocked by other failures | Independent recovery/publish/cleanup passes; per-job reservation and capped retry backoff; storage calls outside DB locks | Broker outage and bad-object tests; PostgreSQL NOWAIT and parallel-cleanup checks |
| Proxy clients share quotas | Nginx overwrites forwarded client IP; isolated Compose edge address is the only trusted proxy | Trusted/untrusted proxy middleware tests; Compose configuration validation; actual external edge/NAT behavior remains a deployment check |
| Deleted jobs replay as new work | Separate admission tombstones outlive conversion grants, including migration backfill; deleted replays return 410 | Submit/cancel/delete/cleanup/replay regression; populated upgrade tests |
| No schema upgrade path | Frozen SQL revisions, transactional runner, PostgreSQL advisory lock, legacy shape validation and schema-aware readiness | Populated SQLite/PostgreSQL upgrades, concurrent PostgreSQL migration, rollback/retry, repeat upgrade and future-revision rejection |
| SIGTERM interrupts cleanup | Signal handlers stop claims and cancel/await owned work; 150-second container grace | Real child-process SIGTERM test, conversion cleanup, upload cancellation/order tests |
| Shared-key-only auth | Identity assertion exchange with per-user subjects/quotas, single-use 60-second assertions and session/grant revocation; production rejects internal mode | Identity isolation, quota, tampering, expiry, replay and revocation tests |
| Derived job-token timeout | Database existence/expiry is authoritative for signed job capabilities | Long-recovery access and terminal-expiry regression; conversion grants remain time-limited |

Additional changes: replaced worker RAM-backed media scratch storage with disk, added
Kubernetes ephemeral-storage budgets, made S3 lifecycle retention-aware, separated API
secrets from worker/dispatcher/setup environments, exposed service-specific credential
overrides, added database timeouts and sanitized 503 responses, separated liveness from
schema/Redis readiness, and wired API readiness into Compose. No new Python dependencies.

**Verification completed:** 88 tests passed with zero skips when explicitly configured
for the disposable PostgreSQL, Redis and SeaweedFS targets. This includes real FFmpeg
fixtures, PostgreSQL concurrency, atomic Redis quota increments and private S3 signed
GET/HEAD/range downloads. One existing Starlette/httpx deprecation warning remains.
Compose validated using synthetic settings, not the existing secret file. API, worker and frontend images built successfully; the API image applied its packaged
migrations twice to an isolated SQLite file and reported revision 2. The built frontend
passed `nginx -t` in a network-isolated container. A GitHub Actions
workflow now runs the offline and service tests; its hosted execution was not triggered.

The PostgreSQL cluster was created under `/private/tmp` solely for these checks, and
Redis/S3 used separately named, loopback-only containers without existing volume mounts.
The first PostgreSQL attempt hit sandbox restrictions; the disposable cluster was then
started with approved access. Its default SQL_ASCII database was incompatible with the
driver, so tests used a separate UTF-8 database. No production workaround was added. The disposable PostgreSQL cluster and Redis/S3
containers were stopped after verification; no existing stack was restarted.

**Deployment action required:** Follow [production operations](docs/production.md).
This release requires schema revision 2 and a coordinated maintenance upgrade; do not
mix old and new admission/cleanup implementations. No existing deployment was changed.
New job capabilities remain bearer links until row expiry/deletion. Session revocation
invalidates sessions and unused conversion grants, not previously issued job/S3 links;
that distinction is documented in the integration contract.

### Remaining release gates

- [ ] Wire the trusted login backend to identity exchange and disable demo access in the
  actual deployment. Configure TLS, explicit CORS, secret rotation and least-privilege
  database/storage roles. No login provider or public infrastructure was provisioned.
- [ ] Run maximum-size split-stream conversions under actual memory/disk budgets. Compose
  scratch now uses disk but the writable layer has no inherent per-container disk quota;
  configure host quotas/monitoring. Forced worker crashes may leave scratch files until
  container replacement. Kubernetes disk-backed scratch has explicit size/resource budgets.
- [ ] Rehearse backups/PITR/restore and deployment rollback with representative data; wire
  alerts for queue age, stale leases, failed cleanup, low disk, pool pressure and service
  failures. Liveness/readiness do not prove worker or object-storage availability.
- [ ] Verify the actual proxy chain with multiple external clients, rolling shutdown under
  slow/unavailable storage, host-failure recovery and maximum-load behavior. Current tests
  do not establish every network partition/failover scenario or live YouTube availability.
- [ ] Enable the checked-in CI workflow in the actual Git repository and resolve the
  TestClient deprecation in a separately validated dependency update. This workspace
  has no Git metadata; no commit, push or hosted workflow run was performed.

## Original audit — 2026-10-06

This section preserves the initial assessment and evidence. See the remediation review
above for current implementation and verification; original line numbers are historical.

**Assessment:** Ready for controlled local testing; public production deployment needs the release gates below. Existing strengths include durable admission, fenced worker attempts, bounded subprocesses, canonical URL validation, signed job capabilities and direct object-storage downloads.

**Scope:** `app/`, local and distributed APIs, persistence, processing, Docker/Compose/Nginx/KEDA, dependency files, scripts, documentation and tests. Source-level readiness review; not a penetration test or dependency vulnerability scan. `../FE` is outside scope.

**Checks:** From `BE`, `.venv/bin/python -m pytest -q`: baseline 59 passed; after fixes 65 passed. Real FFmpeg fixtures included. One existing Starlette/httpx TestClient deprecation warning remains. No project lint/type-check configuration is supplied. An initial test invocation from the workspace root could not locate `.venv`; rerunning from `BE` succeeded.

**Database target:** No existing service database connected. Tests use disposable temporary SQLite databases and fake Redis/S3. No migrations, live downloads, Docker builds, deployments or live-stack smoke checks run. Secrets and `.data` were not read or changed. `AGENTS.md` is empty. There is no Git metadata in the workspace or `BE`, so pre-existing edits cannot be identified by Git.

### Original confirmed findings — status updated after remediation

- [x] **HIGH — Dispatcher failures block unrelated cleanup** — code remediation recorded in the follow-up below; original evidence retained.
  - **Evidence:** `app/distributed/dispatcher.py:33` publishes before cleanup. An `rpush` exception exits the iteration. At line 46 all expired records share one transaction; an S3 deletion failure at line 50 aborts the remaining batch and rolls back database deletions already made in that batch.
  - **Impact:** A broker outage with pending publishing skips cleanup. A persistently undeletable object can repeatedly block reclamation of other selected records. Retained rows count against admission capacity (`app/distributed/api.py:138`), so submissions can remain at 429 after results expire.
  - **Recommendation:** Independently guard recovery, publishing and cleanup passes. Isolate cleanup per job, add retry/backoff state and avoid holding batch-wide database locks during S3 calls. Preserve retryable records when object deletion fails.
  - **Acceptance:** With pending jobs and Redis down, cleanup still runs. With one failing object key, other expired jobs are removed. Verify with PostgreSQL as well as fakes.

- [x] **HIGH — Proxy clients share authentication quotas** — code remediation recorded in the follow-up below; original evidence retained.
  - **Evidence:** `deploy/nginx.conf:13` proxies without setting `X-Forwarded-For`; `app/distributed/api.py:107` keys its 30/minute auth quota on `request.client.host`. `Dockerfile:8` does not explicitly configure trusted proxies. Anonymous owners also derive from that address.
  - **Impact:** Visitors through the same proxy share its source-address quota, allowing one burst to prevent others authenticating. Anonymous identity/quota similarly collapses onto the proxy address.
  - **Recommendation:** Configure the controlled edge to supply the actual client address and Uvicorn to trust only that edge/network. Define the whole trust chain when multiple proxies exist; do not trust arbitrary public forwarding headers. Production job quotas should use authenticated user IDs.
  - **Acceptance:** Two external clients have separate auth quotas through the proxy, and forged client forwarding headers cannot change identity. See [Uvicorn settings](https://www.uvicorn.org/settings/).

- [x] **MEDIUM — Deletion can invalidate the conversion idempotency guarantee** — code remediation recorded in the follow-up below; original evidence retained.
  - **Evidence:** `app/distributed/api.py:130` accepts conversion authorization for 120 seconds and inserts whenever the job row is absent (line 136). DELETE immediately expires terminal jobs (line 190); dispatcher removes them. No separate idempotency record exists.
  - **Impact:** Submit, cancel, delete, run cleanup, then replay the original still-valid conversion URL: the deleted job can be created again. This contradicts the README's unconditional same-URL/no-new-conversion guarantee.
  - **Recommendation:** Retain an admission tombstone until conversion authorization expires, independently of media retention. Return an expired/terminal result for deleted-job replays. Add this complete sequence as a regression test; introducing tombstones requires a schema/lifecycle decision.

- [x] **MEDIUM — No versioned database upgrade path** — code remediation recorded in the follow-up below; original evidence retained.
  - **Evidence:** `app/distributed/db.py:43` uses `Base.metadata.create_all()` plus singleton initialization. `app/distributed/setup.py:11` invokes it. There is no migration history; the README acknowledges this limitation.
  - **Impact:** Rerunning setup will not apply later column/constraint changes to existing installations. Rolling deployment compatibility and recovery from failed upgrades are undefined.
  - **Recommendation:** Add reviewed migrations, an existing-database baseline strategy and a serialized deployment migration job. Rehearse upgrades against a populated restored database and document restoration/rollback. [Alembic autogeneration](https://alembic.sqlalchemy.org/en/latest/autogenerate.html) produces candidates that require review.

- [x] **MEDIUM — Distributed workers lack graceful SIGTERM handling** — code remediation recorded in the follow-up below; original evidence retained.
  - **Evidence:** `app/distributed/worker.py:159` starts its work loop, catches KeyboardInterrupt but installs no SIGTERM handler. Compose/Kubernetes allow 30 seconds for termination.
  - **Impact:** Container termination does not drain work through application cleanup. Lease recovery protects eventual state, but progress is lost and uploads/temp files may await later cleanup. The child watchdog is not worker draining.
  - **Recommendation:** Stop claiming on SIGTERM, then finish within a configured grace period or cancel and await task cleanup. Retain fencing for forced termination. Test during download, upload and completion commit.

- [x] **MEDIUM — Authentication is still a test-client model** — code remediation recorded in the follow-up below; original evidence retained.
  - **Evidence:** `app/distributed/api.py:104` accepts one configured key or development anonymous sessions; the initializer explicitly generates anonymous demo configuration. Local `app/main.py` routes have no account authentication.
  - **Impact:** There is no account-level identity, revocation, tenant quota or authorization model. Sharing the configured key with public browsers gives them one session subject. Job capabilities still restrict access to their specific job; arbitrary enumeration is not demonstrated.
  - **Recommendation:** Disable anonymous access for deployment, keep the key UI internal and issue user-scoped authorization through an application backend. Expose only the intended API with TLS and edge limits. Define revocation and rotation requirements before adding accounts.

### Risks needing verification

- [ ] **HIGH — Worker temporary files can exhaust its memory limit**
  - **Evidence:** `compose.yaml` allows 2 GiB tmpfs but limits worker RAM to 1536 MiB. `app/worker.py` permits 512 MiB source downloads and 512 MiB outputs; separate streams, merged source, output and encoder memory can overlap. [Docker tmpfs counts against container memory](https://docs.docker.com/engine/storage/tmpfs/).
  - **Verification:** Run maximum-size permitted split-stream conversions in the actual image; measure cgroup memory, disk peaks and OOM events. Prefer bounded disk-backed ephemeral storage or size tmpfs plus process headroom together. Budget against host capacity and replica count.

- [ ] **MEDIUM — Production locking and service behavior remain untested here**
  - **Evidence:** `tests/test_distributed.py` uses SQLite and fake Redis/S3. It cannot validate PostgreSQL `FOR UPDATE`/`SKIP LOCKED`. The real-stack smoke script was not run.
  - **Verification:** CI with disposable PostgreSQL/Redis/S3 should exercise concurrent admission, duplicate claims, dispatcher replicas, lease loss during upload, cancellation vs completion, storage failures, Redis restart and schema upgrades. Run private-object and signed GET/HEAD/range checks. Do not target production data.

- [x] **MEDIUM — Job-token lifetime assumes incomplete runtime bounds** — code remediation recorded in the follow-up below; original evidence retained.
  - **Evidence:** `app/distributed/config.py:57` includes queue wait, conversion timeout times attempts, retention and 300 seconds. Worker execution permits another 120 seconds per attempt; lease/recovery outages are not bounded by this formula. There is no owner-authenticated token refresh endpoint.
  - **Verification:** Simulate supported timeout/attempt/lease settings and delayed recovery. Ensure users retain access throughout promised result retention. Consider owner-verified refresh or a documented absolute job deadline; a larger constant cannot bound infrastructure outages.

- [ ] **MEDIUM — Storage lifecycle and operational configuration need deployment validation**
  - **Evidence:** `app/distributed/setup.py` configures one-day object expiry while job retention has no corresponding upper bound. Services share the runtime environment file. API health checks database/Redis only. Compose has no application healthchecks, backup jobs or alerting configuration.
  - **Verification:** Align lifecycle with promised retention; independently verify bucket privacy, scoped runtime credentials and separate setup privileges. Rehearse backup/restore. Monitor queue age, expired leases, cleanup backlog, worker loss, storage errors and capacity. Separate readiness/liveness and configure database connect/statement/lock deadlines; the engine currently only enables pool pre-ping.

### Fixed and removed in this audit

- [x] **Overdue queue messages could start conversions.** The worker now checks the deadline under the claim lock and records `queue_timeout` without starting an attempt. Regression: `test_worker_rejects_expired_queue_message_without_dispatcher`.
- [x] **Cancel/delete raced with expiry cleanup.** Existence, ownership and expiry are now checked in the same locked transaction as mutation, eliminating the redundant earlier read. Regression tests simulate removal immediately before locked access (404) and expiry (410). These deterministic tests do not replace PostgreSQL concurrency testing.
- [x] **Non-ASCII invalid API keys produced HTTP 500.** Key comparison now uses UTF-8 bytes with `hmac.compare_digest`; invalid Unicode keys return 401. Regression: `test_non_ascii_api_key_is_rejected_without_server_error`.
- [x] **Unused code and duplication removed.** Removed the dispatcher's unused `or_` import and local jobs' unused `TERMINAL` constant after reference checks. The direct conversion endpoint reuses `require_tools()` instead of duplicating executable checks. Removed excess blank lines in that module.
- [x] **Stale encoding documentation corrected.** `docs/local-mode.md` now describes H.264 re-encoding with CRF 28/veryfast and 128 kbps AAC, replacing the obsolete stream-copy/192 kbps description.

The local mode, legacy `/convert` endpoint, test UI, lockfiles and smoke script were retained because they have documented purposes and/or callers/tests. Removing them would require a compatibility decision. No data, secrets, virtual environments or dependencies were deleted.

### Database and transaction review

`media_jobs` owns its payload and attempt state. Its owner is a subject digest, not a foreign key to an account table; `media_capacity` is an independent singleton lock. There are no ORM parent/child relations or cascade policies to repair. SQLite also stores standalone jobs.

Admission locks capacity before counting/inserting and responds after commit. Claims/completion use row locks and attempt tokens. Upload precedes completion commit; unpublished objects receive per-attempt cleanup with lifecycle fallback. Preserve these invariants. Object deletion and database deletion cannot be atomic across systems, so cleanup must remain idempotent.

Individual status/expiry/lease/publish fields have indexes. Consider composite queue-selection indexes only after representative query plans; no missing-index defect is asserted without that evidence. When adding migrations, evaluate named state/attempt/size checks against supported transitions and existing rows.

### Prioritized production plan

1. **Before public access:** establish user identity and trusted proxy behavior, disable demo access, configure TLS/private storage/scoped credentials, and validate worker disk/memory budgets.
2. **Before relying on durability:** isolate cleanup failure domains, retain idempotency tombstones, introduce versioned migrations and backup/restore, and test shutdown/recovery on real infrastructure.
3. **Before scaling:** add concurrency/fault-injection CI, load-test admission/polling/cleanup, set dependency timeouts and alerts, and verify dynamic API discovery. Redis queue length counts notifications, not distinct jobs, because messages may duplicate.
4. **Ongoing:** add lint/type checks and dependency/container scanning; regenerate locks in a clean target-compatible environment and test updates. Resolve the TestClient deprecation as a compatibility change. Keep permitted-content live smoke checks alongside offline media fixtures.

Record evidence for these release gates before production sign-off. The passing local suite alone does not establish production readiness.
