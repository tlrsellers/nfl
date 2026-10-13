import sys
import unittest
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import the_odds_api as odds


class FakeClient:
    def __init__(self, limit, event, props=True):
        self.spent = 0
        self.limit = limit
        self.remaining = 500
        self.event = event
        self.props = props
        self.calls = []

    def get(self, path, params, cost):
        if self.spent + cost > self.limit:
            raise odds.FeedError("Cap reached")
        self.spent += cost
        self.calls.append(path)
        if path.endswith("/events/"):
            return [self.event]
        if path.endswith("/odds/"):
            return [self.event]
        if path.endswith("/markets"):
            return {"bookmakers": [{"key": "bovada", "markets": [
                {"key": "player_pass_yds"}, {"key": "player_pass_yds_q1"}]}]}
        return {**self.event, "bookmakers": [{"key": "bovada", "markets": [
            {"key": "player_pass_yds", "last_update": odds.timestamp(),
             "outcomes": [{"name": "Over", "description": "Test QB", "point": 250.5, "price": -110}]}]}]}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.event = {"id": "g1", "home_team": "Home", "away_team": "Away",
                      "commence_time": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                      "bookmakers": [{"key": "bovada", "markets": [
                          {"key": "spreads", "last_update": odds.timestamp(),
                           "outcomes": [{"name": "Home", "point": -2.5, "price": -110}]}]}]}

    def test_bovada_full_game_and_line_identity(self):
        game, offers = odds.convert_event(self.event)
        self.assertEqual(game["id"], "g1")
        self.assertEqual(offers[0]["category"], "Spread")
        changed = {**self.event, "bookmakers": [{"key": "bovada", "markets": [
            {"key": "spreads", "last_update": odds.timestamp(),
             "outcomes": [{"name": "Home", "point": -3.5, "price": -115}]}]}]}
        self.assertEqual(offers[0]["id"], odds.convert_event(changed)[1][0]["id"])
        self.assertFalse(odds.whole_game_prop("player_pass_yds_q1"))

    def test_full_props_fail_before_partial_publication(self):
        client = FakeClient(4, self.event)
        with self.assertRaisesRegex(odds.FeedError, "needs approximately"):
            odds.fetch(client, True)
        self.assertNotIn("/events/g1/odds", client.calls)
        self.assertEqual(client.spent, 4)

    def test_full_props_include_available_offers(self):
        client = FakeClient(5, self.event)
        result = odds.fetch(client, True)
        self.assertEqual(client.spent, 5)
        self.assertEqual([m["category"] for m in result["markets"]], ["Spread", "Player prop"])
        self.assertIn("Test QB", result["markets"][1]["selection"])

    def test_http_error_reports_reason_without_key(self):
        error = HTTPError("https://api.the-odds-api.com/?apiKey=SECRET", 401,
                          "Unauthorized", {}, BytesIO(b'{"error_code":"INVALID_KEY"}'))
        with patch.object(odds.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(odds.FeedError) as raised:
                odds.Client("SECRET", 10).get("/events/", {}, 0)
        self.assertEqual(str(raised.exception), "Provider events HTTP 401 (INVALID_KEY)")
        self.assertNotIn("SECRET", str(raised.exception))

    def test_key_copy_whitespace_is_ignored(self):
        self.assertEqual(odds.Client("  SECRET\n", 10).key, "SECRET")


if __name__ == "__main__":
    unittest.main()
