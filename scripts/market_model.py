"""Market-anchored NFL model: predicts where results land relative to the line.

Self-contained: uses free nflverse data only (schedules with closing lines, and
the team-game summaries from history_data.py). No AI or paid model calls.

How it works
  1. Pregame team ratings (EPA/play and success rate, passing and rushing,
     offense and defense) blend a regressed prior-season baseline with the
     current season's games played so far. Only games before kickoff count.
  2. Margin model: home margin ~ closing spread + rating differences.
     Total model:  total points ~ closing total + rating sums + dome flag.
     Because the line is an input, the model only has to learn where the
     market is wrong; edge = model fair line − market line.
  3. Probabilities use the empirical distribution of real results in games
     whose closing line was near the model's fair line, so key numbers
     (3, 7, 10 …) keep their true weight instead of a smooth bell curve.
  4. Walk-forward backtest: each season is predicted by a model trained only
     on earlier seasons, then bet against the historical closing lines and
     prices. Market types are enabled for picks only if they pass the gate
     in data/model/config.json.

Usage:
  python3 scripts/market_model.py backtest [--schedule FILE]
  python3 scripts/market_model.py fit       [--schedule FILE]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

if __package__:
    from .history_data import ROOT, MODEL_DIR, franchise, load_team_games
else:
    from history_data import ROOT, MODEL_DIR, franchise, load_team_games

SCHEDULE_URL = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
SCHEDULE_CACHE = ROOT / ".cache" / "games.csv"
CONFIG = MODEL_DIR / "config.json"
MODEL_FILE = MODEL_DIR / "model.json"
BACKTEST_FILE = MODEL_DIR / "backtest.json"
MODEL_VERSION = "market-model-1.0"

STATS = ("pass_epa", "pass_sr", "rush_epa", "rush_sr")
MARGIN_FEATURES = ["spread_line"] + [f"d_off_{s}" for s in STATS] + [f"d_def_{s}" for s in STATS] + ["rest_diff"]
TOTAL_FEATURES = ["total_line"] + [f"s_off_{s}" for s in STATS] + [f"s_def_{s}" for s in STATS] + ["dome"]

DEFAULT_CONFIG = {
    "_about": "Built-in parameters. Edit these, then rerun backtest to see the effect.",
    "first_season": 2010,
    "first_test_season": 2015,
    "prior_regression": 0.5,
    "prior_pass_plays": 200,
    "prior_rush_plays": 120,
    "ridge_alpha": 50.0,
    "distribution_bandwidth": 1.0,
    "min_probability_edge": 0.03,
    "gate": {"min_bets": 300, "min_roi_lower_bound": 0.0, "z": 1.64, "min_seasons_profitable": 0.5},
    "stake_tiers": {"7": 6.25, "8": 12.5, "9": 18.75, "10": 25.0},
    "min_value_score": 7,
    "uncertainty_pp": 2.0,
}


# ---------------------------------------------------------------- data

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text()))
    return cfg


def _f(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def load_schedule(path: Path | None = None) -> list[dict]:
    if path is None:
        if not SCHEDULE_CACHE.exists() or (datetime.now().timestamp() - SCHEDULE_CACHE.stat().st_mtime) > 3000:
            req = urllib.request.Request(SCHEDULE_URL, headers={"User-Agent": "FourthDownResearch/0.3"})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read(10_000_000)
            SCHEDULE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            SCHEDULE_CACHE.write_bytes(raw)
        path = SCHEDULE_CACHE
    rows = []
    with Path(path).open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("game_type") != "REG":
                continue
            rows.append({
                "game_id": r["game_id"], "season": int(r["season"]), "week": int(r["week"]),
                "gameday": r["gameday"], "gametime": r.get("gametime") or "13:00",
                "home": franchise(r["home_team"]), "away": franchise(r["away_team"]),
                "home_score": _f(r.get("home_score")), "away_score": _f(r.get("away_score")),
                "spread_line": _f(r.get("spread_line")), "total_line": _f(r.get("total_line")),
                "home_spread_odds": _f(r.get("home_spread_odds")), "away_spread_odds": _f(r.get("away_spread_odds")),
                "over_odds": _f(r.get("over_odds")), "under_odds": _f(r.get("under_odds")),
                "home_moneyline": _f(r.get("home_moneyline")), "away_moneyline": _f(r.get("away_moneyline")),
                "home_rest": _f(r.get("home_rest")), "away_rest": _f(r.get("away_rest")),
                "neutral": r.get("location") == "Neutral",
                "dome": r.get("roof") in ("dome", "closed"),
            })
    return sorted(rows, key=lambda g: (g["season"], g["week"], g["gameday"], g["game_id"]))


# ---------------------------------------------------------------- ratings

def _per_play(rows):
    pn = sum(r["pass_n"] for r in rows); rn = sum(r["rush_n"] for r in rows)
    return {"pass_n": pn, "rush_n": rn,
            "pass_epa": sum(r["pass_epa"] for r in rows), "pass_sr": sum(r["pass_succ"] for r in rows),
            "rush_epa": sum(r["rush_epa"] for r in rows), "rush_sr": sum(r["rush_succ"] for r in rows)}


def pregame_ratings(team_games: list[dict], schedule: list[dict], cfg: dict) -> dict:
    """Return {game_id: {'home': rating, 'away': rating}} using only earlier games.

    Each rating holds offense and defense per-play values for STATS, expressed
    relative to the prior season's league average.
    """
    by_season = defaultdict(list)
    for r in team_games:
        by_season[r["season"]].append(r)
    # Offense rows for team T; defense = opponent offense rows where opp == T.
    season_totals = {}
    for s, rows in by_season.items():
        off, de = defaultdict(list), defaultdict(list)
        for r in rows:
            off[r["team"]].append(r); de[r["opp"]].append(r)
        lg = _per_play(rows)
        season_totals[s] = {"off": {t: _per_play(v) for t, v in off.items()},
                            "def": {t: _per_play(v) for t, v in de.items()},
                            "league": {k: lg[k] / lg[k.split("_")[0] + "_n"] for k in STATS}}
    reg, kp, kr = cfg["prior_regression"], cfg["prior_pass_plays"], cfg["prior_rush_plays"]

    def prior(season, team, side):
        prev = season_totals.get(season - 1)
        if not prev or team not in prev[side]:
            return {k: 0.0 for k in STATS}
        t, lg = prev[side][team], prev["league"]
        out = {}
        for k in STATS:
            n = t[k.split("_")[0] + "_n"]
            out[k] = (1 - reg) * (t[k] / n - lg[k]) if n else 0.0
        return out

    # Index current-season rows by (season, week) for strict "before this week" use.
    rows_by_week = defaultdict(list)
    for r in team_games:
        rows_by_week[(r["season"], r["week"])].append(r)
    result = {}
    cum = {}  # (season, team, side) -> accumulated sums
    current = None
    for g in schedule:
        key = (g["season"], g["week"])
        if current != key:
            # Add every earlier week of this season not yet accumulated.
            if current is None or current[0] != g["season"]:
                cum = {}
                done_weeks = set()
            for w in range(1, g["week"]):
                if w in done_weeks:
                    continue
                for r in rows_by_week.get((g["season"], w), []):
                    for team, side in ((r["team"], "off"), (r["opp"], "def")):
                        c = cum.setdefault((team, side), {"pass_n": 0, "rush_n": 0, **{k: 0.0 for k in STATS}})
                        c["pass_n"] += r["pass_n"]; c["rush_n"] += r["rush_n"]
                        c["pass_epa"] += r["pass_epa"]; c["pass_sr"] += r["pass_succ"]
                        c["rush_epa"] += r["rush_epa"]; c["rush_sr"] += r["rush_succ"]
                done_weeks.add(w)
            current = key
        prev = season_totals.get(g["season"] - 1)
        if not prev:
            continue
        lg = prev["league"]
        ratings = {}
        for side_name, team in (("home", g["home"]), ("away", g["away"])):
            rating = {}
            for side in ("off", "def"):
                pr = prior(g["season"], team, side)
                c = cum.get((team, side), {"pass_n": 0, "rush_n": 0, **{k: 0.0 for k in STATS}})
                for k in STATS:
                    n = c[k.split("_")[0] + "_n"]
                    kk = kp if k.startswith("pass") else kr
                    # Shrink current-season per-play toward the prior with kk pseudo-plays.
                    cur_sum = c[k] - n * lg[k]
                    rating[f"{side}_{k}"] = (kk * pr[k] + cur_sum) / (kk + n)
            ratings[side_name] = rating
        result[g["game_id"]] = ratings
    return result


def game_features(g: dict, r: dict) -> dict:
    h, a = r["home"], r["away"]
    f = {"spread_line": g["spread_line"], "total_line": g["total_line"],
         "rest_diff": max(-7.0, min(7.0, (g.get("home_rest") or 7) - (g.get("away_rest") or 7))),
         "dome": 1.0 if g.get("dome") else 0.0}
    for s in STATS:
        f[f"d_off_{s}"] = h[f"off_{s}"] - a[f"off_{s}"]
        f[f"d_def_{s}"] = h[f"def_{s}"] - a[f"def_{s}"]
        f[f"s_off_{s}"] = h[f"off_{s}"] + a[f"off_{s}"]
        f[f"s_def_{s}"] = h[f"def_{s}"] + a[f"def_{s}"]
    return f


# ---------------------------------------------------------------- fitting

def ridge_fit(X: np.ndarray, y: np.ndarray, alpha: float, anchor: int | None = None) -> dict:
    """Standardized ridge. The anchor column (the market line) is not penalized."""
    mean, scale = X.mean(0), X.std(0)
    scale[scale == 0] = 1.0
    Z = (X - mean) / scale
    Z1 = np.hstack([np.ones((len(Z), 1)), Z])
    pen = np.full(Z1.shape[1], alpha); pen[0] = 0.0
    if anchor is not None:
        pen[anchor + 1] = 0.0
    beta = np.linalg.solve(Z1.T @ Z1 + np.diag(pen), Z1.T @ y)
    return {"mean": mean.tolist(), "scale": scale.tolist(), "intercept": float(beta[0]), "coef": beta[1:].tolist()}


def ridge_predict(m: dict, x) -> float:
    return m["intercept"] + sum((v - mu) / sd * c for v, mu, sd, c in zip(x, m["mean"], m["scale"], m["coef"]))


def training_rows(schedule, ratings):
    rows = []
    for g in schedule:
        if g["game_id"] not in ratings or g["home_score"] is None or g["spread_line"] is None or g["total_line"] is None:
            continue
        f = game_features(g, ratings[g["game_id"]])
        rows.append({**g, "features": f, "margin": g["home_score"] - g["away_score"],
                     "total": g["home_score"] + g["away_score"]})
    return rows


def fit_models(rows, cfg):
    Xm = np.array([[r["features"][k] for k in MARGIN_FEATURES] for r in rows])
    Xt = np.array([[r["features"][k] for k in TOTAL_FEATURES] for r in rows])
    ym = np.array([r["margin"] for r in rows]); yt = np.array([r["total"] for r in rows])
    return {"margin": ridge_fit(Xm, ym, cfg["ridge_alpha"], anchor=0),
            "total": ridge_fit(Xt, yt, cfg["ridge_alpha"], anchor=0)}


# ---------------------------------------------------------------- probabilities

def outcome_table(rows):
    """Historical (line, result) pairs for empirical distributions.

    Spread pairs are mirrored (away perspective) so the table is symmetric.
    """
    spread = [(r["spread_line"], r["margin"]) for r in rows] + [(-r["spread_line"], -r["margin"]) for r in rows]
    total = [(r["total_line"], r["total"]) for r in rows]
    return {"spread": spread, "total": total}


def _distribution(pairs, center, bandwidth):
    weights = defaultdict(float)
    for line, result in pairs:
        d = (line - center) / bandwidth
        if abs(d) < 3:
            weights[result] += math.exp(-0.5 * d * d)
    total = sum(weights.values())
    return {k: v / total for k, v in weights.items()} if total else {}


def cover_probability(dist, threshold, direction="over"):
    """P(result > threshold), P(result == threshold), P(result < threshold)."""
    win = sum(p for v, p in dist.items() if v > threshold)
    push = sum(p for v, p in dist.items() if v == threshold)
    loss = 1.0 - win - push
    if direction == "under":
        win, loss = loss, win
    return {"win": win, "push": push, "loss": max(0.0, loss)}


class Predictor:
    def __init__(self, model: dict, table: dict, cfg: dict):
        self.model, self.table, self.cfg = model, table, cfg
        self._cache = {}

    def fair(self, features):
        m = ridge_predict(self.model["margin"], [features[k] for k in MARGIN_FEATURES])
        t = ridge_predict(self.model["total"], [features[k] for k in TOTAL_FEATURES])
        return m, t

    def dist(self, kind, center):
        key = (kind, round(center * 4) / 4)
        if key not in self._cache:
            self._cache[key] = _distribution(self.table[kind], key[1], self.cfg["distribution_bandwidth"])
        return self._cache[key]

    def spread(self, fair_margin, home_line, side):
        """home_line is the home handicap as quoted (e.g. -3.5 = home favored by 3.5)."""
        if side == "home":
            return cover_probability(self.dist("spread", fair_margin), -home_line)
        return cover_probability(self.dist("spread", -fair_margin), home_line)

    def total(self, fair_total, line, side):
        return cover_probability(self.dist("total", fair_total), line, side)

    def moneyline(self, fair_margin, side):
        p = cover_probability(self.dist("spread", fair_margin if side == "home" else -fair_margin), 0)
        return p  # ties count as push (most books refund or the market is 2-way)


# ---------------------------------------------------------------- betting math

def decimal_odds(american):
    a = float(american)
    return 1 + (a / 100 if a > 0 else 100 / abs(a))


def ev(p, american):
    d = decimal_odds(american)
    return p["win"] * (d - 1) - p["loss"]


def evaluate_bet(p, american, cfg):
    """Central and conservative EV and the repository's 1–10 value score."""
    if __package__:
        from .nfl_scoring import bet_value_score
    else:
        from nfl_scoring import bet_value_score
    d = decimal_odds(american)
    central = ev(p, american)
    hair = min(p["win"], cfg["uncertainty_pp"] / 100)
    cons = (p["win"] - hair) * (d - 1) - (p["loss"] + hair)
    return {"ev": central, "conservative_ev": cons, "value_score": bet_value_score(cons),
            "break_even": 1 / d}


