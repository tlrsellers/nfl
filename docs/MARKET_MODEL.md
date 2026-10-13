# Market model and backtest gate

The dashboard's bet evaluations come from `scripts/market_model.py`. It runs entirely inside GitHub Actions on free nflverse data. There are no AI or paid model calls.

## Data

- `data/model/team_games.csv`: one row per team per regular-season game since 2010 (designed passes and rushes, EPA and successful plays, garbage time excluded). Built once from nflverse play-by-play with `scripts/history_data.py build`; the current season is refreshed every run from the play cache with `history_data.py current`.
- nflverse `games.csv` (schedules release): final scores plus consensus closing spread, total, moneylines and prices for every game. Downloaded to `.cache/` each run; not committed.

## Model

1. **Pregame ratings.** For each team: passing and rushing EPA/play and success rate, on offense and defense. Each rating is last season's value regressed 50% toward average, blended with this season's games played so far (200 pseudo-plays for passing, 120 for rushing). Only weeks before the game count.
2. **Margin and total.** Ridge regressions: home margin on the closing spread plus rating differences and rest; total points on the closing total plus rating sums and a dome flag. The line coefficient is not penalized, so the model only needs to learn where the line is wrong.
3. **Probabilities.** For a fair line *x*, the outcome distribution is the real results of historical games whose closing line was near *x* (Gaussian weights, bandwidth 1 point). This keeps the true frequency of 3, 7, 10 and the push probability on whole-number lines.
4. **Value.** EV at the quoted price, plus a conservative EV that moves 2 percentage points from win to loss, mapped to the repository's 1–10 value score.

## Walk-forward backtest

`python3 scripts/market_model.py backtest` predicts each season from 2015 on with a model trained only on earlier seasons. For each game it takes the best positive-EV side in each market type (needing at least a 3-point probability edge over break-even) and settles it at the historical closing line and price. Results go to `data/model/backtest.json`.

Closing lines are the hardest benchmark. Bets placed earlier in the week face softer lines, but nflverse does not carry historical opening lines, so that cannot be tested here.

## Gate

A market type (spread, total, moneyline) can produce picks only if its backtest shows all of:

- at least 300 bets,
- ROI minus 1.64 standard errors above zero (about 95% one-sided confidence that the edge is real), and
- a profit in at least half of the test seasons.

Even then a pick also needs a fresh Bovada quote, positive central and conservative EV, and a value score of at least 7. Anything else with positive EV is published as a **research lean** with no stake and its blockers listed.

## Current results (2015 – 2026 week 5)

| Market | Bets | Win % | ROI | Cautious ROI | Picks |
|---|---|---|---|---|---|
| Spread | 838 | 50.6% | −0.3% | −5.8% | off |
| Total | 666 | 50.5% | −0.6% | −6.8% | off |
| Moneyline | 740 | 58.9% | +0.5% | −5.3% | off |

Average margin miss: model 9.83 points, closing line 9.82. Public EPA is already priced into closing lines, so no market type passes yet.

## Changing parameters

Edit `data/model/config.json` and push. The workflow re-runs the backtest on every run, so the gate always reflects the current parameters. To test locally:

```bash
python3 -m pip install -r scripts/nfl_scoring_requirements.txt
python3 scripts/market_model.py all
```

Choosing parameters by looking at backtest results overfits them to the past. If you try many settings, judge them on seasons you did not tune on (for example, set `first_test_season` later and tune only on earlier seasons).
