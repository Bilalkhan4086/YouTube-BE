FROM python:3.14-slim-bookworm AS api-deps
WORKDIR /srv/media
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements-api.lock ./
RUN pip install --no-cache-dir -r requirements-api.lock && useradd --uid 10001 --create-home media
FROM api-deps AS api
COPY app ./app
USER media
CMD ["uvicorn", "app.distributed.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

FROM api-deps AS worker
USER root
# Node supplies the JavaScript runtime needed by yt-dlp. No runtime download of code.
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements-distributed.lock ./
RUN pip install --no-cache-dir -r requirements-distributed.lock
COPY app ./app
USER media
CMD ["python", "-m", "app.distributed.worker"]

FROM nginxinc/nginx-unprivileged:1.28-alpine AS frontend
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY app/index.html /usr/share/nginx/html/index.html
