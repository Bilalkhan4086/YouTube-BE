"""Per-conversion server metrics, isolated by the worker process."""

import json
from pathlib import Path
import resource
import sys
import threading
import time


def usage():
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own, children


def cpu_seconds(samples):
    return sum(sample.ru_utime + sample.ru_stime for sample in samples)


def peak_process_bytes(samples, platform=sys.platform):
    # macOS reports bytes; Linux reports KiB. This is the largest individual
    # process high-water mark, NOT simultaneous combined process-tree memory.
    return int(max(sample.ru_maxrss for sample in samples) * (1 if platform == 'darwin' else 1024))


class ConversionMetrics:
    def __init__(self, directory: Path):
        self.directory = directory
        self.started = time.perf_counter()
        self.initial_cpu = cpu_seconds(usage())
        self.stage_name = 'starting'
        self.stage_started = self.started
        self.stages = {}
        self.downloaded_bytes = 0
        self.peak_disk_bytes = 0
        self.disk_lock = threading.Lock()
        self.stop = threading.Event()
        self.sampler = threading.Thread(target=self.sample_loop, daemon=True)

    def __enter__(self):
        self.sampler.start()
        return self

    def sample_disk(self):
        total = 0
        try:
            for path in self.directory.rglob('*'):
                try:
                    if path.is_file() and not path.is_symlink():
                        total += path.stat().st_size
                except OSError:
                    pass  # Files can be renamed/removed by yt-dlp during sampling.
        except OSError:
            return
        with self.disk_lock:
            self.peak_disk_bytes = max(self.peak_disk_bytes, total)

    def sample_loop(self):
        while not self.stop.wait(0.2):
            self.sample_disk()

    def stage(self, name):
        if name == self.stage_name:
            return
        now = time.perf_counter()
        self.stages[self.stage_name] = self.stages.get(self.stage_name, 0) + now - self.stage_started
        self.stage_name, self.stage_started = name, now
        self.sample_disk()

    def __exit__(self, exception_type, exception, traceback):
        self.stop.set()
        self.sampler.join()
        self.sample_disk()
        now = time.perf_counter()
        self.stages[self.stage_name] = self.stages.get(self.stage_name, 0) + now - self.stage_started
        elapsed = now - self.started
        samples = usage()
        cpu = max(0, cpu_seconds(samples) - self.initial_cpu)
        stats = {
            'worker_seconds': round(elapsed, 4),
            'download_seconds': round(self.stages.get('downloading', 0), 4),
            'processing_seconds': round(sum(value for key, value in self.stages.items()
                                             if key != 'downloading'), 4),
            'cpu_seconds': round(cpu, 4),
            'average_cpu_percent': round(cpu / elapsed * 100, 2) if elapsed else 0,
            'peak_process_memory_bytes': peak_process_bytes(samples),
            'peak_temp_disk_bytes': self.peak_disk_bytes,
            'source_media_bytes': self.downloaded_bytes,
        }
        temporary = self.directory / 'stats.tmp'
        temporary.write_text(json.dumps(stats, allow_nan=False))
        temporary.replace(self.directory / 'stats.json')
