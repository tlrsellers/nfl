"""Collect publishable NFL research from free public feeds.

Each feed is independent: a provider failure leaves its previous checked_at intact.
No odds, Bovada archive, probability estimate, or pick is produced here.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pipeline import ROOT, now_iso, parse_time

DATA = ROOT / "data" / "free"
SCHEDULE_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
NEWS_FEEDS = (("ESPN", "https://www.espn.com/espn/rss/nfl/news"),
              ("CBS Sports", "https://www.cbssports.com/rss/headlines/nfl/"),
              ("NBC Sports", "https://www.nbcsports.com/profootballtalk.rss"))
STATS_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv"
USER_AGENT = "FourthDownResearch/0.2 (https://github.com/tlrsellers/nfl)"

# Coordinates are approximate stadium/city locations. Unknown or neutral venues
# are skipped instead of being silently assigned the home team's usual weather.
TEAMS = {
    "ARI": ("Arizona Cardinals", 33.528, -112.263), "ATL": ("Atlanta Falcons", 33.755, -84.401),
    "BAL": ("Baltimore Ravens", 39.278, -76.623), "BUF": ("Buffalo Bills", 42.773, -78.787),
    "CAR": ("Carolina Panthers", 35.226, -80.853), "CHI": ("Chicago Bears", 41.862, -87.617),
    "CIN": ("Cincinnati Bengals", 39.095, -84.516), "CLE": ("Cleveland Browns", 41.506, -81.699),
    "DAL": ("Dallas Cowboys", 32.748, -97.093), "DEN": ("Denver Broncos", 39.743, -105.020),
    "DET": ("Detroit Lions", 42.340, -83.046), "GB": ("Green Bay Packers", 44.501, -88.062),
    "HOU": ("Houston Texans", 29.685, -95.411), "IND": ("Indianapolis Colts", 39.760, -86.164),
    "JAX": ("Jacksonville Jaguars", 30.324, -81.638), "KC": ("Kansas City Chiefs", 39.049, -94.484),
    "LA": ("Los Angeles Rams", 33.953, -118.339), "LAC": ("Los Angeles Chargers", 33.953, -118.339),
    "LV": ("Las Vegas Raiders", 36.090, -115.183), "MIA": ("Miami Dolphins", 25.958, -80.239),
    "MIN": ("Minnesota Vikings", 44.974, -93.258), "NE": ("New England Patriots", 42.091, -71.264),
    "NO": ("New Orleans Saints", 29.951, -90.081), "NYG": ("New York Giants", 40.813, -74.074),
    "NYJ": ("New York Jets", 40.813, -74.074), "PHI": ("Philadelphia Eagles", 39.901, -75.167),
    "PIT": ("Pittsburgh Steelers", 40.447, -80.016), "SEA": ("Seattle Seahawks", 47.595, -122.332),
    "SF": ("San Francisco 49ers", 37.403, -121.970), "TB": ("Tampa Bay Buccaneers", 27.976, -82.503),
    "TEN": ("Tennessee Titans", 36.166, -86.771), "WAS": ("Washington Commanders", 38.908, -76.864),
}
ALIASES = {"Washington Football Team": "WAS", "Oakland Raiders": "LV", "Los Angeles Raiders": "LV",
           "St. Louis Rams": "LA", "San Diego Chargers": "LAC", "Jacksonville Jaguars": "JAX"}
NAME_TO_CODE = {name.casefold(): code for code, (name, _, _) in TEAMS.items()}
NAME_TO_CODE.update({name.casefold(): code for name, code in ALIASES.items()})
SHORT = {code: name.split()[-1] for code, (name, _, _) in TEAMS.items()}
SHORT.update({"SF": "49ers", "NE": "Patriots", "GB": "Packers", "NYG": "Giants", "NYJ": "Jets"})


def fetch(url: str, limit: int = 12_000_000) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("HTTPS required")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=25) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Source exceeds size limit")
    return raw


def put(role: str, value: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / f"{role}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def code(name: str) -> str | None:
    return NAME_TO_CODE.get(str(name).casefold())


def game_key(away: str, home: str) -> frozenset[str]:
    return frozenset((away, home))


def match_row(game: dict, rows: list[dict]) -> dict | None:
    away, home = code(game["away"]), code(game["home"])
    kick = parse_time(game["kickoff"])
    if not away or not home or not kick:
        return None
    candidates = [r for r in rows if r.get("away_team") == away and r.get("home_team") == home
                  and r.get("gameday") and abs((datetime.fromisoformat(r["gameday"]).date() - kick.date()).days) <= 1]
    return min(candidates, key=lambda r: abs((datetime.fromisoformat(r["gameday"]).date() - kick.date()).days)) if candidates else None


def distance_miles(a: tuple[float, float], b: tuple[float, float]) -> int:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    delta = 2 * math.asin(math.sqrt(math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2))
    return round(3959 * delta)


def schedule_data(games: list[dict], rows: list[dict], now: datetime) -> None:
    season = now.year if now.month >= 3 else now.year - 1
    current = [r for r in rows if r.get("season") == str(season) and r.get("away_score") and r.get("home_score")
               and r.get("gameday") and datetime.fromisoformat(r["gameday"]).date() < now.date()]
    mapped = {g["id"]: match_row(g, rows) for g in games}
    travel, stats, history = [], [], {}
    for g in games:
        r = mapped[g["id"]]
        if not r:
            continue
        away, home = r["away_team"], r["home_team"]
        rest = [r.get("away_rest"), r.get("home_rest")]
        if all(v and v.isdigit() for v in rest):
            label = f"Rest: {g['away']} {rest[0]} days; {g['home']} {rest[1]} days"
            if r.get("location") == "Home" and away in TEAMS and home in TEAMS:
                miles = distance_miles(TEAMS[away][1:], TEAMS[home][1:])
                label += f". Away-city to venue: about {miles:,} straight-line miles (not team itinerary)."
            travel.append({"game_id": g["id"], "title": label, "publisher": "nflverse schedule",
                           "url": SCHEDULE_URL})
        parts = []
        for side in (away, home):
            played = [x for x in current if side in (x["away_team"], x["home_team"])]
            if not played:
                continue
            pf = sum(int(x["away_score"] if x["away_team"] == side else x["home_score"]) for x in played)
            pa = sum(int(x["home_score"] if x["away_team"] == side else x["away_score"]) for x in played)
            parts.append(f"{TEAMS[side][0]} {len(played)} games, {pf/len(played):.1f} points for and {pa/len(played):.1f} allowed per game")
        if parts:
            stats.append({"game_id": g["id"], "title": "Current-season scores: " + "; ".join(parts),
                          "publisher": "nflverse schedule", "url": SCHEDULE_URL})
        past = [x for x in rows if {x.get("away_team"), x.get("home_team")} == {away, home}
                and x.get("away_score") and x.get("home_score") and x.get("gameday") < r["gameday"]]
        past.sort(key=lambda x: x["gameday"], reverse=True)
        history[g["id"]] = [{"date": x["gameday"], "score": f"{TEAMS.get(x['away_team'], (x['away_team'],))[0]} {x['away_score']}–{TEAMS.get(x['home_team'], (x['home_team'],))[0]} {x['home_score']}",
                             "performance": "Score source: nflverse. Historical Bovada lines and individual performances unavailable.",
                             "source_url": SCHEDULE_URL, "bovada_lines": []} for x in past[:5]]
    stamp = now_iso()
    if travel:
        put("travel", {"checked_at": stamp, "findings": travel, "coverage_note": "Rest days and approximate distances from nflverse; no verified team itinerary."})
    if stats:
        put("stats", {"checked_at": stamp, "season": str(season), "findings": stats,
                      "coverage_note": "Team scoring from current-season final scores; player and line ratings are not yet available."})
    if history:
        put("score_history", {"checked_at": stamp, "games": history})


def upcoming_slate(rows: list[dict], now: datetime) -> list[dict]:
    season = now.year if now.month >= 3 else now.year - 1
    slate = []
    for r in rows:
        if r.get("season") != str(season) or r.get("game_type") not in ("REG", "WC", "DIV", "CON", "SB"):
            continue
        if r.get("away_team") not in TEAMS or r.get("home_team") not in TEAMS:
            continue
        try:
            local = datetime.strptime(r["gameday"] + " " + r["gametime"], "%Y-%m-%d %H:%M")
            kickoff = local.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        if now < kickoff < now + timedelta(days=8):
            slate.append({"id": "nflverse-" + r["game_id"], "away": TEAMS[r["away_team"]][0],
                          "home": TEAMS[r["home_team"]][0], "kickoff": kickoff.isoformat().replace("+00:00", "Z"),
                          "status": "scheduled"})
    return slate


def combined_slate(snapshot: list[dict], schedule: list[dict], now: datetime) -> list[dict]:
    current = [g for g in snapshot if parse_time(g.get("kickoff")) and
               now < parse_time(g["kickoff"]) < now + timedelta(days=8)]
    for game in schedule:
        kick = parse_time(game["kickoff"])
        if not any(g.get("away") == game["away"] and g.get("home") == game["home"] and
                   abs((parse_time(g["kickoff"]) - kick).total_seconds()) < 24 * 3600 for g in current):
            current.append(game)
    return current


def collect_news(games: list[dict]) -> None:
    findings, seen, checked = [], set(), []
    for publisher, url in NEWS_FEEDS:
        try:
            root = ET.fromstring(fetch(url, 1_000_000))
        except (OSError, ValueError, ET.ParseError):
            continue
        checked.append(publisher)
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title or not link.startswith("https://") or link in seen:
                continue
            seen.add(link)
            try:
                published = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(timezone.utc)
            except (TypeError, ValueError, IndexError):
                continue
            if datetime.now(timezone.utc) - published > timedelta(days=3):
                continue
            text = title.casefold()
            for g in games:
                names = [g["away"], g["home"]]
                tokens = [name.casefold() for name in names]
                tokens += [SHORT[team].casefold() for team in map(code, names) if team in SHORT]
                if any(re.search(r"(?<!\w)" + re.escape(token) + r"(?!\w)", text) for token in tokens):
                    findings.append({"game_id": g["id"], "title": title, "publisher": publisher,
                                     "url": link, "published_at": published.isoformat().replace("+00:00", "Z")})
    if not checked:
        raise ValueError("No news feed responded with RSS")
    put("news", {"checked_at": now_iso(), "findings": findings[:100],
                 "coverage_note": f"NFL RSS headlines from {', '.join(checked)} matched to teams; player-only stories may be missed."})


def collect_injuries(games: list[dict], now: datetime) -> None:
    # Sleeper asks callers to refresh the complete player directory at most daily.
    target = DATA / "injuries.json"
    if target.exists():
        try:
            previous = json.loads(target.read_text())
            if now - parse_time(previous["checked_at"]) < timedelta(hours=23):
                return
        except (OSError, ValueError, TypeError, KeyError):
            pass
    players = json.loads(fetch(PLAYERS_URL, 30_000_000))
    if not isinstance(players, dict) or len(players) < 1000:
        raise ValueError("Sleeper player directory incomplete")
    by_team = {code(g[side]) for g in games for side in ("away", "home")}
    status_values = {"Out", "Doubtful", "Questionable", "IR", "PUP", "NFI", "Suspended"}
    by_game = {g["id"]: [] for g in games}
    for player in players.values():
        if not isinstance(player, dict) or player.get("team") not in by_team:
            continue
        status = player.get("injury_status") or player.get("status")
        if status not in status_values or not player.get("full_name"):
            continue
        for g in games:
            if player["team"] in (code(g["away"]), code(g["home"])):
                by_game[g["id"]].append((0 if status in ("Out", "Doubtful", "Questionable") else 1,
                    {"game_id": g["id"], "title": f"Sleeper lists {player['full_name']} ({player.get('position') or 'NFL'}) as {status}; verify the official team report",
                     "publisher": "Sleeper", "url": PLAYERS_URL}))
    findings = [entry for group in by_game.values() for _, entry in sorted(group, key=lambda v: (v[0], v[1]["title"]))[:12]]
    put("injuries", {"checked_at": now_iso(), "findings": findings,
                     "coverage_note": "Sleeper player-status metadata, refreshed daily; not an official game injury report."})


def collect_weather(games: list[dict], rows: list[dict], now: datetime) -> None:
    findings, errors, outdoor = [], 0, 0
    for g in games:
        kickoff = parse_time(g.get("kickoff"))
        row = match_row(g, rows)
        if not row or not kickoff or not now < kickoff < now + timedelta(days=7):
            continue
        roof = (row.get("roof") or "").lower()
        if roof in ("dome", "closed"):
            findings.append({"game_id": g["id"], "title": f"{row.get('stadium') or 'Venue'}: enclosed roof listed in schedule; outdoor forecast not applied",
                             "publisher": "nflverse schedule", "url": SCHEDULE_URL})
            continue
        if row.get("location") != "Home" or row.get("home_team") not in TEAMS or roof not in ("outdoors", "open"):
            continue
        outdoor += 1
        lat, lon = TEAMS[row["home_team"]][1:]
        try:
            point = json.loads(fetch(f"https://api.weather.gov/points/{lat},{lon}", 300_000))
            hourly_url = point["properties"]["forecastHourly"]
            if not hourly_url.startswith("https://api.weather.gov/"):
                raise ValueError("Unexpected forecast URL")
            forecast = json.loads(fetch(hourly_url, 1_000_000))
            periods = forecast["properties"]["periods"]
            period = next((p for p in periods if parse_time(p["startTime"]) <= kickoff < parse_time(p["endTime"])), None)
            if not period:
                continue
            precip = period.get("probabilityOfPrecipitation") or {}
            chance = precip.get("value")
            extra = f", precipitation {chance}%" if chance is not None else ""
            findings.append({"game_id": g["id"], "title": f"Kickoff forecast near {row.get('stadium') or g['home']}: {period['temperature']}°{period['temperatureUnit']}, wind {period.get('windSpeed', 'unknown')} {period.get('windDirection', '')}{extra}",
                             "publisher": "National Weather Service", "url": hourly_url})
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            errors += 1
    if findings or (outdoor == 0 and any(match_row(g, rows) for g in games)):
        put("weather", {"checked_at": now_iso(), "findings": findings,
                        "coverage_note": f"NWS hourly forecasts for {len(findings)} matched venues; {errors} forecast requests failed. Neutral and unknown roofs excluded."})


def run() -> None:
    now = datetime.now(timezone.utc)
    snapshot_path = ROOT / "data" / "dashboard.json"
    snapshot = json.loads(snapshot_path.read_text()) if snapshot_path.exists() else {}
    games = [g for g in snapshot.get("games", []) if isinstance(g, dict) and g.get("id") and g.get("kickoff")]
    # A manual paid odds fetch supplies a newer event list for the same run.
    odds_path = ROOT / "data" / "the_odds_api_feed.json"
    if odds_path.exists():
        games = json.loads(odds_path.read_text()).get("games", games)
    rows = []
    try:
        rows = list(csv.DictReader(io.StringIO(fetch(SCHEDULE_URL, 5_000_000).decode("utf-8-sig"))))
        if not rows or "away_team" not in rows[0]:
            raise ValueError("Schedule format changed")
        schedule = upcoming_slate(rows, now)
        games = combined_slate(games, schedule, now)
        put("schedule", {"checked_at": now_iso(), "games": schedule})
        schedule_data(games, rows, now)
        collect_weather(games, rows, now)
        print("Free sources: schedule, scores, travel, weather checked")
    except Exception as exc:
        print(f"Free sources: schedule/weather unavailable ({type(exc).__name__})")
    if not games:
        print("Free sources: no published game slate yet")
        return
    for label, callback in (("news", lambda: collect_news(games)), ("injuries", lambda: collect_injuries(games, now))):
        try:
            callback()
            print(f"Free sources: {label} checked")
        except Exception as exc:
            print(f"Free sources: {label} unavailable ({type(exc).__name__})")


if __name__ == "__main__":
    run()
