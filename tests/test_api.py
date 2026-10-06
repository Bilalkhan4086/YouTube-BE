import asyncio
from pathlib import Path
import subprocess
import sys

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app.main import ConversionRequest, app, run_conversion


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.main.shutil.which", lambda name: f"/usr/bin/{name}")
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("url", [
    "https://youtu.be/abcdefghijk?t=4",
    "https://www.youtube.com/watch?v=abcdefghijk&list=ignored",
    "https://youtube.com/shorts/abcdefghijk",
    "https://music.youtube.com/watch?v=abcdefghijk",
])
def test_canonical_url(url):
    assert ConversionRequest(url=url).url == "https://www.youtube.com/watch?v=abcdefghijk"


@pytest.mark.parametrize("url", [
    "https://youtube.com.evil.test/watch?v=abcdefghijk", "http://localhost/video",
    "file:///etc/passwd", "https://youtube.com/playlist?list=123",
    "https://youtube.com@evil.test/watch?v=abcdefghijk",
    "https://youtube.com:8443/watch?v=abcdefghijk", "https://youtu.be/invalid",
])
def test_reject_invalid_urls(client, url):
    assert client.post("/convert", json={"url": url}).status_code == 422


@pytest.mark.parametrize("format,filename,media_type", [("mp3", "audio.mp3", "audio/mpeg"), ("mp4", "video.mp4", "video/mp4")])
def test_download_and_cleanup(client, monkeypatch, format, filename, media_type):
    directories = []

    async def fake_convert(payload, directory, timeout, duration):
        directories.append(directory)
        assert payload.format == format
        output = directory / filename
        output.write_bytes(b"ID3-test-audio")
        return output

    monkeypatch.setattr("app.main.run_conversion", fake_convert)
    response = client.post("/convert", json={"url": "https://youtu.be/abcdefghijk", "format": format})
    assert response.status_code == 200
    assert response.content == b"ID3-test-audio"
    assert response.headers["content-type"] == media_type
    assert filename in response.headers["content-disposition"]
    assert "attachment" in response.headers["content-disposition"]
    assert not directories[0].exists()
    assert app.state.active == 0


@pytest.mark.parametrize("failure,status", [(TimeoutError(), 504), (HTTPException(502, "Failed"), 502)])
def test_failure_cleanup(client, monkeypatch, failure, status):
    directories = []

    async def fail(payload, directory, timeout, duration):
        directories.append(directory)
        (directory / "partial.webm").write_bytes(b"partial")
        raise failure

    monkeypatch.setattr("app.main.run_conversion", fail)
    assert client.post("/convert", json={"url": "https://youtu.be/abcdefghijk"}).status_code == status
    assert not directories[0].exists()
    assert app.state.active == 0


def test_capacity_and_validation(client):
    app.state.active = app.state.limit
    response = client.post("/convert", json={"url": "https://youtu.be/abcdefghijk"})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "10"
    assert client.post("/convert", json={"url": "https://youtu.be/abcdefghijk", "bitrate": 42}).status_code == 422


def test_missing_dependencies(client, monkeypatch):
    monkeypatch.setattr("app.main.shutil.which", lambda name: None)
    assert client.get("/health").json()["status"] == "degraded"
    assert client.post("/convert", json={"url": "https://youtu.be/abcdefghijk"}).status_code == 503


def test_timeout_terminates_process(tmp_path, monkeypatch):
    original_spawn = asyncio.create_subprocess_exec
    processes = []

    async def spawn(*args, **kwargs):
        process = await original_spawn(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(TimeoutError):
        asyncio.run(run_conversion(ConversionRequest(url="https://youtu.be/abcdefghijk"), tmp_path, 0.05, 1800))
    assert processes[0].returncode is not None


def test_real_ffmpeg_mp3_conversion(tmp_path):
    """Exercise the actual yt-dlp postprocessor using local generated audio."""
    from yt_dlp import YoutubeDL
    from yt_dlp.postprocessor import FFmpegExtractAudioPP

    source = tmp_path / "sample.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=0.2", str(source)], check=True)
    with YoutubeDL({"quiet": True}) as downloader:
        processor = FFmpegExtractAudioPP(downloader, preferredcodec="mp3", preferredquality="192")
        _, info = processor.run({"filepath": str(source), "ext": "wav", "vcodec": "none"})
    output = Path(info["filepath"])
    assert output.suffix == ".mp3" and output.stat().st_size > 0
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name",
                             "-of", "csv=p=0", str(output)], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "mp3"


def test_invalid_format(client):
    assert client.post("/convert", json={"url": "https://youtu.be/abcdefghijk", "format": "exe"}).status_code == 422


def test_mp3_is_default():
    assert ConversionRequest(url="https://youtu.be/abcdefghijk").format == "mp3"


def test_real_mp4_merge(tmp_path):
    from yt_dlp import YoutubeDL
    from yt_dlp.postprocessor import FFmpegMergerPP

    video = tmp_path / 'source.mp4'
    audio = tmp_path / 'source.m4a'
    output = tmp_path / 'video.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=blue:s=160x90:d=0.2', '-c:v', 'libx264', str(video)], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=0.2', '-c:a', 'aac', str(audio)], check=True)
    with YoutubeDL({'quiet': True}) as downloader:
        FFmpegMergerPP(downloader).run({
            'filepath': str(output), 'ext': 'mp4',
            '__files_to_merge': [str(video), str(audio)],
            'requested_formats': [
                {'vcodec': 'h264', 'acodec': 'none', 'protocol': 'https'},
                {'vcodec': 'none', 'acodec': 'aac', 'protocol': 'https'},
            ],
        })
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_name',
                             '-of', 'csv=p=0', str(output)], capture_output=True, text=True, check=True)
    assert set(result.stdout.split()) == {'h264', 'aac'}


@pytest.mark.parametrize("code,status", [("upstream_blocked", 502), ("duration_limit", 422)])
def test_worker_error_preserves_code_and_redacts_log(tmp_path, monkeypatch, caplog, code, status):
    import json
    original_spawn = asyncio.create_subprocess_exec
    (tmp_path / 'error.json').write_text(json.dumps({'code': code}))

    async def spawn(*args, **kwargs):
        return await original_spawn(sys.executable, '-c',
            'import sys; sys.stderr.write("x" * 40000 + " https://media.example/signed?secret=abc"); sys.exit(1)', **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    with pytest.raises(HTTPException) as error:
        asyncio.run(run_conversion(ConversionRequest(url='https://youtu.be/abcdefghijk'), tmp_path, 5, 7200))
    assert error.value.status_code == status
    assert error.value.headers['X-Error-Code'] == code
    if code == 'duration_limit':
        assert '120 minutes' in error.value.detail
    assert 'secret=abc' not in caplog.text
    assert len(caplog.text) < 18000


def test_disconnect_cancels_conversion(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.main import convert_until_disconnected
    cancelled = []

    async def slow(*args):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.append(True)

    class DisconnectedRequest:
        async def receive(self):
            return {"type": "http.disconnect"}

    monkeypatch.setattr('app.main.run_conversion', slow)
    with pytest.raises(HTTPException) as error:
        asyncio.run(convert_until_disconnected(
            ConversionRequest(url='https://youtu.be/abcdefghijk'), tmp_path,
            SimpleNamespace(timeout=5, duration=1800), DisconnectedRequest()))
    assert error.value.status_code == 499
    assert cancelled == [True]
