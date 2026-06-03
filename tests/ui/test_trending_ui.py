"""End-to-end UI regression tests for github-trending-digest."""
import re

import pytest
from playwright.sync_api import Page, expect


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _open(page: Page, base_url: str):
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".repo-card", timeout=10_000)


def _starred_count(page: Page) -> int:
    """Read the 'N starred' stat-pill in the header, or 0 if missing."""
    pill = page.locator(".stat-pill", has_text="starred").first
    if pill.count() == 0:
        return 0
    text = pill.inner_text()
    m = re.search(r"(\d[\d,]*)", text)
    return int(m.group(1).replace(",", "")) if m else 0


# ---------------------------------------------------------------------
# A. Star toggle persists across reload + bumps header stat
# ---------------------------------------------------------------------

def test_star_toggle_persists(page: Page, base_url: str):
    _open(page, base_url)

    first = page.locator(".repo-card").first
    repo_id = first.get_attribute("data-repo-id")
    star_btn = first.locator(".star-btn")

    # If the first card happens to already be starred (sticky from a prior
    # session/test), unstar it first to start clean.
    if "starred" in (star_btn.get_attribute("class") or ""):
        star_btn.click()
        page.wait_for_function(
            "(id) => !document.querySelector('[data-repo-id=\"' + id + '\"] .star-btn')"
            ".classList.contains('starred')",
            arg=repo_id, timeout=3_000,
        )

    before = _starred_count(page)

    star_btn.click()
    page.wait_for_function(
        "(id) => document.querySelector('[data-repo-id=\"' + id + '\"] .star-btn')"
        ".classList.contains('starred')",
        arg=repo_id, timeout=3_000,
    )
    # Header pill increments
    page.wait_for_function(
        f"() => /\\b{before + 1}\\b/.test(document.querySelector('.stat-pill:has-text(\"starred\")').innerText)",
        timeout=3_000,
    ) if False else None  # JS regex literal pain — fall back to direct read
    after = _starred_count(page)
    assert after == before + 1, f"starred count did not increment: {before} -> {after}"

    # Reload — DB persistence
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector(f".repo-card[data-repo-id=\"{repo_id}\"]", timeout=10_000)
    star_after_reload = page.locator(f".repo-card[data-repo-id=\"{repo_id}\"] .star-btn")
    assert "starred" in (star_after_reload.get_attribute("class") or ""), (
        "star did not persist across reload"
    )

    # Cleanup — unstar
    star_after_reload.click()
    page.wait_for_function(
        "(id) => !document.querySelector('[data-repo-id=\"' + id + '\"] .star-btn')"
        ".classList.contains('starred')",
        arg=repo_id, timeout=3_000,
    )


# ---------------------------------------------------------------------
# B. Period switching loads different data
# ---------------------------------------------------------------------

def test_period_switch(page: Page, base_url: str):
    _open(page, base_url)

    daily_first = page.locator(".repo-card").first.get_attribute("data-repo-id")

    page.locator(".period-btn", has_text="Weekly").click()
    page.wait_for_function(
        f"() => document.querySelector('.repo-card')?.dataset.repoId !== '{daily_first}'",
        timeout=5_000,
    )
    weekly_first = page.locator(".repo-card").first.get_attribute("data-repo-id")
    assert weekly_first != daily_first, "weekly returned same first repo as daily"

    page.locator(".period-btn", has_text="Monthly").click()
    page.wait_for_function(
        f"() => document.querySelector('.repo-card')?.dataset.repoId !== '{weekly_first}'",
        timeout=5_000,
    )
    monthly_first = page.locator(".repo-card").first.get_attribute("data-repo-id")
    assert monthly_first != weekly_first

    # Switch back to daily, verify match
    page.locator(".period-btn", has_text="Daily").click()
    page.wait_for_function(
        f"() => document.querySelector('.repo-card')?.dataset.repoId === '{daily_first}'",
        timeout=5_000,
    )
    # Verify the active class follows the click
    expect(page.locator(".period-btn", has_text="Daily")).to_have_class(re.compile(r"\bactive\b"))


# ---------------------------------------------------------------------
# C. Starred-only filter
# ---------------------------------------------------------------------

