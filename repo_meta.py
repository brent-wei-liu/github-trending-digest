#!/usr/bin/env python3
"""
Repo metadata from the GitHub REST API — created_at, license, owner type,
plus the latest release/tag (evidence candidate for 「为什么涨」).

Cached per repo in the `repo_meta` table (created_at never changes, so rows are
only refreshed after META_TTL_DAYS). Auth: $GITHUB_TOKEN, else `gh auth token`,
else unauthenticated (60 requests/hour). The token is never printed.

Usage:
  python3 repo_meta.py              # refresh metadata for repos in the latest scrape
  python3 repo_meta.py owner/repo…  # refresh specific repos
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from db import get_db, init_db

API = "https://api.github.com"
META_TTL_DAYS = 14
RELEASE_TTL_HOURS = 12      # releases change often; refetch roughly once per daily run
TIMEOUT = 15

# Owners whose repos are tagged 大厂官方 (case-insensitive; prefix match covers
# variants like google-research, microsoft-foundry, NVIDIA-NeMo).
BIG_TECH_PREFIXES = (
    "google", "microsoft", "azure", "facebook", "meta-", "metaresearch", "pytorch",
    "nvidia", "openai", "anthropics", "apple", "aws", "amazon", "alibaba", "qwenlm",
    "bytedance", "tencent", "baidu", "paddlepaddle", "deepseek-ai", "huggingface",
    "cloudflare", "vercel", "github", "ibm", "intel", "salesforce", "netflix",
    "spotify", "xai-org", "mistralai", "moonshotai", "jetbrains", "adobe", "oracle",
    "sap", "samsung", "xiaomi", "huawei", "volcengine", "ant-", "antgroup", "shopify",
    "stripe", "airbnb", "uber", "linkedin", "twitter", "x-", "deepmind",
)

OWNER_KIND_LABEL = {
    "big_tech": "大厂官方",
    "company": "商业公司",
    "org": "开源团队",
    "user": "个人开发者",
}

LICENSE_NONE = "未声明许可"


def get_token():
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def api_get(path, token=None):
    req = urllib.request.Request(API + path, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-trending-digest",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def is_big_tech(login):
    low = (login or "").lower()
    return any(low.startswith(p) for p in BIG_TECH_PREFIXES)


def owner_kind(owner_type, login, org_verified):
    """User → user; well-known company → big_tech; verified org → company; else org."""
    if is_big_tech(login):
        return "big_tech"
    if owner_type != "Organization":
        return "user"
    return "company" if org_verified else "org"


def license_label(lic):
    spdx = (lic or {}).get("spdx_id")
    if not spdx or spdx == "NOASSERTION":
        return "其他许可" if lic else LICENSE_NONE
    return spdx


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS repo_meta (
            full_name   TEXT PRIMARY KEY,
            created_at  TEXT,
            license     TEXT,
            owner_type  TEXT,
            owner_kind  TEXT,
            api_stars   INTEGER,
            fetched_at  TEXT NOT NULL
        )
    """)


def stale_repos(conn, names, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=META_TTL_DAYS)).isoformat()
    fresh = {r[0] for r in conn.execute(
        f"SELECT full_name FROM repo_meta WHERE fetched_at >= ? AND full_name IN ({','.join('?' * len(names))})",
        (cutoff, *names),
    )} if names else set()
    return [n for n in names if n not in fresh]


