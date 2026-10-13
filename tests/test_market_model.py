import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import market_model as mm


def team_game(season, week, gid, team, opp, epa=0.0):
    return {"season": season, "week": week, "game_id": gid, "team": team, "opp": opp,
            "pass_n": 30, "pass_epa": epa * 30, "pass_succ": 14, "rush_n": 25, "rush_epa": 0.0, "rush_succ": 10}


def sched(season, week, gid, home, away):
    return {"game_id": gid, "season": season, "week": week, "gameday": f"{season}-10-{week:02d}",
            "home": home, "away": away, "home_score": None, "away_score": None,
            "spread_line": 0.0, "total_line": 44.0, "home_rest": 7, "away_rest": 7, "dome": False}


class MarketModelTests(unittest.TestCase):
    def test_key_numbers_are_preserved(self):
        # Results cluster on 3: a -2.5 favorite should cover far more often than -3.5.
        pairs = [(3.0, 3)] * 30 + [(3.0, 7)] * 20 + [(3.0, 0)] * 15 + [(3.0, -3)] * 20 + [(3.0, 10)] * 15
        dist = mm._distribution(pairs, 3.0, 1.0)
        minus_25 = mm.cover_probability(dist, 2.5)
        minus_35 = mm.cover_probability(dist, 3.5)
        self.assertGreater(minus_25["win"] - minus_35["win"], 0.25)
        at_3 = mm.cover_probability(dist, 3.0)
        self.assertAlmostEqual(at_3["push"], 0.30, places=6)

    def test_ratings_use_only_earlier_weeks(self):
        cfg = dict(mm.DEFAULT_CONFIG)
        tg = [team_game(2025, 1, "p", "A", "B"), team_game(2025, 1, "p", "B", "A"),
              # A has a huge week-2 game; it must not affect A's week-2 rating.
              team_game(2026, 1, "x1", "A", "B", 0.0), team_game(2026, 1, "x1", "B", "A", 0.0),
              team_game(2026, 2, "x2", "A", "B", 5.0), team_game(2026, 2, "x2", "B", "A", 0.0)]
        schedule = [sched(2026, 1, "x1", "A", "B"), sched(2026, 2, "x2", "A", "B"), sched(2026, 3, "x3", "A", "B")]
        r = mm.pregame_ratings(tg, schedule, cfg)
        self.assertAlmostEqual(r["x2"]["home"]["off_pass_epa"], r["x1"]["home"]["off_pass_epa"], delta=0.05)
        self.assertGreater(r["x3"]["home"]["off_pass_epa"], r["x2"]["home"]["off_pass_epa"] + 0.3)

    def test_gate_needs_statistical_evidence(self):
        bets = [{"result": "win", "profit": 0.909}] * 55 + [{"result": "loss", "profit": -1.0}] * 45
        s = mm._summary(bets)
        self.assertGreater(s["roi"], 0)  # slightly profitable...
        import numpy as np
        profits = np.array([b["profit"] for b in bets])
        lower = s["roi"] - 1.64 * profits.std(ddof=1) / len(profits) ** 0.5
        self.assertLess(lower, 0)  # ...but not enough to trust

    def test_side_probabilities_are_consistent(self):
        pairs = [(0.0, m) for m in range(-20, 21)] * 3
        table = {"spread": pairs + [(-l, -r) for l, r in pairs], "total": [(44.0, t) for t in range(30, 60)]}
        model = {"margin": {"mean": [0] * len(mm.MARGIN_FEATURES), "scale": [1] * len(mm.MARGIN_FEATURES),
                            "intercept": 0.0, "coef": [1] + [0] * (len(mm.MARGIN_FEATURES) - 1)},
                 "total": {"mean": [0] * len(mm.TOTAL_FEATURES), "scale": [1] * len(mm.TOTAL_FEATURES),
                           "intercept": 0.0, "coef": [1] + [0] * (len(mm.TOTAL_FEATURES) - 1)}}
        pred = mm.Predictor(model, table, dict(mm.DEFAULT_CONFIG))
        home = pred.spread(0.0, -2.5, "home")
        away = pred.spread(0.0, -2.5, "away")  # always the home handicap
        self.assertAlmostEqual(home["win"], away["loss"], places=6)
        over = pred.total(44.0, 44.5, "over"); under = pred.total(44.0, 44.5, "under")
        self.assertAlmostEqual(over["win"] + under["win"], 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
