---
name: github-trending-digest
description: >
  Generate a daily Chinese-language digest of GitHub Trending repositories
  (daily / weekly / monthly periods) via a three-step Draft / Critique /
  Refine isolated reflection pipeline. Use when the user asks for a
  GitHub trending digest, daily GH digest, GitHub 趋势日报, or when this
  task fires on schedule.
---

# GitHub Trending Digest

Generate a daily Chinese-language digest of GitHub Trending across daily/weekly/monthly periods, using a three-step **isolated reflection** pipeline (Draft → Critique → Refine) followed by save + email via Gmail SMTP.

**Project root:** the directory containing this SKILL.md's grand-grandparent (`<project>/.claude/skills/github-trending-digest/SKILL.md`). The scheduled-task wrapper or invoking session should `cd` there before running any of the steps below.

## Workflow

### Web tool budget (applies across Steps 3 / 4 / 5)

Each subagent may use `WebSearch` and `WebFetch` to verify or enrich content. **Hard cap = 20 calls total across all three steps**, split as:

| Step | Budget | Use it for |
|------|--------|------------|
| Draft (3) | **5** | filling in repo / author / project context the raw data doesn't include |
| Critique (4) | **10** | fact-checking project descriptions, verifying categorizations, catching wrong claims |
| Refine (5) | **5** | resolving the specific factual corrections the critique flagged |

When spawning each subagent, tell it explicitly: "you have N web tool calls available — use them on the highest-value facts first." Do not exceed the per-step budget; running over starves later steps. The budget is a hard ceiling, not a target — using fewer calls is fine if the content is solid.

### Step 1 — Refresh trending data (idempotent)

```bash
cd <project-root> && python3 github_fetch.py
```

A single invocation scrapes all three periods (daily / weekly / monthly) from `https://github.com/trending?since=...` and inserts new repos / trending_entries into `data/gh_trending.db`. Deduped on natural keys. If GitHub blocks one period (rare; no auth required), the script reports it in the `failed` array — continue with the periods that succeeded.

### Step 2 — Build orchestration payload

```bash
python3 digest_generate.py query --days 1 --focus default
```

Returns JSON with:
- `meta`: `date`, `days`, `focus`, `total_repos`, `daily_count`, `weekly_count`
- `repos`: top focused repos by period
- `streaks`: repos trending multi-day
- `language_distribution`: top languages
- `prompts.draft`: the Draft prompt with daily/weekly/streak/lang sections embedded
- `prompts.critique_template`: with `{draft}` placeholder
- `prompts.refine_template`: with `{draft}` and `{critique}` placeholders

If `total_repos` is 0, abort and report — no fresh data.

### Step 3 — Draft (subagent #1)

Spawn an Agent with `subagent_type=general-purpose`. Prompt = `prompts.draft` from Step 2. The prompt already contains the trending data inline.

**Append to the prompt — coverage rule (mandatory)**: Every weekly-trending repo in the input data MUST be mentioned by name in the draft, with at minimum a one-line description. Then decide which subset to give deeper coverage (3-5 sentences) based on impact / novelty / category fit. This prevents the Critique step (which is data-isolated) from missing repos that were silently dropped at Draft stage. If a weekly-trending repo seems uninteresting, still include it — let the Critique judge whether to keep or drop it. Coverage suggestions for the other periods:
- **Daily**: aim to cover every entry; daily lists are typically smaller.
- **Monthly**: cover the top 15-20 by impact; remaining go into a "其他值得关注" tail bullet list (one line each).

**Append to the prompt — web budget**: "You may use WebSearch / WebFetch up to **5 times** to fill in context the trending data doesn't include — what a project actually does, who maintains it, why it might be trending today. Use these on the most impactful repos (top of list, novel names) before resorting to generic descriptions. Stay within 5 calls."

Capture the returned draft text. Do not strip or reformat.

### Step 4 — Critique (subagent #2, **DATA-ISOLATED**)

Spawn an Agent with `subagent_type=general-purpose`. Give it ONLY:
- The Critique template (`prompts.critique_template`) with `{draft}` substituted to the Step 3 output
- **Do NOT pass the trending data again.** The critique subagent must not see the raw repo list — that forces it to judge the digest on its own merits rather than checking it against a key.

**However, append to the prompt**: "You may use WebSearch / WebFetch up to **10 times** to fact-check claims the draft makes — verify what each repo actually does, whether descriptions match upstream READMEs, whether categorizations make sense, whether the draft missed obvious headline projects from today. Web access is for *verification*, not for re-fetching the trending list. Spend the budget on the highest-value fact-checks first."

This is the largest budget on purpose — a critic with internet beats a critic working from the prose alone, especially for project descriptions and categorization quality.

Capture the critique text (ends with grade A / B / C).

### Step 5 — Refine (subagent #3)

Spawn an Agent with `subagent_type=general-purpose`. Give it the Refine template with `{draft}` and `{critique}` substituted from Steps 3 and 4.

**Append to the prompt**: "You may use WebSearch / WebFetch up to **5 times** to resolve specific factual corrections the critique flagged (e.g. confirming a project's real purpose if the critique disputed the description). Don't re-litigate the critique — use the budget only when correcting a flagged fact requires a lookup."

Capture the final text.

### Step 6 — Save to DB (MANDATORY — must succeed before Step 7)

This step is **not optional**. If it fails, the whole pipeline must stop and surface the error — do NOT proceed to Step 7 (email) on a save failure unless the orphan fallback below has written the digest to disk, or the recipient gets a digest that exists nowhere on disk authoritatively.

Write the final text to a temp file (heredoc-safe), then pipe into save-summary. Capture the JSON return.

