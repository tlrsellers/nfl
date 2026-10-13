"""Deterministic scoring tools for NFL dashboard agents.

Team and value scoring use the standard library. Optional projection training
requires numpy and scikit-learn. Agents supply facts; this module calculates
scores and records missing inputs. No paid calls or bets are made.
"""
import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from statistics import NormalDist

UNITS = ["pass_offense", "rush_offense", "pass_defense", "rush_defense", "special_teams"]
UNIT_WEIGHTS = dict(zip(UNITS, (.35, .15, .35, .10, .05)))
METRICS = {
    "pass_offense": {"epa": (.60, True), "success_rate": (.30, True), "explosive_rate": (.10, True)},
    "rush_offense": {"epa": (.60, True), "success_rate": (.30, True), "explosive_rate": (.10, True)},
    "pass_defense": {"epa": (.60, False), "success_rate": (.30, False), "explosive_rate": (.10, False)},
    "rush_defense": {"epa": (.60, False), "success_rate": (.30, False), "explosive_rate": (.10, False)},
    "special_teams": {"field_goals_above_expected": (.50, True), "net_punting": (.30, True),
                      "return_coverage_efficiency": (.20, True)},
}
SCORING_VERSION = "nfl-scoring-1.0"


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def clamp(value, lo=1.0, hi=10.0):
    return max(lo, min(hi, value))


def blended_metric(team, unit, metric):
    values = team.get("metrics", {}).get(unit, {}).get(metric, {})
    if not isinstance(values, dict):
        raise ValueError(f"{unit}.{metric} must contain season_to_date and optionally last_three")
    season, recent = values.get("season_to_date"), values.get("last_three")
    if not finite(season):
        return None
    if metric.endswith("rate") and not 0 <= season <= 1:
        raise ValueError("Rates must be fractions between 0 and 1")
    if finite(recent):
        if metric.endswith("rate") and not 0 <= recent <= 1:
            raise ValueError("Rates must be fractions between 0 and 1")
        return .75 * season + .25 * recent
    return float(season)


def rank_score(value, peers, higher_better=True):
    """Average tied rank mapped to 1..10; midpoint is 5.5."""
    if len(peers) < 2:
        return None
    ahead = sum(v > value if higher_better else v < value for v in peers)
    tied = sum(v == value for v in peers)
    rank = 1 + ahead + (tied - 1) / 2
    return 10 - 9 * (rank - 1) / (len(peers) - 1)


def score_league(teams, season):
    """Score current-season raw metrics against the supplied league snapshot.

    Missing components receive an explicitly marked neutral contribution,
    without redistributing their weights. Entirely missing units stay null.
    """
    if not isinstance(teams, list) or len(teams) > 32:
        raise ValueError("teams must be a list of at most 32 NFL teams")
    names = [t["team"] for t in teams]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate team identifier")
    for t in teams:
        if str(t["season"]) != str(season):
            raise ValueError("Every team must use the requested season")
        if not isinstance(t["games_played"], int) or isinstance(t["games_played"], bool) or t["games_played"] < 0:
            raise ValueError("games_played must be a nonnegative integer")
    pools = {(u, m): [blended_metric(t, u, m) for t in teams if t["games_played"] > 0]
             for u in UNITS for m in METRICS[u]}
    pools = {key: [v for v in values if v is not None] for key, values in pools.items()}
    results = {}
    for t in teams:
        units, details, coverage, peer_coverage = {}, {}, 0.0, 0.0
        missing = []
        reliability = t["games_played"] / (t["games_played"] + 4)
        for u in UNITS:
            weighted, available = 0.0, 0.0
            details[u] = {}
            for m, (weight, higher) in METRICS[u].items():
                value = blended_metric(t, u, m) if t["games_played"] else None
                peers = pools[u, m]
                score = rank_score(value, peers, higher) if value is not None else None
                details[u][m] = {"blended_value": value, "peer_count": len(peers), "rank_score": score,
                                 "missing": score is None}
                weighted += weight * (score if score is not None else 5.5)
                if score is None:
                    missing.append(f"{u}.{m}")
                else:
                    available += weight
                    peer_coverage += UNIT_WEIGHTS[u] * weight * len(peers) / 32
            units[u] = 5.5 + reliability * (weighted - 5.5) if available else None
            coverage += UNIT_WEIGHTS[u] * available
        complete = not missing and len(teams) == 32 and all(len(p) == 32 for p in pools.values())
        strength = sum(UNIT_WEIGHTS[u] * (units[u] if units[u] is not None else 5.5) for u in UNITS)
        results[t["team"]] = {"team": t["team"], "season": str(season), "games_played": t["games_played"],
                              "units": units, "team_strength": strength if coverage else None,
                              "metric_coverage": coverage, "league_coverage": peer_coverage,
                              "data_confidence": round(10 * reliability * peer_coverage, 1),
                              "status": "complete" if complete else "incomplete" if coverage else "unavailable",
                              "missing_metrics": missing, "metric_details": details,
                              "sources": t.get("sources", []), "scoring_version": SCORING_VERSION}
    return results


