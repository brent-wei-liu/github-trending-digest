# GitHub Trending Digest

Scrape GitHub Trending across daily / weekly / monthly periods, store in SQLite, and generate a daily Chinese-language digest using a three-step Draft → Critique → Refine reflection pipeline orchestrated by Claude Code skills. Includes a frosted-glass web UI to browse repos and historical digests on desktop or phone.

## 架构

```
┌──────────────────────────────────────────────────────────────────┐
│  Claude Code Scheduled Tasks                                     │
│                                                                  │
│  ┌────────────────────────┐    ┌─────────────────────────────┐   │
│  │ github-trending-fetch  │    │ github-trending-digest      │   │
│  │ daily 09:30 PT         │    │ daily 11:30 PT              │   │
│  │                        │    │                             │   │
│  │ python3 github_fetch.py│    │  github-trending-digest     │   │
│  │   daily/weekly/monthly │    │  skill:                     │   │
│  │       ↓                │    │   1. fetcher refresh        │   │
│  │   SQLite repos +       │    │   2. digest_generate query  │   │
│  │   trending_entries     │    │   3. Draft subagent         │   │
│  └────────────────────────┘    │   4. Critique (isolated)    │   │
│                                │   5. Refine subagent        │   │
│                                │   6. save-summary           │   │
│                                │   7. email_sender.py (SMTP) │   │
│                                │      (md + htmlBody)        │   │
│                                └─────────────────────────────┘   │
│                                              ↓                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ FastAPI web UI (:8082)                                      │ │
│  │   browse repos by period · star · search · view digests     │ │
│  └─────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

## File structure

```
github-trending-digest/
├── github_fetch.py        # scrape https://github.com/trending HTML
├── repo_meta.py           # GitHub API metadata (created_at, license, owner type) + latest release/tag, cached in repo_meta / repo_release
├── digest_generate.py     # today's payload (涨星榜/新锐榜 order, stats lines, ⚠️ 数据待核 flags) + 3-step prompts; `check` validates a digest
├── db.py                  # SQLite schema + migrations
├── api.py                 # FastAPI server (port 8082)
├── data/
│   └── gh_trending.db     # SQLite (gitignored)
├── static/
│   └── index.html         # single-file UI (organic glassmorphism)
├── .claude/skills/
│   └── github-trending-digest/SKILL.md   # 8-step orchestration
├── schedules.yaml         # declarative scheduled-task definitions
└── README.md
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

## Three-step isolated reflection

| Step | Subagent | Sees | Produces |
|------|----------|------|----------|
| Draft | #1 | trending repo data + format spec | first-pass digest |
| Critique | #2 | **only the draft text** | A/B/C grade + line-level edits |
| Refine | #3 | draft + critique | final digest |

Each subagent is isolated via the Claude Code `Agent` (Task) tool, so the Critique subagent is structurally prevented from seeing the original repo list — it must judge the prose on its own merits, which catches lazy categorization and generic descriptions a single-pass writer would miss.

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
