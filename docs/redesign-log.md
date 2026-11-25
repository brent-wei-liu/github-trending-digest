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
