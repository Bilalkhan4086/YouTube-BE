# Backend engineering standards

These rules apply to changes under `BE/`. They define the expected standard for new
and modified code; they do not claim that all existing code already complies.
Existing production gaps remain tracked in [the audit](../BACKEND_CODE_AUDIT.md).

**Must** means required for the affected change. **Prefer** means the default unless
there is a documented reason to do otherwise. Apply improvements within the task's
scope; do not reformat or redesign unrelated modules to enforce this document.

## Architecture and responsibilities

| Location | Responsibility |
| --- | --- |
| `app/distributed/api.py` | HTTP validation, authorization, admission, status and signed download redirects |
| `app/distributed/db.py` | SQLAlchemy models, database connections and session construction |
| `app/distributed/dispatcher.py` | Durable queue publication, lease recovery and expiration cleanup |
| `app/distributed/worker.py` | Claims, heartbeats, attempt ownership, upload and completion |
| `app/distributed/storage.py` | Object-storage operations and URL signing |
| `app/distributed/security.py` | Token purposes, credential checks and rate limiting |
| `app/distributed/config.py` | Distributed configuration loading and validation |
| `app/processor.py` | Conversion subprocess lifecycle and bounded diagnostics |
| `app/worker.py` | Download, normalization and media validation |
| `app/schemas.py`, `app/errors.py` | Shared input contracts and conversion errors |
| `app/main.py`, `app/jobs.py` | Supported local API and single-host SQLite queue |
| `tests/`, `scripts/`, `deploy/` | Verification, explicit operational tools and deployment configuration |

- The distributed API must not download, transcode or proxy media bytes. Keep those
  operations in workers and object storage.
- Keep route handlers focused on HTTP coordination. Extract reusable behavior when
  it has a clear responsibility; do not add generic repository/service layers simply
  to wrap one function.
- Preserve both documented runtime modes unless the requested change explicitly
  removes one. The local mode must retain its single-process ownership guard.
- Dependencies flow toward shared schemas and processing utilities. Shared modules
  must not import application entry points or initialize external services on import.
- Prefer explicit constructor/function dependencies. Avoid hidden global clients,
  mutable process-wide job state in the distributed API, and circular imports.

## Python style and types

- Use four-space indentation, UTF-8, a final newline, and a preferred line length of
  100 characters for new code. Avoid unrelated line wrapping in existing modules.
- Use `snake_case` for functions/modules/variables, `PascalCase` for classes and
  `UPPER_SNAKE_CASE` for constants. Names should express purpose and units, such as
  `timeout_seconds` or `size_bytes`.
- Group imports as standard library, third-party, then application imports. Remove
  unused imports, variables and unreachable branches. Avoid wildcard imports.
- Match the surrounding quote style. Prefer f-strings for ordinary interpolation;
  use logging arguments for log messages and bound parameters for SQL.
- Add parameter and return annotations to new public functions and materially changed
  interfaces. Prefer specific types, typed models and narrow protocols over `Any` or
  unstructured dictionaries. Keep JSON serialization at defined boundaries.
- Use Pydantic models for HTTP input/output contracts and dataclasses or simple typed
  objects for internal values where useful. Do not introduce a model for every local
  variable or a class for behavior that fits a function.
- Keep functions focused. Extract repeated domain behavior, not coincidentally similar
  expressions. Avoid arbitrary function-length limits and speculative abstractions.
- Explain invariants, units, ownership and non-obvious decisions in docstrings/comments.
  Do not narrate self-explanatory statements or leave commented-out implementations.
- Do not introduce syntax incompatible with a documented supported runtime without
  updating runtime documentation and verifying the affected environment. Docker and
  local-mode compatibility must be evaluated separately.

## API contracts and validation

- Validate untrusted input at the boundary, including lengths, allowed values, URLs,
  identifiers and numeric limits. Keep canonical YouTube URL validation centralized.
