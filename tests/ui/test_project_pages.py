"""UI tests for the redesign's project pages (/repo/owner/name), /about and the
/?digest=ID deep link. Run against a server with real data (GITHUB_TRENDING_URL)."""
import json
import urllib.request

import pytest
from playwright.sync_api import Page, expect


def _api(base_url, path):
    with urllib.request.urlopen(base_url + path, timeout=10) as r:
        return json.load(r)


def _some_repo(base_url):
    """A repo with lifecycle data: the first one on today's trending list."""
    data = _api(base_url, "/api/repos?period=monthly&days=3&page_size=5")
    if not data["repos"]:
        pytest.skip("no repos in the DB")
    return data["repos"][0]["full_name"]


def test_project_api_shape(base_url):
    repo = _some_repo(base_url)
    d = _api(base_url, f"/api/project/{repo}")
    assert d["repo"] == repo and d["github"] == f"https://github.com/{repo}"
    assert d["star_history"] and d["lists"] and d["lifecycle"]
    assert set(d["latest"]) >= {"status", "g1", "g7", "g30", "src30", "stars"}


def test_project_api_404(base_url):
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base_url + "/api/project/no-such-owner/no-such-repo", timeout=5)
    assert e.value.code == 404


def test_project_page_renders_everything(page: Page, base_url: str):
    repo = _some_repo(base_url)
    page.goto(f"{base_url}/repo/{repo}")
    expect(page.locator("h1")).to_have_text(repo)
    expect(page.locator("#tiles .tile")).to_have_count(4)
    expect(page.locator("#star-chart svg path").first).to_be_visible()
    expect(page.locator("#timeline .timeline-row")).to_have_count(3)
    assert page.locator("#timeline .cell.on").count() > 0
    assert page.locator("#life li").count() > 0
    assert page.locator("#digests a").count() > 0
    expect(page.locator("#error")).to_be_hidden()
    gh = page.locator("#tags a", has_text="GitHub")
    expect(gh).to_have_attribute("href", f"https://github.com/{repo}")


def test_star_chart_tooltip_and_table(page: Page, base_url: str):
    repo = _some_repo(base_url)
    page.goto(f"{base_url}/repo/{repo}")
    hit = page.locator("#star-chart svg rect")
    box = hit.bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.9, box["y"] + 40)
    expect(page.locator("#tip")).to_be_visible()
    expect(page.locator("#tip")).to_contain_text("⭐")
    page.locator("#star-toggle").click()
    expect(page.locator("#star-table table")).to_be_visible()
    assert page.locator("#star-table tr").count() > 2


def test_unknown_repo_shows_error(page: Page, base_url: str):
    page.goto(f"{base_url}/repo/no-such-owner/no-such-repo")
    expect(page.locator("#error")).to_be_visible()
    expect(page.locator("#error")).to_contain_text("没有这个项目")


def test_digest_link_from_project_page_opens_digest(page: Page, base_url: str):
    repo = _some_repo(base_url)
    page.goto(f"{base_url}/repo/{repo}")
    link = page.locator("#digests a").first
    href = link.get_attribute("href")
    assert href.startswith("/?digest=")
    page.goto(base_url + href)
    expect(page.locator("#modal-backdrop.open")).to_be_visible(timeout=10_000)
    expect(page.locator("#modal-body")).to_contain_text(repo)


def test_about_page_defines_every_status(page: Page, base_url: str):
    page.goto(f"{base_url}/about")
    for word in ("新上榜", "爆发", "回榜", "掉榜", "降温", "持续走红", "里程碑", "新锐榜"):
        expect(page.locator("dt", has_text=word).first).to_be_visible()


def test_project_page_fits_a_phone(page: Page, base_url: str):
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(f"{base_url}/repo/{_some_repo(base_url)}")
    page.wait_for_selector("#tiles .tile")
    assert page.evaluate("document.documentElement.scrollWidth") <= 375
