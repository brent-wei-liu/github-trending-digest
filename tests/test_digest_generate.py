import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import digest_generate as g
from db import init_db
from repo_meta import ensure_table

TODAY = "2026-11-04T19:35:00+00:00"
D1 = "2026-11-03T19:35:00+00:00"
D2 = "2026-11-02T19:35:00+00:00"
WEEK_AGO = "2026-10-28T19:35:00+00:00"


# ---------- number formatting ----------

def test_fmt_num_uses_thousands_separators():
    assert g.fmt_num(111693) == "111,693"
    assert g.fmt_num(426438) == "426,438"
    assert g.fmt_num(999) == "999"
    assert g.fmt_num(0) == "0"
    assert g.fmt_num(None) == "0"


def test_fmt_delta_is_signed_and_separated():
    assert g.fmt_delta(3792) == "+3,792"
    assert g.fmt_delta(422104) == "+422,104"
    assert g.fmt_delta(94) == "+94"


def test_fmt_milestone():
    assert g.fmt_milestone(100_000) == "10 万"
    assert g.fmt_milestone(50_000) == "5 万"
    assert g.fmt_milestone(1_000_000) == "100 万"
    assert g.fmt_milestone(5_000) == "5,000"


def test_milestones():
    assert g.milestone_for(99_325, 99_000) == "差 675 星破 10 万星"
    assert g.milestone_for(100_551, 99_895) == "今日突破 10 万星"
    assert g.milestone_for(4_950, None) == "差 50 星破 5,000 星"
    assert g.milestone_for(90_000, 89_000) is None      # 10% away: not newsworthy
    assert g.milestone_for(100_551, None) is None        # no recent snapshot: can't claim "today"


# ---------- 数据待核 rules ----------

def test_rea_scale_numbers_are_not_flagged():
    # ~420k stars with a +422k month is real (it's a top-5 repo); no absolute ceilings.
    assert g.entry_impossible("monthly", 426438, 422104) is None
    assert g.window_anomaly("monthly", 429179, 426427, 3000, date(2026, 10, 5)) is None


def test_impossible_numbers():
    assert g.entry_impossible("daily", 0, 500) == "抓取到的总星数为 0"
    assert g.entry_impossible("weekly", 1000, 50000) == "周增 +50,000 大于总星数 ⭐1,000"
    # Counter race on a brand-new repo: a few stars over is noise.
    assert g.entry_impossible("monthly", 5711, 5721) is None


def test_snapshot_drop_and_conservative_jump():
    d = date(2026, 11, 4)
    assert g.snapshot_anomaly(50000, 100000, date(2026, 11, 3), d) == \
        "总星数较本地 11-03 快照 ⭐100,000 下降 50%"
    # rea's real growth (68k → 94k in a day) must NOT be flagged.
    assert g.snapshot_anomaly(93949, 68464, date(2026, 11, 3), d) is None
    # Only flag when the total more than doubles AND grows >100k/day.
    assert g.snapshot_anomaly(426000, 68000, date(2026, 11, 3), d) == \
        "总星数较本地 11-03 快照 ⭐68,000 跳增 +358,000"
    assert g.snapshot_anomaly(426000, 68000, date(2026, 10, 10), d) is None
    assert g.snapshot_anomaly(0, 100000, date(2026, 11, 3), d) is None


def test_window_mismatch():
    base = date(2026, 10, 28)
    # Claims +80k this week but our own snapshot a week ago shows only +5k growth.
    assert g.window_anomaly("weekly", 105000, 80000, 100000, base) == \
        "周增 +80,000 与本地快照不符（10-28 ⭐100,000 → 今日 ⭐105,000）"
    assert g.window_anomaly("weekly", 105000, 4500, 100000, base) is None
    assert g.window_anomaly("weekly", 105000, 4500, 0, base) is None


def test_list_streak_skips_days_without_any_scrape():
    s = {date(2026, 11, d) for d in (1, 2, 4)}   # no scrape on 11-03
    assert g.list_streak({date(2026, 11, 2), date(2026, 11, 4)}, s, date(2026, 11, 4)) == 2
    assert g.list_streak({date(2026, 11, 4)}, s, date(2026, 11, 4)) == 1
    assert g.list_streak({date(2026, 11, 1), date(2026, 11, 4)}, s, date(2026, 11, 4)) == 1


# ---------- payload ----------

def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    ensure_table(conn)
    return conn


