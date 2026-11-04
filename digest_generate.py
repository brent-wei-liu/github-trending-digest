#!/usr/bin/env python3
"""
GitHub Trending Digest Generator — builds today's snapshot payload + 3-step prompts.

Outputs JSON to stdout. The Claude Code `github-trending-digest` skill
orchestrates Draft → Critique → Refine via the Agent (Task) tool.

Everything deterministic happens here, not in the LLM: today's snapshot, the
membership and order of 涨星榜 / 新锐榜, milestone candidates, per-item stats
lines (thousands separators, 上线/在榜 days, yesterday's gain), owner/license
tags (from repo_meta.py's cached GitHub API data) and 「⚠️ 数据待核」 flags.

Usage:
  python3 digest_generate.py query [--date YYYY-MM-DD] [--focus default]
  python3 digest_generate.py check FILE [--date YYYY-MM-DD] [--focus default]
  python3 digest_generate.py save-summary [--focus default]  # stdin
  python3 digest_generate.py stats
"""

import json
import re
import sys
from datetime import date as date_cls, datetime, timedelta, timezone

from db import get_db, init_db
from repo_meta import LICENSE_NONE, OWNER_KIND_LABEL, ensure_table

PERIODS = ("daily", "weekly", "monthly")
LABELS = {"daily": ("日榜", "日增"), "weekly": ("周榜", "周增"), "monthly": ("月榜", "月增")}
PERIOD_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}

TOP_N = 10
# 新锐榜: young or small projects, ranked by average gain per day.
YOUNG_DAYS = 180
SMALL_STARS = 20_000
MILESTONE_LOOKBACK_DAYS = 3   # 「今日突破」 only against a recent snapshot
MILESTONES = (1_000, 5_000, 10_000, 20_000, 50_000, 100_000, 200_000, 300_000,
              500_000, 1_000_000)
MILESTONE_GAP = 0.02       # "差 N 星破 X" when within 2% below X

# 数据待核 rules — compare only against our own DB snapshots, conservatively.
# Absolute ceilings were dropped: morluto/rea's ~420k monthly gain is real.
OVER_TOTAL_SLACK = 100     # counter race: increment may exceed total by a few
DROP_RATIO = 0.8           # total fell below 80% of an earlier snapshot
JUMP_PER_DAY = 100_000     # total grew >100k/day AND more than doubled
WINDOW_FACTOR = 2          # weekly/monthly increment vs our snapshot one window ago

ANOMALY_MARK = "⚠️ 数据待核"
HOOK_LABEL = "**今日看点**"
GAINERS_HEADING = "## 涨星榜 Top 10"
RISING_HEADING = "## 新锐榜"
OTHERS_HEADING = "## 其他在榜"
THEMES_HEADING = "## 今天这三件事"


def fmt_num(n):
    """111693 → '111,693'."""
    return f"{int(n or 0):,}"


def fmt_delta(n):
    """3792 → '+3,792'."""
    return f"+{int(n or 0):,}"


def fmt_milestone(n):
    """100000 → '10 万', 5000 → '5,000'."""
    if n >= 10_000:
        w = n / 10_000
        return f"{int(w) if w == int(w) else w:g} 万"
    return fmt_num(n)


def _d(s):
    return date_cls.fromisoformat(s[:10])


# ---------- 数据待核 ----------

def entry_impossible(period, stars, delta):
    """Numbers that cannot be right on their own. Returns a reason or None."""
    if not stars or stars <= 0:
        return "抓取到的总星数为 0"
    if delta > stars + max(OVER_TOTAL_SLACK, stars * 0.02):
        return f"{LABELS[period][1]} {fmt_delta(delta)} 大于总星数 ⭐{fmt_num(stars)}"
    return None


