# Fourth Down — NFL research desk

A responsive GitHub Pages dashboard for NFL market research. It shows games, source health, market coverage, matchup reports, pick rankings, and a private browser-only wager log. The initial deployment shows **unavailable** until verified feeds are configured. The optional **Explore demo** button displays fictional UI samples and never issues a pick.

## What works now

- Static dashboard with mobile layout, game and market views, distinct win-likelihood and betting-value rankings, agent status, and browser-only wagers capped at $25 each.
- Private wager form with results, deletion, and JSON export/import. No personal wagers are committed or uploaded.
- Python pipeline that accepts separate authorized JSON feeds for Bovada odds, injuries, news, weather, travel, current-season statistics, historical Bovada lines, and externally validated probability estimates.
- Odds validation, timestamps, 12-minute freshness gate, American-odds EV calculation with pushes, conservative-value gate, and a valid result of zero recommended picks.
- GitHub Actions and Pages workflow with a five-minute scheduling target. Scheduled deployment is skipped until `ODDS_FEED_URL` is configured. A compact latest-market state is committed only when lines or odds change; line-move alerts compare the next observation to that state.

## What still needs verified integration

No direct Bovada data access, historical Bovada archive, score/stat source, source-specific crawlers, calibrated player-prop probability models, or Claude API agent calls are configured. **Do not read the deployed site as live betting research until those sources are working and audited.** GitHub Actions may start late or skip a scheduled run; the UI shows update times and stale quotes.

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
4. Once a verified and publishable Bovada feed exists, set the repository variable `ODDS_FEED_URL`, then any other `<ROLE>_FEED_URL` variables. Store bearer tokens as Actions secrets. See the [feed contract](docs/DATA_CONTRACT.md).
5. Confirm the feed's market coverage, freshness, and settlement rules. Only then consider enabling a validated model feed. There is no direct website scraper in this repository.

This first version deploys the current published snapshot as a Pages artifact and retains only the latest market state in the repository for change detection. It does not preserve every five-minute quote or provide a comprehensive historical odds archive. A historical storage design, calibrated probability models, paid Claude agent execution, and provider permissions must be approved before those components can operate.

## Ratings and suggested amount

An external validated model can provide `p_win`, `p_loss`, and `p_push`. EV per $1 is `p_win × (decimal_odds − 1) − p_loss`. The conservative scenario is evaluated separately. A recommendation requires fresh odds, model validation, at least 100 calibration observations, positive central and conservative EV, no blockers, and a value score of at least 7. Suggested amounts use a transparent presentation tier: value scores 7, 8, 9, and 10 correspond to $6.25, $12.50, $18.75, and $25.00. This is a configurable amount **relative to the $25 cap, not a bankroll calculation**. With no validated model, recommendations and stake suggestions remain empty.

Historical head-to-head records are limited to the last five meetings irrespective of year and to verifiable Bovada lines. Current-season performance is kept separate. The dashboard does not invent missing historical markets.