def score_matchup(home, away, context=None):
    """Score both sides, keeping venue/rest/travel changes separate from units."""
    if home["season"] != away["season"]:
        raise ValueError("Teams must use the same season")
    context = context or {}
    pair = {"pass_offense": "pass_defense", "rush_offense": "rush_defense",
            "pass_defense": "pass_offense", "rush_defense": "rush_offense", "special_teams": "special_teams"}
    result = {}
    for side, team, opponent in (("home", home, away), ("away", away, home)):
        adjusted = dict(team["units"])
        changes = context.get("unit_adjustments", {}).get(side, [])
        for change in changes:
            unit, delta = change["unit"], change["delta"]
            if unit not in UNITS or not finite(delta) or abs(delta) > 2 or not change.get("reason") or not change.get("source_url"):
                raise ValueError("Unit adjustment needs a supported unit, delta within +/-2, reason, and source_url")
        # Aggregate before clipping so opposite evidence is order-independent.
        for unit in UNITS:
            delta = sum(c["delta"] for c in changes if c["unit"] == unit)
            if adjusted[unit] is not None:
                adjusted[unit] = clamp(adjusted[unit] + delta)
        result[side] = {"team": team["team"], "team_strength": team["team_strength"],
                        "expected_lineup_units": adjusted, "unit_adjustments": changes,
                        "data_confidence": team["data_confidence"], "status": team["status"]}
    for side, other in (("home", "away"), ("away", "home")):
        edges = {}
        for unit, opposing in pair.items():
            a = result[side]["expected_lineup_units"][unit]
            b = result[other]["expected_lineup_units"][opposing]
            edges[unit] = 5.5 + (a - b) / 2 if a is not None and b is not None else None
        rest = context.get("rest_days_difference", 0)
        if not finite(rest):
            raise ValueError("rest_days_difference must be finite")
        neutral = context.get("neutral_site", True)
        if not isinstance(neutral, bool):
            raise ValueError("neutral_site must be a boolean")
        home_bonus = .2 if side == "home" and not neutral else 0
        rest_bonus = clamp(rest * .05, -.2, .2) * (1 if side == "home" else -1)
        travel = context.get("travel_adjustments", {}).get(side, 0)
        if not finite(travel) or not -.2 <= travel <= 0:
            raise ValueError("Travel disadvantage must be between -0.2 and 0")
        if travel and not context.get("travel_evidence", {}).get(side):
            raise ValueError("Travel adjustment needs evidence")
        situational = clamp(home_bonus + rest_bonus + travel, -.5, .5)
        base = sum(UNIT_WEIGHTS[u] * edges[u] for u in UNITS) if all(v is not None for v in edges.values()) else None
        result[side].update({"category_matchups": edges, "matchup_base": base,
                             "situational_adjustment": situational,
                             "weekly_matchup": clamp(base + situational) if base is not None else None})
    h, a = result["home"]["weekly_matchup"], result["away"]["weekly_matchup"]
    result["matchup_difference_home_minus_away"] = h - a if h is not None and a is not None else None
    result["scoring_version"] = SCORING_VERSION
    return result


