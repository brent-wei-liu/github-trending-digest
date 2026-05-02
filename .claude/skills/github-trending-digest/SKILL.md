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

Generate a daily Chinese-language digest of GitHub Trending across daily/weekly/monthly periods, using a three-step **isolated reflection** pipeline (Draft → Critique → Refine) followed by save + email-as-Gmail-draft.

**Project root:** the directory containing this SKILL.md's grand-grandparent (`<project>/.claude/skills/github-trending-digest/SKILL.md`). The scheduled-task wrapper or invoking session should `cd` there before running any of the steps below.

## Workflow

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

Spawn an Agent with `subagent_type=general-purpose`. Prompt = `prompts.draft` from Step 2. The prompt already contains the trending data inline (it's compact for GitHub trending unlike tweets, no separate file needed).

Capture the returned draft text. Do not strip or reformat.

### Step 4 — Critique (subagent #2, **ISOLATED**)

Spawn an Agent with `subagent_type=general-purpose`. Give it ONLY:
- The Critique template (`prompts.critique_template`) with `{draft}` substituted to the Step 3 output
- **Do NOT pass the trending data again.** The critique subagent must judge prose quality only — categorization sense, description accuracy, trend insight, missing repos, readability — without seeing the raw repo list. This forces it to evaluate the digest on its own merits.

Capture the critique text (ends with grade A / B / C).

### Step 5 — Refine (subagent #3)

Spawn an Agent with `subagent_type=general-purpose`. Give it the Refine template with `{draft}` and `{critique}` substituted from Steps 3 and 4.

Capture the final text.

### Step 6 — Save to DB

```bash
echo "$FINAL_TEXT" | python3 digest_generate.py save-summary --days 1 --focus default
```

(Use a heredoc or temp file in practice.)

Appends a row to the `summaries` table with today's date, period='daily', focus, content, created_at.

### Step 7 — Create Gmail draft

Use the Gmail MCP `create_draft` tool to create (not send) a draft email containing the final digest. The user reviews in Gmail and sends manually.

**Convert the digest markdown to HTML before calling `create_draft`** — Gmail's UI does not render markdown. Pass BOTH:

- `to`: read `DIGEST_RECIPIENT` from `<project>/.env` (parse `KEY=VALUE` lines, value of `DIGEST_RECIPIENT`). If missing, abort with a clear error.
- `subject`: `GitHub Trending YYYY-MM-DD`
- `body`: the plain markdown text (fallback for non-HTML clients)
- `htmlBody`: the rendered HTML (used by Gmail web/mobile)

Renderer requirements:
- Headings (`#` / `##` / `###`) → `<h1>` / `<h2>` / `<h3>`
- Bullet lists (`- ` or `* `) → `<ul><li>`
- Bold (`**text**`) → `<strong>`
- Inline code (`` `x` ``) → `<code>`
- Links `[text](url)` → `<a href="url">text</a>`
- Blank lines separate paragraphs (`<p>`)
- HTML-escape the source first

Wrap in a minimal HTML shell with inline styles for readability:
```html
<!DOCTYPE html><html><body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:680px;margin:auto;padding:20px;line-height:1.5;color:#222;">
  ...rendered content...
</body></html>
```

Prefer Python's `markdown` library if available (`python3 -c "import markdown; print(markdown.markdown(open('<file>').read()))"`). Otherwise inline a minimal converter:

```python
import html, re
def md_to_html(md):
    safe = html.escape(md)
    out, in_list, para = [], False, []
    bold = re.compile(r'\*\*(.+?)\*\*')
    code = re.compile(r'`([^`]+?)`')
    link = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')
    def inline(s):
        s = bold.sub(r'<strong>\1</strong>', s)
        s = code.sub(r'<code>\1</code>', s)
        s = link.sub(r'<a href="\2">\1</a>', s)
        return s
    def flush_para():
        nonlocal para
        if para:
            out.append('<p>' + inline(' '.join(para).strip()) + '</p>')
            para = []
    def close_list():
        nonlocal in_list
        if in_list: out.append('</ul>'); in_list = False
    for line in safe.split('\n'):
        s = line.strip()
        m = re.match(r'^(#{1,3})\s+(.+)$', s)
        if m:
            flush_para(); close_list()
            lvl = len(m.group(1))
            out.append(f'<h{lvl}>{inline(m.group(2))}</h{lvl}>')
        elif s.startswith('- ') or s.startswith('* '):
            flush_para()
            if not in_list: out.append('<ul>'); in_list = True
            out.append(f'<li>{inline(s[2:])}</li>')
        elif not s:
            flush_para(); close_list()
        else:
            close_list(); para.append(s)
    flush_para(); close_list()
    return ('<!DOCTYPE html><html><body style="font-family:-apple-system,'
            'BlinkMacSystemFont,Segoe UI,sans-serif;max-width:680px;'
            'margin:auto;padding:20px;line-height:1.5;color:#222;">'
            + '\n'.join(out) + '</body></html>')
```

If the Gmail MCP isn't available, surface that to the user — do not silently fall back.

### Step 8 — Report

Print briefly:
- Total repos covered + per-period breakdown
- Critique grade (A / B / C)
- "Saved digest for {date}" + draft creation status (created with id `<id>` / Gmail MCP unavailable / error)

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
