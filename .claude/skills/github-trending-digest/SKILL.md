---
name: github-trending-digest
description: >
  Generate the daily Chinese-language GitHub Trending digest (v2): Python builds
  every fact from our own star history and lifecycle statuses; one writer agent
  returns JSON (为什么涨, 3 themes, missing 是什么), one fact-check agent verifies
  only those new claims, then Python renders, checks, saves and emails. Use when
  the user asks for a GitHub trending digest, daily GH digest, GitHub 趋势日报, or
  when this task fires on schedule.
---

# GitHub Trending Digest (daily, v2)

Principle: **Python computes all facts; the LLM only selects and explains; every
email is self-contained** (the reader doesn't remember earlier days). Design notes
and verification history: `docs/redesign-log.md`.

Run every command from the project root. `$S` = a scratch directory for this run.

## Step 1 — data (normally already done by the 09:30 PT fetch task)

```bash
python3 github_fetch.py
```

Scrapes the three trending lists, then (best effort) refreshes repo metadata and
releases, records one GitHub API star reading per repo seen in the last 30 days
(`star_history`), refreshes README excerpts and computes today's lifecycle statuses.
In its JSON check `star_history.recorded` (≈ number of active repos) and
`star_history.rate_limited` (should be null). If `star_history` or `lifecycle` shows an
`error`, run `python3 star_history.py record` and `python3 lifecycle.py show`
by hand and report what failed. If today's scrape is missing entirely, stop and report.

## Step 2 — payload

```bash
python3 digest_v2.py query > $S/payload.json
```

Abort and report if `meta.date` isn't today (UTC). The payload holds the 今日变化
items, the 30 天热度总榜 Top 30, 新锐榜, the footnote, and `prompts.writer`.

## Step 3 — writer (one Agent, general-purpose)

Write `prompts.writer` to `$S/writer_prompt.md`, append
`附注：WebSearch/WebFetch 最多 5 次，只用于给「为什么涨」找证据。`, and have the agent
write ONLY the JSON object to `$S/draft.json`, then run
`python3 digest_v2.py render $S/draft.json > $S/draft.md` and fix the JSON until it
exits 0 (exit 1 prints the validation problems: star numbers in 为什么涨, status used as
a reason, wrong keys, foreign repos, theme count, invalid 是什么). Don't add rules to
the prompt; it already carries them.

## Step 4 — targeted fact-check (one Agent, general-purpose)

```bash
python3 digest_v2.py factcheck-prompt $S/draft.json > $S/fc_prompt.md
```

Append `附注：WebSearch/WebFetch 最多 5 次。`. The agent writes ONLY the verdicts JSON to
`$S/fc_verdicts.json`. It sees only the writer's new claims (non-empty 为什么涨 and the
3 themes), never the numbers. Then:

```bash
python3 digest_v2.py apply-factcheck $S/draft.json $S/fc_verdicts.json $S/final.json
python3 digest_v2.py render $S/final.json > $S/final.md     # also saves new 是什么 to the cache
python3 digest_v2.py check $S/final.md                       # must exit 0
```

If `apply-factcheck` reports a theme dropped by the fact-check, rewrite that theme
yourself from the payload (or ask the writer agent to) and re-run render/check.
Never hand-edit numbers, statuses or links; if `check` fails on a Python line,
re-render instead.

## Step 5 — save (mandatory) and email

```bash
python3 digest_v2.py save-summary < $S/final.md
sqlite3 data/gh_trending.db "SELECT id FROM summaries WHERE date='$(date -u +%Y-%m-%d)' AND period='daily' ORDER BY id DESC LIMIT 1"
python3 email_sender.py --date "$(date -u +%Y-%m-%d)"
```

If save or the read-back fails, write the markdown to `data/orphan_digest_<date>.md`
and send with `--file data/orphan_digest_<date>.md --orphan`. `{"skipped": true}` means
this date was already sent: don't pass `--force` unless the user asked for a resend.
On a non-zero exit from the sender, fall back to a Gmail draft (Gmail MCP
`create_draft`, `body` = markdown, `htmlBody` = `email_sender.py --dry-run --html-out`)
or report the failure.

## Step 6 — report

Briefly: date, active repos / listed today, 今日变化 count (detailed + compact), anomalies,
per agent (writer / fact-check): wall time, tokens, tool uses, web calls (from the
agent notifications); fact-check verdict counts; final chars; saved summary id; send
result.

## Format (for reference — Python enforces it)

今日变化 (new or status-changed repos, ≤ 8 detailed with 是什么, numbers, tags, GitHub
link, evidence-only 为什么涨; the rest in one compact line) · 30 天热度总榜 Top 30 by
our own 30-day gain (one line: link · 是什么 · 30 天 · 7 天 · status) · 新锐榜 (≤ 90 days
AND < 20,000 stars, by 7d gain) · 今天这三件事 · 2-line footnote. Repo names link to
`http://little-claws-mac-mini.local:8082/repo/<owner>/<repo>`. Gains: plain = ours,
`*` = borrowed GitHub list number, `≥` = lower bound. Status thresholds: `lifecycle.py`
and `/about`.