def _entry(conn, repo, period, rank, stars, delta, at, created="2026-01-01T00:00:00Z",
           lic="MIT", kind="user", lang="Python"):
    conn.execute(
        "INSERT OR IGNORE INTO repos (full_name, description, language, url, first_seen) VALUES (?,?,?,?,?)",
        (repo, f"{repo} desc", lang, f"https://github.com/{repo}", at),
    )
    if created is not None:
        conn.execute(
            "INSERT OR IGNORE INTO repo_meta (full_name, created_at, license, owner_type, owner_kind, fetched_at) VALUES (?,?,?,?,?,?)",
            (repo, created, lic, "User", kind, TODAY),
        )
    conn.execute(
        "INSERT INTO trending_entries (repo, period, rank, stars, stars_delta, forks, fetched_at) VALUES (?,?,?,?,?,?,?)",
        (repo, period, rank, stars, delta, 0, at),
    )


@pytest.fixture
def conn():
    c = _db()
    # history
    _entry(c, "a/mem", "daily", 1, 110000, 1000, D2)
    _entry(c, "a/mem", "daily", 1, 110500, 1100, D1)
    _entry(c, "v/studio", "monthly", 3, 99895, 47000, D1)
    _entry(c, "w/liar", "weekly", 3, 100000, 1000, WEEK_AGO)
    # today: daily list ranks don't follow gains
    _entry(c, "a/mem", "daily", 1, 111693, 1138, TODAY, kind="user")
    _entry(c, "b/new", "daily", 2, 12325, 3792, TODAY, created="2026-10-20T00:00:00Z", kind="big_tech")
    _entry(c, "c/old", "daily", 3, 95000, 2000, TODAY, created="2024-01-01T00:00:00Z")
    _entry(c, "z/zero", "daily", 4, 0, 9000, TODAY)
    _entry(c, "a/mem", "weekly", 2, 111693, 7255, TODAY)
    _entry(c, "w/liar", "weekly", 1, 105000, 80000, TODAY)
    _entry(c, "y/young", "weekly", 4, 30000, 14000, TODAY, created="2026-09-01T00:00:00Z", lic=None)
    _entry(c, "e/rea", "monthly", 1, 426438, 422104, TODAY, created="2026-04-14T00:00:00Z")
    _entry(c, "v/studio", "monthly", 2, 100551, 47605, TODAY)
    _entry(c, "s/small", "monthly", 4, 3878, 3213, TODAY, created=None, lang=None)
    c.commit()
    return c


def test_payload_uses_only_todays_snapshot(conn):
    p = g.build_payload(conn)
    assert p["meta"]["date"] == "2026-11-04"
    assert p["meta"]["total_repos"] == 9
    assert p["meta"]["per_period"] == {"daily": 4, "weekly": 3, "monthly": 3}


def test_gainers_ranked_by_daily_gain_not_rank(conn):
    p = g.build_payload(conn)
    # z/zero has an impossible total → never ranked.
    assert p["gainers"] == ["b/new", "c/old", "a/mem"]
    assert p["meta"]["impossible"] == ["z/zero"]
    assert "z/zero" in p["others"]


def test_rising_is_young_or_small_and_not_in_gainers(conn):
    p = g.build_payload(conn)
    # y/young (+2,000/day from weekly), s/small (<20k stars, +107/day from monthly);
    # e/rea, v/studio, w/liar are old and big.
    assert p["rising"] == ["y/young", "s/small"]
    assert set(p["others"]) == {"z/zero", "w/liar", "e/rea", "v/studio"}


def test_item_lines(conn):
    by = {r["repo"]: r for r in g.build_payload(conn)["repos"]}
    a = by["a/mem"]["item_lines"]
    assert a["tag_suffix"] == "「个人开发者」 · Python · MIT"
    assert a["stats_line"] == "日增 +1,138（昨日 +1,100） · 总星 ⭐111,693 · 上线 307 天 · 在榜 3 天"
    b = by["b/new"]["item_lines"]
    assert b["tag_suffix"].startswith("「大厂官方」")
    assert b["stats_line"] == "日增 +3,792（昨日未上日榜） · 总星 ⭐12,325 · 上线 15 天 · 在榜 1 天 · 首次上榜"
    y = by["y/young"]["item_lines"]
    assert y["stats_line"].startswith("周增 +14,000（约 +2,000/天） · 总星 ⭐30,000 · 上线 64 天")
    assert y["tag_suffix"] == "「个人开发者」 · Python · 未声明许可"
    # No cached API metadata: tags and age are simply omitted.
    s = by["s/small"]["item_lines"]
    assert s["tag_suffix"] == "—" and "上线" not in s["stats_line"]


def test_line_prefix_formatting(conn):
    by = {r["repo"]: r for r in g.build_payload(conn)["repos"]}
    assert by["a/mem"]["line_prefix"] == (
        "[a/mem](https://github.com/a/mem) · Python · ⭐111,693 · "
        "日榜第 1，日增 +1,138；周榜第 2，周增 +7,255")


