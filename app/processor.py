import asyncio
import json
import logging
import os
from pathlib import Path
import re
import signal
import sys
import time

from fastapi import HTTPException
from app.errors import ERRORS
from app.schemas import ConversionRequest

logger = logging.getLogger("uvicorn.error")


async def run_conversion(payload: ConversionRequest, directory: Path, timeout: int, duration: int):
    request_id = directory.name
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.worker", payload.url, str(directory), str(payload.bitrate), str(duration), payload.format,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        start_new_session=True, cwd=str(Path(__file__).resolve().parent.parent),
    )
    tail = bytearray()

    async def drain_stderr():
        while chunk := await process.stderr.read(4096):
            tail.extend(chunk)
            del tail[:-16384]  # Drain continuously, retain only a bounded diagnostic tail.

    reader = asyncio.create_task(drain_stderr())
    async def finish():
        await process.wait()
        await reader

    try:
        await asyncio.wait_for(finish(), timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        raise
    output = directory / ("video.mp4" if payload.format == "mp4" else "audio.mp3")
    if process.returncode or not output.is_file() or output.stat().st_size == 0:
        code = "download_failed"
        try:
            code = json.loads((directory / "error.json").read_text())["code"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if code not in ERRORS:
            code = "download_failed"
        # Remove signed media URLs from diagnostics before logging them.
        diagnostic = re.sub(r"https?://[^\s]+", "[redacted-url]", tail.decode(errors="replace"))
        logger.error("conversion_failed request_id=%s code=%s diagnostics=%s", request_id, code, diagnostic)
        status, message = ERRORS[code]
        if code == "duration_limit":
            message = (f"Choose a recorded video with a known duration of at most {duration / 60:g} minutes. "
                       "Live and upcoming videos are not supported.")
        raise HTTPException(status, message, headers={"X-Request-ID": request_id, "X-Error-Code": code})
    logger.info("conversion_complete request_id=%s format=%s seconds=%.2f bytes=%s",
                request_id, payload.format, time.monotonic() - started, output.stat().st_size)
    return output

