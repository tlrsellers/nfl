import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pipeline


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.observed = self.now.isoformat().replace("+00:00", "Z")
        self.game = {"id": "g1", "away": "Away", "home": "Home",
                     "kickoff": (self.now + timedelta(days=1)).isoformat().replace("+00:00", "Z")}
        self.market = {"id": "m1", "game_id": "g1", "sportsbook": "Bovada",
                       "period": "full_game", "category": "Spread", "selection": "Home -2.5",
                       "line": -2.5, "odds": -110, "observed_at": self.observed}

    def test_price_and_push_math(self):
        self.assertAlmostEqual(pipeline.american_decimal(-110), 1.9090909)
        self.assertAlmostEqual(pipeline.american_decimal(135), 2.35)
        self.assertIsNone(pipeline.american_decimal(0))
        self.assertAlmostEqual(pipeline.expected_value(.55, .40, -110), .10)

    def test_reject_wrong_book_period_and_stale(self):
        bad = [dict(self.market, id="other", sportsbook="OtherBook"),
               dict(self.market, id="half", period="first_half")]
        old = dict(self.market, id="old", observed_at=(self.now-timedelta(minutes=13)).isoformat())
        markets = pipeline.normalize_markets({"markets": bad+[old,self.market]}, {"g1"}, self.now)
        self.assertEqual(len(markets), 2)
        self.assertEqual({m["id"]: m["status"] for m in markets}, {"old": "stale", "m1": "live"})

    def test_picks_require_gate_freshness_and_conservative_value(self):
        markets = pipeline.normalize_markets({"markets": [self.market]}, {"g1"}, self.now)
        self.assertEqual(pipeline.make_picks(None, markets, self.now), ([], []))
        strong = {"market": "spread", "p_win": .60, "p_push": 0, "p_loss": .40, "break_even": .5238}
        strong.update(pipeline.market_eval_for_test(strong, -110))
        est = {"model_version": "t", "enabled": {"spread": True}, "markets": {"m1": strong}}
        picks, leans = pipeline.make_picks(est, markets, self.now, {"min_value_score": 7})
        self.assertEqual(len(picks), 1)
        self.assertLessEqual(picks[0]["suggested_stake"], 25)
        # Gate off: same numbers become a lean with no stake.
        est["enabled"]["spread"] = False
        picks, leans = pipeline.make_picks(est, markets, self.now, {"min_value_score": 7})
        self.assertEqual(picks, [])
        self.assertEqual(leans[0]["suggested_stake"], 0.0)
        self.assertIn("backtest gate", leans[0]["blockers"][0])
        # Stale quote blocks a pick even when the gate is on.
        est["enabled"]["spread"] = True
        stale = [dict(markets[0], status="stale")]
        picks, leans = pipeline.make_picks(est, stale, self.now, {"min_value_score": 7})
        self.assertEqual(picks, [])
        # Thin edge fails the conservative check.
        thin = {"market": "spread", "p_win": .53, "p_push": 0, "p_loss": .47, "break_even": .5238}
        thin.update(pipeline.market_eval_for_test(thin, -110))
        est["markets"]["m1"] = thin
        picks, _ = pipeline.make_picks(est, markets, self.now, {"min_value_score": 7})
        self.assertEqual(picks, [])

    def test_only_five_historical_meetings(self):
        games = pipeline.normalize_games({"games": [self.game]})
        pipeline.attach_research(games, {"historical": {"games": {"g1": [{"date": str(i)} for i in range(8)]}}})
        self.assertEqual(len(games[0]["head_to_head"]), 5)

    def test_line_move_is_detected_from_previous_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = pipeline.STATE
            pipeline.STATE = Path(tmp) / "state.json"
            try:
                pipeline.STATE.write_text('{"m1":{"selection":"Home -2.5","line":-2.5,"odds":-110}}')
                markets = pipeline.normalize_markets({"markets": [dict(self.market, odds=-120)]}, {"g1"}, self.now)
                alerts, next_state = pipeline.compare_market_state(markets)
                self.assertEqual(len(alerts), 1)
                self.assertEqual(next_state["m1"]["odds"], -120)
            finally:
                pipeline.STATE = original


if __name__ == "__main__":
    unittest.main()
