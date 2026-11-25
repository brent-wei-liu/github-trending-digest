#!/usr/bin/env python3
"""
Deterministic lifecycle status per repo per day (table `lifecycle`).

Inputs: own star history (star_history.py) and list appearances (trending_entries).
"Listed" = on ANY of GitHub's daily / weekly / monthly lists that day.

Primary status (first match wins):
  新上榜    listed today and never listed before in our records (since 2026-03-25)
  爆发      1d gain ≥ BURST_MIN_1D and ≥ BURST_RATIO × its average daily gain over the
            previous 7 days (an unknown previous average counts as 0)
  回榜      listed today, not on the previous scrape day, listed at some earlier time
  掉榜      listed on the previous scrape day, not listed today
  降温      30d average ≥ COOL_MIN_AVG/day (complete 30d window) and the 7d average is
            ≤ COOL_RATIO × the 30d average
  持续走红  7d average ≥ HOT_MIN_AVG/day, listed on ≥ HOT_MIN_DAYS of the last 7 scrape
            days, and (when known) today's 1d gain ≥ HOT_MIN_TODAY — so a single spike
            inside the 7-day window doesn't count as sustained
  (none)    otherwise
Flag (in addition to the primary status):
  里程碑    total crossed one of MILESTONES since the previous own observation
            (which must be ≤ MILESTONE_LOOKBACK days old)

1d gain = own (star_history) when we have yesterday's total, else GitHub's daily-list
number. Previous-7-day average = average daily gain between our observations in the
7 days before today (earliest in [D−8, D−2] → latest before D); if we have fewer than
two, (g7 − g1) / 6 when both are known. A 30d gain only
counts as complete when its source is own / own_interp / github_monthly (not the
own_partial lower bound).

Usage:
  python3 lifecycle.py backfill [DAYS]      # compute + store the last DAYS scrape days
  python3 lifecycle.py show [YYYY-MM-DD]    # print statuses for a day
"""

import json
import sys
from datetime import date, timedelta

import star_history as sh

BURST_MIN_1D = 1_000
BURST_RATIO = 3
COOL_MIN_AVG = 300
COOL_RATIO = 0.5
HOT_MIN_AVG = 500
HOT_MIN_DAYS = 5
HOT_MIN_TODAY = HOT_MIN_AVG // 2
MILESTONE_LOOKBACK = 3
MILESTONES = (1_000, 5_000, 10_000, 20_000, 50_000, 100_000, 200_000, 300_000, 500_000,
              1_000_000)