def snapshot_anomaly(stars, prev_stars, prev_date, snap_date):
    """Today's total vs the repo's most recent earlier snapshot in our DB."""
    if not stars or not prev_stars or prev_stars <= 0:
        return None
    gap = max((snap_date - prev_date).days, 1)
    md = prev_date.strftime("%m-%d")
    if stars < prev_stars * DROP_RATIO:
        pct = round((1 - stars / prev_stars) * 100)
        return f"总星数较本地 {md} 快照 ⭐{fmt_num(prev_stars)} 下降 {pct}%"
    jump = stars - prev_stars
    if jump > JUMP_PER_DAY * gap and stars > prev_stars * 2:
        return f"总星数较本地 {md} 快照 ⭐{fmt_num(prev_stars)} 跳增 {fmt_delta(jump)}"
    return None


def window_anomaly(period, stars, delta, base_stars, base_date):
    """A weekly/monthly increment vs our own snapshot from one window earlier."""
    if not base_stars or base_stars <= 0 or not stars:
        return None
    observed = stars - base_stars
    if observed < 0:
        return None
    slack = 1000
    if delta > WINDOW_FACTOR * observed + slack or observed > WINDOW_FACTOR * delta + slack:
        return (f"{LABELS[period][1]} {fmt_delta(delta)} 与本地快照不符"
                f"（{base_date.strftime('%m-%d')} ⭐{fmt_num(base_stars)} → 今日 ⭐{fmt_num(stars)}）")
    return None


def detect_anomalies(conn, repo, entries, snap_date):
    """Returns (reasons, impossible)."""
    reasons, impossible = [], False
    for e in entries:
        r = entry_impossible(e["period"], e["stars"], e["delta"])
        if r:
            reasons.append(r)
            impossible = True
    stars = max((e["stars"] or 0) for e in entries)
    if not impossible:
        prev = conn.execute(
            """SELECT stars, fetched_at FROM trending_entries
               WHERE repo = ? AND DATE(fetched_at) < ? AND stars > 0
               ORDER BY fetched_at DESC LIMIT 1""",
            (repo, snap_date.isoformat()),
        ).fetchone()
        if prev:
            r = snapshot_anomaly(stars, prev[0], _d(prev[1]), snap_date)
            if r:
                reasons.append(r)
        for e in entries:
            if e["period"] == "daily":
                continue
            base_day = (snap_date - timedelta(days=PERIOD_DAYS[e["period"]])).isoformat()
            base = conn.execute(
                """SELECT stars FROM trending_entries WHERE repo = ? AND stars > 0
                   AND DATE(fetched_at) = ? ORDER BY fetched_at DESC LIMIT 1""",
                (repo, base_day),
            ).fetchone()
            if base:
                r = window_anomaly(e["period"], e["stars"], e["delta"], base[0], _d(base_day))
                if r:
                    reasons.append(r)
    return list(dict.fromkeys(reasons)), impossible


# ---------- snapshot helpers ----------

def latest_snapshot(conn, on_date=None):
    """Return (snap_date, {period: fetched_at}) for the newest scrape day."""
    if on_date is None:
        row = conn.execute("SELECT MAX(fetched_at) FROM trending_entries").fetchone()
        if not row or not row[0]:
            return None, {}
        on_date = row[0][:10]
    snaps = {}
    for p in PERIODS:
        r = conn.execute(
            "SELECT MAX(fetched_at) FROM trending_entries WHERE period = ? AND DATE(fetched_at) = ?",
            (p, on_date),
        ).fetchone()
        if r and r[0]:
            snaps[p] = r[0]
    return _d(on_date), snaps


def list_streak(repo_dates, scrape_dates, snap_date):
    """Consecutive scrape days, ending today, on which the repo was on any list.
    Days with no scrape at all are skipped rather than breaking the streak."""
    n = 0
    for d in sorted((d for d in scrape_dates if d <= snap_date), reverse=True):
        if d in repo_dates:
            n += 1
        else:
            break
    return n


