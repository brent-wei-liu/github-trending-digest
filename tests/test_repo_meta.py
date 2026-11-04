import sqlite3
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import repo_meta as m


def test_owner_kind():
    assert m.owner_kind("Organization", "google-research", False) == "big_tech"
    assert m.owner_kind("Organization", "NVIDIA", False) == "big_tech"
    assert m.owner_kind("Organization", "vercel-labs", True) == "big_tech"
    assert m.owner_kind("Organization", "floci-io", True) == "company"
    assert m.owner_kind("Organization", "hyprwm", False) == "org"
    assert m.owner_kind("User", "thedotmack", False) == "user"


def test_license_label():
    assert m.license_label({"spdx_id": "MIT"}) == "MIT"
    assert m.license_label({"spdx_id": "NOASSERTION"}) == "其他许可"
    assert m.license_label(None) == "未声明许可"


class FakeAPI:
    def __init__(self):
        self.calls = []

    def __call__(self, path, token=None):
        self.calls.append(path)
        if path == "/repos/gone/repo":
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
        if path.startswith("/orgs/"):
            return {"is_verified": path == "/orgs/acme"}
        owner = path.split("/")[2]
        return {
            "created_at": "2026-10-01T00:00:00Z",
            "license": {"spdx_id": "Apache-2.0"},
            "stargazers_count": 123,
            "owner": {"login": owner, "type": "User" if owner == "solo" else "Organization"},
        }


def _conn():
    c = sqlite3.connect(":memory:")
    m.ensure_table(c)
    return c


def test_refresh_caches_and_classifies():
    c, api = _conn(), FakeAPI()
    res = m.refresh(c, ["solo/a", "acme/b", "acme/c", "team/d", "gone/repo"], getter=api)
    assert res["fetched"] == 4 and len(res["failed"]) == 1 and "404" in res["failed"][0]
    kinds = dict(c.execute("SELECT full_name, owner_kind FROM repo_meta"))
    assert kinds == {"solo/a": "user", "acme/b": "company", "acme/c": "company", "team/d": "org"}
    assert api.calls.count("/orgs/acme") == 1          # org lookups are memoised

    api.calls.clear()
    res = m.refresh(c, ["solo/a", "acme/b"], getter=api)
    assert res == {"checked": 2, "fetched": 0, "cached": 2, "failed": []}
    assert api.calls == []


def test_stale_rows_are_refetched():
    c, api = _conn(), FakeAPI()
    m.refresh(c, ["solo/a"], getter=api)
    old = (datetime.now(timezone.utc) - timedelta(days=m.META_TTL_DAYS + 1)).isoformat()
    c.execute("UPDATE repo_meta SET fetched_at = ?", (old,))
    assert m.stale_repos(c, ["solo/a"]) == ["solo/a"]


class ReleaseAPI:
    def __init__(self):
        self.calls = []

    def __call__(self, path, token=None):
        self.calls.append(path)
        if path == "/repos/r/rel/releases?per_page=1":
            return [{"tag_name": "v2.0", "name": "Big one", "published_at": "2026-11-03T10:00:00Z"}]
        if path.endswith("/releases?per_page=1"):
            return []
        if path == "/repos/t/tag/tags?per_page=1":
            return [{"name": "v0.3", "commit": {"sha": "abc"}}]
        if path == "/repos/t/tag/commits/abc":
            return {"commit": {"committer": {"date": "2026-11-01T00:00:00Z"}}}
        if path.endswith("/tags?per_page=1"):
            return []
        raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)


def test_latest_release_prefers_release_then_tag():
    api = ReleaseAPI()
    assert m.latest_release("r/rel", getter=api) == {
        "kind": "release", "tag": "v2.0", "title": "Big one", "published_at": "2026-11-03T10:00:00Z"}
    assert m.latest_release("t/tag", getter=api) == {
        "kind": "tag", "tag": "v0.3", "title": None, "published_at": "2026-11-01T00:00:00Z"}
    assert m.latest_release("n/none", getter=api) is None


def test_refresh_releases_caches_including_none():
    c, api = sqlite3.connect(":memory:"), ReleaseAPI()
    res = m.refresh_releases(c, ["r/rel", "t/tag", "n/none"], getter=api)
    assert res == {"checked": 3, "fetched": 3, "cached": 0, "failed": []}
    rows = dict(c.execute("SELECT full_name, tag FROM repo_release"))
    assert rows == {"r/rel": "v2.0", "t/tag": "v0.3", "n/none": None}
    api.calls.clear()
    assert m.refresh_releases(c, ["r/rel", "n/none"], getter=api)["cached"] == 2
    assert api.calls == []
