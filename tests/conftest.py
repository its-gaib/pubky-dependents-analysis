"""Keep the test suite independent of credentials and external services."""

import subprocess

import pytest
import requests


@pytest.fixture(autouse=True)
def block_external_io(monkeypatch):
    def blocked_request(*args, **kwargs):
        pytest.fail("Tests must mock HTTP requests before using external sources")

    def blocked_subprocess(*args, **kwargs):
        pytest.fail("Tests must mock subprocess.run before invoking external commands")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked_request)
    monkeypatch.setattr(subprocess, "run", blocked_subprocess)
