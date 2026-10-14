# Source feed contract

The pipeline reads independent HTTPS JSON feeds configured with `<ROLE>_FEED_URL` repository variables. A local developer may use `<ROLE>_FEED_FILE` instead. Optional bearer credentials are read from `<ROLE>_FEED_TOKEN` secrets. A feed may supply only the fields it can verify. The app never promotes unverified or stale odds to recommendations.

## Odds feed

This feed must represent **Bovada** observations. An adapter that returns another book's odds must use a different product; the pipeline rejects it. Each market has a stable ID for the event, period, market type, and selection so ordinary line or price changes can be compared; simultaneous alternate-line ladder selections also include the point. The `observed_at` timestamp describes when the provider observed the actual quote, not when this pipeline downloaded the JSON.

The optional `scripts/the_odds_api.py` adapter converts only the Bovada bookmaker from The Odds API into this schema. It excludes period markets and accepts every `player_` key advertised for each event except quarter/half/period-specific keys. It does not claim to cover Bovada offers the provider omits. The manual Actions workflow uses `THE_ODDS_API_KEY` as a server-side secret and requires a per-run credit cap. Its generated feed is not committed to the repository.

```json
{
  "checked_at": "2026-10-12T20:00:00Z",
  "games": [
    {"id":"nfl-game-id","away":"Away Team","home":"Home Team","kickoff":"2026-10-18T17:00:00Z","status":"scheduled"}
  ],
  "markets": [
    {"id":"stable-market-id","game_id":"nfl-game-id","sportsbook":"Bovada","period":"full_game","category":"Spread","selection":"Home Team -2.5","line":-2.5,"odds":-110,"observed_at":"2026-10-12T20:00:00Z","available":true,"source_url":"https://example.com/verified-record"}
  ]
}
```

Categories: `Spread`, `Moneyline`, `Total`, `Player prop`. Alternate lines need separate IDs. A quote stays current until a newer check replaces it. It is marked stale if it is suspended by its provider or the latest check failed; a market missing from the latest successful check is dropped. The provider must document its permission to collect and publish its data. This repository does not contain an unverified Bovada scraper.

## Research feeds

`NEWS`, `INJURIES`, `WEATHER`, and `TRAVEL` feeds may return `findings` and `alerts`:

```json
{"checked_at":"2026-10-12T20:00:00Z","findings":[{"game_id":"nfl-game-id","title":"Verified finding","publisher":"Original source","url":"https://example.com/story","published_at":"2026-10-12T19:30:00Z"}],"alerts":[{"id":"unique-alert","title":"Material update","detail":"What changed","at":"2026-10-12T20:00:00Z"}]}
```

The adapter must distinguish publication time from retrieval time and cite the original source. It must not repeat third-party text as if it were an independent report.

## Statistics, historical Bovada lines, and model

`STATS` may return a `season` and `matchups` keyed by game ID. The current pipeline displays ratings supplied by that feed; it does not claim to have computed them itself. A proper current-season model must document its peer group, sample size, and weights before these ratings become authoritative.

```json
{"checked_at":"2026-10-12T20:00:00Z","season":"2026","matchups":{"nfl-game-id":{"season":"2026","rating_side":"Home Team","offense":6.0,"defense":5.2,"pass":6.2,"rush":5.1,"line":7.0,"confidence":5.6,"summary":"Documented current-season comparison","players":[{"name":"Example player","position":"WR","side":"Home Team","performance":6.4,"matchup":7.1,"sample":"5 games","rationale":"Source-backed comparison"}]}}}
```

`HISTORICAL` may return a `games` object keyed by upcoming game ID. Each array contains at most the last five meetings, newest first. `bovada_lines` must contain only independently verified historical Bovada records; if absent, the UI shows unavailable.

`MODEL` is no longer an input. Probabilities come from the built-in market model; see [MARKET_MODEL.md](MARKET_MODEL.md).

## Deterministic scoring module

Agents can now use `scripts/nfl_scoring.py` for both team strength/weekly matchup
and price-based betting value. The [scoring contract](SCORING.md) documents the
raw `STATS.teams` schema, supported context adjustments, callable Python functions,
missing-data handling, optional projection fitting, and exact score bands.
The pipeline calculates these added team scores instead of treating an agent's
subjective grade as measured performance. Existing legacy `matchups` remain
supported separately. The UI shows both teams' new scores in the game report.
Bet recommendations use conservative EV for the value grade and retain the
existing model-validation and quote-freshness gates.

## Public and private data

The generated `data/dashboard.json` is public. It must contain only publishable research. Wagers entered on the dashboard are stored in the browser under `fourth-down-private-wagers-v1`; no crawler or GitHub workflow reads them. JSON export/import is the backup path. Clearing browser data without a backup erases those entries.

