import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lifecycle as lc
import weekly_digest as wk
from test_digest_v2 import DAY, conn  # noqa: F401  (fixture)


def _llm(p, **over):
    out = {"overview": "本周设计和安全类技能继续领涨。",
           "writeups": {n: {"deep": "它把安全审计拆成几个阶段，每一步都要求独立核实。", "why": ""}
                        for n in p["writeup"]},
           "themes": [{"title": f"观察{i}", "text": "一句话", "repos": [p["month"][0]]} for i in range(3)]}
    out.update(over)
    return out


def test_payload_sections(conn):  # noqa: F811
    p = wk.build_payload(conn, DAY)
    assert p["month"][0] == "c/audit"
    assert p["meta"]["prev_date"] == (DAY - timedelta(days=7)).isoformat()
    r = p["repos"]["c/audit"]
    assert r["rank_prev"] == 1 and r["g7_prev"] is not None
    assert "新上榜" in p["events"] and p["events"]["新上榜"][0][1] == "n/new"
    assert all(k in (lc.NEW, lc.BURST, lc.COOLING, "里程碑") for k in p["events"])
    assert p["writeup"] == list(dict.fromkeys(p["month"] + p["week"]))


def test_render_check_roundtrip_and_rejections(conn):  # noqa: F811
    p = wk.build_payload(conn, DAY)
    llm = _llm(p)
    assert wk.validate_llm(llm, p) == []
    md = wk.render(p, llm)
    rep = wk.check(md, p)
    assert rep["ok"], rep["problems"]
    assert md.startswith(f"# GitHub Trending 周报 · {DAY.isoformat()}")
    assert md.count("**深读**") == len(p["writeup"])
    assert "（详见本月榜 #" in md                    # week list doesn't repeat write-ups

    bad = _llm(p, overview="", themes=[])
    bad["writeups"][p["month"][0]]["why"] = "一周涨了 9,000 星"
    probs = wk.validate_llm(bad, p)
    assert "overview missing" in probs and "themes: 0 items, expected 3" in probs
    assert any(x.startswith("why restates star numbers") for x in probs)
    edited = md.replace("上周 30 天榜第 1", "上周 30 天榜第 2")
    assert any("month #1 c/audit" in x for x in wk.check(edited, p)["problems"])


def test_unknown_last_week_is_not_called_an_entrant(conn):  # noqa: F811
    p = wk.build_payload(conn, DAY)
    for n in p["entrants"]:
        assert p["repos"][n]["prev_known"]
    r = p["repos"]["n/new"]
    assert "上周无 30 天数据" in wk.facts_line(r) or r["rank_prev"]


def test_weekly_factcheck(conn):  # noqa: F811
    p = wk.build_payload(conn, DAY)
    llm = _llm(p)
    llm["writeups"][p["month"][0]]["why"] = "11 月 19 日发布 v2.0"
    prompt = wk.factcheck_prompt(llm, p)
    assert f'"why:{p["month"][0]}"' in prompt and '"theme:2"' in prompt and '"overview"' in prompt
    merged, probs = wk.apply_factcheck(llm, {f"why:{p['month'][0]}": {"verdict": "drop"},
                                             "overview": {"verdict": "fix", "text": "改写后的概览。"}})
    assert merged["writeups"][p["month"][0]]["why"] == "" and merged["overview"] == "改写后的概览。"
    assert wk.validate_llm(merged, p) == [] and probs == []
