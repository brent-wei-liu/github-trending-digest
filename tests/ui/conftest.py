"""Shared fixtures for the Playwright UI test suite.

Assumes `python3 api.py` is already running on port 8082. Tests do NOT
spawn the server themselves (port-binding races + zombie-process pain).
If the server isn't reachable, the suite is skipped with a clear reason.
"""
import json
import os

import pytest
import urllib.request
import urllib.error


def _server_reachable(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url + "/api/stats", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ.get("GITHUB_TRENDING_URL", "http://127.0.0.1:8082")


@pytest.fixture(scope="session", autouse=True)
def _require_server(base_url: str):
    if not _server_reachable(base_url):
        pytest.skip(
            f"github-trending-digest API not reachable at {base_url} — "
            "run `python3 api.py` in another terminal first.",
            allow_module_level=True,
        )


def _starred_ids(base_url: str) -> set[int]:
    ids = set()
    for period in ("daily", "weekly", "monthly"):
        url = f"{base_url}/api/repos?period={period}&starred=true&days=90&page_size=200"
        with urllib.request.urlopen(url, timeout=5) as r:
            ids.update(repo["id"] for repo in json.load(r)["repos"])
    return ids


@pytest.fixture
def preserve_stars(base_url: str):
    """The suite runs against the real DB: unstar whatever a test starred, even if it fails."""
    before = _starred_ids(base_url)
    yield
    for repo_id in _starred_ids(base_url) - before:
        req = urllib.request.Request(f"{base_url}/api/repos/{repo_id}/unstar", method="POST")
        urllib.request.urlopen(req, timeout=5).close()
