"""YouTube media conversion HTTP API. Run with uvicorn app.main:app."""

import asyncio
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.jobs import JobService, JobStore
from app.schemas import ConversionRequest
from app.processor import run_conversion

logger = logging.getLogger("uvicorn.error")



def positive_env(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.active = 0
    app.state.limit = positive_env("MAX_CONCURRENT_CONVERSIONS", 2)
    app.state.timeout = positive_env("CONVERSION_TIMEOUT_SECONDS", 600)
    app.state.duration = positive_env("MAX_DURATION_SECONDS", 1800)
    positive_env("MAX_DOWNLOAD_MB", 512)
    positive_env("MAX_OUTPUT_MB", 512)
    root = Path(os.getenv("JOB_DATA_DIR", str(Path(__file__).resolve().parent.parent / ".data")))
    store = JobStore(root, positive_env("JOB_RETENTION_SECONDS", 3600), positive_env("MAX_STORED_JOBS", 20))

    async def job_converter(data, directory):
        return await run_conversion(ConversionRequest(**data), directory, app.state.timeout, app.state.duration)

    app.state.jobs = JobService(store, app.state, job_converter)
    await app.state.jobs.start()
    try:
        yield
    finally:
        await app.state.jobs.close()


app = FastAPI(title="YouTube to MP3 / MP4", version="1.0.0", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def test_page():
    return FileResponse(Path(__file__).parent / "index.html", media_type="text/html", headers={"Cache-Control": "no-store"})


origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()]
if origins:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["POST", "GET", "DELETE", "HEAD"],
                       allow_headers=["Content-Type", "X-Idempotency-Key"], expose_headers=["Content-Disposition", "X-Request-ID", "X-Error-Code"])


class TemporaryFileResponse(FileResponse):
    """Also remove files if serving the response fails or is cancelled."""

    def __init__(self, path: Path, directory: Path, on_complete=None):
        self.on_complete = on_complete
        super().__init__(path, media_type="video/mp4" if path.suffix == ".mp4" else "audio/mpeg", filename=path.name,
                         headers={"Cache-Control": "no-store"})
        self.directory = directory

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            shutil.rmtree(self.directory, ignore_errors=True)
            if self.on_complete:
                self.on_complete()



async def convert_until_disconnected(payload, directory, state, request):
    async def watch_disconnect():
        # The JSON body has already been consumed by FastAPI. Wait directly for
        # ASGI disconnect rather than polling with nested cancellation scopes.
        while (await request.receive())["type"] != "http.disconnect":
            pass

    conversion = asyncio.create_task(run_conversion(payload, directory, state.timeout, state.duration))
    disconnected = asyncio.create_task(watch_disconnect())
    try:
        done, _ = await asyncio.wait({conversion, disconnected}, return_when=asyncio.FIRST_COMPLETED)
        if conversion in done:
            return await conversion
        raise HTTPException(499, "Client disconnected.")
    finally:
        for task in (conversion, disconnected):
            if not task.done():
                task.cancel()
        await asyncio.gather(conversion, disconnected, return_exceptions=True)


@app.get("/health")
async def health():
    dependencies = {name: bool(shutil.which(name)) for name in ("ffmpeg", "ffprobe")}
    dependencies["javascript_runtime"] = bool(shutil.which("deno") or shutil.which("node"))
    return {"status": "ok" if all(dependencies.values()) else "degraded", "dependencies": dependencies}


