#!/usr/bin/env python3
"""
GitHub Trending Fetch — scrape trending pages and store in SQLite.

Usage:
  python3 gh_fetch.py                    # Fetch all three periods
  python3 gh_fetch.py --report-hour H    # Only output report when local hour == H
  python3 gh_fetch.py stats [days]       # Quick stats
"""

import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
import urllib.request
from html import unescape

from db import get_db, init_db

PERIODS = ["daily", "weekly", "monthly"]
TRENDING_URL = "https://github.com/trending?since={period}"


def parse_trending_page(html):
    """Parse GitHub trending page HTML into repo list."""
    repos = []
    for i, m in enumerate(re.finditer(r'<article class="Box-row">(.*?)</article>', html, re.S), 1):
        block = m.group(1)

        repo_hrefs = re.findall(r'href="/([^"?]+)"', block)
        full_name = None
        for href in repo_hrefs:
            href = href.strip().rstrip("/")
            if href.count("/") == 1 and not href.startswith("login") and not href.startswith("sponsors"):
                full_name = href
                break
        if not full_name:
            continue

        desc_m = re.search(r'<p class="[^"]*col-9[^"]*"[^>]*>\s*(.*?)\s*</p>', block, re.S)
        description = ""
        if desc_m:
            description = re.sub(r'<[^>]+>', '', desc_m.group(1)).strip()
            description = unescape(description)

        lang_m = re.search(r'itemprop="programmingLanguage"[^>]*>\s*(.*?)\s*<', block)
        language = lang_m.group(1).strip() if lang_m else None

        # GitHub Trending HTML wraps an <svg> inside the stargazers/forks
        # link before the count, so we can't anchor on the link's opening
        # `>`. Instead, look for the count between the link's </svg> and
        # </a>. Counts may include commas (e.g. 12,345).
        stars_m = re.search(
            r'href="/[^"]+/stargazers"[^>]*>[\s\S]*?</svg>\s*([\d,]+)\s*</a>',
            block,
        )
        stars = int(stars_m.group(1).replace(",", "")) if stars_m else 0

        forks_m = re.search(
            r'href="/[^"]+/forks"[^>]*>[\s\S]*?</svg>\s*([\d,]+)\s*</a>',
            block,
        )
        forks = int(forks_m.group(1).replace(",", "")) if forks_m else 0

        delta_m = re.search(r'([\d,]+)\s+stars\s+(today|this week|this month)', block)
        stars_delta = int(delta_m.group(1).replace(",", "")) if delta_m else 0

        repos.append({
            "full_name": full_name,
            "description": description[:500],
            "language": language,
            "url": f"https://github.com/{full_name}",
            "stars": stars,
            "stars_delta": stars_delta,
            "forks": forks,
            "rank": i,
        })

    return repos


def fetch_trending(period):
    url = TRENDING_URL.format(period=period)
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        repos = parse_trending_page(html)
        return {"status": "ok", "period": period, "repos": repos, "count": len(repos)}
    except Exception as e:
        return {"status": "failed", "period": period, "repos": [], "error": str(e)}


def cmd_fetch(conn, args=None):
    report_hour = None
    if args:
        for i, a in enumerate(args):
            if a == "--report-hour" and i + 1 < len(args):
                report_hour = int(args[i + 1])

    now = datetime.now(timezone.utc).isoformat()
    stats = {"periods": {}, "total_new_repos": 0, "total_entries": 0, "failed": []}

    for period in PERIODS:
        result = fetch_trending(period)
        if result["status"] == "ok":
            new_repos = 0
            for r in result["repos"]:
                existing = conn.execute(
                    "SELECT full_name FROM repos WHERE full_name = ?", (r["full_name"],)
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT INTO repos (full_name, description, language, url, first_seen) VALUES (?, ?, ?, ?, ?)",
                        (r["full_name"], r["description"], r["language"], r["url"], now),
                    )
                    new_repos += 1
                else:
                    conn.execute(
                        "UPDATE repos SET description = ?, language = ? WHERE full_name = ?",
                        (r["description"], r["language"], r["full_name"]),
                    )

                conn.execute(
                    """INSERT INTO trending_entries (repo, period, rank, stars, stars_delta, forks, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (r["full_name"], period, r["rank"], r["stars"], r["stars_delta"], r["forks"], now),
                )

            stats["periods"][period] = {"count": result["count"], "new_repos": new_repos}
            stats["total_new_repos"] += new_repos
            stats["total_entries"] += result["count"]
        else:
            stats["failed"].append({"period": period, "error": result.get("error", "unknown")})

        time.sleep(1)

    conn.commit()

    import zoneinfo
    local_hour = datetime.now(zoneinfo.ZoneInfo("America/Los_Angeles")).hour
    if report_hour is not None:
        stats["report"] = (local_hour == report_hour)
    else:
        stats["report"] = True

    print(json.dumps(stats, ensure_ascii=False, indent=2))


def cmd_stats(conn, args):
    days = int(args[0]) if args else 7
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    total_repos = conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
    total_entries = conn.execute(
        "SELECT COUNT(*) FROM trending_entries WHERE fetched_at >= ?", (cutoff,)
    ).fetchone()[0]

    by_period = conn.execute(
        "SELECT period, COUNT(*) as cnt FROM trending_entries WHERE fetched_at >= ? GROUP BY period",
        (cutoff,),
    ).fetchall()

    by_lang = conn.execute(
        """SELECT r.language, COUNT(DISTINCT t.repo) as cnt
           FROM trending_entries t JOIN repos r ON t.repo = r.full_name
           WHERE t.fetched_at >= ? AND r.language IS NOT NULL
           GROUP BY r.language ORDER BY cnt DESC LIMIT 10""",
        (cutoff,),
    ).fetchall()

    print(f"📊 过去 {days} 天统计：")
    print(f"   总 repo 数（历史）：{total_repos}")
    print(f"   trending 记录数：{total_entries}")
    print(f"   按周期：")
    for r in by_period:
        print(f"     {r['period']}: {r['cnt']} 条")
    print(f"   热门语言：")
    for r in by_lang:
        print(f"     {r['language']}: {r['cnt']} 个项目")


def main():
    conn = get_db()
    init_db(conn)

    if len(sys.argv) < 2 or sys.argv[1] == "fetch":
        cmd_fetch(conn, sys.argv[1:] if len(sys.argv) > 1 else None)
    elif sys.argv[1] == "stats":
        cmd_stats(conn, sys.argv[2:])
    else:
        print(__doc__)
        sys.exit(1)

    conn.close()


if __name__ == "__main__":
    main()