```bash
TMP=$(mktemp -t gh_trending_digest.XXXXXX.md)
printf '%s' "$FINAL_TEXT" > "$TMP"
SAVE_RESULT=$(python3 digest_generate.py save-summary --days 1 --focus default < "$TMP")
echo "$SAVE_RESULT"   # expect {"saved": true, "date": "YYYY-MM-DD", ...}
```

Then **verify** the row landed by reading back the latest summary id for today:

```bash
NEW_ID=$(sqlite3 data/gh_trending.db \
  "SELECT id FROM summaries WHERE date='$(date -u +%Y-%m-%d)' ORDER BY id DESC LIMIT 1")
test -n "$NEW_ID" || { echo "VERIFY FAILED: no row for today"; exit 1; }
echo "saved as summary id=$NEW_ID"
```

**Fallback on failure** (DB locked / corrupted / disk full / verify came back empty): write the markdown to `data/orphan_digest_<YYYY-MM-DD>.md` and continue to Step 7 BUT prepend `[ORPHAN: not in DB] ` to the email subject so the user notices the archival gap.

```bash
# only if save-summary or VERIFY above failed
DATE=$(date -u +%Y-%m-%d)
cp "$TMP" "data/orphan_digest_${DATE}.md"
ORPHAN=1   # set this flag, Step 7 reads it to mutate the subject
```

### Step 7 — Send email via SMTP

The sender sends immediately, with no review step. Each digest date goes out once: a repeat run prints `{"sent": false, "skipped": true, ...}` and exits 0 — treat that as delivered, create no draft, and don't pass `--force` unless the user asked for a resend. `save-summary` dates rows in UTC, so use the UTC date here:

```bash
DATE=$(date -u +%Y-%m-%d)
python3 email_sender.py --date "$DATE"
# only if Step 6 set ORPHAN=1 (subject gets the [ORPHAN: not in DB] prefix):
python3 email_sender.py --date "$DATE" --file "data/orphan_digest_${DATE}.md" --orphan
```

Success prints `{"sent": true, ...}` and exits 0. The sender reads `DIGEST_RECIPIENT`, `GMAIL_SMTP_USER` and `GMAIL_APP_PASSWORD` from `<project>/.env`, and renders the markdown to HTML with a plain-text alternative. Use `--dry-run` to render and print headers without connecting.

**Fallback on a non-zero exit** (missing credentials, blocked network, auth failure): keep the error text for Step 8, then create a Gmail draft if the Gmail MCP `create_draft` tool is available. Do not retry the send.

```bash
python3 email_sender.py --date "$DATE" --dry-run --html-out "$(mktemp -t gh_trending_digest.XXXXXX.html)"
# add --file/--orphan as above; the same renderer output becomes htmlBody
```

Pass `to` = `DIGEST_RECIPIENT`, `subject` = the Subject line from the dry-run output, `body` = the digest markdown, `htmlBody` = the rendered HTML file's contents. If the Gmail MCP isn't available, report the send failure — do not fail silently.

### Step 8 — Report

Print briefly:
- Total repos covered + per-period breakdown
- Critique grade (A / B / C)
- "Saved digest for {date}" + delivery status (sent via SMTP to `<recipient>` / already sent, skipped / SMTP failed with `<error>`, draft created with id `<id>` / SMTP failed with `<error>`, Gmail MCP unavailable)

## The three prompt templates (verbatim, embedded for self-containment)

Duplicated in `digest_generate.py` and produced inside the JSON returned by Step 2 — kept here so the skill's intent is auditable.

### DRAFT_PROMPT

```
你是 GitHub Trending 中文日报的撰稿人。请根据以下数据撰写一份精炼的中文摘要。

日期：{today}
Focus: {focus_name}
{Focus 说明：{focus_instructions}}

## Daily Trending（今日热门）
{daily_text}

## Weekly Trending（本周热门）
{weekly_text}

## 连续上榜项目
{streak_text}

## 语言分布
{lang_text}

## 要求

1. 用中文撰写，项目名和技术术语保留英文
2. 按类别分组（如 AI/ML、开发工具、基础设施、前端、安全等）
3. 每个项目：名称（带链接）、语言、星数、一句话中文描述
4. 特别标注今日新星（stars_delta 大的）和连续上榜项目
5. 末尾加 "今日趋势"（2-3 句话总结）
6. 总长控制在 800-1200 字
```

### CRITIQUE_PROMPT (isolated — no access to original trending data)

```
你是一位资深开源社区观察者。请审阅以下 GitHub Trending 中文日报初稿。

## 初稿
{draft}

## 审稿要求

1. 分类是否合理？
2. 项目描述是否准确？有没有误解项目用途？
3. "今日趋势" 是否有洞察？
4. 有没有遗漏重要的热门项目？
5. 文字是否简洁流畅？

请按 A/B/C 评级并给出具体修改建议。
```

### REFINE_PROMPT

```
你是 GitHub Trending 中文日报的终稿编辑。请根据审稿意见修改初稿。

## 初稿
{draft}

## 审稿意见
{critique}

## 要求

1. 根据审稿意见逐条修改
2. 保持原有格式和链接
3. 终稿直接输出，不要包含修改说明
```

## Why isolated reflection works

The Critique subagent, denied access to the trending repo list, is forced to grade the *prose* — does it actually feel like an opinionated daily report a senior dev would read, or does it just enumerate? This catches:
- Lazy categorization that lumps unrelated repos together
- Generic descriptions that don't say what each project is *for*
- A "今日趋势" line that just restates counts instead of identifying patterns
- Missing the "so what" — why these particular repos trended today

Don't collapse the three steps into one — the isolation is the mechanism.
