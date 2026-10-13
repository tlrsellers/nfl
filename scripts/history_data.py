"""Compact multi-season team-game history for the market model.

Play-by-play files are ~18 MB each, so historical seasons are summarized once
into data/model/team_games.csv (one row per team per game, offense only;
a team's defense is its opponent's offense row). The current season is
re-summarized each run from the play cache that performance_data.py keeps.

Usage:
  python3 scripts/history_data.py build PBP_DIR [--first 2010] [--last 2025]
  python3 scripts/history_data.py current   # refresh current season from .cache
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "data" / "model"
TEAM_GAMES = MODEL_DIR / "team_games.csv"
CACHE = ROOT / ".cache" / "performance_plays.json"
COLUMNS = ["season", "week", "game_id", "team", "opp",
           "pass_n", "pass_epa", "pass_succ", "rush_n", "rush_epa", "rush_succ"]
# nflverse renamed franchises; keep one code per franchise so history carries over.
FRANCHISE = {"OAK": "LV", "SD": "LAC", "STL": "LA", "LAR": "LA"}


def _num(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def franchise(code: str) -> str:
    return FRANCHISE.get(code, code)


def summarize(plays) -> list[dict]:
    """Aggregate designed passes and rushes per offense per game.

    Excludes kneels/spikes and plays where the game is already decided
    (win probability outside 5–95%), the usual garbage-time filter.
    """
    agg = defaultdict(lambda: {"pass_n": 0, "pass_epa": 0.0, "pass_succ": 0,
                               "rush_n": 0, "rush_epa": 0.0, "rush_succ": 0})
    meta = {}
    for p in plays:
        if p.get("season_type") != "REG":
            continue
        off, de = p.get("posteam"), p.get("defteam")
        epa, wp = _num(p.get("epa")), _num(p.get("wp"))
        if not off or not de or epa is None:
            continue
        if wp is not None and not 0.05 <= wp <= 0.95:
            continue
        if _num(p.get("qb_kneel")) == 1 or _num(p.get("qb_spike")) == 1:
            continue
        if _num(p.get("qb_dropback")) == 1 or _num(p.get("qb_scramble")) == 1:
            kind = "pass"
        elif _num(p.get("rush_attempt")) == 1:
            kind = "rush"
        else:
            continue
        key = (p["game_id"], franchise(off))
        meta[key] = (int(float(p["season"])), int(float(p["week"])), franchise(de))
        a = agg[key]
        a[kind + "_n"] += 1
        a[kind + "_epa"] += epa
        a[kind + "_succ"] += epa > 0
    rows = []
    for (gid, team), a in agg.items():
        season, week, opp = meta[(gid, team)]
        rows.append({"season": season, "week": week, "game_id": gid, "team": team, "opp": opp,
                     **{k: round(v, 4) if isinstance(v, float) else v for k, v in a.items()}})
    return sorted(rows, key=lambda r: (r["season"], r["week"], r["game_id"], r["team"]))


def read_pbp(path: Path):
    wanted = ("game_id", "season", "season_type", "week", "posteam", "defteam", "epa", "wp",
              "qb_dropback", "qb_scramble", "qb_kneel", "qb_spike", "rush_attempt")
    with gzip.open(path, "rt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            yield {k: row.get(k, "") for k in wanted}


def load_team_games(path: Path = TEAM_GAMES) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for r in csv.DictReader(f):
            out.append({"season": int(r["season"]), "week": int(r["week"]), "game_id": r["game_id"],
                        "team": r["team"], "opp": r["opp"],
                        **{k: float(r[k]) for k in COLUMNS[5:]}})
    return out


def write_team_games(rows, path: Path = TEAM_GAMES) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in sorted(rows, key=lambda r: (r["season"], r["week"], r["game_id"], r["team"])):
            w.writerow({k: r[k] for k in COLUMNS})


def build(pbp_dir: Path, first: int, last: int) -> None:
    rows = []
    for season in range(first, last + 1):
        path = pbp_dir / f"play_by_play_{season}.csv.gz"
        if not path.exists():
            path = pbp_dir / f"pbp_{season}.csv.gz"
        part = summarize(read_pbp(path))
        print(f"{season}: {len(part)} team-games", file=sys.stderr)
        rows += part
    write_team_games(rows)


def refresh_current() -> int:
    """Replace the current season's rows using the play cache from performance_data.py."""
    raw = json.loads(CACHE.read_text())
    current = summarize(raw["plays"])
    if not current:
        return 0
    season = current[0]["season"]
    rows = [r for r in load_team_games() if r["season"] != season] + current
    write_team_games(rows)
    return len(current)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("pbp_dir", type=Path)
    b.add_argument("--first", type=int, default=2010)
    b.add_argument("--last", type=int, default=2025)
    sub.add_parser("current")
    args = ap.parse_args()
    if args.cmd == "build":
        build(args.pbp_dir, args.first, args.last)
    else:
        try:
            n = refresh_current()
            print(f"Current-season team-games refreshed: {n}")
        except (OSError, ValueError, KeyError) as exc:
            print(f"Current-season refresh skipped: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
