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

DEFAULT_STAKES = {7: 6.25, 8: 12.50, 9: 18.75, 10: 25.0}

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "dashboard.json"
STATE = ROOT / "data" / "state.json"
SOURCE_NAMES = {
    "odds": "Bovada odds", "injuries": "Injuries", "news": "News",
    "weather": "Weather", "travel": "Travel", "stats": "Statistics",
    "historical": "Historical Bovada",
}
AGENT_NAMES = ["Matchup", "Player props", "Verification"]
FRESHNESS_MINUTES = 12
SOURCE_MAX_AGE = {"odds": 12, "injuries": 1560, "news": 90, "weather": 120,
                  "travel": 1440, "stats": 2880, "historical": 10080}


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


def make_picks(estimates: dict[str, Any] | None, markets: list[dict[str, Any]], now: datetime,
               config: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn the internal model's market evaluations into picks and leans.

    A pick needs: the market type enabled by the walk-forward backtest gate,
    a live (fresh) quote, positive central and conservative EV, and a value
    score at or above config["min_value_score"]. Leans are the best positive-EV
    model views that fail one of those checks; they are research only and
    never carry a stake.
    """
    config = config or {}
    if not estimates:
        return [], []
    min_score = int(config.get("min_value_score", 7))
    stakes = {int(k): float(v) for k, v in config.get("stake_tiers", DEFAULT_STAKES).items()}
    enabled = estimates.get("enabled", {})
    evals = estimates.get("markets", {})
    picks, leans = [], []
    for m in markets:
        e = evals.get(m["id"])
        if not e or e["ev"] <= 0:
            continue
        item = {"id": m["id"], "game_id": m["game_id"], "category": m["category"],
                "selection": m["selection"], "line": m["line"], "odds": m["odds"],
                "observed_at": m["observed_at"], "win_probability": e["p_win"],
                "push_probability": e["p_push"], "break_even_probability": e["break_even"],
                "ev": e["ev"], "conservative_ev": e["conservative_ev"],
                "win_rating": max(1, min(10, int(e["p_win"] * 10 + 0.999999))),
                "value_rating": e["value_score"], "model_version": estimates.get("model_version")}
        blockers = []
        if not enabled.get(e["market"]):
            blockers.append(f"{e['market']} model has not passed the backtest gate")
        if m["status"] != "live":
            blockers.append("quote is stale")
        if e["conservative_ev"] <= 0:
            blockers.append("not positive under conservative assumptions")
        if e["value_score"] < min_score:
            blockers.append(f"value score below {min_score}")
        if not blockers:
            picks.append({**item, "status": "recommended",
                          "suggested_stake": stakes.get(min(10, e["value_score"]), 0.0),
                          "rationale": f"Model win {e['p_win']:.1%} vs break-even {e['break_even']:.1%}; backtest-validated {e['market']} model."})
        else:
            leans.append({**item, "status": "lean", "suggested_stake": 0.0, "blockers": blockers,
                          "rationale": "Research lean only: " + "; ".join(blockers) + "."})
    picks.sort(key=lambda p: p["conservative_ev"], reverse=True)
    leans.sort(key=lambda p: p["ev"], reverse=True)
    return picks, leans[:15]


def market_eval_for_test(p: dict[str, Any], odds: int, uncertainty_pp: float = 2.0) -> dict[str, Any]:
    """EV fields in the same shape market_model.evaluate_bet produces (used by tests)."""
    d = american_decimal(odds)
    hair = min(p["p_win"], uncertainty_pp / 100)
    cons = (p["p_win"] - hair) * (d - 1) - (p["p_loss"] + hair)
    return {"ev": p["p_win"] * (d - 1) - p["p_loss"], "conservative_ev": cons, "value_score": bet_value_score(cons)}


def model_estimates(games: list[dict[str, Any]], markets: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any], str]:
    """Run the internal market model. Returns (estimates, config, detail)."""
    try:
        if __package__:
            from . import market_model
        else:
            import market_model
        cfg = market_model.load_config()
        est = market_model.live_estimates(games, markets, cfg=cfg)
        bt = json.loads(market_model.BACKTEST_FILE.read_text()) if market_model.BACKTEST_FILE.exists() else {}
        est["backtest"] = {k: {kk: v.get(kk) for kk in ("bets", "win_rate", "roi", "roi_lower_bound", "seasons_profitable", "enabled")}
                           for k, v in bt.get("markets", {}).items()}
        est["backtest_seasons"] = bt.get("test_seasons", [])
        on = [k for k, v in est["enabled"].items() if v]
        detail = (f"Internal model {est['model_version']}: {len(est['games'])} games, {len(est['markets'])} markets scored. "
                  + (f"Validated markets: {', '.join(on)}." if on else "No market type has passed the backtest gate; showing research leans only."))
        return est, cfg, detail
    except Exception as exc:  # the dashboard must still publish without the model
        return None, {}, f"Model unavailable: {type(exc).__name__}: {exc}"[:250]


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
    estimates, model_cfg, model_detail = model_estimates(games, markets)
    if estimates:
        for g in games:
            if g["id"] in estimates["games"]:
                g["model"] = estimates["games"][g["id"]]
        for m in markets:
            if m["id"] in estimates["markets"]:
                m["model"] = estimates["markets"][m["id"]]
    sources["Probability & value"] = {"status": "ok" if estimates else "error", "checked_at": now_iso(),
                                      "detail": model_detail}
    picks, leans = make_picks(estimates, markets, now, model_cfg)
    # An unapproved AI workflow is never silently invoked by this job.
    ai_status = "Disabled pending cost approval"
    if os.getenv("AI_ENABLED", "false").lower() == "true":
        ai_status = "Configuration requested; agent execution not installed"
    mode = "live" if sources["Bovada odds"]["status"] == "ok" and any(m["status"] == "live" for m in markets) else "snapshot" if markets else "unavailable"
    if mode != "live":
        picks = []  # leans remain visible as research
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
              "games": games, "markets": markets, "picks": picks, "leans": leans,
              "model": {k: estimates.get(k) for k in ("model_version", "enabled", "backtest", "backtest_seasons")} if estimates else None,
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