# ---------------------------------------------------------------- backtest

def candidate_bets(pred: Predictor, r: dict):
    """All sides the model could bet in a historical game, with closing prices."""
    fm, ft = pred.fair(r["features"])
    out = []
    home_line = -r["spread_line"]
    for side, odds in (("home", r["home_spread_odds"]), ("away", r["away_spread_odds"])):
        if odds is None or abs(odds) < 100:
            continue
        p = pred.spread(fm, home_line, side)
        result = r["margin"] + home_line if side == "home" else -(r["margin"] + home_line)
        out.append(("spread", side, p, odds, result))
    for side, odds in (("over", r["over_odds"]), ("under", r["under_odds"])):
        if odds is None or abs(odds) < 100:
            continue
        p = pred.total(ft, r["total_line"], side)
        result = (r["total"] - r["total_line"]) * (1 if side == "over" else -1)
        out.append(("total", side, p, odds, result))
    for side, odds in (("home", r["home_moneyline"]), ("away", r["away_moneyline"])):
        if odds is None or abs(odds) < 100:
            continue
        p = pred.moneyline(fm, side)
        result = r["margin"] if side == "home" else -r["margin"]
        out.append(("moneyline", side, p, odds, result))
    return fm, ft, out


def settle(result, american):
    if result > 0:
        return decimal_odds(american) - 1
    if result < 0:
        return -1.0
    return 0.0


