# Agent scoring contract

`scripts/nfl_scoring.py` implements the two scoring systems: current-season
team strength/weekly matchup and betting value at the exact offered price.
It also includes optional current-season margin and total model fitting.
No paid source or AI call is made by this module.

## Agent API

```python
from scripts.nfl_scoring import score_league, score_matchup, score_bet

teams = score_league(stats_feed["teams"], stats_feed["season"])
report = score_matchup(teams[home_name], teams[away_name], context)
value = score_bet(
    {"win": 0.57, "push": 0.0, "loss": 0.43},
    american=-110, uncertainty_pp=2.0, validated=False,
)
```

Agents gather and verify numeric inputs and evidence. They should call these
functions to calculate scores instead of inventing a subjective numeric grade.
No player/line rating is fabricated from overall team performance. Those ratings
still need separate role-specific data and can use the existing players feed.

## Statistics feed additions

Add `teams` to the existing STATS JSON feed, along with a timezone-aware `as_of`
timestamp representing the information cutoff. `checked_at` remains retrieval
time. Team identifiers MUST match the dashboard's full home/away names exactly.
The dictionary below describes one team; provide all 32 for complete league
rankings. Example values are fictional.

```json
{
  "season": "2026",
  "as_of": "2026-10-13T10:00:00Z",
  "checked_at": "2026-10-13T10:00:00Z",
  "teams": [{
    "team": "Example Team",
    "season": "2026",
    "games_played": 5,
    "sources": ["https://example.com/source"],
    "metrics": {
      "pass_offense": {
        "epa": {"season_to_date": 0.12, "last_three": 0.18},
        "success_rate": {"season_to_date": 0.48, "last_three": 0.50},
        "explosive_rate": {"season_to_date": 0.10, "last_three": 0.11}
      },
      "rush_offense": {
        "epa": {"season_to_date": -0.02},
        "success_rate": {"season_to_date": 0.42},
        "explosive_rate": {"season_to_date": 0.06}
      },
      "pass_defense": {
        "epa": {"season_to_date": -0.04},
        "success_rate": {"season_to_date": 0.40},
        "explosive_rate": {"season_to_date": 0.07}
      },
      "rush_defense": {
        "epa": {"season_to_date": -0.08},
        "success_rate": {"season_to_date": 0.38},
        "explosive_rate": {"season_to_date": 0.04}
      },
      "special_teams": {
        "field_goals_above_expected": {"season_to_date": 0.03},
        "net_punting": {"season_to_date": 42.5},
        "return_coverage_efficiency": {"season_to_date": 0.01}
      }
    }
  }],
  "scoring_context": {
    "example-game-id": {
      "neutral_site": false,
      "rest_days_difference": 3,
      "travel_adjustments": {"away": -0.1},
      "travel_evidence": {"away": "https://example.com/verified-itinerary"},
      "unit_adjustments": {
        "home": [{"unit": "pass_offense", "delta": -0.5,
          "reason": "Verified expected-lineup change relative to the measured sample",
          "source_url": "https://example.com/injury-report"}]
      }
    }
  }
}
```

All defense metrics are quantities ALLOWED, so lower is better. Offense and
special-teams metrics are higher-is-better. Rates are fractions, not percentages.
EPA is per dropback for passing and per designed run for rushing. Include sacks
and scrambles in dropbacks. Use a consistent definition of success (positive
EPA), explosive passes (20+ yards), and explosive runs (10+ yards), excluding
kneel-downs/spikes and using a documented garbage-time policy.

Special-teams metrics need consistent definitions across peers: field-goal
percentage above expected over attempts; net punting yards per punt; and a
source-defined return/coverage efficiency where higher means better. Do not
mix units or providers across teams. The model does not infer missing expected
field-goal or coverage data from raw points scored.

If an adapter adjusts for opponent strength, it must do so using only earlier
current-season information and document that transformation. This script ranks
the provided metrics; it does not itself calculate opponent-adjusted play data.

## Team strength rules

- Passing offense: 35%; rushing offense: 15%; passing defense: 35%; rushing defense: 10%; special teams: 5%.
- Each offense/defense unit: 60% EPA, 30% success rate, 10% explosive rate.
- Special teams: 50% field goals above expected, 30% net punting, 20% return/coverage efficiency. This fills in the previously unspecified special-teams weights as a provisional starting rule.
- Blend raw metrics 75% season-to-date and 25% last three games when recent data exists, BEFORE ranking.
- Average tied ranks map best to 10, worst to 1, average to 5.5.
- Apply `5.5 + games/(games+4) * (raw_score-5.5)` once to each unit.
- Missing metric components have a disclosed neutral contribution; remaining weights are not inflated. Entire missing units are null. An entirely missing team has null strength, not an invented 5.5.
- Partial league coverage is explicitly incomplete; metric_details records each peer count. data_confidence is a 0–10 evidence indicator, not a prediction probability. It combines sample shrinkage with weighted peer coverage.
- Overall strength can be provisional with partial inputs. Do not train or act on incomplete ratings without an explicit missing-data policy and separate validation.

## Matchup rules

Each category is `5.5 + (own unit - opposing corresponding unit)/2`.
For defense, compare own defense with opposing offense. Apply the same category
weights. If any entire category is missing, overall weekly matchup stays null;
available category comparisons are still shown.

