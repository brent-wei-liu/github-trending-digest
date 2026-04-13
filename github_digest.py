#!/usr/bin/env python3
"""
GitHub Trending Digest — query, summarize, and manage subscribers.

Usage:
  python3 gh_digest.py query [days] [--period X] [--language Y] [--focus Z]
  python3 gh_digest.py save-summary [period] [focus]    # Save summary from stdin
  python3 gh_digest.py focus-profiles                    # List focus profiles
  python3 gh_digest.py add-focus <name> <json>           # Add a focus profile
  python3 gh_digest.py subscribers                       # List subscribers
  python3 gh_digest.py add-subscriber --email <email> [--name <name>] [--focus <focus>]
  python3 gh_digest.py remove-subscriber <email>
  python3 gh_digest.py toggle-subscriber <email>
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

from db import get_db, init_db


# ── Querying ──────────────────────────────────────────────────────────

def cmd_query(conn, args):
    days = 1
    period_filter = None
    language_filter = None
    focus_name = "default"

    i = 0
    while i < len(args):
        if args[i] == "--period":
            period_filter = args[i + 1]; i += 2
        elif args[i] == "--language":
            language_filter = args[i + 1]; i += 2
        elif args[i] == "--focus":
            focus_name = args[i + 1]; i += 2
        elif args[i].isdigit():
            days = int(args[i]); i += 1
        else:
            i += 1

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    profile_row = conn.execute(
        "SELECT rules FROM focus_profiles WHERE name = ?", (focus_name,)
    ).fetchone()
    focus_rules = json.loads(profile_row["rules"]) if profile_row else {}

    where = ["t.fetched_at >= ?"]
    params = [cutoff]

    if period_filter:
        where.append("t.period = ?")
        params.append(period_filter)

    if language_filter:
        where.append("r.language = ?")
        params.append(language_filter)

    sql = f"""
        SELECT t.repo, r.description, r.language, r.url,
               t.period, t.rank, t.stars, t.stars_delta, t.forks, t.fetched_at
        FROM trending_entries t
        JOIN repos r ON t.repo = r.full_name
        WHERE {' AND '.join(where)}
        ORDER BY t.period, t.rank
    """
    rows = conn.execute(sql, params).fetchall()

    by_period = {}
    for r in rows:
        p = r["period"]
        if p not in by_period:
            by_period[p] = []
        by_period[p].append({
            "rank": r["rank"],
            "repo": r["repo"],
            "description": r["description"],
            "language": r["language"],
            "url": r["url"],
            "stars": r["stars"],
            "stars_delta": r["stars_delta"],
            "forks": r["forks"],
        })

    for p in by_period:
        seen = {}
        for entry in by_period[p]:
            name = entry["repo"]
            if name not in seen or entry["rank"] < seen[name]["rank"]:
                seen[name] = entry
        by_period[p] = sorted(seen.values(), key=lambda x: x["rank"])

    streak_sql = """
        SELECT repo, COUNT(DISTINCT DATE(fetched_at)) as days_on_list
        FROM trending_entries
        WHERE period = 'daily' AND fetched_at >= ?
        GROUP BY repo
        HAVING days_on_list > 1
        ORDER BY days_on_list DESC
        LIMIT 10
    """
    streak_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    streaks = [dict(r) for r in conn.execute(streak_sql, (streak_cutoff,)).fetchall()]

    lang_sql = """
        SELECT r.language, COUNT(DISTINCT t.repo) as count
        FROM trending_entries t
        JOIN repos r ON t.repo = r.full_name
        WHERE t.fetched_at >= ? AND t.period = 'daily' AND r.language IS NOT NULL
        GROUP BY r.language
        ORDER BY count DESC
    """
    lang_dist = [dict(r) for r in conn.execute(lang_sql, (cutoff,)).fetchall()]

    output = {
        "query": {
            "days_back": days,
            "cutoff": cutoff,
            "period_filter": period_filter,
            "language_filter": language_filter,
            "focus": focus_name,
        },
        "focus_rules": focus_rules,
        "total_entries": len(rows),
        "data": by_period,
        "streaks": streaks,
        "language_distribution": lang_dist,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


def cmd_save_summary(conn, args):
    content = sys.stdin.read().strip()
    if not content:
        print('{"error": "no content on stdin"}')
        return
    period = args[0] if args else "daily"
    focus = args[1] if len(args) > 1 else "default"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO summaries (date, period, focus, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (today, period, focus, content, now),
    )
    conn.commit()
    print(json.dumps({"saved": True, "date": today, "period": period, "focus": focus}))


# ── Focus Profiles ────────────────────────────────────────────────────

def cmd_focus_profiles(conn):
    rows = conn.execute("SELECT name, description, rules FROM focus_profiles ORDER BY name").fetchall()
    for r in rows:
        rules = json.loads(r["rules"])
        langs = ", ".join(rules.get("languages", [])) or "all"
        print(f"  {r['name']}: {r['description']} (languages: {langs})")


def cmd_add_focus(conn, args):
    if len(args) < 2:
        print('Usage: add-focus <name> <json-rules>')
        return
    name, rules = args[0], args[1]
    now = datetime.now(timezone.utc).isoformat()
    try:
        json.loads(rules)
    except json.JSONDecodeError:
        print('{"error": "invalid JSON"}')
        return
    conn.execute(
        "INSERT OR REPLACE INTO focus_profiles (name, description, rules, created_at) VALUES (?, ?, ?, ?)",
        (name, "", rules, now),
    )
    conn.commit()
    print(json.dumps({"added": name}))


# ── Subscribers ───────────────────────────────────────────────────────

def cmd_subscribers(conn):
    rows = conn.execute(
        "SELECT name, email, focus, enabled FROM subscribers ORDER BY name"
    ).fetchall()
    if not rows:
        print("No subscribers yet. Use: add-subscriber --email <email> [--name <name>] [--focus <focus>]")
        return
    for r in rows:
        status = "✅" if r["enabled"] else "⏸️"
        name = r["name"] or "(no name)"
        print(f"  {status} {r['email']:35s}  {name:20s}  focus={r['focus']}")


def cmd_add_subscriber(conn, args):
    email = None
    name = None
    focus = "default"

    i = 0
    while i < len(args):
        if args[i] == "--email" and i + 1 < len(args):
            email = args[i + 1]; i += 2
        elif args[i] == "--name" and i + 1 < len(args):
            name = args[i + 1]; i += 2
        elif args[i] == "--focus" and i + 1 < len(args):
            focus = args[i + 1]; i += 2
        else:
            if not email and "@" in args[i]:
                email = args[i]
            i += 1

    if not email:
        print('Usage: add-subscriber --email <email> [--name <name>] [--focus <focus>]')
        return

    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            "INSERT INTO subscribers (name, email, focus, created_at) VALUES (?, ?, ?, ?)",
            (name, email, focus, now),
        )
        conn.commit()
        print(json.dumps({"added": email, "name": name, "focus": focus}))
    except sqlite3.IntegrityError:
        print(json.dumps({"error": f"{email} already subscribed"}))


def cmd_remove_subscriber(conn, args):
    if not args:
        print('Usage: remove-subscriber <email>')
        return
    conn.execute("DELETE FROM subscribers WHERE email = ?", (args[0],))
    conn.commit()
    print(json.dumps({"removed": args[0]}))


def cmd_toggle_subscriber(conn, args):
    if not args:
        print('Usage: toggle-subscriber <email>')
        return
    row = conn.execute("SELECT enabled FROM subscribers WHERE email = ?", (args[0],)).fetchone()
    if not row:
        print(json.dumps({"error": f"{args[0]} not found"}))
        return
    new_val = 0 if row["enabled"] else 1
    conn.execute("UPDATE subscribers SET enabled = ? WHERE email = ?", (new_val, args[0]))
    conn.commit()
    print(json.dumps({"email": args[0], "status": "enabled" if new_val else "disabled"}))


# ── Main ──────────────────────────────────────────────────────────────

def main():
    conn = get_db()
    init_db(conn)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    commands = {
        "query": lambda: cmd_query(conn, args),
        "save-summary": lambda: cmd_save_summary(conn, args),
        "focus-profiles": lambda: cmd_focus_profiles(conn),
        "add-focus": lambda: cmd_add_focus(conn, args),
        "subscribers": lambda: cmd_subscribers(conn),
        "add-subscriber": lambda: cmd_add_subscriber(conn, args),
        "remove-subscriber": lambda: cmd_remove_subscriber(conn, args),
        "toggle-subscriber": lambda: cmd_toggle_subscriber(conn, args),
    }

    if cmd in commands:
        commands[cmd]()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)

    conn.close()


if __name__ == "__main__":
    main()
