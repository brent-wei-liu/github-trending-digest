# Redesign log

Branch `redesign` (worktree `../github-trending-digest-redesign`, own copy of the DB).
The daily pipeline on `main` keeps running and sending until the redesign is verified.

## Phase 1 — own star history (2026-11-24/25)

**Built** `star_history.py`: table `star_history(repo, date, stars, source)`.
- `trending` rows: total stars printed on the trending page, backfilled from all
  trending_entries (5,562 repo-days since 2026-03-25).
- `api` rows: `stargazers_count` from the GitHub API once per UTC day for every repo
  seen on any list in the last 30 days (194 repos today; ~90–120 s, ~190 calls of the
  5,000/h budget; stops at <50 remaining or on 403/429; one retry on 5xx/timeouts;
  repos already read today are skipped). Hooked into `github_fetch.py` after the scrape.
- Gains 1d/7d/30d = total(today) − total(today − N). Baseline = own observation within
  0/1/3 days of today − N (normalised to N days), else linear interpolation between
  the own observations either side (`own_interp`, ≤14 / ≤30-day bracket), else GitHub's
  own list number (`github_daily|weekly|monthly`), else for 30d the gain since our first
  observation in the window (`own_partial`, a lower bound). Source stored per gain.

**Verified**
- 11 unit tests (`tests/test_star_history.py`): exact windows, slack normalisation,
  interpolation, GitHub fallback, partial lower bound, backfill idempotency, api-over-
  trending, per-day cache, renames, 404s, rate-limit stop.
- Real run (2026-11-24 data): 193/194 recorded, 1 failure = `YuJunZhiXue/dsh-purge`
  (404, repo deleted), 3 renames detected (NVIDIA/OpenShell → ai-openshell/openshell,
  calesthio/OpenMontage → OpenMontageInc/OpenMontage, tester-army/e2e → e2e-inc/e2e).
  Smoke run of `github_fetch.py` on a scratch DB copy: 186 recorded, 4,563 calls left.
- Own vs GitHub on overlapping repos (same-time-of-day trending totals only):

  | | n | median own/GitHub | range |
  |---|---|---|---|
  | 7d vs weekly list (11-20) | 8 | 0.91 | 0.71–1.00 |
  | 7d vs weekly list (11-24) | 12 | 0.84 | 0.55–0.99 |
  | 30d vs monthly list (11-20) | 4 | 0.94 | 0.91–1.02 |
  | 30d vs monthly list (11-24) | 7 | 0.94 | 0.85–1.06 |
  | all history, 7d vs weekly | 861 | 0.89 | p10 0.73 – p90 1.04 |
  | all history, 30d vs monthly | 273 | 0.94 | p10 0.82 – p90 0.99 |

  Why own is lower: our gain is the *net* change in the total (unstars subtracted),
  GitHub's list counts gross new stars; GitHub's window also lags/extends past ours,
  which matters most for spiky repos (ag-ui: own 7d +1,626 vs weekly +2,966 — its
  +1,144 spike on 11-17 is just outside our 11-17 19:34 → 11-24 window). Fallback numbers
  are therefore ~6–11 % higher than own ones; they stay labelled, not rescaled.
- Interpolation accuracy, leave-one-out on 30 repos with an exact 7d baseline: median
  relative error of the 7d gain 2.1 %, p90 13 %.
- API (read 11-25 ~07:00 UTC) vs trending total (11-24 19:34 UTC), 42 repos: median
  +0.5 % (~11 h of growth), max 5.8 % (fast growers). In production both are read
  within minutes of each other.
- Coverage on 2026-11-24: 30d own 40 / own_interp 20 / github_monthly 12 / own_partial 121
  / none 1; 7d own 43 / own_interp 32 / none 119 (repos last listed >14 days ago — fills
  in after a week of daily API readings).
- 2026-11-20 by our 30d gain: all 10 repos of the v2code monthly Top 10 are in our top 12
  (the other two: obra/superpowers, meta/astryx).

### Note: "zero churn" since 11-19 is GitHub, not the scraper

