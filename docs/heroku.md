# Heroku API deployment, then Azure workers

Deploy the contents of `BE/` as the GitHub repository root. Heroku runs the API and
one dispatcher; Azure will later run the Docker conversion worker. This first stage
can verify API/database/Redis connectivity, but cannot complete conversions without
an Azure worker and a configured private object-storage bucket.

## 1. Prepare the GitHub repository

Commit and push these deployment files with the application:

- `Procfile`: web API, dispatcher, and release-time database migration.
- `.python-version`: Python 3.14.
- `requirements.txt` and `requirements-api.lock`: pinned API dependencies.
- `app/`, including migrations and the Heroku ingress adapter.

Use Heroku's Python buildpack for the API. The existing Dockerfile remains the Azure
worker build path. `requirements-local.txt` is now the unpinned local dependency
input; `requirements.lock` still installs the local demo. No media tools are installed
in the Heroku API slug.

The root must contain `Procfile`, not `BE/Procfile`. If your GitHub repository contains
both frontend and backend folders, publish BE as its own repository first. Do not
connect the parent repository and assume Heroku discovers the nested application.

## 2. Create the Heroku app and backing services

In the Heroku dashboard, create an app in Common Runtime, select your region, and
connect the GitHub repository under **Deploy → GitHub**. Select the intended branch;
leave automatic deployments off for the initial setup. Use the Python buildpack.

Before selecting resource plans, check student-credit balance/expiry and the total
recurring price of **web dyno + dispatcher dyno + PostgreSQL + Key-Value Store**.
Credits do not imply all resources are free. This repository provisions no paid
resources automatically. Use a continuously running dispatcher for expiry/recovery.

Attach PostgreSQL and Redis-compatible Key-Value Store, which provide `DATABASE_URL`
and `REDIS_URL`. Select services that permit external TLS connections from the future
Azure worker; Heroku Private/Shield Key-Value Store is not directly reachable that way.
Do not run databases inside dynos. Provision a private S3-compatible bucket separately.
The storage provider has not yet been selected for this project.

## 3. Configure the app

Set these under **Settings → Config Vars**, using actual values privately:

```text
APP_ENV=production
AUTH_MODE=anonymous
DEV_ALLOW_ANONYMOUS=false
HEROKU_PROXY_MODE=direct
SIGNING_SECRET=<stable random secret of at least 32 characters>
S3_ENDPOINT=<provider S3 API URL; omit for AWS S3>
S3_PUBLIC_ENDPOINT=<externally reachable signing endpoint; omit if same as S3_ENDPOINT>
S3_BUCKET=<private dedicated temporary-media bucket>
S3_ACCESS_KEY=<scoped storage credential>
S3_SECRET_KEY=<scoped storage secret>
S3_REGION=<provider region>
JOB_RETENTION_SECONDS=3600
MAX_DURATION_SECONDS=1800
ANONYMOUS_JOBS_PER_HOUR=5
ANONYMOUS_MAX_ACTIVE_JOBS=2
```

Do not paste secrets into chat or commit them. Generate the signing secret using a
password manager. Leave `CONVERSION_API_BASE` unset for same-origin API calls; set
`CORS_ORIGINS` to exact frontend origins if hosting a separate frontend.

Use the managed add-on URLs without copying credentials into the repository.
PostgreSQL URLs are normalized to psycopg 3; Heroku connections default to
`sslmode=require` unless the URL already specifies a mode. Each process has at most
three pooled DB connections; budget for every replica, worker, release and admin client.
The future Azure worker also needs TLS explicitly configured in its database URL.

For **Heroku Key-Value Store's self-signed certificate**, set
`REDIS_SSL_CERT_REQS=none` only with its `rediss://` URL. This follows the provider's
connection guidance: traffic is encrypted, but the server certificate is not verified.
For Redis providers with trusted certificates, omit the setting (default `required`).
Do not change `rediss://` to plaintext `redis://` to solve certificate errors.

This first deployment uses direct HTTPS access to the Heroku domain. The dedicated
entry point trusts only the rightmost address appended by Heroku's router, rejecting
missing/ambiguous addresses. Uvicorn proxy-header processing is disabled to avoid
interpreting the chain twice. Never run this entry point on a directly exposed VM.
Do not enable Cloudflare's proxy yet: its edge addresses would become the quota
identity. Cloudflare requires a separately verified trust chain and origin controls.

## 4. Deploy and initialize

In **Deploy**, manually deploy the chosen branch. The release process runs
`python -m app.distributed.migrate`; a failed migration must be investigated before
starting traffic. For an existing database, follow the backup/drain procedure in
[production.md](production.md) before deploying this release.

Before generating any signed URLs, install/login with the Heroku CLI and enable router
query-string redaction (replace `YOUR_APP` with the real app name):

```sh
heroku features:enable http-router-no-log-query -a YOUR_APP
```

Uvicorn access logging is disabled, but that does not disable Heroku router logging.
Do not attach logging middleware/drains that record authorization or capability URLs.

Configure the bucket's privacy and lifecycle using provider administration. The
application dispatcher handles roughly one-hour cleanup; lifecycle is an orphan
backstop, not an exact one-hour timer. See the storage policy in production.md.
`python -m app.distributed.setup` can configure the dedicated bucket with temporary
administrative credentials, but owns its lifecycle policy; do not give routine dynos
bucket-administration privileges solely to run this step. The release command does
not create a bucket or configure its lifecycle.

Under **Resources**, enable one web dyno and one dispatcher dyno using your reviewed
plans. For this initial app, config vars are shared between those processes, so its
storage credential needs GetObject (signing) and DeleteObject (cleanup). Separate
service credentials require separate apps; the Azure worker will have its own
upload/delete credential. Do not add a conversion worker dyno on Heroku.

## 5. Verify the first stage

Visit `https://YOUR_APP.herokuapp.com/live` and `/health`; both should return HTTP 200.
`/health` verifies database schema and Redis, not storage permissions or an Azure
worker. Check release and dispatcher logs for startup failures without sharing secrets.
The bundled page is served at `/`; avoid submitting conversion jobs until Azure is
connected because queued jobs will eventually expire.

Before public launch, connect the Azure worker to the same database, Redis and bucket;
verify a permitted short conversion, private download, cancellation, expiry/deletion,
and real-client rate limits (including spoofed forwarding headers). Keep the signing
secret stable across API releases. Update external Azure configuration whenever managed
service credentials rotate. No live deployment or end-to-end cloud test has been
performed merely by adding these files.

## Provider references

- [Python deployment and repository layout](https://devcenter.heroku.com/articles/getting-started-with-python)
- [Python runtime selection](https://devcenter.heroku.com/articles/python-runtimes)
- [Process types and scaling](https://devcenter.heroku.com/articles/procfile)
- [PostgreSQL connections](https://devcenter.heroku.com/articles/connecting-heroku-postgres)
- [Key-Value Store TLS and external connections](https://devcenter.heroku.com/articles/connecting-heroku-redis)
- [Router forwarding and query-string redaction](https://devcenter.heroku.com/articles/http-routing)
