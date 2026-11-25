#!/usr/bin/env python3
"""
Per-repo project memory (table `projects`): first_seen, a cached one-sentence plain
Chinese 「是什么」, and the README excerpt it was written from. Owner type, language,
license and created_at come from `repos` / `repo_meta` (repo_meta.py) and are joined
in by `profile()`.

The LLM writes 是什么 once per repo. It is regenerated only when the description or
README changes materially (token Jaccard similarity of the basis text < SIMILAR).

Usage:
  python3 project_memory.py readme [owner/repo …]   # refresh README excerpts (TTL 7 days)
  python3 project_memory.py pending [--names a/b,c/d] [--limit N]
                                                    # JSON: repos needing a 是什么
  python3 project_memory.py save FILE               # FILE: {"owner/repo": "一句话", …}
  python3 project_memory.py show owner/repo
"""

import base64
import json
import re
import sys
import urllib.error
from datetime import datetime, timedelta, timezone

README_TTL_DAYS = 7
README_CHARS = 1500       # excerpt kept for the LLM and for change detection
SIMILAR = 0.6             # basis similarity below this → regenerate 是什么
WHAT_MAX_CHARS = 60

_TOKEN_RE = re.compile(r"[一-鿿]|[A-Za-z0-9][A-Za-z0-9+#.\-]*")
_NUMBERS_RE = re.compile(r"[日周月]增|总星|⭐|涨星|[+＋]\s?\d|\d[\d,]*\s*(颗)?星")


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            repo              TEXT PRIMARY KEY,
            what_zh           TEXT,
            what_basis        TEXT,     -- description + README excerpt it was written from
            what_written_at   TEXT,
            readme_excerpt    TEXT,
            readme_sha        TEXT,
            readme_fetched_at TEXT
        )
    """)


# ---------- README ----------

def clean_readme(md):
    """Plain-ish text: drop HTML, images, badges, link targets, code fences, tables."""
    md = re.sub(r"```.*?```", " ", md, flags=re.S)
    md = re.sub(r"<!--.*?-->", " ", md, flags=re.S)
    md = re.sub(r"<[^>]+>", " ", md)
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", md)
    md = re.sub(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)", " ", md)
    md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md)
    md = re.sub(r"^\s*\|.*\|\s*$", " ", md, flags=re.M)
    md = re.sub(r"^[#>*\-\s]+", "", md, flags=re.M)
    md = re.sub(r"[ \t]+", " ", md)
    md = re.sub(r"\n\s*\n+", "\n", md)
    return md.strip()


def fetch_readme(name, token=None, getter=None):
    """(excerpt, sha) or (None, None) when the repo has no README."""
    from repo_meta import api_get
    getter = getter or api_get
    try:
        d = getter(f"/repos/{name}/readme", token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        raise
    raw = base64.b64decode(d.get("content") or "").decode("utf-8", "replace")
    return clean_readme(raw)[:README_CHARS], d.get("sha")


def refresh_readmes(conn, names, token=None, getter=None, now=None):
    ensure_table(conn)
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=README_TTL_DAYS)).isoformat()
    fresh = {r[0] for r in conn.execute(
        "SELECT repo FROM projects WHERE readme_fetched_at >= ?", (cutoff,))}
    out = {"checked": len(names), "fetched": 0, "failed": []}
    for name in (n for n in dict.fromkeys(names) if n not in fresh):
        try:
            excerpt, sha = fetch_readme(name, token, getter)
        except (urllib.error.URLError, OSError, ValueError) as e:
            out["failed"].append(f"{name}: {getattr(e, 'code', '') or type(e).__name__}")
            continue
        conn.execute("INSERT OR IGNORE INTO projects (repo) VALUES (?)", (name,))
        conn.execute("UPDATE projects SET readme_excerpt = ?, readme_sha = ?, readme_fetched_at = ? "
                     "WHERE repo = ?", (excerpt, sha, now.isoformat(), name))
        out["fetched"] += 1
    conn.commit()
    return out


# ---------- 是什么 cache ----------

def _tokens(text):
    return {t.lower() for t in _TOKEN_RE.findall(text or "")}


def similarity(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return 1.0
    return len(ta & tb) / len(ta | tb)


def basis_text(description, readme_excerpt):
    # Keep the separator even when the description is empty: line 1 is always the description.
    return f"{(description or '').strip()}\n{(readme_excerpt or '')[:600].strip()}"


def materially_changed(old_basis, description, readme_excerpt):
    """True when the stored basis no longer matches the current description/README."""
    if old_basis is None:
        return True
    old_desc = old_basis.split("\n", 1)[0]
    if similarity(old_desc, description or "") < SIMILAR:
        return True
    return similarity(old_basis, basis_text(description, readme_excerpt)) < SIMILAR


def profile(conn, repo):
    """Everything we know about a repo, without the LLM."""
    ensure_table(conn)
    row = conn.execute("""
        SELECT r.full_name, r.description, r.language,
               (SELECT MIN(fetched_at) FROM trending_entries t WHERE t.repo = r.full_name),
               m.created_at, m.license, m.owner_kind, p.what_zh, p.what_basis, p.readme_excerpt
        FROM repos r
        LEFT JOIN repo_meta m ON m.full_name = r.full_name
        LEFT JOIN projects p ON p.repo = r.full_name
        WHERE r.full_name = ?""", (repo,)).fetchone()
    if not row:
        return None
    keys = ("repo", "description", "language", "first_seen", "created_at", "license",
            "owner_kind", "what_zh", "what_basis", "readme_excerpt")
    return dict(zip(keys, row))


def pending(conn, names):
    """Repos whose 是什么 is missing or stale, with the inputs the LLM needs."""
    out = []
    for n in dict.fromkeys(names):
        p = profile(conn, n)
        if not p:
            continue
        if p["what_zh"] and not materially_changed(p["what_basis"], p["description"], p["readme_excerpt"]):
            continue
        out.append({"repo": n, "description": p["description"] or "", "language": p["language"],
                    "readme_excerpt": (p["readme_excerpt"] or "")[:README_CHARS],
                    "previous": p["what_zh"]})
    return out


def validate_what(text):
    """Problems with a candidate 是什么 sentence ([] = ok)."""
    t = (text or "").strip()
    probs = []
    if not t:
        return ["empty"]
    if len(t) > WHAT_MAX_CHARS:
        probs.append(f"over {WHAT_MAX_CHARS} chars")
    if not re.search(r"[一-鿿]", t):
        probs.append("not Chinese")
    if _NUMBERS_RE.search(t):
        probs.append("mentions star numbers")
    if t.rstrip("。").count("。") > 0 or "\n" in t:
        probs.append("more than one sentence")
    return probs


def save_what(conn, mapping, now=None):
    """Store validated sentences. Returns {"saved": [...], "rejected": {repo: problems}}."""
    ensure_table(conn)
    now = (now or datetime.now(timezone.utc)).isoformat()
    saved, rejected = [], {}
    for repo, text in mapping.items():
        p = profile(conn, repo)
        probs = ["unknown repo"] if not p else validate_what(text)
        if probs:
            rejected[repo] = probs
            continue
        conn.execute("INSERT OR IGNORE INTO projects (repo) VALUES (?)", (repo,))
        conn.execute("UPDATE projects SET what_zh = ?, what_basis = ?, what_written_at = ? WHERE repo = ?",
                     (text.strip().rstrip("。"), basis_text(p["description"], p["readme_excerpt"]),
                      now, repo))
        saved.append(repo)
    conn.commit()
    return {"saved": saved, "rejected": rejected}


def main(argv=None):
    from db import get_db, init_db
    from repo_meta import get_token
    import star_history as sh
    argv = argv if argv is not None else sys.argv[1:]
    conn = get_db()
    init_db(conn)
    ensure_table(conn)
    cmd = argv[0] if argv else "pending"
    latest = conn.execute("SELECT MAX(DATE(fetched_at)) FROM trending_entries").fetchone()[0]
    active = sh.active_repos(conn, datetime.fromisoformat(latest).date()) if latest else []
    if cmd == "readme":
        print(json.dumps(refresh_readmes(conn, argv[1:] or active, get_token()), ensure_ascii=False))
    elif cmd == "pending":
        names = active
        if "--names" in argv:
            names = argv[argv.index("--names") + 1].split(",")
        items = pending(conn, names)
        if "--limit" in argv:
            items = items[:int(argv[argv.index("--limit") + 1])]
        print(json.dumps(items, ensure_ascii=False, indent=2))
    elif cmd == "save":
        with open(argv[1], encoding="utf-8") as f:
            print(json.dumps(save_what(conn, json.load(f)), ensure_ascii=False, indent=2))
    elif cmd == "show":
        print(json.dumps(profile(conn, argv[1]), ensure_ascii=False, indent=2))
    conn.close()


if __name__ == "__main__":
    main()