def test_starred_only_filter(page: Page, base_url: str):
    _open(page, base_url)

    first = page.locator(".repo-card").first
    repo_id = first.get_attribute("data-repo-id")
    full_name = first.locator(".repo-name a").inner_text().strip()

    # Star the first repo
    star_btn = first.locator(".star-btn")
    if "starred" not in (star_btn.get_attribute("class") or ""):
        star_btn.click()
        page.wait_for_function(
            "(id) => document.querySelector('[data-repo-id=\"' + id + '\"] .star-btn')"
            ".classList.contains('starred')",
            arg=repo_id, timeout=3_000,
        )

    # Click "★ Starred" button
    starred_toggle = page.locator("#starred-toggle")
    starred_toggle.click()
    expect(starred_toggle).to_have_class(re.compile(r"\bactive\b"), timeout=3_000)

    # Wait for filtered list — should still contain the starred repo
    page.wait_for_function(
        f"() => document.querySelectorAll('.repo-card[data-repo-id=\"{repo_id}\"]').length === 1",
        timeout=5_000,
    )
    # All visible cards should be starred
    cards = page.locator(".repo-card").all()
    assert len(cards) >= 1
    for c in cards:
        cls = c.locator(".star-btn").get_attribute("class") or ""
        assert "starred" in cls, "non-starred card visible while filter is active"

    # Toggle off
    starred_toggle.click()
    expect(starred_toggle).not_to_have_class(re.compile(r"\bactive\b"), timeout=3_000)
    page.wait_for_function(
        "() => document.querySelectorAll('.repo-card').length > 1",
        timeout=5_000,
    )

    # Cleanup
    page.locator(f".repo-card[data-repo-id=\"{repo_id}\"] .star-btn").click()


# ---------------------------------------------------------------------
# D. Search filter
# ---------------------------------------------------------------------

def test_search_filter(page: Page, base_url: str):
    _open(page, base_url)

    initial_count = page.locator(".repo-card").count()
    if initial_count < 2:
        pytest.skip("need at least 2 repos to test filter")

    # Use the first repo's owner as a search term — guaranteed to match itself
    first_full = page.locator(".repo-name a").first.inner_text().strip()
    owner = first_full.split("/")[0]
    if not owner:
        pytest.skip("could not extract owner from first repo")

    page.fill("#search", owner)
    # Wait for debounce + fetch + render
    page.wait_for_function(
        f"(initial) => {{ "
        f"  const cards = document.querySelectorAll('.repo-card'); "
        f"  if (cards.length === initial) return false; "
        f"  return Array.from(cards).every(c => "
        f"    (c.querySelector('.repo-name a').innerText || '').toLowerCase().includes('{owner.lower()}')); "
        f"}}",
        arg=initial_count, timeout=5_000,
    )
    after_count = page.locator(".repo-card").count()
    assert after_count >= 1
    assert after_count <= initial_count, "filter should not increase result count"

    # Clear search
    page.fill("#search", "")
    page.wait_for_function(
        f"() => document.querySelectorAll('.repo-card').length >= {initial_count}",
        timeout=5_000,
    )


# ---------------------------------------------------------------------
# E. Digest drawer opens with content
# ---------------------------------------------------------------------

def test_digest_drawer_opens(page: Page, base_url: str):
    _open(page, base_url)

    # Switch to Digests tab
    page.locator(".tab", has_text="Digests").click()
    expect(page.locator(".tab", has_text="Digests")).to_have_class(re.compile(r"\bactive\b"))

    # Wait for the actual rows, not just the container (which exists empty
    # while loadDigests is showing its loader).
    try:
        page.wait_for_selector(".digest-row", timeout=5_000)
    except Exception:
        pytest.skip("no digests in DB to open")
    open_btn = page.locator(".digest-row .btn", has_text="Open").first
    open_btn.click()
    backdrop = page.locator("#modal-backdrop.open")
    expect(backdrop).to_be_visible(timeout=3_000)

    # Wait past the loader — modal-body initially contains a loader / "unfurling…"
    # animation. Real content is rendered into #modal-body after the API call.
    page.wait_for_function(
        "() => { const b = document.getElementById('modal-body'); "
        "return b && b.innerText && b.innerText.length > 100 "
        "&& !b.querySelector('.loader'); }",
        timeout=5_000,
    )
    text = page.locator("#modal-body").inner_text()
    assert len(text) >= 100, f"modal opened but content too short ({len(text)} chars)"

    # Close via × button
    page.locator("#modal-close").click()
    expect(backdrop).not_to_be_visible(timeout=3_000)


# ---------------------------------------------------------------------
# F. Repo name links to GitHub correctly
# ---------------------------------------------------------------------

def test_repo_name_links_to_github(page: Page, base_url: str):
    _open(page, base_url)

    first = page.locator(".repo-card").first
    full_name = first.locator(".repo-name a").inner_text().strip()
    href = first.locator(".repo-name a").get_attribute("href")
    target = first.locator(".repo-name a").get_attribute("target")
    rel = first.locator(".repo-name a").get_attribute("rel") or ""

    expected_url = f"https://github.com/{full_name}"
    assert href == expected_url, f"href={href!r}, expected {expected_url!r}"
    assert target == "_blank"
    assert "noopener" in rel, f"rel attr missing noopener: {rel!r}"


