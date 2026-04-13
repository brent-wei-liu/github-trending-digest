#!/usr/bin/env python3
"""
GitHub Trending Digest Generator — outputs trending data + 3-step prompt templates.

Designed for Hermes cron: outputs JSON to stdout, agent orchestrates
Draft → Critique → Refine via delegate_task.

Usage:
  python3 digest_generate.py query [--days 1] [--focus ai-ml]
  python3 digest_generate.py save-summary [--days 1] [--focus default]  # stdin
  python3 digest_generate.py stats
"""

import json
import sys
from datetime import datetime, timezone, timedelta

from db import get_db, init_db


def cmd_query(conn, args):
    days = 1
    focus_name = "default"

    i = 0
    while i < len(args):
        if args[i] == "--days" and i + 1 < len(args):
            days = int(args[i + 1]); i += 2
        elif args[i] == "--focus" and i + 1 < len(args):
            focus_name = args[i + 1]; i += 2
        else:
            i += 1

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Focus profile
    profile_row = conn.execute(
        "SELECT rules FROM focus_profiles WHERE name = ?", (focus_name,)
    ).fetchone()
    focus_rules = json.loads(profile_row["rules"]) if profile_row else {}
    focus_instructions = focus_rules.get("instructions", "")
    focus_languages = focus_rules.get("languages", [])
    keywords = focus_rules.get("keywords", [])
    top_n = focus_rules.get("top_n", 15)

    # Get trending entries grouped by period
    sql = """
        SELECT t.repo, r.description, r.language, r.url,
               t.period, MIN(t.rank) as best_rank,
               MAX(t.stars) as max_stars, MAX(t.stars_delta) as max_delta,
               MAX(t.forks) as max_forks
        FROM trending_entries t
        JOIN repos r ON t.repo = r.full_name
        WHERE t.fetched_at >= ?
        GROUP BY t.repo, t.period
        ORDER BY t.period, best_rank
    """
    rows = conn.execute(sql, (cutoff,)).fetchall()

    repos = []
    for r in rows:
        repos.append({
            "repo": r["repo"],
            "description": r["description"] or "",
            "language": r["language"] or "",
            "url": r["url"],
            "period": r["period"],
            "best_rank": r["best_rank"],
            "stars": r["max_stars"],
            "stars_delta": r["max_delta"],
            "forks": r["max_forks"],
        })

    # Filter by focus
    if focus_languages or keywords:
        def matches(r):
            if focus_languages and r["language"] in focus_languages:
                return True
            if keywords:
                text = (r["repo"] + " " + r["description"]).lower()
                if any(kw in text for kw in keywords):
                    return True
            return False
        focused = [r for r in repos if matches(r)]
        other = [r for r in repos if not matches(r)]
    else:
        focused = repos
        other = []

    # Streaks (repos trending multiple days)
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

    # Language distribution
    lang_sql = """
        SELECT r.language, COUNT(DISTINCT t.repo) as count
        FROM trending_entries t JOIN repos r ON t.repo = r.full_name
        WHERE t.fetched_at >= ? AND t.period = 'daily' AND r.language IS NOT NULL
        GROUP BY r.language ORDER BY count DESC LIMIT 10
    """
    lang_dist = [dict(r) for r in conn.execute(lang_sql, (cutoff,)).fetchall()]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build text for prompts — group by period
    daily = [r for r in focused if r["period"] == "daily"][:top_n]
    weekly = [r for r in focused if r["period"] == "weekly"][:10]

    def repo_line(r, idx):
        return (
            f"{idx}. **{r['repo']}** ({r['language'] or '?'}) ⭐{r['stars']:,} (+{r['stars_delta']})\n"
            f"   {r['description'][:150]}\n"
            f"   {r['url']}"
        )

    daily_text = "\n\n".join(repo_line(r, i) for i, r in enumerate(daily, 1))
    weekly_text = "\n\n".join(repo_line(r, i) for i, r in enumerate(weekly, 1))
    streak_text = "\n".join(
        f"- {s['repo']}（连续 {s['days_on_list']} 天）" for s in streaks[:5]
    ) if streaks else "无"
    lang_text = "\n".join(
        f"- {l['language']}: {l['count']} 个项目" for l in lang_dist[:8]
    ) if lang_dist else "无"

    draft_prompt = f"""你是 GitHub Trending 中文日报的撰稿人。请根据以下数据撰写一份精炼的中文摘要。

日期：{today}
Focus: {focus_name}
{f'Focus 说明：{focus_instructions}' if focus_instructions else ''}

## Daily Trending（今日热门）

{daily_text}

## Weekly Trending（本周热门）

{weekly_text}

## 连续上榜项目

{streak_text}

## 语言分布

{lang_text}

## 要求

1. 用中文撰写，项目名和技术术语保留英文
2. 按类别分组（如 AI/ML、开发工具、基础设施、前端、安全等）
3. 每个项目：名称（带链接）、语言、星数、一句话中文描述
4. 特别标注今日新星（stars_delta 大的）和连续上榜项目
5. 末尾加 "今日趋势"（2-3 句话总结）
6. 总长控制在 800-1200 字"""

    critique_template = """你是一位资深开源社区观察者。请审阅以下 GitHub Trending 中文日报初稿。

## 初稿

{draft}

## 审稿要求

1. 分类是否合理？
2. 项目描述是否准确？有没有误解项目用途？
3. "今日趋势" 是否有洞察？
4. 有没有遗漏重要的热门项目？
5. 文字是否简洁流畅？

请按 A/B/C 评级并给出具体修改建议。"""

    refine_template = """你是 GitHub Trending 中文日报的终稿编辑。请根据审稿意见修改初稿。

## 初稿

{draft}

## 审稿意见

{critique}

## 要求

1. 根据审稿意见逐条修改
2. 保持原有格式和链接
3. 终稿直接输出，不要包含修改说明"""

    output = {
        "meta": {
            "date": today,
            "days": days,
            "focus": focus_name,
            "focus_instructions": focus_instructions,
            "total_repos": len(repos),
            "focused_repos": len(focused),
            "daily_count": len(daily),
            "weekly_count": len(weekly),
        },
        "repos": focused[:top_n],
        "streaks": streaks,
        "language_distribution": lang_dist,
        "prompts": {
            "draft": draft_prompt,
            "critique_template": critique_template,
            "refine_template": refine_template,
        },
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


def cmd_save_summary(conn, args):
    content = sys.stdin.read().strip()
    if not content:
        print('{"error": "no content on stdin"}')
        return
    days = 1
    focus = "default"
    i = 0
    while i < len(args):
        if args[i] == "--days" and i + 1 < len(args):
            days = int(args[i + 1]); i += 2
        elif args[i] == "--focus" and i + 1 < len(args):
            focus = args[i + 1]; i += 2
        else:
            i += 1

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO summaries (date, period, focus, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (today, "daily", focus, content, now),
    )
    conn.commit()
    print(json.dumps({"saved": True, "date": today, "focus": focus}))


def cmd_stats(conn):
    total_repos = conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
    entries = conn.execute("SELECT COUNT(*) FROM trending_entries").fetchone()[0]
    summaries = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
    last_fetch = conn.execute("SELECT MAX(fetched_at) FROM trending_entries").fetchone()[0]
    print(json.dumps({
        "total_repos": total_repos,
        "total_entries": entries,
        "total_summaries": summaries,
        "last_fetch": last_fetch,
    }, indent=2))


def main():
    conn = get_db()
    init_db(conn)

    if len(sys.argv) < 2 or sys.argv[1] == "query":
        cmd_query(conn, sys.argv[2:] if len(sys.argv) > 2 else [])
    elif sys.argv[1] == "save-summary":
        cmd_save_summary(conn, sys.argv[2:])
    elif sys.argv[1] == "stats":
        cmd_stats(conn)
    else:
        print(__doc__)
        sys.exit(1)

    conn.close()


if __name__ == "__main__":
    main()
