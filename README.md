# GitHub Trending Digest

Scrape GitHub Trending (daily / weekly / monthly lists), record our own star history for every repo seen in the last 30 days, and send a self-contained Chinese digest every day plus a deeper report every Monday. Python computes every fact (our own 1d/7d/30d gains, lifecycle statuses, rankings, numbers); the LLM only writes 为什么涨 (evidence-only), three themes and missing one-line 是什么, and a second agent fact-checks just those claims. A web UI shows each project's star trend, list timeline and status history. Design and verification history: `docs/redesign-log.md`.

## 架构

```
09:30 PT  github-trending-fetch   python3 github_fetch.py
            scrape 3 lists → repos / trending_entries
            repo_meta (created_at, license, owner type) + latest release      (repo_meta.py)
            one GitHub API star reading per active repo → star_history       (star_history.py)
            README excerpts (7-day cache) → projects                         (project_memory.py)
            today's lifecycle status per repo → lifecycle                    (lifecycle.py)

11:30 PT  github-trending-digest  skill .claude/skills/github-trending-digest
            digest_v2.py query → writer agent (JSON) → fact-check agent (claims only)
            → apply-factcheck → render → check → save-summary → email_sender.py

Mon 11:45 PT  github-trending-weekly  skill .claude/skills/github-trending-weekly
            weekly_digest.py: 本月/本周 Top 10, 深读, 周涨跌, 状态事件 → email_sender.py --kind weekly

FastAPI UI :8082   repo list · digests · /repo/<owner>/<name> project pages · /about
```

## File structure

```
github-trending-digest/
├── github_fetch.py        # scrape trending + run the per-day data steps below
├── star_history.py        # own star history; 1d/7d/30d gains with source labels
├── lifecycle.py           # deterministic statuses: 新上榜/爆发/回榜/掉榜/降温/持续走红 + 里程碑
├── project_memory.py      # cached 是什么 per repo, README excerpts, profile()
├── repo_meta.py           # GitHub API metadata + latest release/tag
├── digest_v2.py           # daily payload / writer prompt / fact-check / render / check / save
├── weekly_digest.py       # Monday report, same contract
├── digest_generate.py     # v1 digest (kept for its helpers: anomaly rules, number formatting)
├── email_sender.py        # Gmail SMTP; --kind daily|weekly (own subject + dedupe key)
├── db.py / api.py         # SQLite schema, FastAPI server (GH_TRENDING_PORT, default 8082)
├── static/                # index.html (lists + digests), repo.html (project page), about.html
├── docs/redesign-log.md   # what was built and how each phase was verified
├── .claude/skills/        # github-trending-digest (daily), github-trending-weekly
└── schedules.yaml
```

## Quick start

