import csv
import gzip
import io
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import performance_data as perf
import paper_tracking as paper


NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


def schedule(gid="g1", week=1, date="2026-09-13", home="DEN", away="SEA"):
    return {"game_id": gid, "season": "2026", "game_type": "REG", "week": str(week),
            "gameday": date, "gametime": "13:00", "home_team": home, "away_team": away,
            "home_score": "21", "away_score": "17", "location": "Home", "home_rest": "7", "away_rest": "7"}


def play(**changes):
    row = {k: "" for k in perf.FIELDS}
    row.update(game_id="g1", season="2026", season_type="REG", week="1", posteam="SEA",
               defteam="DEN", wp="0.5", epa="0.2", yards_gained="10")
    row.update(changes)
    return row


def team(feed, name):
    return next(r for r in feed["teams"] if r["team"] == name)


class PerformanceDataTests(unittest.TestCase):
    def test_current_season_and_schema(self):
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=perf.FIELDS)
        writer.writeheader()
        writer.writerows([play(), play(season="2025")])
        rows = perf.load_plays(gzip.compress(stream.getvalue().encode()), 2026)
        self.assertEqual(len(rows), 1)
        with self.assertRaises(ValueError):
            perf.load_plays(gzip.compress(b"season,season_type\n2026,REG\n"), 2026)

    def test_scrambles_sacks_kneels_spikes_and_garbage_time(self):
        rows = [play(qb_dropback="1", epa="1", yards_gained="25"),
                play(qb_dropback="1", qb_scramble="1", rush_attempt="1", epa="0", yards_gained="5"),
                play(qb_dropback="1", epa="-1", yards_gained="-7"),
                play(rush_attempt="1", epa=".4", yards_gained="12"),
                play(rush_attempt="1", qb_kneel="1", epa="-8"),
                play(qb_dropback="1", qb_spike="1", epa="-8"),
                play(qb_dropback="1", wp=".99", epa="20")]
        feed = perf.make_team_feed(rows, [schedule()], 2026, NOW)
        sea, den = team(feed, "Seattle Seahawks"), team(feed, "Denver Broncos")
        self.assertAlmostEqual(sea["metrics"]["pass_offense"]["epa"]["season_to_date"], 0)
        self.assertEqual(sea["metrics"]["pass_offense"]["epa"]["season_sample"], 3)
        self.assertAlmostEqual(sea["metrics"]["rush_offense"]["epa"]["season_to_date"], .4)
        self.assertEqual(den["metrics"]["pass_defense"]["epa"]["season_sample"], 3)

    def test_punt_net_yards_and_kickoff_ownership(self):
        rows = [play(punt_attempt="1", kick_distance="48", return_yards="5", epa="-.3"),
                play(posteam="DEN", defteam="SEA", kickoff_attempt="1", epa=".2")]
        feed = perf.make_team_feed(rows, [schedule()], 2026, NOW)
        sea, den = team(feed, "Seattle Seahawks"), team(feed, "Denver Broncos")
        self.assertEqual(sea["metrics"]["special_teams"]["net_punting"]["season_to_date"], 43)
        self.assertAlmostEqual(sea["metrics"]["special_teams"]["return_coverage_efficiency"]["season_to_date"], -.25)
        self.assertAlmostEqual(den["metrics"]["special_teams"]["return_coverage_efficiency"]["season_to_date"], .25)

    def test_field_goal_baseline_excludes_own_team(self):
        rows = [play(field_goal_result="made", kick_distance="42")]
        rows += [play(posteam="DEN", defteam="SEA", field_goal_result="missed", kick_distance="44") for _ in range(5)]
        feed = perf.make_team_feed(rows, [schedule()], 2026, NOW)
        sea = team(feed, "Seattle Seahawks")
        self.assertEqual(sea["metrics"]["special_teams"]["field_goals_above_expected"]["season_to_date"], 1)
        den = team(feed, "Denver Broncos")
        self.assertEqual(den["metrics"]["special_teams"]["field_goals_above_expected"], {})
        self.assertEqual(den["field_goal_unrated_attempts"], 5)

    def test_zero_score_and_missing_games(self):
        r = schedule()
        r["home_score"] = "0"
        self.assertEqual(len(perf.schedule_complete([r], 2026, NOW)), 1)
        future = schedule("future", date="2026-12-01")
        self.assertEqual(perf.schedule_complete([future], 2026, NOW), [])
        feed = perf.make_team_feed([play(qb_dropback="1")], [r, schedule("g2", week=2)], 2026, NOW)
        self.assertEqual(feed["missing_game_ids"], ["g2"])
        self.assertEqual(team(feed, "Seattle Seahawks")["games_played"], 1)

    def test_recent_window_is_last_three_games_not_last_three_weeks(self):
        games = [schedule(f"g{w}", week=w, date=f"2026-09-{w:02d}") for w in (1, 2, 4, 5)]
        rows = [play(game_id=f"g{w}", qb_dropback="1", epa=str(w)) for w in (1, 2, 4, 5)]
        feed = perf.make_team_feed(rows, games, 2026, NOW)
        metric = team(feed, "Seattle Seahawks")["metrics"]["pass_offense"]["epa"]
        self.assertAlmostEqual(metric["last_three"], 11 / 3)
        self.assertEqual(metric["recent_sample"], 3)

    def test_historical_features_exclude_the_target_week(self):
        games = [schedule(f"g{w}", week=w, date=f"2026-09-{w:02d}") for w in (1, 2, 3)]
        rows = [play(game_id=f"g{w}", qb_dropback="1", epa=str(w)) for w in (1, 2, 3)]
        first = paper.training_rows(rows, games, 2026, NOW)
        for r in rows:
            if r["game_id"] == "g3":
                r["epa"] = "-100"
        changed = paper.training_rows(rows, games, 2026, NOW)
        self.assertEqual(first, changed)
        self.assertEqual(first[0]["home"]["pass_offense"], 5.5)
        self.assertEqual(first[0]["feature_origin"], "reconstructed_from_earlier_weeks")

    def test_only_prospective_forecasts_settle(self):
        record = {"nflverse_game_id": "g1", "forecast_as_of": "2026-09-12T12:00:00Z",
                  "kickoff": "2026-09-13T17:00:00Z", "projection": {"margin": 3, "total": 40},
                  "outcome_probabilities": {"home": .6, "tie": .02, "away": .38}}
        archive = {"season": 2026, "predictions": {"g1": record}}
        result = paper.evaluate_archive(archive, [schedule()], 2026, NOW)
        self.assertEqual(result["settled_predictions"], 1)
        self.assertAlmostEqual(result["mean_three_outcome_brier"], .4 ** 2 + .02 ** 2 + .38 ** 2)
        record.pop("settlement")
        record["forecast_as_of"] = "2026-09-13T18:00:00Z"
        self.assertEqual(paper.evaluate_archive(archive, [schedule()], 2026, NOW)["settled_predictions"], 0)


if __name__ == "__main__":
    unittest.main()