def milestone_for(stars, prev_stars):
    """('差 675 星破 10 万' | '突破 10 万' | None)."""
    for t in MILESTONES:
        label = fmt_milestone(t) + ("星" if t >= 10_000 else " 星")
        if prev_stars and prev_stars < t <= stars:
            return f"今日突破 {label}"
        if 0 < t - stars <= t * MILESTONE_GAP:
            return f"差 {fmt_num(t - stars)} 星破 {label}"
    return None


def _focus_rules(conn, focus):
    row = conn.execute("SELECT rules FROM focus_profiles WHERE name = ?", (focus,)).fetchone()
    return json.loads(row[0]) if row else {}


# ---------- payload ----------

def build_payload(conn, on_date=None, focus="default"):
    ensure_table(conn)
    snap_date, snaps = latest_snapshot(conn, on_date)
    rules = _focus_rules(conn, focus)
    payload = {
        "meta": {"date": snap_date.isoformat() if snap_date else None, "focus": focus,
                 "focus_instructions": rules.get("instructions", ""),
                 "snapshots": snaps, "total_repos": 0},
        "repos": [], "gainers": [], "rising": [], "others": [],
    }
    if not snaps:
        return payload
    today = snap_date.isoformat()

    info = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT full_name, language, description FROM repos")}
    meta = {r[0]: r[1:] for r in conn.execute(
        "SELECT full_name, created_at, license, owner_kind FROM repo_meta")}

    order, by_repo = [], {}
    for p in PERIODS:
        if p not in snaps:
            continue
        for rank, repo, stars, delta in conn.execute(
            """SELECT rank, repo, stars, stars_delta FROM trending_entries
               WHERE period = ? AND fetched_at = ? ORDER BY rank""",
            (p, snaps[p]),
        ):
            if repo not in by_repo:
                by_repo[repo] = []
                order.append(repo)
            by_repo[repo].append({"period": p, "rank": rank, "stars": stars or 0,
                                  "delta": delta or 0})

    langs_f, kws = rules.get("languages", []), rules.get("keywords", [])

    def keep(repo):
        if not (langs_f or kws):
            return True
        lang, desc = info.get(repo, (None, None))
        if langs_f and lang in langs_f:
            return True
        return any(k in f"{repo} {desc or ''}".lower() for k in kws)

    scrape_dates = {_d(r[0]) for r in conn.execute(
        "SELECT DISTINCT DATE(fetched_at) FROM trending_entries WHERE DATE(fetched_at) <= ?", (today,))}
    prev_day = conn.execute(
        "SELECT MAX(DATE(fetched_at)) FROM trending_entries WHERE DATE(fetched_at) < ?", (today,)
    ).fetchone()[0]
    prev_daily = {}
    if prev_day:
        _, prev_snaps = latest_snapshot(conn, prev_day)
        if "daily" in prev_snaps:
            prev_daily = {r[0]: r[1] for r in conn.execute(
                "SELECT repo, stars_delta FROM trending_entries WHERE period = 'daily' AND fetched_at = ?",
                (prev_snaps["daily"],))}

    repos = []
    for repo in order:
        if not keep(repo):
            continue
        entries = by_repo[repo]
        lang, desc = info.get(repo, (None, None))
        created, lic, kind = meta.get(repo, (None, None, None))
        stars = max(e["stars"] for e in entries)
        dates = {_d(r[0]) for r in conn.execute(
            "SELECT DISTINCT DATE(fetched_at) FROM trending_entries WHERE repo = ? AND DATE(fetched_at) <= ?",
            (repo, today))}
        recent = (snap_date - timedelta(days=MILESTONE_LOOKBACK_DAYS)).isoformat()
        prev_total = conn.execute(
            """SELECT stars FROM trending_entries WHERE repo = ? AND DATE(fetched_at) < ?
               AND DATE(fetched_at) >= ? AND stars > 0 ORDER BY fetched_at DESC LIMIT 1""",
            (repo, today, recent)).fetchone()
        reasons, impossible = detect_anomalies(conn, repo, entries, snap_date)
        best = min(entries, key=lambda e: PERIOD_DAYS[e["period"]])  # shortest window
        daily = next((e for e in entries if e["period"] == "daily"), None)
        url = f"https://github.com/{repo}"
        ranks = "；".join(
            f"{LABELS[e['period']][0]}第 {e['rank']}，{LABELS[e['period']][1]} {fmt_delta(e['delta'])}"
            for e in entries)
        stats = f"⭐{fmt_num(stars)} · {ranks}"
        age = (snap_date - _d(created)).days if created else None
        streak = list_streak(dates, scrape_dates, snap_date)
        first_ever = not any(d < snap_date for d in dates)
        repos.append({
            "repo": repo,
            "url": url,
            "language": lang or "—",
            "description": desc or "",
            "lists": [e["period"] for e in entries],
            "entries": entries,
            "stars": stars,
            "daily_delta": daily["delta"] if daily else None,
            "yesterday_daily_delta": prev_daily.get(repo),
            "per_day_gain": round(best["delta"] / PERIOD_DAYS[best["period"]]),
            "gain_period": best["period"],
            "age_days": age,
            "created_at": created,
            "license": (lic or LICENSE_NONE) if created else None,
            "owner_kind": OWNER_KIND_LABEL.get(kind) if kind else None,
            "streak_days": streak,
            "first_ever": first_ever,
            "milestone": None if reasons else milestone_for(stars, prev_total[0] if prev_total else None),
            "anomaly": bool(reasons),
            "anomaly_reason": "；".join(reasons),
            "impossible": impossible,
            "stats": stats,
            "line_prefix": f"[{repo}]({url}) · {lang or '—'} · {stats}",
        })

    # --- rankings (deterministic) ---
    rankable = [r for r in repos if not r["impossible"]]
    gainers = sorted((r for r in rankable if r["daily_delta"] is not None),
                     key=lambda r: (-r["daily_delta"], r["repo"]))[:TOP_N]
    g_names = {r["repo"] for r in gainers}

    def young(r):
        return (r["age_days"] is not None and r["age_days"] <= YOUNG_DAYS) or r["stars"] < SMALL_STARS

    rising = sorted((r for r in rankable if r["repo"] not in g_names and young(r)),
                    key=lambda r: (-r["per_day_gain"], r["repo"]))[:TOP_N]
    featured = g_names | {r["repo"] for r in rising}
    for r in gainers + rising:
        r["item_lines"] = _item_lines(r)
    others = [r for r in repos if r["repo"] not in featured]

    milestones = [f"{r['repo']} {r['milestone']}（⭐{fmt_num(r['stars'])}）"
                  for r in repos if r["milestone"]]
    top = gainers[0] if gainers else None
    hook_facts = milestones[:]
    if top:
        hook_facts.append(f"日增第一：{top['repo']} {fmt_delta(top['daily_delta'])}"
                          + (f"（昨日 {fmt_delta(top['yesterday_daily_delta'])}）"
                             if top["yesterday_daily_delta"] is not None else ""))

    ts = max(snaps.values())
    scraped = f"{ts[:10]} {ts[11:16]} UTC"
    footnote = (
        f"口径：涨星榜按今日日增排序；新锐榜为上线 ≤{YOUNG_DAYS} 天或总星 <{fmt_num(SMALL_STARS)} 的其余项目，"
        "按日均增量（日增，或周增÷7、月增÷30）排序；日/周/月增来自三个独立榜单，不能相减。"
        f"「{ANOMALY_MARK}」是与本地历史快照比对后的提示，不代表刷星。"
        f"数据：GitHub Trending（{scraped}）+ GitHub API。"
    )

    counts = {p: sum(1 for r in repos if p in r["lists"]) for p in PERIODS}
    payload["meta"].update({
        "total_repos": len(repos),
        "per_period": counts,
        "gainers_count": len(gainers),
        "rising_count": len(rising),
        "others_count": len(others),
        "anomalies": [r["repo"] for r in repos if r["anomaly"]],
        "impossible": [r["repo"] for r in repos if r["impossible"]],
        "missing_meta": [r["repo"] for r in repos if r["created_at"] is None],
        "scraped_at": scraped,
    })
    payload.update({
        "repos": repos,
        "gainers": [r["repo"] for r in gainers],
        "rising": [r["repo"] for r in rising],
        "others": [r["repo"] for r in others],
        "milestones": milestones,
        "hook_facts": hook_facts,
        "footnote": footnote,
    })
    payload["prompts"] = render_prompts(payload)
    return payload