@app.post("/convert", response_class=FileResponse, responses={
    200: {"content": {"audio/mpeg": {}, "video/mp4": {}}, "description": "Downloadable MP3 audio or MP4 video"},
    500: {"description": "Encoding or output validation failed"},
    413: {"description": "Download or output exceeds size limit"},
    429: {"description": "Conversion capacity reached"},
    502: {"description": "Video download or conversion failed"},
    503: {"description": "Required executable missing"},
    504: {"description": "Conversion timed out"},
})
async def convert(payload: ConversionRequest, request: Request):
    require_tools()
    state = request.app.state
    # No await between the capacity check and increment: atomic on this event loop.
    if state.active >= state.limit:
        raise HTTPException(429, "Server is busy. Try again shortly.", headers={"Retry-After": "10"})
    state.active += 1
    directory = None
    handed_off = False
    request_id = uuid.uuid4().hex
    def release_capacity():
        state.active -= 1

    try:
        directory = Path(tempfile.mkdtemp(prefix=f"youtube-media-{request_id}-"))
        output = await convert_until_disconnected(payload, directory, state, request)
        response = TemporaryFileResponse(output, directory, on_complete=release_capacity)
        response.headers["X-Request-ID"] = directory.name
        handed_off = True
        return response
    except BaseException as error:
        if directory:
            shutil.rmtree(directory, ignore_errors=True)
        if isinstance(error, TimeoutError):
            logger.warning("conversion_timeout request_id=%s", directory.name if directory else request_id)
            raise HTTPException(504, "Conversion timed out. Try a shorter video.", headers={"X-Error-Code": "timeout", "X-Request-ID": directory.name if directory else request_id}) from None
        raise
    finally:
        if not handed_off:
            release_capacity()


class RetainedFileResponse(FileResponse):
    def __init__(self, path, service, job_id, download):
        super().__init__(path, media_type="video/mp4" if path.suffix == ".mp4" else "audio/mpeg",
                         filename=path.name, content_disposition_type="attachment" if download else "inline",
                         headers={"Cache-Control": "private, no-store", "X-Request-ID": job_id})
        self.service = service
        self.job_id = job_id
        service.pinned[job_id] = service.pinned.get(job_id, 0) + 1

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.service.pinned[self.job_id] -= 1
            if not self.service.pinned[self.job_id]:
                self.service.pinned.pop(self.job_id)


def require_tools():
    if not all(shutil.which(name) for name in ("ffmpeg", "ffprobe")):
        raise HTTPException(503, "Install FFmpeg and ffprobe on the server.")
    if not (shutil.which("deno") or shutil.which("node")):
        raise HTTPException(503, "Install Deno or a supported Node.js runtime on the server.")


@app.post("/jobs", status_code=202)
async def create_job(payload: ConversionRequest, request: Request, x_idempotency_key: uuid.UUID | None = Header(default=None)):
    require_tools()
    service = request.app.state.jobs
    service.store.expire(service.pinned)
    return service.store.public(service.store.create(payload.model_dump(), x_idempotency_key.hex if x_idempotency_key else None))


@app.get("/jobs/{job_id}")
async def job_status(job_id: uuid.UUID, request: Request):
    store = request.app.state.jobs.store
    return store.public(store.get(job_id.hex))


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: uuid.UUID, request: Request):
    return await request.app.state.jobs.cancel(job_id.hex)


@app.delete("/jobs/{job_id}", status_code=204)
async def delete_job(job_id: uuid.UUID, request: Request):
    service = request.app.state.jobs
    job = service.store.get(job_id.hex)
    if job['status'] in {'queued', 'running'}:
        raise HTTPException(409, "Cancel the conversion before deleting it.")
    if service.pinned.get(job_id.hex):
        raise HTTPException(409, "A download is active. Try again after it finishes.")
    service.store.delete(job_id.hex)


@app.api_route("/jobs/{job_id}/file", methods=["GET", "HEAD"], response_class=FileResponse)
async def job_file(job_id: uuid.UUID, request: Request, download: bool = False):
    service = request.app.state.jobs
    job = service.store.get(job_id.hex)
    if job['status'] != 'completed':
        raise HTTPException(409, "The file is not ready. Check the job status.")
    path = service.store.root / job_id.hex / job['filename']
    if not path.is_file():
        raise HTTPException(410, "This file is no longer available. Submit the video again.")
    return RetainedFileResponse(path, service, job_id.hex, download)
