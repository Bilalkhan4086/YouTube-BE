from types import SimpleNamespace

from app.metrics import cpu_seconds, peak_process_bytes


def test_resource_units_and_cpu_aggregation():
    samples = [SimpleNamespace(ru_maxrss=100, ru_utime=2.0, ru_stime=0.5),
               SimpleNamespace(ru_maxrss=200, ru_utime=3.0, ru_stime=1.0)]
    assert peak_process_bytes(samples, platform='darwin') == 200
    assert peak_process_bytes(samples, platform='linux') == 200 * 1024
    assert cpu_seconds(samples) == 6.5