# ---------------------------------------------------------------------
# G. Star button is inline in stats row, no empty action bar
# ---------------------------------------------------------------------

def test_stats_row_inline_star_button(page: Page, base_url: str):
    _open(page, base_url)

    first = page.locator(".repo-card").first
    # The star button must live inside .repo-meta, not a separate .card-actions
    star_in_meta = first.locator(".repo-meta .star-btn")
    assert star_in_meta.count() == 1, (
        "expected exactly one .star-btn inside .repo-meta (stats row); "
        f"found {star_in_meta.count()}"
    )

    # No standalone .card-actions row should exist
    card_actions = first.locator(".card-actions")
    assert card_actions.count() == 0, (
        "found a .card-actions row — was supposed to be removed"
    )

    # The stats row should be the LAST content block in the card. A
    # geometric "gap" check is flaky because the cards live in a CSS grid
    # that stretches each card to the row's tallest height — empty space
    # below stats can mean "neighboring card has a longer description",
    # not "leftover action bar".
    next_sibling_html = first.evaluate(
        "(card) => { const m = card.querySelector('.repo-meta'); "
        "return m && m.nextElementSibling ? m.nextElementSibling.outerHTML : null; }"
    )
    assert next_sibling_html is None, (
        f".repo-meta has a next sibling, expected to be the last content block. "
        f"got: {next_sibling_html[:120]}..."
    )


# ---------------------------------------------------------------------
# H. Digest read/unread — opening a digest marks it read; toggle works
# ---------------------------------------------------------------------

def test_digest_mark_read_persists(page: Page, base_url: str):
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".tab[data-tab='digests']", timeout=10_000)
    page.locator(".tab[data-tab='digests']").click()
    try:
        page.wait_for_selector(".digest-row", timeout=5_000)
    except Exception:
        pytest.skip("no digests")

    first = page.locator(".digest-row").first
    digest_id = first.get_attribute("data-id")
    page.evaluate("(id) => fetch('/api/digests/' + id + '/unread', {method:'POST'})", arg=digest_id)
    page.reload(wait_until="domcontentloaded")
    page.locator(".tab[data-tab='digests']").click()
    page.wait_for_selector(".digest-row", timeout=5_000)
    row = page.locator(f".digest-row[data-id='{digest_id}']")
    expect(row).to_have_class(re.compile(r"\bis-unread\b"), timeout=3_000)

    row.locator("[data-action='open']").click()
    page.wait_for_function(
        "(id) => document.querySelector(`.digest-row[data-id='${id}']`)?.dataset.isRead === '1'",
        arg=digest_id, timeout=3_000,
    )
    expect(row).to_have_class(re.compile(r"\bis-read\b"), timeout=2_000)

    page.locator("#modal-close").click()
    page.wait_for_function("() => !document.querySelector('.modal-backdrop.open')", timeout=2_000)

    page.reload(wait_until="domcontentloaded")
    page.locator(".tab[data-tab='digests']").click()
    page.wait_for_selector(f".digest-row[data-id='{digest_id}']", timeout=5_000)
    expect(page.locator(f".digest-row[data-id='{digest_id}']")).to_have_class(re.compile(r"\bis-read\b"), timeout=3_000)


def test_digest_unread_toggle(page: Page, base_url: str):
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator(".tab[data-tab='digests']").click()
    try:
        page.wait_for_selector(".digest-row", timeout=5_000)
    except Exception:
        pytest.skip("no digests")

    first = page.locator(".digest-row").first
    digest_id = first.get_attribute("data-id")
    page.evaluate("(id) => fetch('/api/digests/' + id + '/unread', {method:'POST'})", arg=digest_id)
    page.reload(wait_until="domcontentloaded")
    page.locator(".tab[data-tab='digests']").click()
    page.wait_for_selector(f".digest-row[data-id='{digest_id}']", timeout=5_000)
    page.locator(f".digest-row[data-id='{digest_id}'] [data-action='open']").click()

    btn = page.locator("#modal-read-toggle")
    expect(btn).to_be_visible(timeout=3_000)
    expect(btn).to_have_text(re.compile(r"mark unread", re.I), timeout=3_000)
    btn.click()
    expect(btn).to_have_text(re.compile(r"mark read", re.I), timeout=3_000)
    final = page.evaluate(
        "async (id) => (await (await fetch('/api/digests/' + id)).json()).is_read",
        arg=digest_id,
    )
    assert final == 0
    page.locator("#modal-close").click()
