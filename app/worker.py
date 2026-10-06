"""Isolated, resource-bounded download and media normalization pipeline."""

import json
import logging
import math
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import sys

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from app.errors import ConversionFailure, classify_download_error
from app.metrics import ConversionMetrics

logger = logging.getLogger(__name__)
# Prefer compatible streams but allow WebM/VP9/AV1/Opus sources too.
MP4_FORMAT = (
    "bestvideo[height<=720][vcodec^=avc1]+bestaudio[acodec^=mp4a]"
    "/best[height<=720][vcodec^=avc1][acodec^=mp4a]"
    "/bestvideo[height<=720]+bestaudio/best[height<=720]"
)


def limit_bytes(name, default_mb):
    value = int(os.getenv(name, str(default_mb)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value * 1024 * 1024


def probe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise ConversionFailure("invalid_output")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise ConversionFailure("invalid_output") from None


def validate_audio_decode(path):
    """An audio stream header alone does not prove there are decodable samples."""
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-xerror", "-i", str(path),
         "-map", "0:a:0", "-vn", "-progress", "pipe:1", "-nostats", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120,
    )
    decoded_times = []
    for line in result.stdout.splitlines():
        if line.startswith("out_time_us="):
            try:
                decoded_times.append(int(line.partition("=")[2]))
            except ValueError:
                pass
    if result.returncode or not decoded_times or max(decoded_times) <= 0:
        raise ConversionFailure("invalid_audio")


def validate_output(path, output_format, max_duration, max_bytes):
    if not path.is_file() or path.stat().st_size == 0:
        raise ConversionFailure("invalid_output")
    if path.stat().st_size > max_bytes:
        raise ConversionFailure("size_limit")
    metadata = probe(path)
    duration = float(metadata.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0 or duration > max_duration + 2:
        raise ConversionFailure("invalid_output")
    streams = metadata.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    if output_format == "mp3":
        valid = audio.get("codec_name") == "mp3"
    else:
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        valid = (audio.get("codec_name") == "aac" and audio.get("profile") == "LC"
                 and audio.get("channels") == 2 and audio.get("sample_rate") == "48000"
                 and audio.get("disposition", {}).get("default") == 1
                 and video.get("codec_name") == "h264"
                 and video.get("pix_fmt") == "yuv420p" and 0 < video.get("height", 0) <= 720)
    if not valid:
        raise ConversionFailure("invalid_output")
    validate_audio_decode(path)


def normalize_mp4(source, output, max_bytes):
    metadata = probe(source)
    streams = metadata.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    if not video or not audio:
        raise ConversionFailure("format_unavailable")
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
               "-map", "0:v:0", "-map", "0:a:0", "-map_metadata", "-1", "-map_chapters", "-1"]
    # Re-encode compatible sources too: stream copy does not compress video.
    command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-threads", "2",
                "-vf", "scale=-2:trunc(min(720\\,ih)/2)*2", "-pix_fmt", "yuv420p"]
    # Normalize every audio source, including AAC: HE-AAC/surround/default-track
    # differences can otherwise survive a remux and fail in browser players.
    command += ["-c:a", "aac", "-profile:a", "aac_low", "-b:a", "128k", "-ac", "2", "-ar", "48000",
                "-disposition:a:0", "default"]
    # -fs prevents runaway output; reaching the limit is treated as failure below.
    command += ["-movflags", "+faststart", "-fs", str(max_bytes), str(output)]
    subprocess.run(command, check=True)
    if output.stat().st_size >= max_bytes:
        raise ConversionFailure("size_limit")
    # Catch truncated output (including FFmpeg's -fs limit) before publishing it.
    source_duration = float(metadata.get("format", {}).get("duration", 0))
    output_duration = float(probe(output).get("format", {}).get("duration", 0))
    if source_duration > 0 and output_duration < source_duration - 1:
        raise ConversionFailure("invalid_output")


def convert(url: str, directory: Path, bitrate: int, max_duration: int, output_format: str = "mp3"):
    with ConversionMetrics(directory) as metrics:
        return convert_with_metrics(url, directory, bitrate, max_duration, output_format, metrics)


def convert_with_metrics(url, directory, bitrate, max_duration, output_format, metrics):
    def progress(stage):
        metrics.stage(stage)
        temporary = directory / "progress.tmp"
        temporary.write_text(json.dumps({"stage": stage}))
        temporary.replace(directory / "progress.json")

    max_download = limit_bytes("MAX_DOWNLOAD_MB", 512)
    max_output = limit_bytes("MAX_OUTPUT_MB", 512)

    def validate(info, *, incomplete):
        if incomplete:
            return None
        duration = info.get("duration")
        if (info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}
                or duration is None or not math.isfinite(duration) or duration <= 0 or duration > max_duration):
            raise ConversionFailure("duration_limit")
        formats = info.get("requested_formats") or [info]
        if sum(f.get("filesize") or 0 for f in formats) > max_download:
            raise ConversionFailure("size_limit")
        return None

    downloaded = {}

    def check_size(progress):
        name = progress.get("filename", "default")
        downloaded[name] = max(downloaded.get(name, 0), progress.get("downloaded_bytes") or 0)
        metrics.downloaded_bytes = sum(downloaded.values())
        if metrics.downloaded_bytes > max_download:
            raise ConversionFailure("size_limit")

    completed = []
    options = {
        "format": MP4_FORMAT if output_format == "mp4" else "bestaudio/best",
        "outtmpl": str(directory / "source.%(ext)s"),
        "merge_output_format": "mkv",  # Accept mixed codecs before normalizing.
        "noplaylist": True, "quiet": True, "noprogress": True, "cachedir": False,
        "socket_timeout": 20, "retries": 2, "fragment_retries": 2,
        "concurrent_fragment_downloads": 1,
        "match_filter": validate, "progress_hooks": [check_size],
        "post_hooks": [completed.append],
        "postprocessor_hooks": [lambda event: progress("processing")],
        "js_runtimes": {"deno": {}, "node": {}},
        "postprocessors": [] if output_format == "mp4" else [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(bitrate)}],
    }
    progress("downloading")
    logger.info("stage=download format=%s", output_format)
    with YoutubeDL(options) as downloader:
        downloader.download([url])
    if len(completed) != 1:
        raise ConversionFailure("download_failed")
    source = Path(completed[0])
    output = directory / ("video.mp4" if output_format == "mp4" else "audio.mp3")
    if output_format == "mp4":
        progress("encoding")
        logger.info("stage=normalize")
        staging = directory / "encoded.mp4"
        normalize_mp4(source, staging, max_output)
    else:
        staging = source
    progress("validating")
    validate_output(staging, output_format, max_duration, max_output)
    staging.replace(output)
    logger.info("stage=complete bytes=%s", output.stat().st_size)
    return output


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    directory = Path(sys.argv[2])
    parent_pid = os.getppid()

    def watch_parent():
        while True:
            time.sleep(1)
            if os.getppid() != parent_pid:
                # The API died unexpectedly; do not leave a downloader/encoder orphaned.
                if os.getpgrp() == os.getpid():
                    os.killpg(os.getpid(), signal.SIGKILL)
                os._exit(1)

    threading.Thread(target=watch_parent, daemon=True).start()
    try:
        convert(sys.argv[1], directory, int(sys.argv[3]), int(sys.argv[4]), sys.argv[5])
    except Exception as error:
        if isinstance(error, ConversionFailure):
            code = error.code
        elif isinstance(error, DownloadError):
            code = classify_download_error(error)
        else:
            code = "encoding_failed"
        logger.exception("stage=failed code=%s", code)
        (directory / "error.json").write_text(json.dumps({"code": code}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