- Add explicit response contracts for new endpoints. Keep existing response keys,
  status codes, default formats and capability URLs compatible unless a contract
  change is requested and documented.
- Use mutation methods for state changes. GET/HEAD must not enqueue work or delete data.
- Authenticate and verify job ownership before returning private state or mutating it.
  A job ID alone must not replace distributed authorization.
- For mutations, check existence, ownership, expiry and allowed state within the same
  locked transaction that performs the update.
- Use stable public error codes and actionable messages. Do not expose tracebacks,
  database errors, credentials, signed upstream URLs or storage internals to clients.
- Preserve existing distinctions such as 401 invalid authorization, 404 missing job,
  410 expired distributed job, 409 invalid state and 429 capacity/rate limits. Include
  retry guidance where applicable. Do not force unrelated local contracts to change.
- Specify the lifetime and replay semantics of idempotency keys. A retry must not
  create duplicate work within the promised window, including after result deletion.
- Keep duration, byte, concurrency, retention and queue limits server-authoritative.
  Do not invent conversion percentages when actual progress is unavailable.

## Persistence and job correctness

- PostgreSQL is authoritative for distributed job state; Redis carries work
  notifications. Commit accepted work before acknowledging admission.
- Assume queue notifications can be duplicated or lost. Claim transactionally, fence
  updates with attempt ownership, and preserve durable republishing/recovery.
- Keep transactions short and explicit. Do not share sessions across concurrent tasks
  or threads. Each blocking thread operation must create/use its own session.
- Prefer external network work outside database locks. When ordering requires an
  exception, document the invariant, bounded timeout and failure/retry behavior.
- Upload and validate output before marking a job completed. Use attempt-specific
  object keys; a stale worker must never replace or delete a newer attempt's result.
- Make cleanup idempotent and retryable. Failure to delete one object must not prevent
  unrelated jobs from progressing or expiring. Do not discard the only retry record.
- Parameterize SQL. Enforce required uniqueness, relationships and state constraints
  in the database when adding relevant schema; assess existing data before enforcing.
- Add indexes based on actual filter/join/order paths and query plans. Avoid adding
  indexes or cascades without a demonstrated access pattern or ownership rule.
- Schema changes require a versioned migration, compatibility strategy and verification
  against a populated disposable database. `create_all()` is initialization, not an
  upgrade mechanism. Use the versioned SQL runner in `app/distributed/migrate.py` and follow
  the upgrade procedure in [production operations](production.md).
- Never run destructive tests, reset commands, seeds or migrations against an existing
  service database without explicit authorization for the identified target/operation.

## Async work, resources and cancellation

- Do not execute synchronous database, Redis, S3 or subprocess waits directly on the
  event loop. Use synchronous FastAPI handlers or a bounded, appropriate thread bridge
  for existing blocking clients. Async code is not automatically non-blocking.
- Give external calls and subprocess work finite timeouts. Bound retries, apply
  backoff to infrastructure failures and avoid retrying permanent content failures.
- On cancellation, propagate cancellation after cleanup. Cancel and await owned tasks,
  terminate the conversion process group, and bound diagnostic capture.
- Cancelling a thread await does not stop its underlying operation. Coordinate upload
  cancellation explicitly and await completion before deleting its files or object.
- Manage clients, sessions, files, temporary directories and tasks with clear ownership
  and context managers or `try/finally`. Add shutdown handling for new long-running loops.
- Respect queue deadlines, lease ownership and maximum attempts when changing workers.
  Normal shutdown should stop admission/claims before draining or cancelling work.
- Bound memory, source/output bytes, disk and subprocess concurrency. Account for
  temporary intermediates and process memory together when using tmpfs.
- Preserve media validation before publication, including duration/size, codec and
  audio decoding checks. Never publish partially written output as completed media.

## Configuration, security and logging

- Load configuration explicitly and validate it at startup. Validate dependent settings,
  not just individual positive values: retention, capability lifetime, object lifecycle,
  lease interval and execution limits must form a coherent policy.