From 11-19 to 11-24 the daily list held the same 16 repos and the weekly list the
same 13 (monthly grew 17 → 19). Re-fetched live with a cache-busting query, a no-cache
header and a different user agent on 11-25: the same membership and order, but with
updated numbers (Claude-BugHunter +1,833 at our scrape → +1,826 live), so it's not a
cached page. Main needed no fix. The redesign's own 30d ranking and 「今日变化」 are meant
to keep the digest informative when GitHub's lists barely move.

## Phase 2 — project memory + lifecycle (2026-11-25)

**Built**
- `lifecycle.py`, table `lifecycle(repo, date, status, milestone, g1/g7/g30 + sources,
  stars, listed, reason)`. Primary status, first match wins ("listed" = on any list):
  - 新上榜: listed today, never listed before in our records
  - 爆发: 1d ≥ +1,000 and ≥ 3× the average daily gain over the week before (from own
    observations; an unknown pace counts as 0)
  - 回榜: listed today, not on the previous scrape day, listed earlier (pairs with 掉榜;
    added for come-and-go repos)
  - 掉榜: listed on the previous scrape day, not today
  - 降温: complete 30d window (own / own_interp / github_monthly) with ≥ 300/day and 7d
    pace ≤ 0.5 × the 30d pace
  - 持续走红: 7d average ≥ 500/day, listed ≥ 5 of the last 7 scrape days, and today's 1d
    ≥ 250 when known (one spike inside the window isn't "sustained")

  Flag 里程碑: the total crossed 1k/5k/10k/20k/50k/100k/200k/300k/500k/1M since the
  previous own observation (≤ 3 days old). Backfilled for 30 scrape days (0.5 s).
- `project_memory.py`, table `projects`: cached 是什么 (one sentence, validated: Chinese,
  ≤ 60 chars, one sentence, no star numbers), the description + README excerpt it was
  written from, and README excerpts (API, TTL 7 days). `profile()` joins first_seen
  (first trending appearance), owner type, language, license and created_at. A sentence
  is regenerated only when the description or README token-Jaccard similarity drops
  below 0.6; the old sentence is passed in as context.
- `github_fetch.py` now also refreshes READMEs and today's lifecycle after the scrape.

**Verified**
- Tests: 13 lifecycle tests (rule table; superpowers-like come-and-go → 掉榜 on the
  day after leaving, nothing while absent, 回榜 on return; monthly giant → 持续走红 then
  降温; one-day spike → 爆发, then nothing the next day; brand-new repo → 新上榜 + 里程碑;
  prior pace from consecutive readings), 6 project-memory tests (cache reuse, cosmetic
  edit kept, pivot regenerates, README change regenerates, empty description, README
  TTL / 404). Full suite: 78 passed.
- Bugs found and fixed during verification:
  - A one-day spike made the repo 持续走红 for the following week → added the today ≥ 250
    condition.
  - 爆发 fired with "此前无日均数据" for repos that had 5 consecutive daily totals
    (no exact D−7 reading) → previous pace is now computed from the observations
    themselves.
  - In this verification run the API reading was ~11 h newer than the trending total
    and inflated 1d gains → a same-day trending total (read at the same moment as
    GitHub's lists) now wins when it agrees with the API within 5 %.
  - Empty descriptions made 是什么 look stale on every run → basis format fixed.
- Real data, 2026-11-24: 持续走红 9 (t3code, impeccable, hallmark, superpowers,
  hyperframes, BugHunter, ffmpeg-skill, last30days, minimind), 降温 3
  (security-audit: 7d +461/day vs 30d +1,048/day; DeepTutor; claude-mem), 新上榜 1
  (Ebony-Vinyl/dsh-our-free-model), milestones: anthropics/skills 破 20 万,
  BugHunter 破 2 万, app-store-compliance 破 5,000. No 回榜/掉榜 since 11-20 is correct
  (GitHub's lists didn't change).
- obra/superpowers timeline: 持续走红 to 11-07 → 掉榜 11-08 → (absent) → 回榜 11-17 →
  持续走红 from 11-21.
- 2026-11-20, v2code's ten: 7 持续走红, 3 降温 (security-audit, DeepTutor, claude-mem).
- 是什么 seeded once for all 194 active repos by two writer agents (~181k tokens in
  total, one-off): 194 saved, 0 rejected by validation, length 10–60 chars (avg 41).
  Spot-checked 22, all accurate to the README; none mention star numbers.
- Smoke run of the full `github_fetch.py` on a scratch DB copy: 186 star readings,
  0 README refetches (cached), 187 lifecycle rows, ~2 min.

## Phase 3 — new self-contained daily email (2026-11-25)

**Built** `digest_v2.py` (query / render / check / save-summary).
- Python builds the payload and every line: 今日变化 (new or status-changed repos +
  milestones, priority 新上榜 > 爆发 > 里程碑 > 回榜 > 持续走红 > 降温; ≤ 8 detailed, the rest
  and all 掉榜 on one compact line), 30 天热度总榜 Top 30 by our 30d gain (every day,
  one line: link · cached 是什么 · 30 天 · 7 天 · status), 新锐榜 (≤ 90 days AND < 20,000
  stars, by 7d gain, ≤ 10, never padded), a 2-line footnote. Gains print plain when own,
  `*` when borrowed from GitHub's list, `≥` for a lower bound.
- Every repo name links to its web UI page `http://little-claws-mac-mini.local:8082/repo/owner/repo`;
  detailed items also link to GitHub.
- The LLM returns JSON only: `why` (exactly the upward 今日变化 repos; "" = omit),
  `themes` (exactly 3, repos must be in the email), `what` (only for repos missing
  from the cache, saved to the cache after validation). `validate_llm` rejects star
  numbers, status-as-reason (首次上榜/新上榜), > 80 chars, wrong keys, foreign repos.
  `check` re-validates the final markdown: title, headings, verbatim Python lines,
  links only to today's repos, 为什么涨 rules, 「新上榜」 only where the data says so,
  3 themes, footnote.
- Draft → Critique → Refine kept for now (Phase 6 compares); the critic sees only the
  rendered email.

**Verified**
- 5 new tests (rankings on a synthetic DB: monthly giant leads the 30d list, brand-new
  repo → 新上榜 change, come-and-go → 回榜, render/check round-trip, bad-JSON rejection,
  edited numbers / foreign links / fake 「新上榜」 / missing footnote caught). Suite: 85 passed.
- Fixes found while verifying: own_partial now beats a smaller GitHub monthly number
  (BugHunter showed 30d +13,817* next to 7d +12,296) and needs ≥ 7 days of own history
  (firecrawl showed 30d ≥+1,112 from one day); 新锐榜 no longer requires being listed
  today; footnote cut to 2 lines.
- 2026-11-24 end to end: writer JSON passed validation first time; critique B (BugHunter's
  "reason" was coverage that followed the rise, dsh cited a community build, one theme
  mis-cast magpie); refine fixed all, passed after shortening two over-long lines;
  `check` ok. 4 detailed changes (dsh 新上榜, BugHunter 破 2 万, app-store-compliance
  破 5,000, minimind 持续走红), Top 30, 新锐榜 3.
- 2026-11-20 end to end (writer only): `check` ok. All ten of v2code's monthly Top 10
  are in the 30 天热度总榜 at #1–#12 with their 30d/7d gains and status.
- Email size (MIME, as sent):

  | | chars | MIME | repos |
  |---|---|---|---|
  | current main, 11-17 | 4,619 | 23,770 B | 16 |
  | current main, 11-24 | 5,784 | 28,592 B | 18 |
  | v2, 11-20 | 7,288 | 29,896 B | 37 |
  | v2, 11-24 | 7,741 | 32,209 B | 37 |

  ~+12 % bytes for twice the repos covered; HTML structure ok (h2 sections, one `<ol>`
  for the Top 30, `<ul>` for 新锐榜); plain-text part is clean.
- Per stage (11-24 v2 vs the current pipeline's last measured run, 11-17):

  | Stage | current: time / web / tokens | v2: time / web / tokens |
  |---|---|---|
  | Draft | 46 s / 0 / 62k | 50 s / 2 / 61k |
  | Critique | 134 s / 10 / 72k | 111 s / 8 / 70k |
  | Refine | 70 s / 0 / 72k | 70 s / 0 / 65k |
  | Total | 250 s / 10 / 206k | 231 s / 10 / 196k |

  (11-20 v2 writer: 60 s / 3 web / 63k.) Cost is about the same; the LLM now writes
  far less text, so Phase 6 tests dropping Critique/Refine.
- Not verified here: the UI links resolve (Phase 4).