def backtest(schedule, team_games, cfg):
    ratings = pregame_ratings(team_games, schedule, cfg)
    rows = training_rows(schedule, ratings)
    seasons = sorted({r["season"] for r in rows})
    tests = [s for s in seasons if s >= cfg["first_test_season"]]
    markets = ("spread", "total", "moneyline")
    bets = {m: [] for m in markets}
    accuracy = {"model_margin_mae": [], "line_margin_mae": [], "model_total_mae": [], "line_total_mae": []}
    by_season = []
    for s in tests:
        train = [r for r in rows if r["season"] < s]
        test = [r for r in rows if r["season"] == s]
        if len(train) < 500 or not test:
            continue
        pred = Predictor(fit_models(train, cfg), outcome_table(train), cfg)
        season_bets = {m: [] for m in markets}
        for r in test:
            fm, ft, cands = candidate_bets(pred, r)
            accuracy["model_margin_mae"].append(abs(r["margin"] - fm))
            accuracy["line_margin_mae"].append(abs(r["margin"] - r["spread_line"]))
            accuracy["model_total_mae"].append(abs(r["total"] - ft))
            accuracy["line_total_mae"].append(abs(r["total"] - r["total_line"]))
            best = {}
            for market, side, p, odds, result in cands:
                e = evaluate_bet(p, odds, cfg)
                edge = p["win"] / max(1e-9, p["win"] + p["loss"]) - e["break_even"]
                if e["ev"] <= 0 or edge < cfg["min_probability_edge"]:
                    continue
                if market not in best or e["ev"] > best[market][0]["ev"]:
                    best[market] = (e, side, odds, result, edge)
            for market, (e, side, odds, result, edge) in best.items():
                b = {"season": s, "game_id": r["game_id"], "side": side, "odds": odds,
                     "ev": e["ev"], "value_score": e["value_score"], "edge": edge,
                     "profit": settle(result, odds), "result": "win" if result > 0 else "loss" if result < 0 else "push"}
                bets[market].append(b); season_bets[market].append(b)
        by_season.append({"season": s, "games": len(test),
                          **{m: _summary(season_bets[m]) for m in markets}})
    report = {"model_version": MODEL_VERSION,
              "test_seasons": [x["season"] for x in by_season],
              "note": "Walk-forward: each season predicted by a model trained only on earlier seasons, settled at nflverse closing lines and prices (consensus market, not Bovada).",
              "accuracy": {k: round(float(np.mean(v)), 3) for k, v in accuracy.items()},
              "markets": {}, "by_season": by_season, "config_used": cfg}
    for m in markets:
        summ = _summary(bets[m])
        seasons_profit = [x[m]["roi"] > 0 for x in by_season if x[m]["bets"]]
        summ["seasons_profitable"] = round(sum(seasons_profit) / len(seasons_profit), 3) if seasons_profit else 0.0
        summ["by_value_score"] = {str(v): _summary([b for b in bets[m] if b["value_score"] == v])
                                  for v in sorted({b["value_score"] for b in bets[m]})}
        g = cfg["gate"]
        profits = np.array([b["profit"] for b in bets[m]]) if bets[m] else np.zeros(1)
        se = float(profits.std(ddof=1) / math.sqrt(len(profits))) if len(profits) > 1 else 1.0
        summ["roi_standard_error"] = round(se, 4)
        summ["roi_lower_bound"] = round(summ["roi"] - g["z"] * se, 4)
        summ["enabled"] = bool(summ["bets"] >= g["min_bets"] and summ["roi_lower_bound"] > g["min_roi_lower_bound"]
                               and summ["seasons_profitable"] >= g["min_seasons_profitable"])
        report["markets"][m] = summ
    return report


