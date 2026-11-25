import sqlite3
import sys
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import star_history as sh
from db import init_db

D = date(2026, 11, 20)


def _obs(**kw):
    """_obs(d0=100, d7=90) → {D: 100, D-7: 90}"""
    from datetime import timedelta
    return {D - timedelta(days=int(k[1:])): v for k, v in kw.items()}


def test_own_gains_exact_windows():
    g = sh.gains(_obs(d0=10_000, d1=9_900, d7=9_000, d30=6_000), D)
    assert (g["g1"], g["g7"], g["g30"]) == (100, 1_000, 4_000)
    assert g["src1"] == g["src7"] == g["src30"] == "own"


def test_baseline_slack_is_normalised():
    # No reading exactly 30 days ago; one 32 days ago → scaled to 30 days.
    g = sh.gains(_obs(d0=10_000, d32=6_800), D)
    assert g["g30"] == round(3_200 * 30 / 32) and g["src30"] == "own" and g["span30"] == 32
    # 34 days is beyond the 3-day slack → not own.
    assert sh.gains(_obs(d0=10_000, d34=6_000), D)["src30"] != "own"


def test_fallback_to_github_numbers_while_history_is_short():
    g = sh.gains(_obs(d0=76_218), D, {"monthly": 44_608, "weekly": 9_000})
    assert (g["g30"], g["src30"]) == (44_608, "github_monthly")
    assert (g["g7"], g["src7"]) == (9_000, "github_weekly")
    assert g["g1"] is None and g["src1"] is None


def test_partial_window_is_a_lower_bound():
    # First seen 12 days ago, not on the monthly list: gain since first seen.
    g = sh.gains(_obs(d0=5_000, d12=1_000, d5=3_000), D)
    assert (g["g30"], g["src30"], g["span30"]) == (4_000, "own_partial", 12)


def test_no_reading_today_gives_no_own_gain():
    g = sh.gains(_obs(d2=5_000, d9=4_000), D, {"weekly": 1_200})
    assert g["stars"] == 5_000 and g["src7"] == "github_weekly" and g["g30"] is None


def _db():
    c = sqlite3.connect(":memory:")
    init_db(c)
    sh.ensure_table(c)
    return c


def test_backfill_takes_latest_scrape_per_day_and_skips_zero():
    c = _db()
    rows = [("a/b", "daily", 1, 100, 10, "2026-11-19T17:35:00+00:00"),
            ("a/b", "weekly", 2, 100, 50, "2026-11-19T17:35:00+00:00"),
            ("a/b", "daily", 1, 120, 12, "2026-11-19T19:35:00+00:00"),
            ("z/z", "daily", 3, 0, 9, "2026-11-19T19:35:00+00:00")]
    c.executemany("INSERT INTO trending_entries (repo, period, rank, stars, stars_delta, forks, fetched_at)"
                  " VALUES (?,?,?,?,?,0,?)", rows)
    sh.backfill_from_trending(c)
    sh.backfill_from_trending(c)                     # idempotent
    assert c.execute("SELECT repo, date, stars, source FROM star_history").fetchall() == \
        [("a/b", "2026-11-19", 120, "trending")]


def test_api_reading_wins_over_trending_same_day():
    c = _db()
    c.execute("INSERT INTO star_history VALUES ('a/b','2026-11-20',100,'trending','t',NULL)")
    c.execute("INSERT INTO star_history VALUES ('a/b','2026-11-20',104,'api','t',NULL)")
    assert sh.observations(c, "a/b") == {D: 104}


class FakeAPI:
    def __init__(self, remaining=4000):
        self.calls, self.remaining = [], remaining

    def __call__(self, path, token=None):
        self.calls.append(path)
        self.remaining -= 1
        name = path.removeprefix("/repos/")
        if name == "gone/repo":
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
        if name == "limit/hit":
            raise sh.RateLimited("HTTP 403")
        full = "new/name" if name == "old/name" else name
        return {"stargazers_count": 1234, "full_name": full}, self.remaining


def test_record_api_caches_per_day_tracks_renames_and_failures():
    c, api = _db(), FakeAPI()
    now = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)
    res = sh.record_api(c, ["a/b", "old/name", "gone/repo"], getter=api, now=now)
    assert res["recorded"] == 2 and res["failed"] == ["gone/repo: 404"]
    assert res["renamed"] == {"old/name": "new/name"}
    api.calls.clear()
    res = sh.record_api(c, ["a/b", "old/name"], getter=api, now=now)
    assert res["cached"] == 2 and api.calls == []


def test_record_api_stops_on_rate_limit():
    c, api = _db(), FakeAPI()
    res = sh.record_api(c, ["a/b", "limit/hit", "c/d"], getter=api, today=D)
    assert res["recorded"] == 1 and res["rate_limited"] == "HTTP 403"
    c2, low = _db(), FakeAPI(remaining=sh.MIN_REMAINING + 1)
    res = sh.record_api(c2, ["a/b", "c/d", "e/f"], getter=low, today=D)
    assert res["recorded"] == 2 and "stopped early" in res["rate_limited"]


def test_interpolated_baseline_between_bracketing_observations():
    # Seen 10 and 3 days ago, not 7: total on D-7 interpolated (7,000 + 3/7 * 2,100 = 7,900).
    g = sh.gains(_obs(d0=10_000, d3=9_100, d10=7_000), D)
    assert (g["g7"], g["src7"]) == (2_100, "own_interp")
    # Bracket wider than 14 days → no interpolation, falls back to GitHub weekly.
    g = sh.gains(_obs(d0=10_000, d3=9_100, d20=5_000), D, {"weekly": 1_500})
    assert g["src7"] == "github_weekly"
    # Exact baselines still win over interpolation.
    assert sh.gains(_obs(d0=10_000, d7=9_000, d10=7_000, d3=9_500), D)["src7"] == "own"


def test_interpolation_can_use_todays_reading_as_the_later_point():
    # Last seen on a list 10 days ago, API reading today: 7d ≈ 7/10 of the 10-day gain.
    g = sh.gains(_obs(d0=11_000, d10=10_000), D)
    assert (g["g7"], g["src7"]) == (700, "own_interp")
