# Fourth Down — NFL research desk

A responsive GitHub Pages dashboard for NFL market research. It shows games, source health, market coverage, matchup reports, pick rankings, and a private browser-only wager log. The initial deployment shows **unavailable** until verified feeds are configured. The optional **Explore demo** button displays fictional UI samples and never issues a pick.

## What works now

- Deterministic 1–10 team strength and weekly matchup ratings for both teams, computed from current-season nflverse play data. The adapter supplies passing/rushing EPA, success and explosive rates, a distance-binned field-goal baseline, net punting, and return/coverage efficiency. Missing inputs remain explicit. See [agent scoring contract](docs/SCORING.md).
- Paper-only margin, total, and projected-score forecasts. First pregame forecasts are frozen in `data/paper/<season>.json` and evaluated after final scores arrive. Historical earlier-week feature reconstruction is labeled as backtesting, separate from prospective calibration. Paper forecasts never enable recommendations by themselves.

- Static dashboard with mobile layout, game and market views, distinct win-likelihood and betting-value rankings, agent status, and browser-only wagers capped at $25 each.
- Private wager form with results, deletion, and JSON export/import. No personal wagers are committed or uploaded.
- Python pipeline that accepts separate authorized JSON feeds for Bovada odds, injuries, news, weather, travel, current-season statistics, and historical Bovada lines. Probabilities come from the built-in market model, not an external feed.
- Manual The Odds API adapter for current Bovada NFL game lines and provider-listed full-game player props. It requires a private API key and enforces a credit limit per run; provider coverage is not guaranteed to contain every Bovada website offer.
- Odds validation, timestamps, 12-minute freshness gate, American-odds EV calculation with pushes, conservative-value gate, and a valid result of zero recommended picks.
- GitHub Actions and Pages workflow with an hourly research refresh. Scheduled runs make no paid odds or AI calls. The latest public snapshot and market state are retained; line-move alerts compare the next odds observation to that state.
- Free hourly research collectors: nflverse schedule, scores, rest and current-season team scoring; National Weather Service kickoff forecasts for supported outdoor home venues; ESPN NFL RSS headlines; and daily Sleeper player-status metadata. The schedule supplies upcoming games without purchasing odds credits. Each result retains a publisher link and source check time.

## What still needs verified integration

The Odds API key has been used for capped manual tests. Historical Bovada archives, individual player/line data, calibrated betting probability models, and Claude API agent calls still need verified integration. Team and matchup research runs independently of those gaps. Betting recommendations remain paused without fresh odds and an audited probability model. GitHub Actions may start late or skip a scheduled run; the UI shows update times and stale quotes.

The free feeds may fail, change format, or have incomplete coverage. Sleeper player status is not an official game injury report; headline matching can miss player-only stories; weather skips neutral/unknown venues. Team performance metrics and matchup ratings are connected, but individual player and offensive/defensive line ratings are not. Special-teams baselines are provisional. Injury and weather reports do not generate automatic numeric penalties without verified source-backed adjustments. The last five head-to-head scores come from nflverse, while historical Bovada lines remain unavailable until actual records can be verified. Paid AI analysis remains disabled pending the required cost estimate and approved limit. The Dashboard's `Agents & sources` page exposes these gaps.

## Run locally

```bash
python3 scripts/pipeline.py
python3 scripts/paper_tracking.py
python3 -m http.server 8000
```

Open `http://localhost:8000`. Use **Explore demo** to inspect the layout without confusing sample content for actual games or picks.

For a full free-source refresh, first run `python3 -m pip install -r scripts/nfl_scoring_requirements.txt` and `python3 scripts/free_sources.py`, then the commands above. Ordinary team/value scoring does not require the model dependencies. The hourly workflow installs them and runs source collection, scoring, and paper tracking automatically. Only static frontend files and the published dashboard JSON enter the Pages artifact.

For local feed testing, set `ODDS_FEED_FILE=/absolute/path/to/odds.json` and the analogous `<ROLE>_FEED_FILE` variables before running the pipeline. The [feed contract](docs/DATA_CONTRACT.md) gives example JSON. Feed URLs require HTTPS; secrets belong in GitHub Actions secrets.

## Deploy on GitHub

1. Put these files at the root of the `nfl` repository's `main` branch.
2. In repository Settings → Pages, select **GitHub Actions** as the build/deploy source.
3. Push to `main` or run **Refresh NFL research desk** manually. Free research refreshes on the hourly workflow schedule. The last Bovada quotes are retained as stale until a new, separately approved odds check runs.
4. For an initial live-price test, register for [The Odds API](https://the-odds-api.com/), save the key as repository **Settings → Secrets and variables → Actions → New repository secret**, named `THE_ODDS_API_KEY`. Never put it in the repository or browser. On the Actions tab, run **Refresh NFL research desk** manually with **Fetch Bovada prices** checked. Leave **player props** unchecked and use the default 10-credit cap for the first test.
5. Inspect the coverage and credit usage in the Actions log. A later manual run can request all provider-listed full-game player props with a higher per-run cap after reviewing its estimated usage. If the discovered markets exceed the cap, the adapter stops before publishing a partial prop board. The paid historical API and recurring direct API checks remain disabled pending a cost estimate and approved limit.
6. Alternatively, once a verified and publishable normalized Bovada feed exists, set the repository variable `ODDS_FEED_URL`, then any other `<ROLE>_FEED_URL` variables. Store bearer tokens as Actions secrets. See the [feed contract](docs/DATA_CONTRACT.md).
7. Confirm the feed's market coverage, freshness, and settlement rules. Picks also depend on the market model's backtest gate ([details](docs/MARKET_MODEL.md)). There is no direct website scraper in this repository.

This version deploys the current published snapshot as a Pages artifact and retains the latest public snapshot, free-feed checks, and market state in the repository after runs. It does not preserve every hourly quote or provide a comprehensive historical odds archive. A historical storage design, calibrated probability models, paid Claude agent execution, and provider permissions must be approved before those components can operate.

### Quota review before recurring polling

The Odds API charges credits by region/bookmaker group and market. The featured `h2h`, `spreads`, and `totals` request is budgeted at 3 credits. Discovering props costs 1 credit per game; requesting all discovered prop types adds approximately 1 credit per type per game. The adapter uses the provider's actual `x-requests-last` response header and stops at the manual run cap. A 24/7 hourly schedule is 720 runs in 30 days; at 3 credits per run, game lines alone would use 2,160 credits before props. Actual usage depends on available markets. We will measure coverage and usage before proposing a monthly plan or enabling scheduled paid requests. Quotes and picks become stale after 12 minutes even though the next scheduled check is an hour away.

## Bet evaluation, picks and suggested amount

Every quoted market is scored by the built-in [market model](docs/MARKET_MODEL.md): free nflverse data only, no outside AI or model feed. It produces win/push/loss probabilities, EV at the Bovada price, and a conservative EV. A walk-forward backtest against historical closing lines decides which market types may issue picks; parameters live in `data/model/config.json`.

A recommendation requires a market type that has passed the backtest gate, a fresh quote, positive central and conservative EV, and a value score of at least 7. Suggested amounts use a transparent presentation tier: value scores 7, 8, 9, and 10 correspond to $6.25, $12.50, $18.75, and $25.00. This is a configurable amount **relative to the $25 cap, not a bankroll calculation**. Positive-EV views that fail a requirement appear as research leans with no stake. As of the current backtest, no market type has passed.

Historical head-to-head records are limited to the last five meetings irrespective of year and to verifiable Bovada lines. Current-season performance is kept separate. The dashboard does not invent missing historical markets.
