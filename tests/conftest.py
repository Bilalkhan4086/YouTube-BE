import pytest


@pytest.fixture(autouse=True)
def isolated_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv('JOB_DATA_DIR', str(tmp_path / 'jobs'))
