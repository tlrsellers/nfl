"""Manual, quota-capped Bovada NFL adapter for The Odds API.

The provider key is used server-side only. This script does not place wagers,
produce probability estimates, or schedule itself.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
BOOK = "bovada"
FEATURED = ("h2h", "spreads", "totals")
SOURCE = "https://the-odds-api.com/sports-odds-data/nfl-odds.html"
OUTPUT = Path(__file__).resolve().parents[1] / "data" / "the_odds_api_feed.json"


class FeedError(Exception):
    pass


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def whole_game_prop(key: str) -> bool:
    return key.startswith("player_") and not key.endswith(tuple(
        f"_{period}{n}" for period in ("q", "h", "p") for n in range(1, 5)
    ))


def market_id(event_id: str, key: str, outcome: dict) -> str:
    # The featured line is intentionally omitted so a line move retains its ID.
    # Alternate ladders need the point to distinguish simultaneous selections.
    identity = [event_id, key, outcome.get("description", ""), outcome.get("name", "")]
    if key.endswith("_alternate"):
        identity.append(outcome.get("point"))
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:20]


def convert_event(event: dict) -> tuple[dict | None, list[dict]]:
    if not isinstance(event, dict) or not all(event.get(k) for k in ("id", "home_team", "away_team", "commence_time")):
        return None, []
    game = {"id": str(event["id"]), "home": str(event["home_team"]),
            "away": str(event["away_team"]), "kickoff": event["commence_time"],
            "status": "scheduled"}
    offers = []
    for book in event.get("bookmakers", []):
        if not isinstance(book, dict) or book.get("key") != BOOK:
            continue
        for market in book.get("markets", []):
            key = market.get("key")
            if key not in FEATURED and (not isinstance(key, str) or not whole_game_prop(key)):
                continue
            category = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}.get(key, "Player prop")
            observed = market.get("last_update") or book.get("last_update")
            if not observed:
                continue
            for outcome in market.get("outcomes", []):
                if not isinstance(outcome, dict):
                    continue
                try:
                    price = int(outcome["price"])
                except (KeyError, TypeError, ValueError):
                    continue
                if abs(price) < 100 or not outcome.get("name"):
                    continue
                player = outcome.get("description", "")
                label = f"{player} {outcome['name']}".strip() if player else str(outcome["name"])
                if category == "Player prop":
                    label = f"{label} · {key.removeprefix('player_').replace('_', ' ')}"
                offers.append({"id": market_id(game["id"], key, outcome), "game_id": game["id"],
                               "sportsbook": "Bovada", "period": "full_game", "category": category,
                               "selection": label, "line": outcome.get("point"), "odds": price,
                               "observed_at": observed, "available": True, "source_url": SOURCE})
    return game, offers


class Client:
    def __init__(self, key: str, limit: int):
        if not key:
            raise FeedError("THE_ODDS_API_KEY secret is missing")
        if not 3 <= limit <= 1000:
            raise FeedError("Credit limit must be from 3 to 1000")
        self.key = key
        self.limit = limit
        self.spent = 0
        self.remaining = None

    def get(self, path: str, params: dict[str, str], planned_cost: int) -> object:
        if self.spent + planned_cost > self.limit:
            raise FeedError(f"Credit cap reached: estimated {self.spent + planned_cost} > {self.limit}")
        params = {**params, "apiKey": self.key}
        url = BASE + path + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "FourthDownResearch/0.2"})
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                body = response.read(10_000_001)
                if len(body) > 10_000_000:
                    raise FeedError("Provider response exceeds 10 MB")
                actual = response.headers.get("x-requests-last")
                remaining = response.headers.get("x-requests-remaining")
                self.spent += int(actual) if actual is not None else planned_cost
                self.remaining = int(remaining) if remaining is not None else self.remaining
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            # An HTTPError contains the request URL, including the private key.
            # Report only its status and a constrained provider error code.
            endpoint = "events" if path == "/events/" else "featured odds" if path == "/odds/" else "event markets" if path.endswith("/markets") else "event odds"
            try:
                payload = json.loads(exc.read(2048))
                raw_code = payload.get("error_code", "") if isinstance(payload, dict) else ""
                code = raw_code if isinstance(raw_code, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", raw_code) else ""
            except (OSError, ValueError):
                code = ""
            raise FeedError(f"Provider {endpoint} HTTP {exc.code}" + (f" ({code})" if code else "")) from None
        except (urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
            # Never print an HTTP error object: its URL can contain the API key.
            raise FeedError(f"Provider request failed ({type(exc).__name__})") from None


def fetch(client: Client, include_props: bool) -> dict:
    events = client.get("/events/", {}, 0)
    featured = client.get("/odds/", {"bookmakers": BOOK, "markets": ",".join(FEATURED),
                                      "oddsFormat": "american"}, 3)
    if not isinstance(events, list) or not isinstance(featured, list):
        raise FeedError("Unexpected provider response")
    now = datetime.now(timezone.utc)
    upcoming = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            kickoff = datetime.fromisoformat(str(event["commence_time"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if kickoff.tzinfo and kickoff > now and event.get("id"):
            upcoming[str(event["id"])] = event
    data = {event_id: event for event_id, event in upcoming.items()}
    for event in featured:
        if isinstance(event, dict) and str(event.get("id")) in data:
            data[str(event["id"])] = event
    if include_props:
        if client.spent + len(upcoming) > client.limit:
            raise FeedError("Credit cap too low to discover player prop markets for every upcoming game")
        prop_keys = {}
        for event_id in upcoming:
            listing = client.get(f"/events/{urllib.parse.quote(event_id, safe='')}/markets",
                                 {"bookmakers": BOOK}, 1)
            if not isinstance(listing, dict):
                raise FeedError("Unexpected event market response")
            book = next((b for b in listing.get("bookmakers", []) if isinstance(b, dict) and b.get("key") == BOOK), {})
            prop_keys[event_id] = sorted({m["key"] for m in book.get("markets", []) if isinstance(m, dict)
                                          and isinstance(m.get("key"), str) and whole_game_prop(m["key"])})
        total = sum(len(keys) for keys in prop_keys.values())
        if client.spent + total > client.limit:
            raise FeedError(f"Full prop board needs approximately {client.spent + total} credits this run; cap is {client.limit}. Nothing published")
        for event_id, keys in prop_keys.items():
            if not keys:
                continue
            # Chunk requests to avoid provider URL length limits.
            for start in range(0, len(keys), 20):
                batch = keys[start:start + 20]
                event = client.get(f"/events/{urllib.parse.quote(event_id, safe='')}/odds",
                                   {"bookmakers": BOOK, "markets": ",".join(batch),
                                    "oddsFormat": "american"}, len(batch))
                if not isinstance(event, dict):
                    raise FeedError("Unexpected prop odds response")
                data.setdefault(event_id, upcoming[event_id])
                _, offers = convert_event(event)
                data[event_id].setdefault("_prop_offers", []).extend(offers)
    games, markets = [], []
    for event in data.values():
        game, offers = convert_event(event)
        if game:
            games.append(game)
            markets.extend(offers)
            markets.extend(event.get("_prop_offers", []))
    result = {"checked_at": timestamp(), "games": games, "markets": markets,
              "coverage_note": "Bovada markets returned by The Odds API; provider coverage may omit Bovada website offers",
              "credits_used_this_run": client.spent, "credits_remaining": client.remaining,
              "player_props_requested": include_props}
    return result


def main() -> None:
    try:
        limit = int(os.getenv("ODDS_API_CREDIT_LIMIT", "10"))
        client = Client(os.getenv("THE_ODDS_API_KEY", ""), limit)
        result = fetch(client, os.getenv("ODDS_API_INCLUDE_PROPS", "false").lower() == "true")
        OUTPUT.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        print(f"Bovada feed: {len(result['games'])} games, {len(result['markets'])} offers; {client.spent} credits used, {client.remaining} remaining")
    except (FeedError, ValueError) as exc:
        print(f"Bovada feed unavailable: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