def _summary(bets):
    n = len(bets)
    if not n:
        return {"bets": 0, "wins": 0, "losses": 0, "pushes": 0, "win_rate": None, "roi": 0.0, "units": 0.0}
    w = sum(b["result"] == "win" for b in bets); l = sum(b["result"] == "loss" for b in bets)
    units = sum(b["profit"] for b in bets)
    return {"bets": n, "wins": w, "losses": l, "pushes": n - w - l,
            "win_rate": round(w / (w + l), 4) if w + l else None,
            "roi": round(units / n, 4), "units": round(units, 2)}


# ---------------------------------------------------------------- final fit for live use

def fit_current(schedule, team_games, cfg, backtest_report=None):
    ratings = pregame_ratings(team_games, schedule, cfg)
    rows = training_rows(schedule, ratings)
    models = fit_models(rows, cfg)
    latest = max(g["season"] for g in team_games)
    enabled = {m: bool(v.get("enabled")) for m, v in (backtest_report or {}).get("markets", {}).items()}
    return {"model_version": MODEL_VERSION,
            "training_games": len(rows), "training_seasons": [min(r["season"] for r in rows), max(r["season"] for r in rows)],
            "current_season": latest, "margin": models["margin"], "total": models["total"],
            "margin_features": MARGIN_FEATURES, "total_features": TOTAL_FEATURES,
            "markets_enabled": enabled}