- Keep credentials out of code, examples, logs, reports and image layers. Use redacted
  placeholders in documentation and narrowly scoped runtime credentials in deployment.
- The development anonymous bypass and shared-key mode are internal facilities.
  Public anonymous access uses `AUTH_MODE=anonymous` with enforced network quotas and
  signed job capabilities. Identity mode is optional for login-required deployments.
- Keep token purposes separate. Compare credential bytes with constant-time comparison.
  Never log bearer tokens, capability query strings or complete signed storage URLs.
- Trust forwarded client identity only from the configured proxy boundary. Use explicit
  CORS origins for separate frontend origins; CORS is not authentication.
- Run application containers as non-root, retain existing capability restrictions and
  keep object storage private. Do not weaken controls to make a test pass.
- Use module loggers and stable event names with safe job/attempt identifiers. Log an
  exception at the boundary responsible for handling it, not at every layer.
- Catch specific exceptions where possible. Broad catches belong at process/cleanup
  boundaries that log, preserve recoverability and retry deliberately. Never silently
  convert infrastructure failures into successful responses.
- Health, readiness and operational metrics must describe actual service state. Do not
  treat per-conversion statistics as a substitute for queue/worker/cleanup monitoring.

## Dependencies and tooling

- Reuse existing dependencies where practical. Add a package only for a concrete need;
  explain its runtime scope and maintenance cost. Keep API images free of media tools.
- Update dependency inputs and affected lockfiles together. Regenerate locks in a clean,
  target-compatible environment and verify them; do not copy unrelated installed packages
  or manually claim a lockfile was tested when it was not.
- Document configuration defaults, units and compatibility changes in the README or
  mode-specific documentation when code changes them.
- No formatter, linter or type checker is configured in this repository today. The
  conventions above are review rules until tooling is introduced explicitly with a
  checked-in configuration, dependency and documented command. Do not claim those
  automated checks ran or require nonexistent commands.

## Testing and review

Run commands from `BE` using the project virtual environment:

```sh
# Once, when preparing a development environment:
.venv/bin/python -m pip install -r requirements-dev.txt

# Focused checks while changing distributed behavior:
.venv/bin/python -m pytest tests/test_distributed.py -q

# Full regression suite for application changes:
.venv/bin/python -m pytest -q
```

The suite requires FFmpeg/ffprobe and uses generated local media. Default tests must
not depend on live YouTube access, personal credentials or existing service data.

- Test externally observable behavior and important invariants. Bug fixes should have
  regressions that fail for the original defect; avoid tests that only mirror code.
- Use temporary databases/directories and deterministic external-service fakes for unit
  tests. Cover relevant validation, authorization, expiry, cancellation, resource cleanup,
  retries and duplicate requests when modifying those paths.
- SQLite results do not prove PostgreSQL locking correctness. Changes to admission,
  leases or distributed coordination need disposable real-service integration evidence
  before production release; if unavailable, record that limitation explicitly.
- Use generated media to verify actual codecs/playability when processing changes.
  Live smoke checks are separate, require permitted content and an identified test stack.
- Documentation-only changes require link/command consistency review, not a new test
  suite. Do not add tests solely for formatting or trivial dead-import removal.
- Keep changes reviewable and scoped. Preserve existing user work; never reset files
  or delete stored data to obtain a clean test run.
- Remove code only after checking entry points, imports, tests, scripts and documented
  contracts. Deprecate supported behavior deliberately rather than calling it unused.
- Finish with a concise record of behavior changed, checks/results, compatibility or
  migration effects, and unresolved risks. Update the audit when a tracked finding is
  actually resolved, with evidence; preserve useful review history.

## Definition of done

A change is complete when the requested behavior is implemented, affected contracts
and docs agree, relevant checks pass (or limitations are clearly recorded), resources
and failure paths are accounted for, and no unrelated changes or secrets are included.
Production release additionally requires the applicable gates in the backend audit;
a passing local suite does not waive them.
