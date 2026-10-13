"""Build the public static feed from configured, authorized JSON sources.

This module does not scrape Bovada or call a paid model. It is safe to schedule
before the source and AI-cost approvals have been completed.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__:
    from .nfl_scoring import bet_value_score, dashboard_team_scores
else:
    from nfl_scoring import bet_value_score, dashboard_team_scores

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "dashboard.json"
STATE = ROOT / "data" / "state.json"
SOURCE_NAMES = {
    "odds": "Bovada odds", "injuries": "Injuries", "news": "News",
    "weather": "Weather", "travel": "Travel", "stats": "Statistics",
    "historical": "Historical Bovada", "model": "Probability & value",
}
AGENT_NAMES = ["Matchup", "Player props", "Verification"]
FRESHNESS_MINUTES = 12
SOURCE_MAX_AGE = {"odds": 12, "injuries": 1560, "news": 90, "weather": 120,
                  "travel": 1440, "stats": 2880, "historical": 10080, "model": 90}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except ValueError:
        return None


def age_minutes(value: Any, now: datetime) -> float:
    dt = parse_time(value)
    return (now - dt).total_seconds() / 60 if dt else float("inf")


def get_json(role: str) -> tuple[dict[str, Any] | None, str]:
    """Read a role-specific JSON source; bearer tokens never enter output."""
    prefix = role.upper()
    path = os.getenv(f"{prefix}_FEED_FILE")
    if not path and role in ("injuries", "news", "weather", "travel", "stats", "score_history", "schedule"):
        bundled = ROOT / "data" / "free" / f"{role}.json"
        if bundled.exists():
            path = str(bundled)
    url = os.getenv(f"{prefix}_FEED_URL")
    try:
        if path:
            raw = Path(path).read_bytes()
        elif url:
            if not url.startswith("https://"):
                return None, "Feed URL must use HTTPS"
            headers = {"Accept": "application/json", "User-Agent": "FourthDownResearch/0.1"}
            token = os.getenv(f"{prefix}_FEED_TOKEN")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=25) as response:
                raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                return None, "Feed exceeds 8 MB limit"
        else:
            return None, "No feed configured"
        value = json.loads(raw)
        if not isinstance(value, dict):
            return None, "Feed root must be an object"
        return value, "Source parsed"
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return None, f"Source error: {type(exc).__name__}"


def american_decimal(odds: Any) -> float | None:
    if isinstance(odds, bool):
        return None
    try:
        value = int(odds)
    except (TypeError, ValueError):
        return None
    if abs(value) < 100:
        return None
    return 1 + (value / 100 if value > 0 else 100 / abs(value))


def expected_value(p_win: float, p_loss: float, odds: int) -> float:
    decimal = american_decimal(odds)
    if decimal is None:
        raise ValueError("Invalid odds")
    return p_win * (decimal - 1) - p_loss


def value_rating(ev: float) -> int:
    return bet_value_score(ev)


def normalize_games(feed: dict[str, Any] | None) -> list[dict[str, Any]]:
    games = feed.get("games", []) if feed else []
    if not isinstance(games, list):
        return []
    normalized = []
    seen = set()
    for g in games:
        if not isinstance(g, dict) or not all(g.get(key) for key in ("id", "away", "home", "kickoff")):
            continue
        if not parse_time(g["kickoff"]) or g["id"] in seen:
            continue
        seen.add(g["id"])
        normalized.append({"id": str(g["id"]), "away": str(g["away"]),
                           "home": str(g["home"]), "kickoff": g["kickoff"],
                           "status": str(g.get("status", "scheduled")), "score": g.get("score")})
    return sorted(normalized, key=lambda g: g["kickoff"])


def normalize_markets(feed: dict[str, Any] | None, game_ids: set[str], now: datetime) -> list[dict[str, Any]]:
    values = feed.get("markets", []) if feed else []
    if not isinstance(values, list):
        return []
    markets = []
    seen = set()
    for m in values:
        if not isinstance(m, dict) or m.get("sportsbook", "").lower() != "bovada":
            continue
        if m.get("period") != "full_game" or m.get("category") not in ("Spread", "Moneyline", "Total", "Player prop"):
            continue
        if str(m.get("game_id")) not in game_ids or not m.get("selection") or not m.get("id"):
            continue
        decimal = american_decimal(m.get("odds"))
        if decimal is None or not parse_time(m.get("observed_at")):
            continue
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        status = "live" if age_minutes(m["observed_at"], now) <= FRESHNESS_MINUTES and m.get("available", True) else "stale"
        markets.append({"id": str(m["id"]), "game_id": str(m["game_id"]),
                        "category": m["category"], "selection": str(m["selection"]),
                        "line": m.get("line"), "odds": int(m["odds"]),
                        "observed_at": m["observed_at"], "status": status,
                        "source_url": str(m.get("source_url", "")), "sportsbook": "Bovada"})
    return markets


def attach_research(games: list[dict[str, Any]], feeds: dict[str, dict[str, Any] | None]) -> None:
    stats = feeds.get("stats") or {}
    historical = feeds.get("historical") or {}
    matchups = stats.get("matchups", {}) if isinstance(stats.get("matchups"), dict) else {}
    history = historical.get("games", {}) if isinstance(historical.get("games"), dict) else {}
    scores = (feeds.get("score_history") or {}).get("games", {})
    if not isinstance(scores, dict):
        scores = {}
    for g in games:
        metrics = matchups.get(g["id"], {})
        if isinstance(metrics, dict) and metrics.get("season") == (stats.get("season")):
            g["rating_side"] = str(metrics.get("rating_side", ""))[:80]
            for key in ("offense", "defense", "pass", "rush", "line", "confidence"):
                value = metrics.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and 1 <= value <= 10 and (key == "confidence" or g["rating_side"]):
                    g[key] = round(value, 1)
            g["summary"] = str(metrics.get("summary", ""))[:600]
            players = metrics.get("players", [])
            if isinstance(players, list):
                g["players"] = [{"name": str(p.get("name", ""))[:80],
                                 "position": str(p.get("position", ""))[:20],
                                 "side": str(p.get("side", ""))[:80],
                                 "performance": p.get("performance") if isinstance(p.get("performance"), (int, float)) and 1 <= p["performance"] <= 10 else None,
                                 "matchup": p.get("matchup") if isinstance(p.get("matchup"), (int, float)) and 1 <= p["matchup"] <= 10 else None,
                                 "sample": str(p.get("sample", ""))[:80],
                                 "rationale": str(p.get("rationale", ""))[:300]}
                                for p in players if isinstance(p, dict) and p.get("name")][:100]
        h = history.get(g["id"], [])
        if not h:
            h = scores.get(g["id"], [])
        verified_history = []
        if isinstance(h, list):
            for x in h[:5]:
                if not isinstance(x, dict):
                    continue
                lines = x.get("bovada_lines", [])
                verified_history.append({"date": str(x.get("date", "")), "score": str(x.get("score", "")),
                                         "performance": str(x.get("performance", ""))[:500],
                                         "source_url": str(x.get("source_url", "")),
                                         "bovada_lines": [{"selection": str(m.get("selection", "")),
                                                           "line": m.get("line"), "odds": m.get("odds"),
                                                           "result": str(m.get("result", "Unavailable")),
                                                           "source_url": str(m.get("source_url", ""))}
                                                          for m in lines if isinstance(m, dict) and m.get("sportsbook", "").lower() == "bovada" and m.get("selection") and m.get("source_url")][:100]
                                        })
        g["head_to_head"] = verified_history
        evidence = []
        for role in ("injuries", "news", "weather", "travel", "stats"):
            items = (feeds.get(role) or {}).get("findings", [])
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and str(item.get("game_id")) == g["id"] and item.get("title") and item.get("url"):
                    evidence.append({"title": str(item["title"])[:200], "url": str(item["url"]),
                                     "publisher": str(item.get("publisher", role))[:70],
                                     "published_at": item.get("published_at"), "role": role})
        g["evidence"] = evidence[:30]


def make_picks(model: dict[str, Any] | None, markets: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Only external, validated probability estimates can become recommendations."""
    if not model or model.get("validated") is not True or not model.get("model_version"):
        return []
    estimates = model.get("estimates", [])
    if not isinstance(estimates, list):
        return []
    by_id = {m["id"]: m for m in markets if m["status"] == "live"}
    picks = []
    for estimate in estimates:
        if not isinstance(estimate, dict):
            continue
        market = by_id.get(str(estimate.get("market_id")))
        if not market or estimate.get("model_version") != model["model_version"]:
            continue
        if estimate.get("samples", 0) < 100 or estimate.get("blockers"):
            continue
        try:
            p_w, p_l, p_push = (float(estimate[k]) for k in ("p_win", "p_loss", "p_push"))
            c_w, c_l = float(estimate["conservative_p_win"]), float(estimate["conservative_p_loss"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(0 <= p <= 1 for p in (p_w, p_l, p_push, c_w, c_l)) or abs(p_w+p_l+p_push-1)>1e-5 or abs(c_w+c_l+p_push-1)>1e-5:
            continue
        if c_w > p_w or c_l < p_l:
            continue
        if "line" in estimate and estimate["line"] != market["line"]:
            continue
        if "odds" in estimate and estimate["odds"] != market["odds"]:
            continue
        ev = expected_value(p_w, p_l, market["odds"])
        conservative = expected_value(c_w, c_l, market["odds"])
        grade = value_rating(conservative)
        if ev <= 0 or conservative <= 0 or grade < 7:
            continue
        stake = {7: 6.25, 8: 12.50, 9: 18.75, 10: 25.0}[grade]
        picks.append({"id": market["id"], "game_id": market["game_id"],
                      "category": market["category"], "selection": market["selection"],
                      "line": market["line"], "odds": market["odds"],
                      "observed_at": market["observed_at"], "win_probability": p_w,
                      "break_even_probability": 1/american_decimal(market["odds"]),
                      "ev": round(ev, 5), "conservative_ev": round(conservative, 5),
                      "win_rating": max(1, min(10, int(p_w*10+0.999999))),
                      "value_rating": grade, "suggested_stake": stake,
                      "rationale": str(estimate.get("rationale", ""))[:600],
                      "model_version": model["model_version"], "status": "recommended"})
    return sorted(picks, key=lambda p: p["conservative_ev"], reverse=True)


def compare_market_state(markets: list[dict[str, Any]]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    try:
        previous = json.loads(STATE.read_text())
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    current = {m["id"]: {"selection": m["selection"], "line": m["line"],
                         "odds": m["odds"]} for m in markets}
    changes = []
    for m in markets:
        old = previous.get(m["id"])
        if not isinstance(old, dict):
            continue
        if old.get("line") != m["line"] or old.get("odds") != m["odds"]:
            changes.append({"id": "line-" + m["id"], "title": "Bovada line or price changed",
                            "detail": f"{m['selection']}: {old.get('line')} {old.get('odds')} → {m['line']} {m['odds']}",
                            "at": m["observed_at"]})
    return changes[:40], current


def build() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    feeds = {}
    sources = {}
    for role, name in SOURCE_NAMES.items():
        feed, detail = get_json(role)
        if role == "odds" and not feed and OUTPUT.exists():
            try:
                old = json.loads(OUTPUT.read_text())
                old_source = old.get("sources", {}).get("Bovada odds", {})
                if old.get("games") and isinstance(old.get("markets"), list):
                    feed = {"checked_at": old_source.get("checked_at"), "games": old["games"],
                            "markets": [dict(m, period="full_game", available=False)
                                        for m in old["markets"] if isinstance(m, dict)]}
                    detail = "Retained previous Bovada snapshot; no new odds fetched"
            except (OSError, ValueError, TypeError):
                pass
        feeds[role] = feed
        observed = feed.get("checked_at") if feed else None
        source_status = "ok" if feed else "error" if detail.startswith("Source error") else "unavailable"
        if feed and age_minutes(observed, now) > SOURCE_MAX_AGE[role]:
            source_status = "stale"
            detail = "Source check is overdue"
        elif feed and feed.get("coverage_note"):
            detail = str(feed["coverage_note"])[:250]
            if role == "odds" and feed.get("player_props_requested") is False:
                detail += "; player props were not requested in this test"
        sources[name] = {"status": source_status,
                         "checked_at": observed if parse_time(observed) else None,
                         "detail": detail}
    for name in AGENT_NAMES:
        sources[name] = {"status": "unavailable", "checked_at": None,
                         "detail": "Requires connected evidence and approved AI budget"}
    odds = feeds["odds"]
    games = [g for g in normalize_games(odds) if parse_time(g["kickoff"]) > now]
    schedule, _ = get_json("schedule")
    for scheduled in normalize_games(schedule):
        kick = parse_time(scheduled["kickoff"])
        if not any(g["away"] == scheduled["away"] and g["home"] == scheduled["home"] and
                   abs((parse_time(g["kickoff"]) - kick).total_seconds()) < 24 * 3600 for g in games):
            games.append(scheduled)
    games.sort(key=lambda g: g["kickoff"])
    markets = normalize_markets(odds, {g["id"] for g in games}, now)
    feeds["score_history"], _ = get_json("score_history")
    attach_research(games, feeds)
    try:
        scoring = dashboard_team_scores(feeds["stats"], games, now)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        scoring = {"teams": {}, "games": {}, "status": "error",
                   "detail": f"Scoring input rejected: {exc}"}
    for g in games:
        if g["id"] in scoring["games"]:
            report = scoring["games"][g["id"]]
            g["scoring"] = report
            home, away = report["home"], report["away"]
            g["rating_side"] = g["home"]
            g["pass"] = home["category_matchups"]["pass_offense"]
            g["rush"] = home["category_matchups"]["rush_offense"]
            base = scoring["teams"][g["home"]]["units"]
            if base["pass_offense"] is not None and base["rush_offense"] is not None:
                g["offense"] = (.35 * base["pass_offense"] + .15 * base["rush_offense"]) / .5
            if base["pass_defense"] is not None and base["rush_defense"] is not None:
                g["defense"] = (.35 * base["pass_defense"] + .10 * base["rush_defense"]) / .45
            g["confidence"] = min(home["data_confidence"], away["data_confidence"])
            g["summary"] = "Current-season play-data comparison. Passing/rushing advantages are from the home team's perspective. Special-teams baseline is provisional; verify injuries and expected lineups."
    sources["Scoring engine"] = {"status": scoring["status"],
                                 "checked_at": now_iso(), "detail": scoring["detail"]}
    if scoring["games"]:
        sources["Matchup"] = {"status": "ok", "checked_at": now_iso(),
                              "detail": "Deterministic team matchup ratings active. Paid AI narrative agents remain disabled."}
    picks = make_picks(feeds["model"], markets, now)
    # An unapproved AI workflow is never silently invoked by this job.
    ai_status = "Disabled pending cost approval"
    if os.getenv("AI_ENABLED", "false").lower() == "true":
        ai_status = "Configuration requested; agent execution not installed"
    mode = "live" if sources["Bovada odds"]["status"] == "ok" and any(m["status"] == "live" for m in markets) else "snapshot" if markets else "unavailable"
    if mode != "live":
        picks = []
    alerts, latest_state = compare_market_state(markets)
    for role in ("injuries", "news", "weather", "travel"):
        items = (feeds[role] or {}).get("alerts", [])
        if isinstance(items, list):
            alerts += [{k: item.get(k) for k in ("id", "title", "detail", "at")}
                       for item in items if isinstance(item, dict) and item.get("title")]
    result = {"schema_version": 1, "mode": mode, "generated_at": now_iso(),
              "season": str((feeds["stats"] or {}).get("season", now.year)),
              "coverage": {"games": len(games), "markets": len(markets),
                           "analyzed": sum(1 for g in games if g.get("confidence"))},
              "games": games, "markets": markets, "picks": picks,
              "team_ratings": scoring["teams"], "scoring_version": "nfl-scoring-1.0",
              "alerts": alerts[:40], "sources": sources, "ai_status": ai_status,
              "changed_market_count": len([a for a in alerts if str(a.get("id", "")).startswith("line-")])}
    if markets and latest_state:
        serialized = json.dumps(latest_state, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if not STATE.exists() or STATE.read_text() != serialized:
            STATE.write_text(serialized)
    return result


def main() -> None:
    result = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    digest = hashlib.sha256(json.dumps(result["markets"], sort_keys=True).encode()).hexdigest()[:12]
    print(f"Built {result['mode']} feed: {len(result['games'])} games, {len(result['markets'])} markets; market fingerprint {digest}")


if __name__ == "__main__":
    main()

