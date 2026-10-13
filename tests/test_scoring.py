import copy
import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import nfl_scoring as scoring
import pipeline


def league():
    teams = []
    for i in range(32):
        metrics = {}
        for unit, spec in scoring.METRICS.items():
            metrics[unit] = {}
            for metric in spec:
                v = i / 40 if metric.endswith("rate") else float(i)
                metrics[unit][metric] = {"season_to_date": v, "last_three": v}
        teams.append({"team": f"TEST_{i:02d}", "season": 2026,
                      "games_played": 6, "metrics": metrics})
    return teams


class TeamScoringTests(unittest.TestCase):
    def test_rank_direction_ties_and_shrinkage(self):
        result = scoring.score_league(league(), 2026)
        best_offense = result["TEST_31"]["units"]["pass_offense"]
        best_defense = result["TEST_00"]["units"]["pass_defense"]
        self.assertAlmostEqual(best_offense, 8.2)
        self.assertAlmostEqual(best_defense, 8.2)
        self.assertEqual(result["TEST_00"]["status"], "complete")
        self.assertAlmostEqual(scoring.rank_score(2, [1, 2, 2, 3]), 5.5)
        self.assertAlmostEqual(scoring.rank_score(1, [1] * 32), 5.5)

    def test_recent_blend(self):
        t = league()[0]
        t["metrics"]["pass_offense"]["epa"] = {"season_to_date": .2, "last_three": .6}
        self.assertAlmostEqual(scoring.blended_metric(t, "pass_offense", "epa"), .3)

    def test_missing_stats_are_not_presented_as_average(self):
        teams = league()
        teams[0]["metrics"] = {}
        r = scoring.score_league(teams, 2026)["TEST_00"]
        self.assertIsNone(r["team_strength"])
        self.assertTrue(all(v is None for v in r["units"].values()))
        self.assertEqual(r["status"], "unavailable")
        self.assertEqual(r["metric_coverage"], 0)

    def test_partial_data_stays_flagged(self):
        teams = league()
        del teams[1]["metrics"]["pass_offense"]["epa"]
        r = scoring.score_league(teams, 2026)["TEST_01"]
        self.assertEqual(r["status"], "incomplete")
        self.assertAlmostEqual(r["metric_coverage"], 1 - .35 * .6)
        self.assertIn("pass_offense.epa", r["missing_metrics"])

    def test_wrong_season_duplicate_and_percentage_units_rejected(self):
        teams = league()
        teams[0]["season"] = 2025
        with self.assertRaises(ValueError):
            scoring.score_league(teams, 2026)
        teams = league()
        teams[0]["team"] = teams[1]["team"]
        with self.assertRaises(ValueError):
            scoring.score_league(teams, 2026)
        teams = league()
        teams[0]["metrics"]["pass_offense"]["success_rate"]["season_to_date"] = 50
        with self.assertRaises(ValueError):
            scoring.score_league(teams, 2026)

    def test_matchup_direction_and_context(self):
        teams = scoring.score_league(league(), 2026)
        home, away = copy.deepcopy(teams["TEST_01"]), copy.deepcopy(teams["TEST_02"])
        home["units"] = {u: 8.5 for u in scoring.UNITS}
        away["units"] = {u: 4.5 for u in scoring.UNITS}
        report = scoring.score_matchup(home, away, {"neutral_site": False})
        self.assertAlmostEqual(report["home"]["matchup_base"], 7.5)
        self.assertAlmostEqual(report["home"]["weekly_matchup"], 7.7)
        self.assertAlmostEqual(report["away"]["weekly_matchup"], 3.5)
        report = scoring.score_matchup(home, home, {"neutral_site": True})
        self.assertAlmostEqual(report["home"]["weekly_matchup"], 5.5)

    def test_adjustment_evidence_and_single_application(self):
        teams = scoring.score_league(league(), 2026)
        home = copy.deepcopy(teams["TEST_01"])
        home["units"] = {u: 6 for u in scoring.UNITS}
        changes = [{"unit": "pass_offense", "delta": -1, "reason": "Test injury",
                    "source_url": "https://example.com/test"}]
        context = {"unit_adjustments": {"home": changes}}
        r = scoring.score_matchup(home, home, context)
        self.assertEqual(r["home"]["expected_lineup_units"]["pass_offense"], 5)
        self.assertEqual(home["units"]["pass_offense"], 6)
        self.assertEqual(r["home"]["team_strength"], home["team_strength"])
        with self.assertRaises(ValueError):
            scoring.score_matchup(home, home, {"travel_adjustments": {"home": -.2}})

    def test_feed_adapter_names_and_time_checks(self):
        now = datetime(2026, 10, 13, tzinfo=timezone.utc)
        stats = {"season": 2026, "as_of": now.isoformat(), "teams": league()}
        games = [{"id": "g1", "home": "TEST_01", "away": "TEST_02",
                  "kickoff": (now + timedelta(days=1)).isoformat()}]
        r = scoring.dashboard_team_scores(stats, games, now)
        self.assertIn("g1", r["games"])
        stats["as_of"] = (now + timedelta(hours=1)).isoformat()
        with self.assertRaises(ValueError):
            scoring.dashboard_team_scores(stats, games, now)
        self.assertEqual(scoring.dashboard_team_scores({}, games, now)["teams"], {})


