import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lifecycle as lc
import star_history as sh
from db import init_db


def _c(**kw):
    base = dict(listed_today=True, listed_before=True, listed_prev_scrape=True, listed_last7=7,
                g={"g1": 100, "g7": 700, "g30": 3000, "src30": "own", "stars": 50_000},
                prev_total=49_900)
    base.update(kw)
    return lc.classify(**base)


def test_classify_rules():
    assert _c(listed_before=False)[0] == "新上榜"
    assert _c(g={"g1": 3_000, "g7": 3_600, "stars": 9_000})[0] == "爆发"      # prev avg 100
    assert _c(g={"g1": 1_200, "g7": 7_200, "stars": 9_000})[0] != "爆发"      # prev avg 1,000
    assert _c(listed_prev_scrape=False)[0] == "回榜"
    assert _c(listed_today=False)[0] == "掉榜"
    # 7d pace (300/day) is under half the 30d pace (1,000/day) → 降温.
    assert _c(g={"g1": 300, "g7": 2_100, "g30": 30_000, "src30": "own", "stars": 9e4})[0] == "降温"
    # A 30d lower bound (own_partial) can't establish cooling.
    assert _c(g={"g1": 300, "g7": 2_100, "g30": 30_000, "src30": "own_partial", "stars": 9e4})[0] is None
    assert _c(g={"g1": 700, "g7": 4_900, "g30": 15_000, "src30": "own", "stars": 9e4})[0] == "持续走红"
    assert _c(listed_last7=4, g={"g1": 700, "g7": 4_900, "g30": 15_000, "src30": "own",
                                 "stars": 9e4})[0] is None
    assert _c()[0] is None


def test_milestones():
    assert lc.milestone_crossed(99_895, 100_551) == 100_000
    assert lc.milestone_crossed(4_000, 21_000) == 20_000           # largest crossed
    assert lc.milestone_crossed(None, 100_551) is None
    assert lc.milestone_label(100_000) == "破 10 万星" and lc.milestone_label(5_000) == "破 5,000 星"
    assert _c(g={"g1": 700, "g7": 700, "stars": 100_200}, prev_total=99_500)[1] == 100_000


# ---------- realistic histories through compute_day ----------

T0 = date(2026, 10, 20)


def _db():
    c = sqlite3.connect(":memory:")
    init_db(c)
    sh.ensure_table(c)
    lc.ensure_table(c)
    return c


def _list(c, repo, day, period="daily", stars=1000, delta=100):
    c.execute("INSERT INTO trending_entries (repo, period, rank, stars, stars_delta, forks, fetched_at)"
              " VALUES (?,?,1,?,?,0,?)", (repo, period, stars, delta, f"{day.isoformat()}T19:35:00+00:00"))


def _api(c, repo, day, stars):
    c.execute("INSERT OR REPLACE INTO star_history VALUES (?,?,?,'api','t',NULL)", (repo, day.isoformat(), stars))


def _keepalive(c, days):
    for d in days:      # another repo keeps every scrape day present
        _list(c, "x/filler", d, "monthly", 50_000, 9_000)


def _day(n):
    return T0 + timedelta(days=n)


def test_superpowers_come_and_go():
    c = _db()
    _keepalive(c, [_day(i) for i in range(0, 29)])
    for i in range(0, 5):                       # daily list days 0-4
        _list(c, "o/sp", _day(i), "daily", 300_000 + 600 * i, 600)
    for i in range(5, 12):                      # weekly only, days 5-11
        _list(c, "o/sp", _day(i), "weekly", 303_000 + 500 * (i - 5), 4_000)
    for i in range(0, 29):
        _api(c, "o/sp", _day(i), 300_000 + 550 * i)
    _list(c, "o/sp", _day(28), "daily", 315_400, 598)   # back after a 16-day absence
    sh.backfill_from_trending(c)
    assert lc.compute_day(c, _day(4))["o/sp"]["status"] is None or \
        lc.compute_day(c, _day(4))["o/sp"]["status"] == "持续走红"
    assert lc.compute_day(c, _day(12))["o/sp"]["status"] == "掉榜"
    assert lc.compute_day(c, _day(20))["o/sp"]["status"] is None       # just absent
    back = lc.compute_day(c, _day(28))["o/sp"]
    assert back["status"] == "回榜" and back["listed"]


def test_monthly_giant_hot_then_cooling():
    # security-audit-like: on the monthly list every day; +1,500/day for 3 weeks, then +300/day.
    c = _db()
    total = 30_000
    for i in range(0, 36):
        total += 1_500 if i < 27 else 300
        _list(c, "c/audit", _day(i), "monthly", total, 40_000)
        _api(c, "c/audit", _day(i), total)
    sh.backfill_from_trending(c)
    assert lc.compute_day(c, _day(25))["c/audit"]["status"] == "持续走红"
    cool = lc.compute_day(c, _day(35))["c/audit"]
    assert cool["status"] == "降温", cool                # 7d avg 300 vs 30d avg ~880
    assert cool["src30"] == "own" and cool["g30"] > cool["g7"] * 2


def test_one_day_spike():
    c = _db()
    total = 5_000
    for i in range(0, 20):
        total += 3_000 if i == 15 else 100
        _list(c, "s/spike", _day(i), "daily", total, 3_000 if i == 15 else 100)
        _api(c, "s/spike", _day(i), total)
    sh.backfill_from_trending(c)
    day15 = lc.compute_day(c, _day(15))["s/spike"]
    assert day15["status"] == "爆发" and day15["g1"] == 3_000
    assert lc.compute_day(c, _day(16))["s/spike"]["status"] is None   # spike over, not cooling


def test_brand_new_repo_and_milestone():
    c = _db()
    _keepalive(c, [_day(0), _day(1)])
    _api(c, "n/new", _day(0), 9_400)
    _list(c, "n/new", _day(1), "daily", 10_300, 900)
    _api(c, "n/new", _day(1), 10_300)
    sh.backfill_from_trending(c)
    row = lc.compute_day(c, _day(1))["n/new"]
    assert row["status"] == "新上榜" and row["milestone"] == 10_000


def test_store_and_previous_status():
    c = _db()
    _keepalive(c, [_day(0), _day(1)])
    _list(c, "n/new", _day(0), "daily", 1_000, 900)
    sh.backfill_from_trending(c)
    lc.refresh(c, _day(0))
    lc.refresh(c, _day(1))
    assert lc.previous_status(c, "n/new", _day(1)) == "新上榜"
    assert c.execute("SELECT status FROM lifecycle WHERE repo='n/new' AND date=?",
                     (_day(1).isoformat(),)).fetchone()[0] == "掉榜"


def test_prior_pace_from_consecutive_observations():
    obs = {_day(i): 1_000 + 200 * i for i in range(10, 15)}      # days 10-14, +200/day
    assert lc.prior_pace(obs, _day(15)) == 200
    assert lc.prior_pace({_day(14): 5}, _day(15)) is None          # one point only
    assert lc.prior_pace({_day(5): 1, _day(10): 9}, _day(15)) is None   # latest too old
    # Known pace of 1,200/day: +1,500 today is not a burst.
    assert _c(g={"g1": 1_500, "stars": 9_000}, prev_avg=1_200)[0] != "爆发"