def test_anomaly_flags_are_conservative(conn):
    p = g.build_payload(conn)
    by = {r["repo"]: r for r in p["repos"]}
    assert p["meta"]["anomalies"] == ["z/zero", "w/liar"]
    assert by["w/liar"]["anomaly_reason"] == \
        "周增 +80,000 与本地快照不符（10-28 ⭐100,000 → 今日 ⭐105,000）"
    assert not by["w/liar"]["impossible"]
    assert not by["e/rea"]["anomaly"]


def test_hook_facts_include_milestone_and_top_gainer(conn):
    p = g.build_payload(conn)
    assert "v/studio 今日突破 10 万星（⭐100,551）" in p["hook_facts"]
    assert p["hook_facts"][-1] == "日增第一：b/new +3,792"


def test_prompts_carry_verbatim_lines(conn):
    p = g.build_payload(conn)
    d = p["prompts"]["draft"]
    assert "数据行：日增 +3,792（昨日未上日榜） · 总星 ⭐12,325" in d
    assert "## 涨星榜 Top 10" in d and "## 新锐榜 Top 2" in d and "## 今天这三件事" in d
    assert "⚠️ 数据待核：周增 +80,000 与本地快照不符" in d
    assert "{draft}" in p["prompts"]["critique_template"]
    assert "{critique}" in p["prompts"]["refine_template"]
    assert "z/zero、w/liar" in p["prompts"]["refine_template"]


def test_empty_db_payload():
    p = g.build_payload(_db())
    assert p["meta"]["total_repos"] == 0 and p["repos"] == []


# ---------- check ----------

def _digest(p, themes=3, swap=False):
    by = {r["repo"]: r for r in p["repos"]}
    out = ["# GitHub Trending 日报 · 2026-11-04", "", "**今日看点**：v/studio 今日突破 10 万星", ""]
    for heading, names in ((g.GAINERS_HEADING, p["gainers"]),
                           (f"{g.RISING_HEADING} Top {len(p['rising'])}", p["rising"])):
        names = list(reversed(names)) if swap and heading == g.GAINERS_HEADING else names
        out += [heading, ""]
        for i, n in enumerate(names, 1):
            r, il = by[n], by[n]["item_lines"]
            out += [f"### {i}. [{n}]({r['url']}) · 白话标题", f"「AI 编程」{il['tag_suffix']}", il["stats_line"]]
            if "anomaly_line" in il:
                out.append(il["anomaly_line"])
            out += ["**是什么**：一句话。", "**为什么涨**：今日日增 +1。", ""]
    out += [g.OTHERS_HEADING]
    for n in p["others"]:
        r = by[n]
        note = f"{g.ANOMALY_MARK}：{r['anomaly_reason']}" if r["anomaly"] else "短备注"
        out.append(f"- {r['line_prefix']} · {note}")
    out += ["", g.THEMES_HEADING] + [f"**{i}. 标题**：一句话（a/mem）" for i in range(1, themes + 1)]
    out += ["", "---", p["footnote"]]
    return "\n".join(out)


def test_check_accepts_valid_digest(conn):
    p = g.build_payload(conn)
    report = g.check_digest(_digest(p), p)
    assert report["ok"], report["problems"]
    assert report["repos_found"] == report["repos_expected"] == 9


def test_check_rejects_reordered_ranking_and_wrong_theme_count(conn):
    p = g.build_payload(conn)
    probs = g.check_digest(_digest(p, themes=4, swap=True), p)["problems"]
    assert any(x.startswith("## 涨星榜 Top 10 order/membership differs") for x in probs)
    assert "今天这三件事 has 4 items, expected 3" in probs


def test_check_catches_edited_numbers_missing_repo_and_unknown_repo(conn):
    p = g.build_payload(conn)
    text = _digest(p).replace("总星 ⭐12,325", "总星 ⭐12325").replace("⭐426,438", "⭐426438")
    text = "\n".join(ln for ln in text.split("\n") if "v/studio](" not in ln)
    text += "\n- [x/y](https://github.com/x/y)"
    probs = g.check_digest(text, p)["problems"]
    assert any(x.startswith("b/new: stats_line missing or edited") for x in probs)
    assert any(x.startswith("numbers changed for e/rea") for x in probs)
    assert "missing repo: v/studio" in probs
    assert "repo not in today's data: x/y" in probs


def test_check_requires_anomaly_mark_and_hook(conn):
    p = g.build_payload(conn)
    text = _digest(p).replace(f"{g.ANOMALY_MARK}：周增", "周增").replace("**今日看点**", "看点")
    probs = g.check_digest(text, p)["problems"]
    assert "anomaly not marked: w/liar" in probs
    assert "今日看点 line missing" in probs
