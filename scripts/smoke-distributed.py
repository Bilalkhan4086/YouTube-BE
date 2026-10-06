"""Exercise a running stack with real conversions (network and FFmpeg required)."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://localhost:8000')
    parser.add_argument('--url', required=True, help='A short YouTube video you can download')
    args = parser.parse_args()
    with httpx.Client(base_url=args.base_url, timeout=45) as client:
        def request(method, url, **kwargs):
            response = client.request(method, url, **kwargs)
            # Avoid logging signed capability URLs on failure.
            if response.is_error:
                raise RuntimeError(f'{method} returned HTTP {response.status_code}')
            return response

        auth = request('POST', '/api/v1/auth', json={'api_key': os.getenv('CLIENT_API_KEY', '')}).json()
        jobs = {}
        for format in ('mp3', 'mp4'):
            init = request('POST', '/api/v1/init', headers={'Authorization': 'Bearer ' + auth['key']},
                           json={'url': args.url, 'format': format}).json()
            job = request('POST', init['convertURL']).json()
            assert request('POST', init['convertURL']).json()['id'] == job['id']
            jobs[format] = job
        deadline = time.monotonic() + 900
        previous = {}
        while jobs:
            if time.monotonic() > deadline:
                raise TimeoutError('Conversion smoke test exceeded 15 minutes')
            for format, job in list(jobs.items()):
                progress = request('GET', job['progressURL']).json()
                if progress['stage'] != previous.get(format):
                    print(format, progress['status'], progress['stage'], flush=True)
                    previous[format] = progress['stage']
                if progress['status'] in {'failed', 'cancelled'}:
                    raise RuntimeError(f"{format}: {progress['error_code']}: {progress['error']}")
                if progress['status'] != 'completed':
                    continue
                redirect = request('GET', job['downloadURL'])
                assert redirect.status_code == 307 and redirect.content == b''
                location = redirect.headers['location']
                parts = urlsplit(location)
                unsigned = urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
                assert client.get(unsigned).status_code == 403, 'Object must be private'
                ranged = request('GET', location, headers={'Range': 'bytes=0-31'})
                assert ranged.status_code == 206 and len(ranged.content) == 32
                head = request('HEAD', job['downloadURL'], follow_redirects=True)
                assert int(head.headers['content-length']) == progress['size_bytes']
                download = request('GET', location)
                assert len(download.content) == progress['size_bytes']
                with tempfile.TemporaryDirectory() as directory:
                    media = Path(directory) / f'result.{format}'
                    media.write_bytes(download.content)
                    probe = json.loads(subprocess.check_output([
                        'ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(media),
                    ]))
                    streams = probe['streams']
                    audio = next(stream for stream in streams if stream['codec_type'] == 'audio')
                    if format == 'mp4':
                        assert any(stream['codec_name'] == 'h264' for stream in streams)
                        assert audio['codec_name'] == 'aac' and audio['channels'] == 2
                        assert audio['sample_rate'] == '48000'
                    else:
                        assert audio['codec_name'] == 'mp3'
                    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(media), '-map', '0:a:0',
                                    '-f', 'null', '-'], check=True, capture_output=True)
                assert progress['stats']['upload_seconds'] >= 0
                print(json.dumps({'format': format, 'storage_host': parts.netloc,
                                  'audio_codec': audio['codec_name'], 'stats': progress['stats']}), flush=True)
                del jobs[format]
            if jobs:
                time.sleep(2)
        print('PASS: both formats, private storage, signed GET/HEAD, byte ranges, codecs, audio decode, stats.')


if __name__ == '__main__':
    main()
