import base64
import sqlite3
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import project_memory as pm
from db import init_db
from repo_meta import ensure_table as ensure_meta


def _db():
    c = sqlite3.connect(":memory:")
    init_db(c)
    ensure_meta(c)
    pm.ensure_table(c)
    c.execute("INSERT INTO repos (full_name, description, language, url, first_seen) VALUES "
              "('a/b', 'Fast local AWS emulator', 'Java', 'u', 't')")
    c.execute("INSERT INTO trending_entries (repo, period, rank, stars, stars_delta, forks, fetched_at)"
              " VALUES ('a/b','daily',1,10,1,0,'2026-11-02T19:35:00+00:00')")
    c.execute("INSERT INTO repo_meta (full_name, created_at, license, owner_type, owner_kind, fetched_at)"
              " VALUES ('a/b','2026-01-01T00:00:00Z','MIT','Organization','company','t')")
    return c


def test_clean_readme_strips_markup():
    md = ("# Title\n\n[![badge](https://x/b.svg)](https://x)\n<p align=center><img src=x></p>\n"
          "A **fast** [emulator](https://e.com).\n```bash\nnpm i\n```\n| a | b |\n")
    assert pm.clean_readme(md) == "Title\nA **fast** emulator."


def test_validate_what():
    assert pm.validate_what("在本机模拟 AWS 云服务，测试时不用连真云") == []
    assert "mentions star numbers" in pm.validate_what("一周涨了 9,770 星的 AWS 模拟器")
    assert "more than one sentence" in pm.validate_what("本地 AWS 模拟器。免费。")
    assert "not Chinese" in pm.validate_what("A local AWS emulator")
    assert "over 60 chars" in pm.validate_what("很" * 61)


def test_profile_and_cache_lifecycle():
    c = _db()
    assert [p["repo"] for p in pm.pending(c, ["a/b"])] == ["a/b"]          # nothing cached yet
    res = pm.save_what(c, {"a/b": "在本机模拟 AWS 云服务。", "x/y": "未知项目"})
    assert res == {"saved": ["a/b"], "rejected": {"x/y": ["unknown repo"]}}
    p = pm.profile(c, "a/b")
    assert p["what_zh"] == "在本机模拟 AWS 云服务" and p["owner_kind"] == "company"
    assert p["first_seen"].startswith("2026-11-02") and p["license"] == "MIT"
    assert pm.pending(c, ["a/b"]) == []                                     # cached, reused
    # Cosmetic description tweak → still cached.
    c.execute("UPDATE repos SET description = 'Fast local AWS emulator!' WHERE full_name='a/b'")
    assert pm.pending(c, ["a/b"]) == []
    # The project pivots → regenerate, and the old sentence is offered as context.
    c.execute("UPDATE repos SET description = 'AI agent that writes Terraform plans' WHERE full_name='a/b'")
    again = pm.pending(c, ["a/b"])
    assert again and again[0]["previous"] == "在本机模拟 AWS 云服务"


def test_readme_change_triggers_regeneration():
    c = _db()
    c.execute("INSERT OR IGNORE INTO projects (repo, readme_excerpt) VALUES ('a/b', ?)",
              ("Floci is a lightweight AWS emulator for local tests " * 5,))
    pm.save_what(c, {"a/b": "在本机模拟 AWS 云服务"})
    c.execute("UPDATE projects SET readme_excerpt = ? WHERE repo='a/b'",
              ("Floci is a lightweight AWS emulator for local tests and CI " * 5,))
    assert pm.pending(c, ["a/b"]) == []
    c.execute("UPDATE projects SET readme_excerpt = ? WHERE repo='a/b'",
              ("我们现在是一个面向设计师的图片生成工具，支持多种模型 " * 5,))
    assert pm.pending(c, ["a/b"])


def test_refresh_readmes_ttl_and_missing():
    c = _db()
    calls = []

    def getter(path, token=None):
        calls.append(path)
        if path == "/repos/no/readme/readme":
            raise urllib.error.HTTPError(path, 404, "nf", {}, None)
        return {"content": base64.b64encode("# Hi\nLocal AWS.".encode()).decode(), "sha": "abc"}

    res = pm.refresh_readmes(c, ["a/b", "no/readme"], getter=getter)
    assert res["fetched"] == 2 and res["failed"] == []
    assert c.execute("SELECT readme_excerpt, readme_sha FROM projects WHERE repo='a/b'").fetchone() == \
        ("Hi\nLocal AWS.", "abc")
    calls.clear()
    pm.refresh_readmes(c, ["a/b"], getter=getter)
    assert calls == []


def test_empty_description_stays_cached():
    c = _db()
    c.execute("UPDATE repos SET description = '' WHERE full_name='a/b'")
    c.execute("INSERT OR IGNORE INTO projects (repo, readme_excerpt) VALUES ('a/b', 'Burn images onto CDs')")
    pm.save_what(c, {"a/b": "用普通刻录机在光盘上刻出图片"})
    assert pm.pending(c, ["a/b"]) == []