def dashboard_team_scores(stats, games, now):
    """Repository feed adapter. Never substitutes points/game for missing EPA."""
    stats = stats or {}
    if not stats.get("teams"):
        return {"teams": {}, "games": {}, "status": "unavailable",
                "detail": "Need current-season EPA, success rates, explosive rates, and special-teams metrics."}
    as_of = timestamp(stats.get("as_of", stats.get("checked_at", "")))
    if not 0 <= (now - as_of).total_seconds() <= 2880 * 60:
        raise ValueError("Statistics snapshot is future-dated or older than 48 hours")
    teams = score_league(stats["teams"], stats["season"])
    reports = {}
    for g in games:
        if g["home"] not in teams or g["away"] not in teams:
            continue
        if as_of >= timestamp(g["kickoff"]):
            continue
        context = (stats.get("scoring_context") or {}).get(g["id"], {})
        reports[g["id"]] = score_matchup(teams[g["home"]], teams[g["away"]], context)
    return {"teams": teams, "games": reports, "status": "ok" if teams else "unavailable",
            "detail": f"Calculated {len(teams)} team ratings; missing components are explicitly flagged."}


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return result


def features(game):
    # Unit ratings already incorporate expected lineup; no extra injury penalty.
    home, away = game["home"], game["away"]
    for team in (home, away):
        for unit in UNITS:
            v = float(team[unit])
            if not math.isfinite(v) or not 1 <= v <= 10:
                raise ValueError(f"{unit} must be finite and between 1 and 10")
    rest = float(game.get("rest_days_difference", 0))
    if not math.isfinite(rest):
        raise ValueError("Rest difference must be finite")
    if not isinstance(game["neutral_site"], bool):
        raise ValueError("neutral_site must be a boolean")
    venue = 0.0 if game["neutral_site"] else 1.0
    diff = [float(home[u]) - float(away[u]) for u in UNITS]
    level = [(float(home[u]) + float(away[u])) / 2 - 5.5 for u in UNITS]
    # Unit differences predict margin; combined levels predict total.
    return diff + [venue, rest], level + [venue, abs(rest)]


def probability(mu, sigma, threshold, direction="over", nonnegative=False):
    """Rounded normal integer-score approximation, with explicit push mass."""
    if not all(math.isfinite(x) for x in (mu, sigma, threshold)) or sigma <= 0:
        raise ValueError("Invalid distribution parameters")
    if not math.isclose(threshold * 2, round(threshold * 2), abs_tol=1e-8):
        raise ValueError("Only integer and half-point lines are supported")
    d = NormalDist(mu, sigma)
    # For totals, truncate the rounded distribution below integer zero.
    floor = d.cdf(-0.5) if nonnegative else 0.0
    def cdf(x):
        return max(0.0, min(1.0, (d.cdf(x) - floor) / (1.0 - floor)))
    if float(threshold).is_integer():
        loss = cdf(threshold - 0.5)
        push = cdf(threshold + 0.5) - loss
        win = 1 - cdf(threshold + 0.5)
    else:
        loss, push = cdf(threshold), 0.0
        win = 1 - loss
    if direction == "under":
        win, loss = loss, win
    elif direction != "over":
        raise ValueError("direction must be over or under")
    return {"win": win, "push": push, "loss": loss}


def decimal_odds(american):
    a = float(american)
    if not math.isfinite(a) or abs(a) < 100:
        raise ValueError("American odds must be <= -100 or >= +100")
    return 1 + (a / 100 if a > 0 else 100 / abs(a))


def bet_value_score(conservative_ev):
    if not finite(conservative_ev):
        raise ValueError("Expected return must be finite")
    ev = round(conservative_ev, 12)
    return 1 if ev <= 0 else 2 + sum(ev >= b for b in (.01, .02, .03, .04, .05, .06, .08, .10))


