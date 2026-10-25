import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from results_tracking import build_results
import paper_tracking

NOW = datetime(2026, 10, 25, 1, tzinfo=timezone.utc)


def forecast(**changes):
    value = {"origin": "prospective", "home": "Home", "away": "Away",
             "forecast_as_of": "2026-10-12T12:00:00Z", "kickoff": "2026-10-18T17:00:00Z",
             "model_version": "frozen-v1", "projection": {"margin": 3, "total": 41,
                 "projected_home_points": 22, "projected_away_points": 19},
             "outcome_probabilities": {"home": .6, "away": .38, "tie": .02}}
    value.update(changes)
    return value


def archive(*records):
    return {"season": 2026, "predictions": {f"2026_{6+i:02d}_A_H": f for i, f in enumerate(records)}}


class ResultsTrackingTests(unittest.TestCase):
    def test_final_scores_accuracy_and_errors_use_frozen_projection(self):
        a = archive(forecast(settlement={"home_score": 24, "away_score": 17}),
                    forecast(settlement={"home_score": 10, "away_score": 14}))
        original = copy.deepcopy(a)
        output = build_results(a, NOW)
        self.assertEqual(a, original)
        summary = output["summary"]
        self.assertEqual((summary["wins"], summary["losses"]), (1, 1))
        self.assertEqual(summary["winner_accuracy"], .5)
        self.assertEqual(summary["margin_mae"], 5.5)  # abs(7 - 3), abs(-4 - 3)
        self.assertEqual(summary["total_mae"], 8.5)  # abs(41 - 41), abs(24 - 41)
        self.assertEqual(output["records"][0]["model_version"], "frozen-v1")

    def test_ties_even_predictions_and_zero_scores(self):
        even = forecast(outcome_probabilities={"home": .49, "away": .49, "tie": .02},
                        settlement={"home_score": 21, "away_score": 14})
        output = build_results(archive(
            forecast(settlement={"home_score": 0, "away_score": 0}),
            forecast(settlement={"home_score": 0, "away_score": 7}), even), NOW)
        self.assertEqual([r["winner_result"] for r in output["records"]], ["tie", "loss", "no_pick"])
        self.assertEqual(output["summary"]["winner_accuracy"], 0)
        self.assertEqual(output["summary"]["ties"], 1)
        self.assertEqual(output["summary"]["final"], 3)

    def test_upcoming_and_no_final_score_are_never_graded(self):
        output = build_results(archive(forecast(), forecast(kickoff="2026-10-26T17:00:00Z",
            settlement={"home_score": 24, "away_score": 17}),
            forecast(settlement={"home_score": None, "away_score": 17})), NOW)
        self.assertEqual(output["summary"]["final"], 0)
        self.assertIsNone(output["summary"]["winner_accuracy"])
        self.assertEqual({r["status"] for r in output["records"]}, {"upcoming", "awaiting_final"})
        self.assertTrue(all(r["winner_result"] == "pending" for r in output["records"]))

    def test_excludes_reconstructed_late_future_and_invalid_forecasts(self):
        bad_projection = forecast()["projection"] | {"total": float("nan")}
        output = build_results(archive(forecast(origin="backtest"),
            forecast(forecast_as_of="2026-10-18T17:00:00Z"),
            forecast(forecast_as_of="2026-10-26T12:00:00Z", kickoff="2026-10-27T17:00:00Z"),
            forecast(kickoff="invalid"), forecast(projection=bad_projection), forecast()), NOW)
        self.assertEqual(len(output["records"]), 1)
        self.assertEqual(output["records"][0]["week"], 11)

    def test_results_survive_unavailable_forecast_training(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data/free").mkdir(parents=True)
            (root / "data/paper").mkdir()
            dashboard = root / "data/dashboard.json"
            dashboard.write_text(json.dumps({"games": []}))
            (root / "data/free/stats.json").write_text(json.dumps({"season": 2026, "as_of": "2000-01-01T00:00:00Z"}))
            a = archive(forecast(settlement={"home_score": 24, "away_score": 17}))
            (root / "data/paper/2026.json").write_text(json.dumps(a))
            with patch.object(paper_tracking, "ROOT", root), patch.object(paper_tracking, "OUTPUT", dashboard), \
                 patch.object(paper_tracking, "PAPER", root / "data/paper"), patch.object(paper_tracking, "datetime") as clock:
                clock.now.return_value = NOW
                paper_tracking.run()
            result = json.loads(dashboard.read_text())
            self.assertEqual(result["paper_tracking"]["status"], "unavailable")
            self.assertEqual(result["prediction_results"]["summary"]["wins"], 1)


if __name__ == "__main__":
    unittest.main()
