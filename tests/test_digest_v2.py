import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import digest_v2 as v2
import lifecycle as lc
import project_memory as pm
import star_history as sh
from db import init_db
from repo_meta import ensure_release_table, ensure_table

DAY = date(2026, 11, 20)


def _d(n):
    return DAY - timedelta(days=n)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    init_db(c)
    for f in (ensure_table, ensure_release_table, sh.ensure_table, lc.ensure_table, pm.ensure_table):
        f(c)

    def repo(name, created="2025-01-01T00:00:00Z", what=None, desc="desc"):
        c.execute("INSERT INTO repos (full_name, description, language, url, first_seen) VALUES (?,?,?,?,?)",
                  (name, desc, "Python", "u", "t"))
        c.execute("INSERT INTO repo_meta (full_name, created_at, license, owner_type, owner_kind, fetched_at)"
                  " VALUES (?,?,?,?,?,?)", (name, created, "MIT", "User", "user", "t"))
        if what:
            c.execute("INSERT INTO projects (repo, what_zh, what_basis) VALUES (?,?,?)",
                      (name, what, pm.basis_text(desc, None)))

    def listed(name, n, period, stars, delta):
        c.execute("INSERT INTO trending_entries (repo, period, rank, stars, stars_delta, forks, fetched_at)"
                  " VALUES (?,?,1,?,?,0,?)", (name, period, stars, delta, f"{_d(n).isoformat()}T19:35:00+00:00"))

    # monthly giant (security-audit-like): on the monthly list for 31 days, own 30d +40,000
    repo("c/audit", what="分阶段做代码安全审计的 Agent 技能")
    for n in range(31, -1, -1):
        listed("c/audit", n, "monthly", 76_000 - 1_250 * n, 44_608)
    # brand-new young repo on today's daily list
    repo("n/new", created="2026-10-25T00:00:00Z", what="新出的本地视频剪辑技能")
    listed("n/new", 0, "daily", 9_000, 2_500)
    # come-and-go repo: listed 20..10 days ago, back today
    repo("o/sp", what="给编程 Agent 的技能框架")
    for n in range(20, 9, -1):
        listed("o/sp", n, "daily", 300_000 - 600 * n, 600)
    listed("o/sp", 0, "daily", 300_000, 900)
    # repo without a cached 是什么 yet
    repo("w/nowhat", created="2026-10-01T00:00:00Z", what=None, desc="A tiny tool")
    listed("w/nowhat", 0, "weekly", 4_000, 1_500)
    sh.backfill_from_trending(c)
    for n in range(32, 0, -1):          # previous days' lifecycle, so 'changes' work
        lc.refresh(c, _d(n))
    c.commit()
    return c


def _llm(p, **over):
    why = {n: "" for n in p["prompts"]["why_keys"]}
    out = {"why": why,
           "themes": [{"title": f"主题{i}", "text": "一句话洞察", "repos": [p["top"][0]]} for i in range(3)],
           "what": {n: "一个很小的命令行工具" for n in p["meta"]["what_pending"]}}
    out.update(over)
    return out


def test_payload_rankings_and_changes(conn):
    p = v2.build_payload(conn, DAY)
    assert p["top"][0] == "c/audit"                       # monthly giant leads the 30d ranking
    r = p["repos"]["c/audit"]
    assert (r["g30"], r["src30"]) == (37_500, "own")      # 30 × 1,250
    assert p["repos"]["n/new"]["change"] == "新上榜" and p["detail"][0] == "n/new"
    assert p["repos"]["o/sp"]["status"] == "回榜"
    assert "n/new" in p["rising"] and "c/audit" not in p["rising"]
    assert p["meta"]["what_pending"] == ["w/nowhat"]
    assert p["repos"]["c/audit"]["ui"] == "http://little-claws-mac-mini.local:8082/repo/c/audit"


def test_render_and_check_roundtrip(conn):
    p = v2.build_payload(conn, DAY)
    llm = _llm(p, why={**{n: "" for n in p["prompts"]["why_keys"]},
                       "n/new": "11 月 19 日发布 v1.0，支持加字幕"})
    assert v2.validate_llm(llm, p) == []
    md = v2.render(p, llm)
    rep = v2.check(md, p)
    assert rep["ok"], rep["problems"]
    assert "**为什么涨**：11 月 19 日发布 v1.0，支持加字幕" in md
    assert "一个很小的命令行工具" in md                    # pending 是什么 filled
    assert md.count("**为什么涨**") == 1                    # empty why → line omitted
    assert "30 天 +37,500 · 7 天 +8,750" in md


def test_validate_llm_rejects_bad_json(conn):
    p = v2.build_payload(conn, DAY)
    bad = _llm(p, why={"n/new": "日增 +2,500，热度持续"}, themes=[{"title": "x", "text": "y", "repos": ["z/z"]}])
    probs = v2.validate_llm(bad, p)
    assert any(x.startswith("why restates star numbers") for x in probs)
    assert any(x.startswith("why keys differ") for x in probs)
    assert "themes: 1 items, expected 3" in probs
    assert any("not in today's digest" in x for x in probs)
    assert v2.validate_llm(_llm(p, what={}), p) == ["what missing for w/nowhat"]
    status_as_reason = _llm(p, why={**{n: "" for n in p["prompts"]["why_keys"]}, "n/new": "首次上榜"})
    assert any("status as a reason" in x for x in v2.validate_llm(status_as_reason, p))


def test_check_catches_edits(conn):
    p = v2.build_payload(conn, DAY)
    md = v2.render(p, _llm(p))
    assert v2.check(md, p)["ok"]
    edited = md.replace("30 天 +37,500", "30 天 +40,000")
    assert any("top #1 c/audit" in x for x in v2.check(edited, p)["problems"])
    foreign = md + "\n- [x/y](https://github.com/x/y)\n"
    assert "unknown link: https://github.com/x/y" in v2.check(foreign, p)["problems"]
    fake_new = md.replace("[o/sp](http://little-claws-mac-mini.local:8082/repo/o/sp) · 给编程 Agent 的技能框架",
                          "[o/sp](http://little-claws-mac-mini.local:8082/repo/o/sp) · 「新上榜」 给编程 Agent 的技能框架")
    assert "o/sp: 「新上榜」 doesn't match the data" in v2.check(fake_new, p)["problems"]
    no_foot = md.replace(p["footnote"], "")
    assert "footnote missing or edited" in v2.check(no_foot, p)["problems"]


def test_fmt_gain_marks_sources():
    assert v2.fmt_gain(44_608, "github_monthly") == "+44,608*"
    assert v2.fmt_gain(4_000, "own_partial") == "≥+4,000"
    assert v2.fmt_gain(1_234, "own_interp") == "+1,234"
    assert v2.fmt_gain(None, None) == "—"