def _item_lines(r):
    """Verbatim tag line, stats line and optional anomaly line for a ranked item."""
    parts = [f"「{r['owner_kind']}」" if r["owner_kind"] else None, r["language"],
             r["license"]]
    tag_suffix = " · ".join(p for p in parts if p)

    p = r["gain_period"]
    if p == "daily":
        y = r["yesterday_daily_delta"]
        gain = f"日增 {fmt_delta(r['daily_delta'])}" + (
            f"（昨日 {fmt_delta(y)}）" if y is not None else "（昨日未上日榜）")
    else:
        e = next(e for e in r["entries"] if e["period"] == p)
        gain = f"{LABELS[p][1]} {fmt_delta(e['delta'])}（约 {fmt_delta(r['per_day_gain'])}/天）"
    bits = [gain, f"总星 ⭐{fmt_num(r['stars'])}"]
    if r["age_days"] is not None:
        bits.append(f"上线 {r['age_days']} 天")
    bits.append(f"在榜 {r['streak_days']} 天")
    if r["first_ever"]:
        bits.append("首次上榜")
    out = {"tag_suffix": tag_suffix, "stats_line": " · ".join(bits)}
    if r["anomaly"]:
        out["anomaly_line"] = f"{ANOMALY_MARK}：{r['anomaly_reason']}"
    return out