def refresh(conn, names, token=None, getter=api_get):
    """Fetch metadata for stale repos. Returns {"fetched": n, "failed": [...]}."""
    ensure_table(conn)
    todo = stale_repos(conn, list(dict.fromkeys(names)))
    org_cache, failed, fetched = {}, [], 0
    now = datetime.now(timezone.utc).isoformat()
    for name in todo:
        try:
            d = getter(f"/repos/{name}", token)
            owner = d.get("owner") or {}
            login, otype = owner.get("login", name.split("/")[0]), owner.get("type")
            verified = False
            if otype == "Organization" and not is_big_tech(login):
                if login not in org_cache:
                    try:
                        org_cache[login] = bool(getter(f"/orgs/{login}", token).get("is_verified"))
                    except (urllib.error.URLError, OSError, ValueError):
                        org_cache[login] = False
                verified = org_cache[login]
            conn.execute(
                """INSERT OR REPLACE INTO repo_meta
                   (full_name, created_at, license, owner_type, owner_kind, api_stars, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (name, d.get("created_at"), license_label(d.get("license")), otype,
                 owner_kind(otype, login, verified), d.get("stargazers_count"), now),
            )
            fetched += 1
        except (urllib.error.URLError, OSError, ValueError) as e:
            failed.append(f"{name}: {type(e).__name__}: {getattr(e, 'code', '') or e}")
    conn.commit()
    return {"checked": len(names), "fetched": fetched, "cached": len(names) - len(todo),
            "failed": failed}


def ensure_release_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS repo_release (
            full_name     TEXT PRIMARY KEY,
            kind          TEXT,      -- 'release' | 'tag' | NULL (none found)
            tag           TEXT,
            title         TEXT,
            published_at  TEXT,
            fetched_at    TEXT NOT NULL
        )
    """)


def latest_release(name, token=None, getter=api_get):
    """Newest release (incl. prerelease), else newest tag with its commit date. None if neither."""
    rels = getter(f"/repos/{name}/releases?per_page=1", token) or []
    if rels:
        r = rels[0]
        return {"kind": "release", "tag": r.get("tag_name"),
                "title": (r.get("name") or "").strip() or None,
                "published_at": r.get("published_at") or r.get("created_at")}
    tags = getter(f"/repos/{name}/tags?per_page=1", token) or []
    if not tags:
        return None
    t = tags[0]
    date = None
    sha = (t.get("commit") or {}).get("sha")
    if sha:
        c = getter(f"/repos/{name}/commits/{sha}", token) or {}
        date = ((c.get("commit") or {}).get("committer") or {}).get("date")
    return {"kind": "tag", "tag": t.get("name"), "title": None, "published_at": date}


def refresh_releases(conn, names, token=None, getter=api_get, now=None):
    """Cache the latest release/tag per repo (TTL RELEASE_TTL_HOURS)."""
    ensure_release_table(conn)
    now = now or datetime.now(timezone.utc)
    names = list(dict.fromkeys(names))
    cutoff = (now - timedelta(hours=RELEASE_TTL_HOURS)).isoformat()
    fresh = {r[0] for r in conn.execute(
        f"SELECT full_name FROM repo_release WHERE fetched_at >= ? AND full_name IN ({','.join('?' * len(names))})",
        (cutoff, *names))} if names else set()
    failed, fetched = [], 0
    for name in (n for n in names if n not in fresh):
        try:
            rel = latest_release(name, token, getter) or {}
            conn.execute(
                """INSERT OR REPLACE INTO repo_release
                   (full_name, kind, tag, title, published_at, fetched_at) VALUES (?, ?, ?, ?, ?, ?)""",
                (name, rel.get("kind"), rel.get("tag"), rel.get("title"), rel.get("published_at"),
                 now.isoformat()))
            fetched += 1
        except (urllib.error.URLError, OSError, ValueError) as e:
            failed.append(f"{name}: {type(e).__name__}: {getattr(e, 'code', '') or e}")
    conn.commit()
    return {"checked": len(names), "fetched": fetched, "cached": len(fresh), "failed": failed}


def latest_repos(conn):
    row = conn.execute("SELECT MAX(DATE(fetched_at)) FROM trending_entries").fetchone()
    if not row or not row[0]:
        return []
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT repo FROM trending_entries WHERE DATE(fetched_at) = ?", (row[0],))]


def main():
    conn = get_db()
    init_db(conn)
    names = sys.argv[1:] or latest_repos(conn)
    token = get_token()
    print(json.dumps({"meta": refresh(conn, names, token),
                      "releases": refresh_releases(conn, names, token)},
                     ensure_ascii=False, indent=2))
    conn.close()


if __name__ == "__main__":
    main()