NEW, BURST, BACK, DROPPED, COOLING, HOT = "新上榜", "爆发", "回榜", "掉榜", "降温", "持续走红"
STATUSES = (NEW, BURST, BACK, DROPPED, COOLING, HOT)
COMPLETE_30D = ("own", "own_interp", "github_monthly")


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lifecycle (
            repo       TEXT NOT NULL,
            date       TEXT NOT NULL,
            status     TEXT,            -- one of STATUSES or NULL
            milestone  INTEGER,         -- threshold crossed that day, or NULL
            g1 INTEGER, g7 INTEGER, g30 INTEGER,
            src1 TEXT, src7 TEXT, src30 TEXT,
            stars      INTEGER,
            listed     INTEGER NOT NULL,   -- on any list that day
            reason     TEXT,            -- the numbers behind the status, for the UI
            PRIMARY KEY (repo, date)
        )
    """)


def fmt(n):
    return f"{n:,}"


def milestone_crossed(prev, now):
    if prev is None or now is None:
        return None
    crossed = [m for m in MILESTONES if prev < m <= now]
    return max(crossed) if crossed else None


def milestone_label(m):
    return f"破 {m // 10_000} 万星" if m >= 10_000 else f"破 {fmt(m)} 星"


def prior_pace(obs, day):
    """Average daily gain over the week before `day` from own observations, or None."""
    before = [d for d in obs if d < day]
    if not before:
        return None
    last = max(before)
    early = [d for d in before if day - timedelta(days=8) <= d <= day - timedelta(days=2) and d < last]
    if not early or last < day - timedelta(days=2):
        return None
    first = min(early)
    return (obs[last] - obs[first]) / (last - first).days


def classify(*, listed_today, listed_before, listed_prev_scrape, listed_last7, g, prev_total,
             prev_avg=None):
    """Pure function → (status, milestone, reason). `g` is a star_history.gains() dict."""
    g1, g7, stars = g.get("g1"), g.get("g7"), g.get("stars")
    if prev_avg is None and g1 is not None and g7 is not None:
        prev_avg = (g7 - g1) / 6
    avg7 = g7 / 7 if g7 is not None else None
    avg30 = g["g30"] / 30 if g.get("g30") is not None and g.get("src30") in COMPLETE_30D else None
    milestone = milestone_crossed(prev_total, stars)

    if listed_today and not listed_before:
        return NEW, milestone, "首次出现在任何榜单"
    if (g1 is not None and g1 >= BURST_MIN_1D
            and g1 >= BURST_RATIO * max(prev_avg or 0, 0)):
        base = f"前 7 天日均 +{fmt(round(prev_avg))}" if prev_avg is not None else "此前无日均数据"
        return BURST, milestone, f"1 天 +{fmt(g1)}，{base}"
    if listed_today and not listed_prev_scrape:
        return BACK, milestone, "上次抓取不在榜，今天重新上榜"
    if listed_prev_scrape and not listed_today:
        return DROPPED, milestone, "上次抓取在榜，今天不在任何榜单"
    if (avg30 is not None and avg30 >= COOL_MIN_AVG and avg7 is not None
            and avg7 <= COOL_RATIO * avg30):
        return COOLING, milestone, f"7 天日均 +{fmt(round(avg7))}，30 天日均 +{fmt(round(avg30))}"
    if (avg7 is not None and avg7 >= HOT_MIN_AVG and listed_last7 >= HOT_MIN_DAYS
            and (g1 is None or g1 >= HOT_MIN_TODAY)):
        return HOT, milestone, f"7 天日均 +{fmt(round(avg7))}，近 7 次抓取 {listed_last7} 天在榜"
    return None, milestone, None


def _scrape_days(conn, upto):
    return [date.fromisoformat(r[0]) for r in conn.execute(
        "SELECT DISTINCT DATE(fetched_at) FROM trending_entries WHERE DATE(fetched_at) <= ? "
        "ORDER BY 1", (upto.isoformat(),))]


def compute_day(conn, day):
    """{repo: row} for every repo listed in the 30 days up to `day`."""
    sh.ensure_table(conn)
    days = _scrape_days(conn, day)
    if not days or days[-1] != day:
        return {}
    prev_scrape = days[-2] if len(days) > 1 else None
    last7 = set(days[-7:])
    listed = {}
    for repo, d in conn.execute(
            "SELECT DISTINCT repo, DATE(fetched_at) FROM trending_entries WHERE DATE(fetched_at) <= ?",
            (day.isoformat(),)):
        listed.setdefault(repo, set()).add(date.fromisoformat(d))
    gh = sh.github_deltas(conn, day)
    out = {}
    for repo in sh.active_repos(conn, day):
        ds = listed.get(repo, set())
        obs = {d: v for d, v in sh.observations(conn, repo).items() if d <= day}
        g = sh.gains(obs, day, gh.get(repo))
        prev_obs = [d for d in obs if day - timedelta(days=MILESTONE_LOOKBACK) <= d < day]
        prev_total = obs[max(prev_obs)] if prev_obs else None
        status, ms, reason = classify(
            listed_today=day in ds, listed_before=any(d < day for d in ds),
            listed_prev_scrape=prev_scrape in ds if prev_scrape else False,
            listed_last7=len(ds & last7), g=g, prev_total=prev_total if g.get("as_of") == day else None,
            prev_avg=prior_pace(obs, day))
        out[repo] = {"status": status, "milestone": ms, "reason": reason, "listed": day in ds,
                     **{k: g.get(k) for k in ("stars", "g1", "g7", "g30", "src1", "src7", "src30")}}
    return out


def store_day(conn, day, rows):
    ensure_table(conn)
    conn.execute("DELETE FROM lifecycle WHERE date = ?", (day.isoformat(),))
    conn.executemany(
        """INSERT INTO lifecycle (repo, date, status, milestone, g1, g7, g30, src1, src7, src30,
                                  stars, listed, reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(r, day.isoformat(), v["status"], v["milestone"], v["g1"], v["g7"], v["g30"],
          v["src1"], v["src7"], v["src30"], v["stars"], int(v["listed"]), v["reason"])
         for r, v in rows.items()])
    conn.commit()


def refresh(conn, day):
    rows = compute_day(conn, day)
    if rows:
        store_day(conn, day, rows)
    return rows


def previous_status(conn, repo, day):
    """Status on the latest stored day before `day` (None if none stored)."""
    row = conn.execute(
        "SELECT status FROM lifecycle WHERE repo = ? AND date < ? ORDER BY date DESC LIMIT 1",
        (repo, day.isoformat())).fetchone()
    return row[0] if row else None


def main(argv=None):
    from db import get_db, init_db
    argv = argv if argv is not None else sys.argv[1:]
    conn = get_db()
    init_db(conn)
    ensure_table(conn)
    cmd = argv[0] if argv else "show"
    latest = date.fromisoformat(conn.execute(
        "SELECT MAX(DATE(fetched_at)) FROM trending_entries").fetchone()[0])
    if cmd == "backfill":
        n = int(argv[1]) if len(argv) > 1 else 30
        days = [d for d in _scrape_days(conn, latest)][-n:]
        for d in days:
            refresh(conn, d)
        print(json.dumps({"days": len(days), "from": str(days[0]), "to": str(days[-1])}))
    elif cmd == "show":
        day = date.fromisoformat(argv[1]) if len(argv) > 1 else latest
        rows = refresh(conn, day)
        for r, v in sorted(rows.items(), key=lambda kv: -(kv[1]["g30"] or 0)):
            if v["status"] or v["milestone"]:
                ms = milestone_label(v["milestone"]) if v["milestone"] else ""
                print(f"{r:45s} {v['status'] or '':5s} {ms:8s} {v['reason'] or ''}")
    conn.close()


if __name__ == "__main__":
    main()
