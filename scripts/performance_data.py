"""Free current-season play-data adapter for the deterministic scoring engine.

Uses completed regular-season games only. Field goals use a current-season
distance-binned peer baseline; this is explicitly a provisional estimate.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from free_sources import TEAMS, fetch, match_row, put, SCHEDULE_URL
from nfl_scoring import score_league
from pipeline import ROOT, parse_time

PBP_URL = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz"
CACHE = ROOT / ".cache"
FIELDS = ("game_id", "season", "season_type", "week", "posteam", "defteam", "play_type",
          "qb_dropback", "qb_scramble", "qb_kneel", "qb_spike", "rush_attempt", "epa",
          "wp", "yards_gained", "field_goal_attempt", "field_goal_result", "kick_distance",
          "punt_attempt", "punt_blocked", "return_yards", "touchback", "kickoff_attempt")
REQUIRED = {"game_id", "season", "season_type", "posteam", "defteam", "epa", "qb_dropback",
            "qb_scramble", "qb_kneel", "qb_spike", "rush_attempt", "yards_gained",
            "field_goal_result", "kick_distance", "punt_attempt", "kickoff_attempt", "wp"}


def num(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def flag(row, key):
    return num(row.get(key)) == 1


def load_plays(raw, season):
    # Stream decompression; retain only needed columns, not full 380-column rows.
    with io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(raw)), encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not REQUIRED.issubset(reader.fieldnames or []):
            raise ValueError("Play-data schema is missing required fields")
        rows = []
        for i, row in enumerate(reader):
            if i > 200_000:
                raise ValueError("Play-data row limit exceeded")
            if row.get("season") == str(season) and row.get("season_type") == "REG":
                rows.append({k: row.get(k, "") for k in FIELDS})
    if not rows:
        raise ValueError("No current-season regular-season plays")
    return rows


def schedule_complete(rows, season, now):
    result = []
    for r in rows:
        if str(r.get("season")) != str(season) or r.get("game_type") != "REG":
            continue
        hs, aws = num(r.get("home_score")), num(r.get("away_score"))
        date = r.get("gameday", "")
        if hs is None or aws is None or hs < 0 or aws < 0 or not date:
            continue
        # The final-score feed is the completion signal. Future dated games cannot enter.
        if date > now.date().isoformat():
            continue
        if r.get("home_team") not in TEAMS or r.get("away_team") not in TEAMS:
            continue
        result.append(r)
    return sorted(result, key=lambda r: (int(r["week"]), r["gameday"], r["game_id"]))


def distance_bin(distance):
    return min(6, int(distance) // 10)


def make_team_feed(plays, schedule, season, now):
    completed = schedule_complete(schedule, season, now)
    games = {r["game_id"]: r for r in completed}
    played = {team: [r["game_id"] for r in completed if team in (r["home_team"], r["away_team"])] for team in TEAMS}
    sample = [p for p in plays if p["game_id"] in games]
    if not sample and completed:
        raise ValueError("Play data does not cover any completed games")
    # Do not infer that a completed game is covered just because another game is present.
    covered_ids = {p["game_id"] for p in sample}
    missing_games = sorted(set(games) - covered_ids)
    by_team = defaultdict(lambda: defaultdict(list))
    kicks = []
    for p in sample:
        offense, defense = p["posteam"], p["defteam"]
        if offense not in TEAMS or defense not in TEAMS:
            continue
        game = p["game_id"]
        epa, yards, wp = num(p["epa"]), num(p["yards_gained"]), num(p["wp"])
        live = wp is None or .05 <= wp <= .95
        if epa is not None and live and not flag(p, "qb_kneel") and not flag(p, "qb_spike"):
            kind = "pass" if flag(p, "qb_dropback") else "rush" if flag(p, "rush_attempt") and not flag(p, "qb_scramble") else None
            if kind:
                explosive = float(yards is not None and yards >= (20 if kind == "pass" else 10))
                observation = (game, epa, float(epa > 0), explosive)
                by_team[offense][kind + "_offense"].append(observation)
                by_team[defense][kind + "_defense"].append(observation)
        if p["field_goal_result"] in ("made", "missed", "blocked"):
            distance = num(p["kick_distance"])
            if distance is not None:
                kicks.append((offense, game, distance_bin(distance), float(p["field_goal_result"] == "made")))
        if flag(p, "punt_attempt") and not flag(p, "punt_blocked"):
            distance, returned = num(p["kick_distance"]), num(p["return_yards"])
            if distance is not None:
                net = distance - (returned or 0) - (20 if flag(p, "touchback") else 0)
                by_team[offense]["net_punting"].append((game, net))
        if epa is not None and (flag(p, "punt_attempt") or flag(p, "kickoff_attempt")):
            # nflverse EPA is relative to posteam. On kickoffs posteam is usually
            # the receiving team; on punts it is the punting team. Credit both sides.
            by_team[offense]["return_coverage_efficiency"].append((game, epa))
            by_team[defense]["return_coverage_efficiency"].append((game, -epa))
    fg_unrated = defaultdict(int)
    for team, game, bucket, made in kicks:
        peers = [x[3] for x in kicks if x[0] != team and x[2] == bucket]
        if len(peers) < 5:
            fg_unrated[team] += 1
            continue
        by_team[team]["field_goals_above_expected"].append((game, made - sum(peers) / len(peers)))
    def metric(observations, last_three, column):
        if not observations:
            return {}
        recent = [r for r in observations if r[0] in last_three]
        out = {"season_to_date": sum(r[column] for r in observations) / len(observations),
               "season_sample": len(observations)}
        if recent:
            out.update(last_three=sum(r[column] for r in recent) / len(recent), recent_sample=len(recent))
        return out
    teams = []
    for team, (name, _, _) in TEAMS.items():
        ids = [gid for gid in played[team] if gid in covered_ids]
        recent = set(ids[-3:])
        metrics = {}
        for unit in ("pass_offense", "rush_offense", "pass_defense", "rush_defense"):
            values = by_team[team][unit]
            metrics[unit] = {key: metric(values, recent, column) for key, column in
                             (("epa", 1), ("success_rate", 2), ("explosive_rate", 3))}
        metrics["special_teams"] = {key: metric(by_team[team][key], recent, 1) for key in
                                    ("field_goals_above_expected", "net_punting", "return_coverage_efficiency")}
        teams.append({"team": name, "team_code": team, "season": str(season), "games_played": len(ids),
                      "metrics": metrics, "sources": [PBP_URL.format(season=season), SCHEDULE_URL],
                      "field_goal_unrated_attempts": fg_unrated[team],
                      "missing_game_ids": [gid for gid in played[team] if gid not in covered_ids]})
    return {"season": str(season), "as_of": now.isoformat().replace("+00:00", "Z"),
            "checked_at": now.isoformat().replace("+00:00", "Z"), "teams": teams,
            "data_game_ids": sorted(covered_ids), "missing_game_ids": missing_games,
            "metric_method": {"version": "nflverse-pbp-1", "season_type": "REG",
                "garbage_time": "Exclude offense/defense plays outside preplay WP 5%-95%; retain unknown WP",
                "passing": "All qb_dropback plays, including sacks and scrambles; no spikes/kneels",
                "rushing": "Designed rush attempts excluding scrambles and kneels",
                "field_goals": "Made indicator minus other teams' current-season distance-bin make rate, >=5 peer attempts",
                "net_punting": "Nonblocked punt distance minus return yards minus 20 for touchbacks",
                "return_coverage": "Team-perspective EPA per punt/kickoff opportunity; provisional combined return/coverage metric",
                "opponent_adjusted": False},
            "coverage_note": f"Current-season nflverse play data for {len(covered_ids)} completed games; {len(missing_games)} missing. Special-teams baseline is provisional; no automatic injury or weather penalties."}


def add_context(feed, games, schedule):
    contexts = {}
    for g in games:
        r = match_row(g, schedule)
        if not r:
            continue
        context = {"neutral_site": r.get("location") != "Home"}
        home, away = num(r.get("home_rest")), num(r.get("away_rest"))
        if home is not None and away is not None:
            context["rest_days_difference"] = home - away
        contexts[g["id"]] = context
    feed["scoring_context"] = contexts
    return feed


def collect(games, schedule, now, raw=None):
    season = now.year if now.month >= 3 else now.year - 1
    raw = raw if raw is not None else fetch(PBP_URL.format(season=season), 40_000_000)
    plays = load_plays(raw, season)
    feed = add_context(make_team_feed(plays, schedule, season, now), games, schedule)
    target = ROOT / "data" / "free" / "stats.json"
    if target.exists():
        previous = json.loads(target.read_text())
        # Preserve verified schedule scoring findings, not a previous team's grades.
        feed["findings"] = previous.get("findings", [])
    put("stats", feed)
    CACHE.mkdir(exist_ok=True)
    (CACHE / "performance_plays.json").write_text(json.dumps({"season": season, "plays": plays, "schedule": schedule}))
    ratings = score_league(feed["teams"], season)
    return {"teams": len(ratings), "complete": sum(r["status"] == "complete" for r in ratings.values()),
            "covered_games": len(feed["data_game_ids"]), "missing_games": len(feed["missing_game_ids"])}


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    schedule = list(csv.DictReader(io.StringIO(fetch(SCHEDULE_URL, 5_000_000).decode("utf-8-sig"))))
    snapshot = json.loads((ROOT / "data" / "dashboard.json").read_text())
    print(json.dumps(collect(snapshot.get("games", []), schedule, now)))
