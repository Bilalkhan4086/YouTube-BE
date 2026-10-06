# Backend contributor instructions

Applies to all files under `BE/`.

Read [docs/coding-standards.md](docs/coding-standards.md) before changing backend code.
It is the canonical source for Python style, API contracts, transactions, worker
correctness, configuration, testing and review rules. Consult
[BACKEND_CODE_AUDIT.md](BACKEND_CODE_AUDIT.md) for known production gaps; do not assume
that documented standards are already implemented.

## Working rules

- Keep changes within the requested scope and preserve existing user work.
- Preserve distributed and local modes unless removal is explicitly requested.
- The distributed API coordinates; workers convert; storage serves media bytes.
- Preserve durable admission, idempotent claims, attempt fencing and retryable cleanup.
- Never expose credentials or signed URLs in code, logs, reports or tests.
- Use disposable targets for tests. Do not inspect secret values or mutate existing
  databases/media merely to verify a code change.
- Check callers, scripts, tests and documented contracts before deleting code.
- Update affected documentation and add meaningful regressions for behavior fixes.

## Verification

From `BE`, use `.venv/bin/python -m pytest -q` for application changes. The suite uses
local generated media and needs FFmpeg/ffprobe. Run focused tests during development.
No lint/type-check tooling is currently configured. Documentation-only changes need
link and command review. Report skipped checks and real-service verification limits;
SQLite/fake-service tests do not establish PostgreSQL concurrency correctness.