# ---------- prompts ----------

def _ranks(r):
    return "；".join(f"{LABELS[e['period']][0]}第 {e['rank']}，{LABELS[e['period']][1]} {fmt_delta(e['delta'])}"
                    for e in r["entries"])


def _ranked_block(label, i, r):
    il = r["item_lines"]
    lines = [
        f"【{label} #{i}】行首：### {i}. [{r['repo']}]({r['url']}) · <白话标题>",
        f"  标签行：「<分类>」{il['tag_suffix']}",
        f"  数据行：{il['stats_line']}",
    ]
    if "anomaly_line" in il:
        lines.append(f"  待核行：{il['anomaly_line']}")
    lines += [f"  官方描述：{r['description'] or '（官方描述为空）'}",
              f"  榜单原始数据：{_ranks(r)}"]
    return "\n".join(lines)


def _other_block(r):
    s = f"- 行前缀：{r['line_prefix']}\n  官方描述：{r['description'] or '（官方描述为空）'}"
    if r["anomaly"]:
        s += f"\n  备注位必须写：{ANOMALY_MARK}：{r['anomaly_reason']}"
    return s


def render_prompts(payload):
    m = payload["meta"]
    by = {r["repo"]: r for r in payload["repos"]}
    g = [by[n] for n in payload["gainers"]]
    ri = [by[n] for n in payload["rising"]]
    ot = [by[n] for n in payload["others"]]
    focus_line = f"Focus 说明：{m['focus_instructions']}\n" if m["focus_instructions"] else ""
    anomalies = "、".join(m["anomalies"]) or "无"
    hook = "\n".join(f"- {f}" for f in payload["hook_facts"]) or "- 无"
    rising_heading = f"{RISING_HEADING} Top {len(ri)}"

    draft = f"""你是「GitHub 飙升日报」的中文撰稿人。读者不一定是资深开发者：标题要让普通技术爱好者一眼看懂，正文要具体、克制、有依据。

日期：{m['date']}
{focus_line}
## 数据（脚本已定好榜单成员和顺序，不得增删或调换）

共 {m['total_repos']} 个不重复项目：涨星榜 {len(g)} 个、新锐榜 {len(ri)} 个、其他在榜 {len(ot)} 个。
「行首」「标签行」「数据行」「待核行」「行前缀」里除尖括号占位符外的内容都必须逐字复制（数字已带千分位）。

### 今日看点候选事实
{hook}

### 涨星榜（按今日日增排序）
{chr(10).join(_ranked_block('涨星榜', i, r) for i, r in enumerate(g, 1))}

### 新锐榜（上线 ≤{YOUNG_DAYS} 天或总星 <{fmt_num(SMALL_STARS)}，按日均增量排序）
{chr(10).join(_ranked_block('新锐榜', i, r) for i, r in enumerate(ri, 1)) or '（无）'}

### 其他在榜
{chr(10).join(_other_block(r) for r in ot) or '（无）'}

### 标记为数据待核的项目：{anomalies}

## 输出格式（严格遵守，Markdown）

# GitHub Trending 日报 · {m['date']}

{HOOK_LABEL}：<一句话>

{GAINERS_HEADING}

### 1. [owner/repo](url) · <白话标题>
「<分类>」「<主体类型>」 · 语言 · 许可
<数据行>
<待核行，仅被标记的项目才有>
**是什么**：<1–2 句>
**为什么涨**：<1–2 句>

（空一行，下一个项目……）

{rising_heading}

（同上格式，编号从 1 开始）

{OTHERS_HEADING}
- <行前缀> · <备注>

{THEMES_HEADING}
**1. <短标题>**：<一句话>（owner/repo、owner/repo）
**2. …**
**3. …**

---
{payload['footnote']}

## 写作规则

1. 今日看点：从「今日看点候选事实」里挑最有新闻性的一条（里程碑优先，其次日增第一），写成一句吸引人但不夸张的话，数字照抄。
2. 白话标题：不超过 20 个汉字，不用术语和英文缩写，说清「给谁解决什么」，例如「不用 U 盘，在 Windows 旁边装 Linux」「让 AI 帮你把网页设计做得不千篇一律」。
3. 分类：一个短词，如 AI 编程、Agent 技能、开发工具、多媒体、安全、效率工具、学习资料、金融。主体类型、语言、许可已在标签行里给出，照抄。
4. 是什么：1–2 句，具体说它做什么、怎么用，以官方描述为准，README 细节只作补充。不要复述功能清单。
5. 为什么涨：1–2 句，必须引用数据行或榜单原始数据里的具体数字（如「今日日增 +3,792，昨日 +2,243」「上线 45 天」「连续在榜 3 天」「周增 +9,770」）。原因只写有依据的（README、发布记录、你查到的新闻/讨论）；查不到原因就只陈述数据事实，不要猜测、不要编造事件。禁用「爆火」「疯狂」「炸裂」「神器」等夸张词。
6. 其他在榜：每个项目一行 `- <行前缀> · <备注>`，备注不超过 20 个汉字、用白话。
7. 数据待核：被标记项目照抄「待核行」或指定备注，不加评论；不要断言刷星，也不要对未标记项目做任何刷星、异常的暗示。被标记项目照常留在榜单里。
8. 今天这三件事：正好 3 条，每条一个短标题 + 一句话，括号里列出相关项目名（不加链接），写模式和洞察，不复述数字。
9. 每个项目只以链接形式出现一次；全部 {m['total_repos']} 个项目都必须出现，不得新增数据之外的项目。
10. 反杜撰：只写数据或你实际查到的事实，不编造数字、基准、客户、合规清单；查不到就不写，也不要写「数据未提供」。最高级（最、第一）只在数据能直接核对时使用。
11. 金融/交易类项目注明「不构成投资建议」；安全/逆向类项目保持中性。
12. 全文（含链接 Markdown）控制在 9,000 字符以内。"""

    critique = f"""你是一位资深开源社区观察者，也是挑剔的科技编辑。请审阅以下「GitHub 飙升日报」初稿。

## 初稿

{{draft}}

## 背景（编辑部说明）

- 榜单成员与顺序、每个项目的数据行（日增、昨日日增、总星、上线天数、在榜天数、首次上榜）、主体类型/语言/许可、「{ANOMALY_MARK}」行、链接行前缀与末尾口径脚注，都由脚本根据抓取数据和 GitHub API 生成，是权威数据。不要建议修改、调换或增删项目，也不要用你查到的实时星数替换。
- 日/周/月增来自三个独立榜单，不成比例是正常的。

## 审稿要求

1. 白话标题：普通技术爱好者能否一眼看懂？有没有术语、夸张或不准确？
2. 「是什么」是否准确具体（可用网络核实项目真实用途）？
3. 「为什么涨」是否引用了具体数字、原因是否有依据？有没有猜测或编造的事件、夸张措辞？
4. 分类标签是否合理？其他在榜的一行备注是否准确、不超过 20 字？
5. 今日看点是否抓人且属实？「今天这三件事」是否有洞察、正好 3 条？
6. 整体是否简洁，有没有套话和重复？

请按 A/B/C 评级并给出具体、可执行的修改建议（指明项目名）。"""

    refine = f"""你是「GitHub 飙升日报」的终稿编辑。请根据审稿意见修改初稿。

## 初稿

{{draft}}

## 审稿意见

{{critique}}

## 范围约束（编辑部掌握原始数据，审稿人看不到，以下为最终裁定）

1. 共 {m['total_repos']} 个项目，必须全部保留并带链接；涨星榜 {len(g)} 个、新锐榜 {len(ri)} 个的成员与顺序不得改变；不得新增项目。
2. 行首链接、标签行里的主体类型/语言/许可、数据行、待核行、其他在榜的行前缀、末尾口径脚注一字不改。
3. 数据待核项目：{anomalies}。保留待核行原样，不断言刷星，项目留在原位置。
4. 审稿意见与数据冲突时以数据为准；初稿写了官方描述、README 或可查来源都没有的具体事实（尤其是「为什么涨」里的原因）时，按审稿意见删改为只陈述数据事实。
5. 保持格式：白话标题 ≤20 字；是什么、为什么涨各 1–2 句；其他在榜每项一行、备注 ≤20 字；今天这三件事正好 3 条。
6. 终稿直接输出，不要包含修改说明。"""

    return {"draft": draft, "critique_template": critique, "refine_template": refine}