def score_bet(probabilities, american, uncertainty_pp=2.0, validated=False):
    p = {k: float(probabilities[k]) for k in ("win", "push", "loss")}
    if any(not math.isfinite(v) or not 0 <= v <= 1 for v in p.values()):
        raise ValueError("Probabilities must be finite and between zero and one")
    if not math.isclose(sum(p.values()), 1.0, abs_tol=1e-8):
        raise ValueError("Win, push, and loss probabilities must sum to one")
    if not math.isfinite(uncertainty_pp) or not 0 <= uncertainty_pp <= 100:
        raise ValueError("uncertainty_pp must be between 0 and 100")
    d = decimal_odds(american)
    ev = p["win"] * (d - 1) - p["loss"]
    # A policy haircut, not a statistical confidence interval: move win mass to loss.
    haircut = min(p["win"], uncertainty_pp / 100)
    conservative_ev = (p["win"] - haircut) * (d - 1) - (p["loss"] + haircut)
    raw = bet_value_score(conservative_ev)
    settled = p["win"] + p["loss"]
    return {
        "decimal_odds": d,
        "break_even_probability_excluding_pushes": 1 / d,
        "win_probability_excluding_pushes": p["win"] / settled if settled else None,
        "probabilities": p,
        "expected_roi": ev,
        "conservative_expected_roi": conservative_ev,
        "uncertainty_haircut_percentage_points": uncertainty_pp,
        "raw_value_score": raw,
        "bet_score": raw if validated else min(raw, 5),
        "status": ("positive_model_value" if conservative_ev > 0 else "pass")
        if validated else "provisional_paper_tracking_only",
    }


# Compatibility with the earlier standalone betting package.
value_rating = score_bet