class BetScoringTests(unittest.TestCase):
    def test_example_and_every_band_boundary(self):
        r = scoring.score_bet({"win": .57, "push": 0, "loss": .43}, -110, 2)
        self.assertAlmostEqual(r["expected_roi"], .0881818181818182)
        self.assertAlmostEqual(r["conservative_expected_roi"], .05)
        self.assertEqual(r["raw_value_score"], 7)
        self.assertEqual(r["bet_score"], 5)
        cases = [(-.1, 1), (0, 1), (.005, 2), (.01, 3), (.02, 4), (.03, 5),
                 (.04, 6), (.05, 7), (.06, 8), (.08, 9), (.10, 10)]
        for ev, expected in cases:
            self.assertEqual(scoring.bet_value_score(ev), expected)

    def test_pushes_spread_signs_totals_and_invalid_inputs(self):
        p = scoring.probability(3, 12, 3)
        self.assertGreater(p["push"], 0)
        self.assertAlmostEqual(sum(p.values()), 1)
        home = scoring.probability(3, 12, 3.5)
        away = scoring.probability(-3, 12, -3.5)
        self.assertAlmostEqual(home["win"] + away["win"], 1)
        under = scoring.probability(44, 12, 44, "under", True)
        over = scoring.probability(44, 12, 44, "over", True)
        self.assertAlmostEqual(under["win"] + over["win"] + over["push"], 1)
        r = scoring.score_bet({"win": .5, "push": .1, "loss": .4}, 100, 0)
        self.assertAlmostEqual(r["expected_roi"], .1)
        self.assertAlmostEqual(r["win_probability_excluding_pushes"], 5 / 9)
        with self.assertRaises(ValueError):
            scoring.score_bet({"win": .7, "push": 0, "loss": .4}, -110)
        with self.assertRaises(ValueError):
            scoring.decimal_odds(0)

    def test_repository_pick_grade_uses_conservative_return(self):
        now = datetime.now(timezone.utc)
        market = {"id": "m1", "game_id": "g1", "status": "live", "category": "Spread",
                  "selection": "Home -3.5", "line": -3.5, "odds": -110, "observed_at": now.isoformat()}
        estimate = {"market_id": "m1", "model_version": "test", "samples": 120,
                    "p_win": .65, "p_loss": .35, "p_push": 0,
                    "conservative_p_win": .55, "conservative_p_loss": .45, "blockers": []}
        model = {"validated": True, "model_version": "test", "estimates": [estimate]}
        picks = pipeline.make_picks(model, [market], now)
        self.assertEqual(picks[0]["value_rating"], 7)
        estimate["line"] = -4.5
        self.assertEqual(pipeline.make_picks(model, [market], now), [])
        estimate.pop("line")
        estimate["conservative_p_loss"] = .44
        self.assertEqual(pipeline.make_picks(model, [market], now), [])

    @unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("sklearn"), "Optional training dependencies")
    def test_training_is_forward_only(self):
        rows = []
        for week in range(1, 7):
            for n in range(16):
                rows.append({"game_id": f"TEST_{week}_{n}", "season": 2026, "week": week,
                             "as_of": f"2026-09-{week:02d}T12:00:00Z",
                             "kickoff": f"2026-09-{week:02d}T17:00:00Z",
                             "neutral_site": False, "rest_days_difference": 0,
                             "home": {u: 5.5 + (n % 3) for u in scoring.UNITS},
                             "away": {u: 5.5 for u in scoring.UNITS},
                             "home_score": 20 + n % 10, "away_score": 17 + n % 5})
        a = scoring.train(rows)
        self.assertEqual(a["validation_games"], 64)
        for r in rows:
            if r["week"] == 6:
                r["home_score"] = 60
        b = scoring.train(rows)
        self.assertEqual([r for r in a["walk_forward"] if r["week"] < 6],
                         [r for r in b["walk_forward"] if r["week"] < 6])
        future = dict(rows[-1], week=7, as_of="2026-10-18T15:00:00Z", kickoff="2026-10-18T17:00:00Z")
        self.assertIsInstance(scoring.predict(a, future)["margin"], float)
        bet = {"market": "spread", "side": "home", "line": -3.5, "american_odds": -110,
               "quoted_at": "2026-10-18T14:00:00Z"}
        with self.assertRaises(ValueError):
            scoring.rate_market(a, future, bet)


if __name__ == "__main__":
    unittest.main()
