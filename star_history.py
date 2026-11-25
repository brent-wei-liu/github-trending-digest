#!/usr/bin/env python3
"""
Own star history and our own 1d / 7d / 30d gains on one basis.

Why: GitHub Trending's daily / weekly / monthly lists are three opaque windows
(gross new stars, GitHub's own window boundaries) and a repo only shows a number
for the lists it happens to be on. We record total stars ourselves and diff them.

Observations (table `star_history`, one row per repo / UTC day / source):
  api       — stargazers_count from the GitHub REST API, recorded daily for every
              repo seen on any list in the last ACTIVE_DAYS days (own)
  trending  — total stars printed on the trending page when we scraped it (own;
              backfilled from trending_entries, available since 2026-03-25)

Gains (`gains()`): total(today) − total(today − N), using the nearest own
observation within a tolerance and normalising to N days; if there is none, the
total on today − N is interpolated linearly between the own observations either
side of it (own_interp; 7d: ≤14-day gap, 30d: ≤30-day gap). While history is short:
  7d  → falls back to GitHub's weekly-list number   (source github_weekly)
  30d → falls back to GitHub's monthly-list number  (source github_monthly),
        then to the gain since our first observation in the window (own_partial,
        a lower bound)
  1d  → falls back to GitHub's daily-list number    (source github_daily)
Every gain carries its source so the digest / UI can label it.

Backtest (docs/redesign-log.md): own/github ≈ 0.89 (weekly), 0.94 (monthly) at the
median — GitHub counts gross new stars (unstars aren't subtracted) and its windows
don't align with UTC days, so own numbers are systematically a bit lower.

Usage:
  python3 star_history.py backfill          # import trending totals (idempotent)
  python3 star_history.py record [--date D]  # today's API readings for active repos
  python3 star_history.py gains [owner/repo …]
"""

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

API = "https://api.github.com"
TIMEOUT = 15
ACTIVE_DAYS = 30           # record every repo seen on any list in the last 30 days
MIN_REMAINING = 50         # stop recording when the rate-limit budget gets this low
WINDOWS = {1: 0, 7: 1, 30: 3}   # N-day gain → allowed baseline slack (days)
DISAGREE = 0.05            # api vs trending total on the same day: >5% apart → keep api only

SRC_OWN, SRC_INTERP, SRC_PARTIAL = "own", "own_interp", "own_partial"
INTERP_MAX_GAP = {1: 0, 7: 14, 30: 30}   # max days between the two bracketing observations
GH_FALLBACK = {1: ("daily", "github_daily"), 7: ("weekly", "github_weekly"),
               30: ("monthly", "github_monthly")}


class RateLimited(Exception):
    pass


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS star_history (
            repo        TEXT NOT NULL,
            date        TEXT NOT NULL,       -- UTC day of the observation
            stars       INTEGER NOT NULL,
            source      TEXT NOT NULL,       -- 'api' | 'trending'
            observed_at TEXT NOT NULL,
            api_name    TEXT,                -- full_name the API answered with (renames)
            PRIMARY KEY (repo, date, source)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_star_history_date ON star_history(date)")


# ---------- collection ----------

def backfill_from_trending(conn):
    """Copy the latest trending-page total per repo per day. Idempotent."""
    ensure_table(conn)
    before = conn.total_changes
    conn.execute("""
        INSERT OR REPLACE INTO star_history (repo, date, stars, source, observed_at)
        SELECT t.repo, DATE(t.fetched_at), MAX(t.stars), 'trending', MAX(t.fetched_at)
        FROM trending_entries t
        WHERE t.stars > 0
          AND t.fetched_at = (SELECT MAX(t2.fetched_at) FROM trending_entries t2
                              WHERE t2.repo = t.repo AND DATE(t2.fetched_at) = DATE(t.fetched_at))
        GROUP BY t.repo, DATE(t.fetched_at)
    """)
    conn.commit()
    return conn.total_changes - before


def active_repos(conn, today, days=ACTIVE_DAYS):
    lo = (today - timedelta(days=days - 1)).isoformat()
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT repo FROM trending_entries WHERE DATE(fetched_at) BETWEEN ? AND ? ORDER BY repo",
        (lo, today.isoformat()))]