def _match(game, schedule, code):
    away, home = code(game["away"]), code(game["home"])
    if not away or not home:
        return None
    day = game["kickoff"][:10]
    best = None
    for r in schedule:
        if r["home"] == franchise(home) and r["away"] == franchise(away):
            gap = abs((datetime.fromisoformat(r["gameday"]) - datetime.fromisoformat(day)).days)
            if gap <= 1 and (best is None or gap < best[0]):
                best = (gap, r)
    return best[1] if best else None


def live_estimates(games, markets, schedule=None, team_games=None, cfg=None, model=None):
    """Score upcoming games and every quoted market with the fitted model.

    Returns {"games": {game_id: fair lines}, "markets": {market_id: evaluation},
    "enabled": {...}}. The market's own line is the model's anchor, matching
    the backtest. Games without a quoted line fall back to the nflverse
    consensus line so fair lines are still shown.
    """
    if __package__:
        from .free_sources import code
    else:
        from free_sources import code
    cfg = cfg or load_config()
    model = model or json.loads(MODEL_FILE.read_text())
    schedule = schedule if schedule is not None else load_schedule()
    team_games = team_games if team_games is not None else load_team_games()
    ratings = pregame_ratings(team_games, schedule, cfg)
    history = training_rows(schedule, ratings)
    pred = Predictor(model, outcome_table(history), cfg)
    by_game = defaultdict(list)
    for m in markets:
        by_game[m["game_id"]].append(m)
    out_games, out_markets = {}, {}
    for g in games:
        row = _match(g, schedule, code)
        if not row or row["game_id"] not in ratings:
            continue
        quoted = by_game.get(g["id"], [])
        home_spread = next((m["line"] for m in quoted if m["category"] == "Spread" and m["selection"] == g["home"]
                            and isinstance(m["line"], (int, float))), None)
        total_line = next((m["line"] for m in quoted if m["category"] == "Total" and isinstance(m["line"], (int, float))), None)
        spread_anchor = -home_spread if home_spread is not None else row["spread_line"]
        total_anchor = total_line if total_line is not None else row["total_line"]
        if spread_anchor is None or total_anchor is None:
            continue
        base = {**row, "spread_line": spread_anchor, "total_line": total_anchor}
        f = game_features(base, ratings[row["game_id"]])
        fm, ft = pred.fair(f)
        out_games[g["id"]] = {"nflverse_game_id": row["game_id"], "fair_home_margin": round(fm, 2),
                              "fair_home_spread": round(-fm, 1), "fair_total": round(ft, 1),
                              "market_home_spread": round(-spread_anchor, 1), "market_total": total_anchor,
                              "anchor_source": "Bovada" if home_spread is not None else "nflverse consensus",
                              "spread_edge_points": round(fm - spread_anchor, 2),
                              "total_edge_points": round(ft - total_anchor, 2),
                              "projected_home_points": round((ft + fm) / 2, 1),
                              "projected_away_points": round((ft - fm) / 2, 1)}
        for m in quoted:
            try:
                if m["category"] == "Spread":
                    side = "home" if m["selection"] == g["home"] else "away" if m["selection"] == g["away"] else None
                    home_line = m["line"] if side == "home" else -m["line"] if side == "away" else None
                    p = pred.spread(fm, home_line, side) if side else None
                    market = "spread"
                elif m["category"] == "Total":
                    side = "over" if m["selection"].lower().startswith("over") else "under"
                    p = pred.total(ft, float(m["line"]), side)
                    market = "total"
                elif m["category"] == "Moneyline":
                    side = "home" if m["selection"] == g["home"] else "away" if m["selection"] == g["away"] else None
                    p = pred.moneyline(fm, side) if side else None
                    market = "moneyline"
                else:
                    continue
            except (TypeError, ValueError, KeyError):
                continue
            if not p:
                continue
            e = evaluate_bet(p, m["odds"], cfg)
            out_markets[m["id"]] = {"market": market, "p_win": round(p["win"], 4), "p_push": round(p["push"], 4),
                                    "p_loss": round(p["loss"], 4), "ev": round(e["ev"], 4),
                                    "conservative_ev": round(e["conservative_ev"], 4),
                                    "value_score": e["value_score"], "break_even": round(e["break_even"], 4)}
    return {"model_version": model["model_version"], "games": out_games, "markets": out_markets,
            "enabled": model.get("markets_enabled", {})}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["backtest", "fit", "all"])
    ap.add_argument("--schedule", type=Path)
    args = ap.parse_args()
    cfg = load_config()
    schedule = load_schedule(args.schedule)
    team_games = load_team_games()
    if args.action in ("backtest", "all"):
        report = backtest(schedule, team_games, cfg)
        BACKTEST_FILE.write_text(json.dumps(report, indent=1) + "\n")
        for m, s in report["markets"].items():
            print(f"{m:9s} bets={s['bets']:5d} win%={s['win_rate']} roi={s['roi']:+.3f} low={s['roi_lower_bound']:+.3f} "
                  f"seasons+={s['seasons_profitable']} enabled={s['enabled']}")
        print("accuracy", report["accuracy"])
    if args.action in ("fit", "all"):
        bt = json.loads(BACKTEST_FILE.read_text()) if BACKTEST_FILE.exists() else None
        model = fit_current(schedule, team_games, cfg, bt)
        MODEL_FILE.write_text(json.dumps(model, indent=1) + "\n")
        print(f"Fitted {model['model_version']} on {model['training_games']} games; enabled={model['markets_enabled']}")


if __name__ == "__main__":
    main()
