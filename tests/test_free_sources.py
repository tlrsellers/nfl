import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import free_sources as fs


class FreeSourceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 13, 5, 0, tzinfo=timezone.utc)
        self.game = {"id": "provider-1", "away": "Seattle Seahawks", "home": "Denver Broncos",
                     "kickoff": "2026-10-16T00:15:00Z"}
        self.rows = [
            {"game_id": "2026_06_SEA_DEN", "season": "2026", "game_type": "REG", "gameday": "2026-10-15",
             "gametime": "20:15", "away_team": "SEA", "home_team": "DEN", "away_score": "", "home_score": "",
             "away_rest": "10", "home_rest": "7", "location": "Home", "roof": "outdoors", "stadium": "Empower Field"},
            {"game_id": "2025_01_DEN_SEA", "season": "2025", "game_type": "REG", "gameday": "2025-09-07",
             "away_team": "DEN", "home_team": "SEA", "away_score": "24", "home_score": "17"},
            {"game_id": "2026_05_SEA_LA", "season": "2026", "game_type": "REG", "gameday": "2026-10-04",
             "away_team": "SEA", "home_team": "LA", "away_score": "31", "home_score": "17"},
        ]

    def test_schedule_keeps_provider_game_id_and_historical_scores_without_odds(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(fs, "DATA", Path(tmp)):
            slate = fs.combined_slate([self.game], fs.upcoming_slate(self.rows, self.now), self.now)
            self.assertEqual(len(slate), 1)
            self.assertEqual(slate[0]["id"], "provider-1")
            fs.schedule_data(slate, self.rows, self.now)
            travel = json.loads((Path(tmp) / "travel.json").read_text())
            stats = json.loads((Path(tmp) / "stats.json").read_text())
            history = json.loads((Path(tmp) / "score_history.json").read_text())
            self.assertIn("10 days", travel["findings"][0]["title"])
            self.assertIn("31.0 points", stats["findings"][0]["title"])
            self.assertEqual(history["games"]["provider-1"][0]["bovada_lines"], [])
            self.assertIn("24", history["games"]["provider-1"][0]["score"])

    def test_weather_uses_kickoff_period_and_skips_unknown_venue(self):
        point = {"properties": {"forecastHourly": "https://api.weather.gov/gridpoints/BOU/1,1/forecast/hourly"}}
        forecast = {"properties": {"periods": [{"startTime": "2026-10-16T00:00:00Z",
                    "endTime": "2026-10-16T01:00:00Z", "temperature": 48, "temperatureUnit": "F",
                    "windSpeed": "12 mph", "windDirection": "NW", "probabilityOfPrecipitation": {"value": 20}}]}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(fs, "DATA", Path(tmp)):
            def fake_fetch(url, limit=0):
                return json.dumps(point if "/points/" in url else forecast).encode()
            with patch.object(fs, "fetch", side_effect=fake_fetch):
                fs.collect_weather([self.game], self.rows, self.now)
            weather = json.loads((Path(tmp) / "weather.json").read_text())
            self.assertIn("48°F", weather["findings"][0]["title"])
            self.assertIn("20%", weather["findings"][0]["title"])

    def test_injury_feed_is_daily_and_has_no_official_report_claim(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(fs, "DATA", Path(tmp)):
            players = {"1": {"team": "SEA", "full_name": "Example Player", "position": "WR", "injury_status": "Questionable"}}
            players.update({str(i): {"team": "FA"} for i in range(2, 1002)})
            with patch.object(fs, "fetch", return_value=json.dumps(players).encode()) as fetch:
                fs.collect_injuries([self.game], self.now)
                fs.collect_injuries([self.game], self.now + timedelta(hours=1))
                self.assertEqual(fetch.call_count, 1)
            feed = json.loads((Path(tmp) / "injuries.json").read_text())
            self.assertIn("verify the official team report", feed["findings"][0]["title"])


if __name__ == "__main__":
    unittest.main()
