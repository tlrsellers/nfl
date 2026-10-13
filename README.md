# Fourth Down — NFL research desk

A responsive GitHub Pages dashboard for NFL market research. It shows games, source health, market coverage, matchup reports, pick rankings, and a private browser-only wager log. The initial deployment shows **unavailable** until verified feeds are configured. The optional **Explore demo** button displays fictional UI samples and never issues a pick.

## What works now

- Static dashboard with mobile layout, game and market views, distinct win-likelihood and betting-value rankings, agent status, and browser-only wagers capped at $25 each.
- Private wager form with results, deletion, and JSON export/import. No personal wagers are committed or uploaded.
- Python pipeline that accepts separate authorized JSON feeds for Bovada odds, injuries, news, weather, travel, current-season statistics, historical Bovada lines, and externally validated probability estimates.
- Manual The Odds API adapter for current Bovada NFL game lines and provider-listed full-game player props. It requires a private API key and enforces a credit limit per run; provider coverage is not guaranteed to contain every Bovada website offer.
- Odds validation, timestamps, 12-minute freshness gate, American-odds EV calculation with pushes, conservative-value gate, and a valid result of zero recommended picks.
- GitHub Actions and Pages workflow with an hourly schedule target. Scheduled deployment is skipped until an approved recurring source is configured. The latest public snapshot and market state are retained when a verified feed runs; line-move alerts compare the next observation to that state.

## What still needs verified integration

The Odds API key has been used for capped manual tests. Historical Bovada archives, score/stat sources, source-specific crawlers, calibrated player-prop probability models, and Claude API agent calls are not configured. **Do not read the deployed site as live betting research until those sources are working and audited.** GitHub Actions may start late or skip a scheduled run; the UI shows update times and stale quotes.

The project supports independent collector inputs but does not yet contain fully operating news, travel, injury, weather, and historical crawlers. Paid AI analysis is disabled while the required cost estimate and user-approved limit are pending. The Dashboard's `Agents & sources` page exposes these gaps.

## Run locally

```bash
python3 scripts/pipeline.py
python3 -m http.server 8000
```

Open `http://localhost:8000`. Use **Explore demo** to inspect the layout without confusing sample content for actual games or picks.

For local feed testing, set `ODDS_FEED_FILE=/absolute/path/to/odds.json` and the analogous `<ROLE>_FEED_FILE` variables before running the pipeline. The [feed contract](docs/DATA_CONTRACT.md) gives example JSON. Feed URLs require HTTPS; secrets belong in GitHub Actions secrets.

## Deploy on GitHub

1. Put these files at the root of the `nfl` repository's `main` branch.
2. In repository Settings → Pages, select **GitHub Actions** as the build/deploy source.
3. Push to `main` or run **Refresh NFL research desk** manually to deploy the unavailable-state dashboard.
4. For an initial live-price test, register for [The Odds API](https://the-odds-api.com/), save the key as repository **Settings → Secrets and variables → Actions → New repository secret**, named `THE_ODDS_API_KEY`. Never put it in the repository or browser. On the Actions tab, run **Refresh NFL research desk** manually with **Fetch Bovada prices** checked. Leave **player props** unchecked and use the default 10-credit cap for the first test.
5. Inspect the coverage and credit usage in the Actions log. A later manual run can request all provider-listed full-game player props with a higher per-run cap after reviewing its estimated usage. If the discovered markets exceed the cap, the adapter stops before publishing a partial prop board. The paid historical API and recurring direct API checks remain disabled pending a cost estimate and approved limit.
6. Alternatively, once a verified and publishable normalized Bovada feed exists, set the repository variable `ODDS_FEED_URL`, then any other `<ROLE>_FEED_URL` variables. Store bearer tokens as Actions secrets. See the [feed contract](docs/DATA_CONTRACT.md).
7. Confirm the feed's market coverage, freshness, and settlement rules. Only then consider enabling a validated model feed. There is no direct website scraper in this repository.

This version deploys the current published snapshot as a Pages artifact and retains the latest public snapshot and market state in the repository after verified runs. It does not preserve every hourly quote or provide a comprehensive historical odds archive. A historical storage design, calibrated probability models, paid Claude agent execution, and provider permissions must be approved before those components can operate.

### Quota review before recurring polling

The Odds API charges credits by region/bookmaker group and market. The featured `h2h`, `spreads`, and `totals` request is budgeted at 3 credits. Discovering props costs 1 credit per game; requesting all discovered prop types adds approximately 1 credit per type per game. The adapter uses the provider's actual `x-requests-last` response header and stops at the manual run cap. A 24/7 hourly schedule is 720 runs in 30 days; at 3 credits per run, game lines alone would use 2,160 credits before props. Actual usage depends on available markets. We will measure coverage and usage before proposing a monthly plan or enabling scheduled paid requests. Quotes and picks become stale after 12 minutes even though the next scheduled check is an hour away.

## Ratings and suggested amount

An external validated model can provide `p_win`, `p_loss`, and `p_push`. EV per $1 is `p_win × (decimal_odds − 1) − p_loss`. The conservative scenario is evaluated separately. A recommendation requires fresh odds, model validation, at least 100 calibration observations, positive central and conservative EV, no blockers, and a value score of at least 7. Suggested amounts use a transparent presentation tier: value scores 7, 8, 9, and 10 correspond to $6.25, $12.50, $18.75, and $25.00. This is a configurable amount **relative to the $25 cap, not a bankroll calculation**. With no validated model, recommendations and stake suggestions remain empty.

Historical head-to-head records are limited to the last five meetings irrespective of year and to verifiable Bovada lines. Current-season performance is kept separate. The dashboard does not invent missing historical markets.