# ---------- check ----------

_HEAD_RE = re.compile(r"^###\s*(\d+)\.\s*\[([^\]]+)\]\(https://github\.com/([^)\s]+)\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(https://github\.com/([^)\s]+)\)")


def check_digest(text, payload):
    """Validate a finished digest against the payload. Returns a report dict."""
    problems, warnings = [], []
    repos = {r["repo"]: r for r in payload["repos"]}
    lines = text.split("\n")

    def section(prefix):
        out, on = [], False
        for ln in lines:
            if ln.startswith("## "):
                on = ln.strip().startswith(prefix)
                continue
            if on:
                out.append(ln)
        return out

    def blocks(sec):
        """[(repo, [lines])] for ### items in a section."""
        out = []
        for ln in sec:
            m = _HEAD_RE.match(ln.strip())
            if m:
                out.append((m.group(3), [ln]))
            elif out and not ln.startswith("### "):
                out[-1][1].append(ln)
        return out

    if not any(ln.startswith(HOOK_LABEL) for ln in lines):
        problems.append("今日看点 line missing")

    for heading, want in ((GAINERS_HEADING, payload["gainers"]), (RISING_HEADING, payload["rising"])):
        got = blocks(section(heading))
        if [b[0] for b in got] != want:
            problems.append(f"{heading} order/membership differs: got {[b[0] for b in got]}, want {want}")
        for name, blk in got:
            if name not in repos or "item_lines" not in repos[name]:
                continue
            body = "\n".join(blk)
            il = repos[name]["item_lines"]
            for key in ("tag_suffix", "stats_line", "anomaly_line"):
                if key in il and il[key] not in body:
                    problems.append(f"{name}: {key} missing or edited (want '{il[key]}')")
            for label in ("**是什么**", "**为什么涨**"):
                if label not in body:
                    problems.append(f"{name}: {label} missing")
            title = blk[0].split(")", 1)[1].lstrip(" ·") if ")" in blk[0] else ""
            if len(title) > 22:
                warnings.append(f"title over 20 chars for {name}: {title}")

    for name in payload["others"]:
        r = repos[name]
        hits = [ln for ln in section(OTHERS_HEADING) if f"[{name}]({r['url']})" in ln]
        if not hits:
            continue  # reported below as missing
        ln = hits[0]
        if r["stats"] not in ln:
            problems.append(f"numbers changed for {name}: expected '{r['stats']}'")
        if r["anomaly"] and ANOMALY_MARK not in ln:
            problems.append(f"anomaly not marked: {name}")
        note = ln.split(r["stats"], 1)[-1].lstrip(" ·")
        if ANOMALY_MARK not in note and len(note) > 24:
            warnings.append(f"note over 20 chars for {name}: {note}")

    linked = [m.group(2) for m in _LINK_RE.finditer(text)]
    for name in repos:
        n = linked.count(name)
        if n == 0:
            problems.append(f"missing repo: {name}")
        elif n > 1:
            warnings.append(f"repo linked {n} times: {name}")
    for name in dict.fromkeys(linked):
        if name not in repos:
            problems.append(f"repo not in today's data: {name}")
    for name in repos:
        if not repos[name]["anomaly"] and any(
                ANOMALY_MARK in ln and f"[{name}]" in ln for ln in lines):
            problems.append(f"unflagged repo marked as anomaly: {name}")

    themes = [ln for ln in section(THEMES_HEADING) if re.match(r"^\*\*\d+\.", ln.strip())]
    if len(themes) != 3:
        problems.append(f"今天这三件事 has {len(themes)} items, expected 3")

    if payload["footnote"] not in text:
        problems.append("口径 footnote missing or edited")

    return {
        "ok": not problems,
        "problems": problems,
        "warnings": warnings,
        "chars": len(text),
        "repos_expected": len(repos),
        "repos_found": sum(1 for n in repos if n in linked),
    }


def _opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def cmd_query(conn, args):
    payload = build_payload(conn, _opt(args, "--date"), _opt(args, "--focus", "default"))
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_check(conn, args):
    if not args or args[0].startswith("--"):
        print('{"error": "usage: check FILE [--date YYYY-MM-DD] [--focus NAME]"}')
        return 1
    with open(args[0], encoding="utf-8") as f:
        text = f.read()
    payload = build_payload(conn, _opt(args, "--date"), _opt(args, "--focus", "default"))
    report = check_digest(text, payload)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def cmd_save_summary(conn, args):
    content = sys.stdin.read().strip()
    if not content:
        print('{"error": "no content on stdin"}')
        return
    focus = _opt(args, "--focus", "default")
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
    code = 0
    cmd = sys.argv[1] if len(sys.argv) > 1 else "query"
    if cmd == "query":
        cmd_query(conn, sys.argv[2:])
    elif cmd == "check":
        code = cmd_check(conn, sys.argv[2:])
    elif cmd == "save-summary":
        cmd_save_summary(conn, sys.argv[2:])
    elif cmd == "stats":
        cmd_stats(conn)
    else:
        print(__doc__)
        code = 1
    conn.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