def train(rows):
    import numpy as np
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    if len(rows) < 48:
        raise ValueError("Need at least 48 completed games with pregame snapshots")
    seasons = {r["season"] for r in rows}
    if len(seasons) != 1:
        raise ValueError("Use exactly one season; earlier seasons are excluded")
    ids = [r["game_id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate game_id")
    rows = sorted(rows, key=lambda r: (r["week"], timestamp(r["kickoff"])))
    for r in rows:
        if timestamp(r["as_of"]) >= timestamp(r["kickoff"]):
            raise ValueError("Snapshot as_of must be before kickoff")
        if not isinstance(r["week"], int) or r["week"] < 1:
            raise ValueError("week must be a positive integer")
        for k in ("home_score", "away_score"):
            if not isinstance(r[k], int) or r[k] < 0:
                raise ValueError("Final scores must be nonnegative integers")
        features(r)
    xm = np.array([features(r)[0] for r in rows])
    xt = np.array([features(r)[1] for r in rows])
    ym = np.array([r["home_score"] - r["away_score"] for r in rows])
    yt = np.array([r["home_score"] + r["away_score"] for r in rows])
    weeks = np.array([r["week"] for r in rows])
    def estimator():
        # Fixed regularization avoids tuning against the evaluation weeks.
        return make_pipeline(StandardScaler(), Ridge(alpha=20.0))
    residuals = {"margin": [], "total": []}
    records = []
    for week in sorted(set(weeks)):
        past, test = weeks < week, weeks == week
        if past.sum() < 32:
            continue
        fitted = [estimator().fit(x[past], y[past]) for x, y in ((xm, ym), (xt, yt))]
        predictions = [f.predict(x[test]) for f, x in zip(fitted, (xm, xt))]
        # Probability evaluation uses residuals only from earlier prediction weeks.
        sigma = max(10.0, math.sqrt(np.mean(np.square(residuals["margin"])))) if residuals["margin"] else 13.5
        for i, pm, pt in zip(np.flatnonzero(test), *predictions):
            p = probability(float(pm), sigma, 0)
            outcome = "win" if ym[i] > 0 else "loss" if ym[i] < 0 else "push"
            # Three-outcome Brier score; unlike binary Brier its range is 0..2.
            brier = sum((p[k] - float(k == outcome)) ** 2 for k in p)
            records.append({"game_id": rows[i]["game_id"], "week": int(week),
                            "margin_prediction": float(pm), "total_prediction": float(pt),
                            "actual_margin": int(ym[i]), "actual_total": int(yt[i]),
                            "moneyline_probabilities": p, "brier_three_outcome": brier})
        residuals["margin"].extend((ym[test] - predictions[0]).tolist())
        residuals["total"].extend((yt[test] - predictions[1]).tolist())
    if len(records) < 16:
        raise ValueError("Need at least 16 later-week games for forward evaluation")
    model = {"season": rows[0]["season"], "training_games": len(rows),
             "training_max_week": int(weeks.max()), "validation_games": len(records),
             "validation_status": "unapproved", "walk_forward": records,
             "mean_three_outcome_moneyline_brier": float(np.mean([r["brier_three_outcome"] for r in records]))}
    for name, x, y, floor in (("margin", xm, ym, 10.0), ("total", xt, yt, 12.0)):
        fit = estimator().fit(x, y)
        scaler, ridge = fit.steps[0][1], fit.steps[1][1]
        rmse = math.sqrt(np.mean(np.square(residuals[name])))
        model[name] = {"mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(),
                       "coef": ridge.coef_.tolist(), "intercept": float(ridge.intercept_),
                       "sigma": max(floor, rmse), "walk_forward_rmse": rmse}
    return model


def predict(model, game):
    if game["season"] != model["season"]:
        raise ValueError("Game season must match model season")
    if game["week"] <= model["training_max_week"]:
        raise ValueError("Predict a later week; use saved walk-forward records for past weeks")
    if timestamp(game["as_of"]) >= timestamp(game["kickoff"]):
        raise ValueError("Snapshot must be before kickoff")
    vectors = features(game)
    output = {}
    for name, vector in zip(("margin", "total"), vectors):
        m = model[name]
        output[name] = m["intercept"] + sum((v - mean) / scale * coef
            for v, mean, scale, coef in zip(vector, m["mean"], m["scale"], m["coef"]))
    output["total"] = max(abs(output["margin"]), output["total"])
    output["projected_home_points"] = (output["total"] + output["margin"]) / 2
    output["projected_away_points"] = (output["total"] - output["margin"]) / 2
    output["fair_home_spread"] = -output["margin"]
    output["model_status"] = "provisional_unapproved"
    return output


def rate_market(model, game, bet):
    projection = predict(model, game)
    market = bet["market"]
    if market in ("moneyline", "spread"):
        side = bet["side"]
        if side not in ("home", "away"):
            raise ValueError("Side must be home or away")
        mu = projection["margin"] * (1 if side == "home" else -1)
        if market == "moneyline":
            # Ties may be refunds or losses depending on the book and product.
            if bet.get("tie_rule") not in ("refund", "loss"):
                raise ValueError("Specify moneyline tie_rule: refund or loss")
            p = probability(mu, model["margin"]["sigma"], 0)
            if bet["tie_rule"] == "loss":
                p["loss"] += p["push"]
                p["push"] = 0.0
        else:
            # Line is the selected team's handicap: e.g. home -3.5.
            p = probability(mu, model["margin"]["sigma"], -float(bet["line"]))
    elif market == "total":
        p = probability(projection["total"], model["total"]["sigma"],
                        float(bet["line"]), bet["side"], nonnegative=True)
    else:
        raise ValueError("Supported markets: moneyline, spread, total")
    quote = timestamp(bet["quoted_at"])
    cutoff = timestamp(game["as_of"])
    age = (cutoff - quote).total_seconds()
    if not 0 <= age <= 12 * 60:
        raise ValueError("Quote must be available as_of and no more than 12 minutes old")
    result = value_rating(p, bet["american_odds"], bet.get("uncertainty_pp", 3.0), validated=False)
    return {"projection": projection, "bet": bet, "evaluation": result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["teams", "matchup", "train", "predict", "rate", "value"])
    parser.add_argument("input", help="Input JSON file")
    parser.add_argument("--model", help="Fitted model JSON for predict/rate")
    parser.add_argument("--output", help="Output JSON file")
    args = parser.parse_args()
    data = json.loads(Path(args.input).read_text())
    if args.action == "teams":
        result = score_league(data["teams"], data["season"])
    elif args.action == "matchup":
        result = score_matchup(data["home"], data["away"], data.get("context"))
    elif args.action == "train":
        result = train(data)
    elif args.action == "value":
        result = value_rating(data["probabilities"], data["american_odds"],
                              data.get("uncertainty_pp", 2.0), validated=False)
    else:
        if not args.model:
            parser.error("--model is required")
        model = json.loads(Path(args.model).read_text())
        result = predict(model, data) if args.action == "predict" else rate_market(model, data["game"], data["bet"])
    rendered = json.dumps(result, indent=2, allow_nan=False)
    if args.output:
        Path(args.output).write_text(rendered + "\n")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
