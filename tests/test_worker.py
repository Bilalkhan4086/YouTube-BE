import json
import subprocess

import pytest
from yt_dlp import YoutubeDL

from app.errors import ConversionFailure, classify_download_error
from app.worker import MP4_FORMAT, convert, normalize_mp4, validate_output


@pytest.fixture
def webm(tmp_path):
    source = tmp_path / 'input.webm'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:d=0.5',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.5', '-c:v', 'libvpx-vp9',
                    '-c:a', 'libopus', '-shortest', str(source)], check=True)
    return source


@pytest.mark.parametrize("output_format", ["mp4", "mp3"])
def test_complete_worker_with_webm_fallback(webm, tmp_path, monkeypatch, output_format):
    """Real yt-dlp selection/download/post-hooks and FFmpeg; only extraction is local."""
    info = {
        'id': 'local-test', 'title': 'VP9 plus Opus fixture', 'duration': 0.5,
        'extractor': 'fixture', 'webpage_url': 'https://www.youtube.com/watch?v=abcdefghijk',
        'formats': [{'format_id': 'webm-only', 'url': webm.as_uri(), 'ext': 'webm',
                     'vcodec': 'vp9', 'acodec': 'opus', 'width': 160, 'height': 90}],
    }

    class LocalDownloader(YoutubeDL):
        def __init__(self, options):
            super().__init__({**options, 'enable_file_urls': True})

        def download(self, urls):
            self.process_ie_result(info, download=True)
            return 0

    monkeypatch.setattr('app.worker.YoutubeDL', LocalDownloader)
    output_dir = tmp_path / 'job'
    output_dir.mkdir()
    output = convert('https://www.youtube.com/watch?v=abcdefghijk', output_dir, 192, 1800, output_format)
    assert output.name == ('video.mp4' if output_format == 'mp4' else 'audio.mp3')
    validate_output(output, output_format, 1800, 10 * 1024 * 1024)
    stats = json.loads((output_dir / 'stats.json').read_text())
    assert stats['worker_seconds'] > 0
    assert stats['download_seconds'] > 0
    assert stats['processing_seconds'] > 0
    assert stats['cpu_seconds'] > 0
    assert stats['peak_process_memory_bytes'] > 0
    assert stats['source_media_bytes'] == webm.stat().st_size
    assert stats['peak_temp_disk_bytes'] >= output.stat().st_size
    if output_format == 'mp4':
        data = output.read_bytes()
        assert data.index(b'moov') < data.index(b'mdat')  # Fast-start playback.


def test_audio_only_is_not_accepted_as_mp4():
    with YoutubeDL({'quiet': True}) as downloader:
        selected = list(downloader.build_format_selector(MP4_FORMAT)({
            'formats': [{'format_id': 'audio', 'ext': 'm4a', 'vcodec': 'none',
                         'acodec': 'mp4a.40.2', 'url': 'https://example.com/audio'}],
            'has_merged_format': False, 'incomplete_formats': False,
        }))
    assert selected == []


def test_output_limit(webm, tmp_path):
    output = tmp_path / 'out.mp4'
    normalize_mp4(webm, output, 10 * 1024 * 1024)
    with pytest.raises(ConversionFailure) as error:
        validate_output(output, 'mp4', 1800, 1)
    assert error.value.code == 'size_limit'


def test_corrupt_output(tmp_path):
    output = tmp_path / 'video.mp4'
    output.write_bytes(b'not an mp4')
    with pytest.raises(ConversionFailure) as error:
        validate_output(output, 'mp4', 1800, 1024)
    assert error.value.code == 'invalid_output'


@pytest.mark.parametrize('message,code', [
    ('Requested format is not available', 'format_unavailable'),
    ('Sign in to confirm you are not a bot', 'upstream_blocked'),
    ('HTTP Error 403 Forbidden', 'upstream_blocked'),
    ('This video is unavailable', 'video_unavailable'),
    ('Connection reset', 'download_failed'),
])
def test_error_classification(message, code):
    assert classify_download_error(message) == code


def test_split_video_audio_download_preserves_audible_sound(tmp_path, monkeypatch):
    """Exercise the real separate-stream path used by YouTube, not just a muxed fixture."""
    from array import array
    from app.worker import probe

    video = tmp_path / 'video-source.mp4'
    audio = tmp_path / 'audio-source.m4a'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=blue:s=160x90:d=1', '-an', '-c:v', 'libx264', str(video)], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=1', '-c:a', 'aac', '-ac', '6', '-ar', '44100', str(audio)], check=True)
    info = {
        'id': 'split-streams', 'title': 'Separate video and surround AAC audio', 'duration': 1,
        'extractor': 'fixture',
        'formats': [
            {'format_id': 'video', 'url': video.as_uri(), 'ext': 'mp4', 'vcodec': 'avc1.4d400b',
             'acodec': 'none', 'width': 160, 'height': 90},
            {'format_id': 'audio', 'url': audio.as_uri(), 'ext': 'm4a', 'vcodec': 'none',
             'acodec': 'mp4a.40.2', 'abr': 128},
        ],
    }

    class SplitDownloader(YoutubeDL):
        def __init__(self, options):
            super().__init__({**options, 'enable_file_urls': True})

        def download(self, urls):
            self.process_ie_result(info, download=True)
            return 0

    monkeypatch.setattr('app.worker.YoutubeDL', SplitDownloader)
    directory = tmp_path / 'split-job'
    directory.mkdir()
    output = convert('https://www.youtube.com/watch?v=abcdefghijk', directory, 192, 1800, 'mp4')
    stream = next(s for s in probe(output)['streams'] if s['codec_type'] == 'audio')
    assert stream['profile'] == 'LC'
    assert stream['channels'] == 2
    assert stream['sample_rate'] == '48000'
    assert stream['disposition']['default'] == 1
    result = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(output), '-map', '0:a:0',
                             '-f', 'f32le', '-acodec', 'pcm_f32le', '-'], capture_output=True, check=True)
    samples = array('f')
    samples.frombytes(result.stdout)
    assert len(samples) >= 90000  # About one second of stereo PCM at 48 kHz.
    for channel in (samples[::2], samples[1::2]):
        rms = (sum(sample * sample for sample in channel) / len(channel)) ** 0.5
        assert rms > 0.005  # Verify actual signal energy in both channels.


def test_missing_audio_is_rejected(tmp_path):
    from app.worker import validate_audio_decode
    output = tmp_path / 'silent-trackless.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=blue:s=160x90:d=0.2', '-an', '-c:v', 'libx264', str(output)], check=True)
    with pytest.raises(ConversionFailure) as error:
        validate_audio_decode(output)
    assert error.value.code == 'invalid_audio'


def test_deliberate_silence_is_still_valid(tmp_path):
    from app.worker import validate_audio_decode
    output = tmp_path / 'silence.m4a'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'anullsrc=r=48000:cl=stereo', '-t', '0.2', '-c:a', 'aac', str(output)], check=True)
    validate_audio_decode(output)


def test_compresses_compatible_h264_source(tmp_path):
    """Already compatible input must actually shrink, retaining playable A/V."""
    source = tmp_path / 'large.mp4'
    output = tmp_path / 'compressed.mp4'
    subprocess.run([
        'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24:duration=2',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '10', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '192k', '-shortest', str(source),
    ], check=True)
    normalize_mp4(source, output, 10 * 1024 * 1024)
    validate_output(output, 'mp4', 10, 10 * 1024 * 1024)
    assert output.stat().st_size < source.stat().st_size * 0.7
    data = output.read_bytes()
    assert data.index(b'moov') < data.index(b'mdat')