1. Clone the repo.
2. Copy `.env.example` to `.env` and set `DIGEST_RECIPIENT`, `GMAIL_SMTP_USER` and `GMAIL_APP_PASSWORD` — digests are sent via Gmail SMTP (see [Email delivery](#email-delivery)).
3. On first run, the SQLite DB is auto-created (`init_db()` in `db.py`); the first `python3 github_fetch.py daily` populates it.
4. Register the scheduled tasks per [Setup scheduled tasks](#setup-scheduled-tasks).
5. (Optional) Launch the Web UI: `python3 api.py` → http://127.0.0.1:8082.

## Tracked tables

- **repos** — unique GitHub repos (`full_name`, `description`, `language`, `url`). Migration adds `starred` / `starred_at` for the web UI.
- **trending_entries** — one row per (repo, period, fetch_run); records rank, stars, stars_delta, forks at fetch time.
- **summaries** — generated Chinese digests, one row per day per focus.
- **focus_profiles** — saved filter configs (default / ai-ml / rust-go / frontend); choose which profile each digest uses.
- **subscribers** — historical email subscriber list (legacy from cron mailer; not used by the current delivery path).

## Writer + targeted fact-check

| Step | Who | Sees | Produces |
|------|-----|------|----------|
| payload | Python | DB | every number, status, ranking, link, 是什么 |
| writer | agent #1 | payload prompt | JSON: 为什么涨 (evidence-only or empty), 3 themes, missing 是什么 |
| fact-check | agent #2 | **only the writer's new claims** | keep / drop / fix per claim |
| render + check | Python | payload + JSON | final markdown; rejects edited numbers, foreign links, star numbers as reasons, fake statuses |

This replaced Draft → Critique → Refine after a same-data comparison (Phase 6 in
`docs/redesign-log.md`): ~26 % of the wall time and ~59 % of the tokens with output at
least as clean.

## Web UI

A single-file FastAPI + HTML browser for the trending feed and digest archive, themed in **organic glassmorphism**: a slow-shifting dreamy gradient (lake-blue / lavender / peach / cream), three drifting blurred blobs, frosted-glass cards, and an organic wavy divider beneath the header. Reads the same `data/gh_trending.db`, adds `starred` / `starred_at` columns via additive `ALTER TABLE` migration on startup.

**Dependencies** (shared with sibling projects):
```bash
pip3 install fastapi uvicorn
```

**Start the server**:
```bash
cd /path/to/github-trending-digest && python3 api.py
```

The server binds `0.0.0.0:8082`, so:
- Local desktop: http://127.0.0.1:8082
- Phone on the same Wi-Fi: `http://<mac-lan-ip>:8082` (find with `ipconfig getifaddr en0`)

First time accessing from another device, **macOS firewall** will prompt: System Settings → Network → Firewall → Allow incoming connections for `python3`.

**Routes**:
- `/` — single-page UI (Trending tab + Digests tab)
- `/api/stats`, `/api/repos`, `/api/digests`, `/api/digests/{id}`
- `POST /api/repos/{id}/star` and `/unstar`

**Features**:
- Browse repos by period (daily / weekly / monthly segmented control)
- Search repos by name or description
- "★ Starred" toggle to filter to starred repos
- Star / unstar repos (persisted)
- Open any historical digest as rendered HTML in a modal
- Mobile-friendly (≤768 px breakpoint, 44 px touch targets, reduced blur on mobile)

## Setup scheduled tasks

Scheduled tasks are declared in [`schedules.yaml`](./schedules.yaml). To register them on your machine, open Cowork (Claude Code) in this project and say:

> Read schedules.yaml and register every task defined there as a Cowork scheduled task.

Claude will use the `schedule` skill to create each task, copying the relevant SKILL.md (or embedding the inline `script:` for skill-less tasks like `github-trending-fetch`) into `~/Documents/Claude/Scheduled/`.

| Task | 时间 (PT) | 说明 |
|------|----------|------|
| `github-trending-fetch` | 09:30 | scrape daily/weekly/monthly trending into SQLite |
| `github-trending-digest` | 11:30 | three-step reflection digest + email via SMTP |

## Email delivery

Digests are sent by `email_sender.py` through `smtp.gmail.com:587` (STARTTLS) as a clean plain-text rendering (no raw markdown) + rendered HTML. Set `DIGEST_RECIPIENT`, `GMAIL_SMTP_USER` and `GMAIL_APP_PASSWORD` in `.env` (see `.env.example`); the App Password comes from https://myaccount.google.com/apppasswords and requires 2-Step Verification. Preview without sending using `python3 email_sender.py --dry-run`. Each digest date is sent once (logged in `data/email_sent.jsonl`); use `--force` to resend. If sending fails, the skill falls back to a Gmail draft via the Gmail MCP `create_draft` tool when that connector is available.

## Manual usage

```bash
cd /path/to/github-trending-digest

# fetch one period
python3 github_fetch.py daily
python3 github_fetch.py weekly
python3 github_fetch.py monthly

# inspect what's in the DB
python3 digest_generate.py stats
python3 digest_generate.py query --focus ai-ml | jq .meta      # latest scrape day; --date YYYY-MM-DD for an older one
python3 digest_generate.py check path/to/digest.md             # exit 0 = numbers, coverage, ⚠️ flags all match the payload

# launch web UI
python3 api.py
```

## Testing

End-to-end UI regression suite using Playwright (Python). Hits the real local API — does not mock, does not spawn the server itself.

Install once:

```bash
pip install -r requirements-dev.txt
python3 -m playwright install chromium
```

Run (in a separate terminal from the running server):

```bash
python3 api.py                      # terminal 1 — keep the server up
pytest tests/ui/                    # terminal 2 — run the suite
```

Useful flags:
- `pytest tests/ui/ --headed` — watch the browser drive the page in real time
- `pytest tests/ui/ -k digest` — run a single test by name fragment
- `GITHUB_TRENDING_URL=http://192.168.1.x:8082 pytest tests/ui/` — point at a remote / phone-LAN server

The whole suite is gated by `_require_server` in `tests/ui/conftest.py` — if the API isn't reachable the suite is cleanly skipped, not failed.

## Migration notes

Evolved through OpenClaw → Hermes (`delegate_task` orchestration) → Claude Code (this version).
- `digest_generate.py` outputs JSON + 3-step Prompt templates; LLM calls now happen via the Claude Code `Agent` tool inside the `github-trending-digest` skill.
- Scheduling moved from Hermes cron to Claude Code scheduled-tasks (`~/.claude/scheduled-tasks/`); no standalone daemon required.
- Email path moved from cron-driven SMTP to Gmail MCP draft creation, then back to SMTP via `email_sender.py` with the Gmail draft as a fallback (sent without manual review).

## Known limitations

- GitHub HTML markup occasionally changes (column order, class names) and breaks the `parse_trending_page()` regex; if a fetch returns 0 rows, the parser likely needs tweaking — check `github_fetch.py`.
- Three subagent steps run serially; total digest run can take 5–15 minutes.
- The web UI's blob-drift animation is heavier than typical web pages; reduced-motion users see a static gradient (handled).
