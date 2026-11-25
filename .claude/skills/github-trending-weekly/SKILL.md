---
name: github-trending-weekly
description: >
  Generate the Monday Chinese-language GitHub Trending weekly report (本月 Top 10 and
  本周 Top 10 by our own star gains, deeper write-ups, week-over-week movers, the
  week's lifecycle events) and email it with subject 「GitHub Trending 周报 YYYY-MM-DD」.
  Use when the user asks for the weekly GitHub report / 周报, or when the Monday task fires.
---

# GitHub Trending Weekly Report

Same contract as the daily digest (`.claude/skills/github-trending-digest/SKILL.md`):
Python computes every fact, one writer agent returns JSON, one fact-check agent
verifies only the writer's new claims, Python renders and checks. Run from the
project root; `$S` = a scratch directory.

1. **Data.** The daily fetch (09:30 PT) must have run today: `python3 weekly_digest.py
   query > $S/payload.json`; abort if `meta.date` isn't today (UTC).
2. **Writer.** Write `prompts.writer` to `$S/writer_prompt.md`, append
   `附注：WebSearch/WebFetch 最多 8 次，只用于给「为什么涨」找证据。`. One general-purpose
   agent writes ONLY the JSON to `$S/draft.json` and fixes it until
   `python3 weekly_digest.py render $S/draft.json > $S/draft.md` exits 0.
3. **Fact-check.** `python3 weekly_digest.py factcheck-prompt $S/draft.json > $S/fc_prompt.md`,
   append `附注：WebSearch/WebFetch 最多 5 次。`; one agent writes ONLY the verdicts JSON
   to `$S/fc_verdicts.json`. Then
   `python3 weekly_digest.py apply-factcheck $S/draft.json $S/fc_verdicts.json $S/final.json`,
   `python3 weekly_digest.py render $S/final.json > $S/final.md`,
   `python3 weekly_digest.py check $S/final.md` (must exit 0). A dropped theme: rewrite it
   from the payload and re-render. Never hand-edit numbers or links.
4. **Save + send.**
   ```bash
   python3 weekly_digest.py save-summary < $S/final.md
   python3 email_sender.py --kind weekly --date "$(date -u +%Y-%m-%d)"
   ```
   Weekly sends have their own dedupe key; `{"skipped": true}` = already sent, don't
   `--force` unless asked. On a send failure fall back to a Gmail draft or report it.
5. **Report.** Month/week Top 10 leaders, movers, per-agent time / tokens / web calls,
   fact-check verdicts, chars, summary id, send result.