Confirmed injury or weather/style evidence can change an affected expected unit
by at most +/-2 rating points per evidence item. Combined changes are clipped to
1–10. Agents must cite a source and reason. That range is a policy bound, not an
estimated player value. Do not count a player's absence again if the observed
season sample already reflects it. The original team strength remains the
measured performance score; expected_lineup_units feeds the weekly comparison.

Non-neutral home field adds 0.2 to the home matchup. Rest advantage uses 0.05
per day, capped at +/-0.2 for each side. Travel penalties are from 0 to -0.2 with
evidence. Combined situational changes are capped at +/-0.5. These provisional
context rules need backtesting. Unknown venue defaults to neutral/no bonus.
Do not send these matchup bonuses into the point model again; venue/rest already
have separately learned regression coefficients there.

## Betting value rules

`score_bet` takes win/push/loss probabilities summing to one. Net EV per $1 is
`p_win*(decimal_odds-1)-p_loss`. Push-aware break-even is 1/decimal_odds among
settled outcomes. The default uncertainty allowance moves 2 percentage points
from win to loss, preserving pushes. This is a policy adjustment, not a confidence
interval. Optional fitted projections use a 3-point allowance by default.

| Score | Conservative EV |
|---|---|
| 1 | <=0%, pass |
| 2 | >0% and <1% |
| 3 | >=1% and <2% |
| 4 | >=2% and <3% |
| 5 | >=3% and <4% |
| 6 | >=4% and <5% |
| 7 | >=5% and <6% |
| 8 | >=6% and <8% |
| 9 | >=8% and <10% |
| 10 | >=10% |

Unvalidated standalone results retain raw_value_score but cap bet_score at 5.
Passing `validated=True` is an operator assertion, not automatic validation.
Never let an agent promote its own output to validated based on a high score.
The deployed pipeline continues to require an externally audited model feed,
at least 100 calibration observations, positive central/conservative EV,
fresh odds, no blockers, and grade >=7. Its grade now uses conservative EV.
Optional `line` and `odds` fields bind estimates to a particular quote; mismatched
values are rejected. Conservative probabilities must preserve push mass and
must not be more optimistic than central probabilities.

## Optional points/probability model

```bash
python -m pip install -r scripts/nfl_scoring_requirements.txt
python scripts/nfl_scoring.py train training_games.json --output model.json
python scripts/nfl_scoring.py predict upcoming_game.json --model model.json
python scripts/nfl_scoring.py rate upcoming_bet.json --model model.json
```

Training records are an array with game_id, season, week, timezone-aware as_of
and kickoff, neutral_site, rest_days_difference, home_score, away_score, and home
and away objects containing five NUMERIC unit ratings. Use expected_lineup_units
from saved complete pregame reports, not the season-end ratings or weekly matchup
score. Upcoming games omit final scores and must belong to a week AFTER the
training data. The scorer needs at least 48 games with 16 forward evaluations.

Example training record (fictional):

```json
{"game_id":"example","season":2026,"week":4,
 "as_of":"2026-09-27T15:00:00Z","kickoff":"2026-09-27T17:00:00Z",
 "neutral_site":false,"rest_days_difference":0,
 "home":{"pass_offense":6,"rush_offense":5,"pass_defense":6,"rush_defense":5,"special_teams":5.5},
 "away":{"pass_offense":5,"rush_offense":5,"pass_defense":5,"rush_defense":5,"special_teams":5.5},
 "home_score":24,"away_score":20}
```

For `rate`, input is {"game": GAME, "bet": BET}. BET has market
(`spread`, `moneyline`, `total`), side (`home`/`away` or `over`/`under`), line for
spread/total, american_odds, quoted_at, and optional uncertainty_pp. Moneyline
also requires tie_rule (`refund` or `loss`) matching the sportsbook's settlement.
Spread line is the selected team's handicap, e.g. home -3.5 or away +3.5.
Quotes must be at most 12 minutes old relative to the game snapshot cutoff.
Only full-game two-way markets with compatible overtime rules are supported.

The fitter uses standardized Ridge(alpha=20) for margin and total with
current-season unit differences/levels, venue, and rest. Standardization is fitted
only on earlier weeks. Forward errors set distribution width, with provisional
floors of 10 margin points and 12 total points. A rounded normal approximation
handles integer-line pushes; totals are truncated below zero. It does not fully
model NFL key numbers. Fitting and Brier/RMSE reporting do NOT establish
calibration. These outputs stay provisional and are NOT automatically exported
as an approved MODEL feed.

Before deployment as a probability model, evaluate separate moneyline/spread/
total reliability tables, Brier and log loss on untouched later weeks, benchmark
against simple and market baselines, and examine errors around 3/7 and score bands.
Any probability calibrator must be trained on earlier out-of-sample predictions
and tested on still-later games. Current-season-only samples limit early confidence.

## CLI and dashboard integration

```bash
python scripts/nfl_scoring.py teams stats_feed.json --output team_ratings.json
python scripts/nfl_scoring.py value examples/scoring_value.json
python -m unittest discover -s tests
python scripts/pipeline.py
```

The pipeline consumes additions to STATS automatically. Its output includes
team_ratings at the top level and games[].scoring for matching games. The game
report displays both teams' strength, weekly matchup, and expected unit ratings.
Scoring engine source health explains missing inputs or rejects invalid data.
Existing scoring-only free feeds cannot populate the EPA model. Supply a full
current-season play-data adapter or verified agent feed to enable team grades.
No scheduled paid calls, personal wagers, or AI budget settings are changed.