def api_get(path, token=None):
    """GET with rate-limit awareness. Returns (json, remaining)."""
    req = urllib.request.Request(API + path, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-trending-digest",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                remaining = r.headers.get("X-RateLimit-Remaining")
                return json.load(r), int(remaining) if remaining is not None else None
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and (e.headers.get("X-RateLimit-Remaining") == "0"
                                         or e.headers.get("Retry-After")):
                raise RateLimited(f"HTTP {e.code}, reset {e.headers.get('X-RateLimit-Reset')}")
            if e.code >= 500 and attempt == 1:
                time.sleep(2)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 1:
                time.sleep(2)
                continue
            raise


def record_api(conn, names, token=None, getter=api_get, today=None, now=None):
    """One API reading per repo per UTC day (cached: repos already read today are skipped)."""
    ensure_table(conn)
    now = now or datetime.now(timezone.utc)
    today = today or now.date()
    done = {r[0] for r in conn.execute(
        "SELECT repo FROM star_history WHERE date = ? AND source = 'api'", (today.isoformat(),))}
    todo = [n for n in dict.fromkeys(names) if n not in done]
    out = {"checked": len(names), "cached": len(names) - len(todo), "recorded": 0,
           "failed": [], "renamed": {}, "rate_limited": None, "remaining": None}
    for name in todo:
        try:
            d, remaining = getter(f"/repos/{name}", token)
        except RateLimited as e:
            out["rate_limited"] = str(e)
            break
        except (urllib.error.URLError, OSError, ValueError) as e:
            out["failed"].append(f"{name}: {getattr(e, 'code', '') or type(e).__name__}")
            continue
        stars = d.get("stargazers_count")
        if stars is None:
            out["failed"].append(f"{name}: no stargazers_count")
            continue
        api_name = d.get("full_name")
        if api_name and api_name.lower() != name.lower():
            out["renamed"][name] = api_name
        conn.execute(
            """INSERT OR REPLACE INTO star_history (repo, date, stars, source, observed_at, api_name)
               VALUES (?, ?, ?, 'api', ?, ?)""",
            (name, today.isoformat(), int(stars), now.isoformat(), api_name))
        out["recorded"] += 1
        out["remaining"] = remaining
        if remaining is not None and remaining < MIN_REMAINING:
            out["rate_limited"] = f"stopped early: {remaining} API calls left"
            break
    conn.commit()
    return out


# ---------- gains ----------

def observations(conn, repo):
    """{date: stars} using one own total per day: api preferred; trending kept only
    when it agrees with api (within DISAGREE) or there is no api reading that day."""
    by_day = {}
    for d, stars, src in conn.execute(
            "SELECT date, stars, source FROM star_history WHERE repo = ?", (repo,)):
        by_day.setdefault(d, {})[src] = stars
    out = {}
    for d, s in by_day.items():
        out[date.fromisoformat(d)] = s.get("api", s.get("trending"))
    return out


def _baseline(obs, target, slack):
    """Latest observation in [target − slack, target]."""
    cands = [d for d in obs if target - timedelta(days=slack) <= d <= target]
    return max(cands) if cands else None


def _interpolate(obs, target, max_gap):
    """Linear estimate of the total on `target` from the nearest observations before and
    after it (both strictly inside a gap of at most max_gap days). None if not bracketed."""
    before = max((d for d in obs if d < target), default=None)
    after = min((d for d in obs if d > target), default=None)
    if before is None or after is None or (after - before).days > max_gap:
        return None
    w = (target - before).days / (after - before).days
    return obs[before] + (obs[after] - obs[before]) * w


def gains(obs, today, github=None):
    """{'stars', 'g1','g7','g30', 'src1','src7','src30', 'span30'} for one repo.

    obs: {date: total} own observations; github: {'daily'|'weekly'|'monthly': delta}
    for the lists the repo is on today. Own gains are normalised to N days when the
    baseline is up to WINDOWS[N] days older than today − N.
    """
    github = github or {}
    now_d = max((d for d in obs if d <= today), default=None)
    res = {"stars": obs.get(now_d) if now_d else None, "as_of": now_d}
    for n, slack in WINDOWS.items():
        g = src = span = None
        if now_d == today:
            b = _baseline(obs, today - timedelta(days=n), slack)
            if b is not None:
                span = (today - b).days
                g = round((obs[today] - obs[b]) * n / span)
                src = SRC_OWN
            elif INTERP_MAX_GAP[n]:
                est = _interpolate({d: v for d, v in obs.items() if d <= today},
                                   today - timedelta(days=n), INTERP_MAX_GAP[n])
                if est is not None:
                    g, src, span = round(obs[today] - est), SRC_INTERP, n
        if g is None:
            period, gh_src = GH_FALLBACK[n]
            if github.get(period) is not None:
                g, src = github[period], gh_src
        if g is None and n == 30 and now_d == today:
            first = min((d for d in obs if today - timedelta(days=n) <= d < today), default=None)
            if first is not None:
                g, src, span = obs[today] - obs[first], SRC_PARTIAL, (today - first).days
        res[f"g{n}"], res[f"src{n}"] = g, src
        if n == 30:
            res["span30"] = span
    return res


def github_deltas(conn, today):
    """{repo: {period: delta}} from today's latest scrape of each list."""
    out = {}
    for period in ("daily", "weekly", "monthly"):
        ts = conn.execute(
            "SELECT MAX(fetched_at) FROM trending_entries WHERE period = ? AND DATE(fetched_at) = ?",
            (period, today.isoformat())).fetchone()[0]
        if not ts:
            continue
        for repo, delta in conn.execute(
                "SELECT repo, stars_delta FROM trending_entries WHERE period = ? AND fetched_at = ?",
                (period, ts)):
            out.setdefault(repo, {})[period] = delta
    return out


def all_gains(conn, today, names=None):
    ensure_table(conn)
    gh = github_deltas(conn, today)
    names = names or active_repos(conn, today)
    return {n: gains(observations(conn, n), today, gh.get(n)) | {"github": gh.get(n, {})}
            for n in names}


def main(argv=None):
    from db import get_db, init_db
    from repo_meta import get_token
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "record"
    conn = get_db()
    init_db(conn)
    ensure_table(conn)
    today = datetime.now(timezone.utc).date()
    if "--date" in argv:   # label readings with the scrape day (e.g. verification runs after midnight UTC)
        i = argv.index("--date")
        today = date.fromisoformat(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    if cmd == "backfill":
        print(json.dumps({"rows": backfill_from_trending(conn)}))
    elif cmd == "record":
        backfill_from_trending(conn)
        res = record_api(conn, active_repos(conn, today), get_token(), today=today)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif cmd == "gains":
        g = all_gains(conn, today, argv[1:] or None)
        print(json.dumps({k: {x: (str(y) if isinstance(y, date) else y) for x, y in v.items()}
                          for k, v in g.items()}, ensure_ascii=False, indent=2))
    else:
        print(__doc__)
        return 2
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
