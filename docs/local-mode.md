# YouTube to MP3 / MP4

FastAPI backend with a persistent conversion queue and a small test UI. Download a
single YouTube video as MP3 audio or MP4 video. Use content you own or have permission
to download.

## Start (macOS / Linux)

Requires Python 3.10+, FFmpeg/ffprobe with libx264 and AAC support, and Deno or a
supported Node.js runtime. The current local environment uses Node.js 22.

```sh
brew install ffmpeg deno
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --timeout-graceful-shutdown 15
```

Open http://localhost:8000 for the UI or http://localhost:8000/docs for API docs.
No separate frontend server is needed. `requirements.lock` pins the tested runtime
versions; `requirements.txt` is the dependency update input. See the
[yt-dlp dependency documentation](https://github.com/yt-dlp/yt-dlp#dependencies)
for JavaScript runtime and EJS requirements.

## Browser flow

The UI submits a background job and polls its status. When the conversion completes,
it gives the media player a server URL and the download link an attachment URL.
It never uses `response.blob()` to load a large MP4 into JavaScript memory. HTTP range
requests support seeking and repeated downloads. Preview and download remain separate:
an unsupported browser preview does not prevent downloading the file. **Play with
sound** explicitly unmutes the player and starts playback from a user gesture.

The address bar stores the job ID, so refreshing returns to the same job. Pending
jobs survive server restarts; completed files remain accessible until expiration.
Interrupted running jobs become failed with an explicit restart error and can be
resubmitted. A disconnect from the browser does not cancel a background job; use
**Cancel conversion**. **Delete saved result** frees storage immediately.

## Completion stats

After an MP3 or MP4 is ready, the test UI shows its server measurements. `GET /jobs/{id}`
returns the same values in `stats`, persisted in SQLite across page/server restarts.
Existing jobs created before this feature show "not recorded"; reconvert to collect stats.

- Total time to ready, queue wait, and server job duration.
- YouTube download time (including metadata extraction) and processing/validation time.
- CPU seconds for the worker plus its reaped child processes, including FFmpeg;
  average CPU uses 100% for one core and can exceed 100%.
- Peak individual process RSS: the maximum of the worker and child-process high-water
  marks, **not** the sum of concurrent processes. macOS/Linux units are normalized to bytes.
- Peak logical size of temporary files sampled every 0.2 seconds and at stage changes;
  this can miss short-lived peaks and is not cumulative disk I/O or allocated disk blocks.
- Source media bytes reported by download progress, excluding HTTP overhead and retry traffic,
  plus final file size. The UI displays binary MiB/GiB.

Worker measurements exclude interpreter startup; server job time includes it. These
stats describe preparing the file on the server, not the browser/OS download-to-disk
completion time. Native browser downloads do not expose that completion event to this page.

## Job API (recommended)

### Submit

`POST /jobs` returns HTTP 202 immediately:

```sh
curl http://localhost:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=YOUR_VIDEO_ID","format":"mp4"}'
```

Input fields:

| Field | Meaning |
| --- | --- |
| `url` | YouTube watch, Shorts, embed, music, or shortened URL |
| `format` | `mp3` (default) or `mp4` |
| `bitrate` | MP3 only: 128, 192 (default), 256, or 320 kbps |

An optional UUID `X-Idempotency-Key` header makes submission retries return the same
job. Reusing a key with different input returns 409. The UI supplies this key.
Idempotency keys expire with their job records.

### Poll, preview, download, cancel, delete

| Endpoint | Behavior |
| --- | --- |
| `GET /jobs/{id}` | State, processing stage, error, size, expiry, and result URLs |
| `GET /jobs/{id}/file` | Inline MP3/MP4; supports byte ranges and repeated requests |
| `GET /jobs/{id}/file?download=true` | Attachment download |
| `HEAD /jobs/{id}/file` | Media headers without the body |
| `POST /jobs/{id}/cancel` | Cancel queued/running work, including its FFmpeg process |
| `DELETE /jobs/{id}` | Delete a finished result; active work must first be cancelled |

Job states: `queued`, `running`, `completed`, `failed`, `cancelled`. Processing stages
include downloading, merging/processing, encoding, and validation. On failure,
`error_code` and `error` remain available with the job ID for support. These distinguish
unavailable videos, blocked requests, missing formats, size limits, timeouts, and
encoding/validation failures. Expired or unknown jobs return 404, and a premature
file request returns 409. Malformed input returns 422; full job storage returns 429.

Job IDs act as unguessable access links in this local app. Do not share them for
private media. Put the complete application, including media routes, behind your
authentication layer before public deployment.

## Conversion pipeline

1. Prefer H.264/AAC sources up to 720p, falling back to downloadable codecs such as
   VP9/Opus. Live streams and unknown/over-limit durations are rejected.
2. Download and merge streams in an isolated job directory. Bound combined source
   bytes, duration, conversion slots, and total processing time.
3. Inspect actual media with ffprobe. Re-encode video as H.264
   with yuv420p, CRF 28, and the veryfast preset. Always normalize audio to AAC-LC,
   stereo, 48 kHz, 128 kbps, and mark
   it as the default track. Video encoding uses two threads.
4. Write MP4 with `faststart`. Verify audio/video codecs, duration, resolution,
   nonempty output, and final size. Decode the entire audio stream with FFmpeg to
   reject empty or undecodable audio before atomically publishing the completed file.
   Silent source content remains valid; no sound is synthesized.
5. Remove source/intermediate files, retain the validated result, and serve it using
   native file responses. Expiration does not delete a file during an active transfer.

Worker stderr is drained continuously with a bounded diagnostic tail. Failure logs
redact signed URLs and include the job/request ID. Worker timeouts and cancellation
kill the process group, including FFmpeg. A parent-process watchdog also stops workers
if the API process dies unexpectedly.

YouTube can still reject a video because of availability, region, IP restrictions,
or sign-in requirements. No credentials or cookie bypass is configured.

## Configuration

Export variables before starting the service; `.env.example` is a reference and is
not loaded automatically. Byte settings use MiB despite the `_MB` variable names.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAX_CONCURRENT_CONVERSIONS` | `2` | Conversion slots shared by jobs and legacy requests |
| `CONVERSION_TIMEOUT_SECONDS` | `600` | Per-job download + merge + encoding deadline |
| `MAX_DURATION_SECONDS` | `1800` | Maximum source duration |
| `MAX_DOWNLOAD_MB` | `512` | Combined source download limit |
| `MAX_OUTPUT_MB` | `512` | Final media size limit |
| `JOB_DATA_DIR` | `.data` beside the app | SQLite database and retained media directories |
| `JOB_RETENTION_SECONDS` | `3600` | Result/error retention after completion |
| `MAX_STORED_JOBS` | `20` | Total queued, running, and retained job records |
| `CORS_ORIGINS` | empty | Comma-separated allowed frontend origins |

SQLite transactions enforce queue capacity. A process lock rejects a second queue
owner for the same data directory: **run one Uvicorn worker**. The queue starts and
stops with the application. Completed results and database files need persistent disk.
The default upper bound is 20 retained jobs of up to 512 MiB each, plus active
conversion intermediates. Set a filesystem quota for a hard disk limit.

## Deployment

This implementation supports a controlled single-host deployment. Run the pinned
dependencies under a supervisor as a non-root user, use persistent local storage,
and configure CPU/memory/disk limits. Protect the app with TLS, authentication,
per-user rate limiting, and request-body limits at your reverse proxy. Avoid response
buffering for media downloads and forward Range headers. Job submission/status
requests are short, so video processing no longer depends on a long HTTP request.

This is not a horizontally distributed queue. For replicas across hosts, use a shared
broker/database, independent workers, and object storage with signed download URLs.
SQLite and the process lock here intentionally support one application process.

`GET /health` reports executable presence; it does not guarantee that YouTube is
reachable. Revalidate dependency updates with the test suite and a live sample,
then regenerate the lockfile before deployment.

## Legacy direct endpoint

`POST /convert` still accepts the same input and returns `audio.mp3` or `video.mp4`
as an attachment after processing. It retains its existing status/error headers and
cancels work on client disconnect. It requires a proxy timeout longer than conversion
plus transfer time. New clients should use `/jobs` for resilient delivery.

## Verification

```sh
pip install -r requirements-dev.txt
pytest -q
```

Tests exercise yt-dlp selection/download/postprocessing on local fixtures, real
VP9/Opus → H.264/AAC encoding, MP3 regression, faststart, output validation,
redacted diagnostics, process cancellation, retained file ranges, repeated downloads,
queue capacity/idempotency, restart recovery, expiration, and cleanup. Automated tests
need FFmpeg but no YouTube network access. Live checks verify the API against short
public videos; availability still depends on the video and deployment IP.
