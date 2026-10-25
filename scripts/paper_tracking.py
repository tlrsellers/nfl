"""Train a provisional points model and freeze prospective research forecasts.

Historical feature rows are reconstructed from earlier current-season games.
They are backtests, not prospective calibration observations. This script never
creates validated MODEL estimates, picks, or personal wager records.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from free_sources import TEAMS, match_row
from nfl_scoring import predict, probability, score_league, train
from performance_data import CACHE, make_team_feed, num, schedule_complete
from pipeline import ROOT, parse_time
from results_tracking import build_results

PAPER = ROOT / "data" / "paper"
OUTPUT = ROOT / "data" / "dashboard.json"


def kickoff(row):
    return datetime.strptime(row["gameday"] + " " + row.get("gametime", "13:00"), "%Y-%m-%d %H:%M").replace(
        tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


def training_rows(plays, schedule, season, now):
    completed = schedule_complete(schedule, season, now)
    weeks = sorted({int(r["week"]) for r in completed})
    rows = []
    for week in weeks:
        group = [r for r in completed if int(r["week"]) == week]
        cutoff = min(kickoff(r) for r in group) - timedelta(minutes=5)
        earlier = [r for r in completed if int(r["week"]) < week]
        feed = make_team_feed(plays, earlier, season, cutoff)
        ratings = score_league(feed["teams"], season)
        for r in group:
            home, away = (ratings[TEAMS[r[side + "_team"]][0]] for side in ("home", "away"))
            units = lambda t: {u: v if v is not None else 5.5 for u, v in t["units"].items()}
            hrest, arest = num(r.get("home_rest")), num(r.get("away_rest"))
            rows.append({"game_id": r["game_id"], "season": int(season), "week": week,
                         "as_of": cutoff.isoformat(), "kickoff": kickoff(r).isoformat(),
                         "home": units(home), "away": units(away),
                         "neutral_site": r.get("location") != "Home",
                         "rest_days_difference": hrest - arest if hrest is not None and arest is not None else 0,
                         "home_score": int(float(r["home_score"])), "away_score": int(float(r["away_score"])),
                         "feature_origin": "reconstructed_from_earlier_weeks",
                         "missing_units_imputed": {"home": [u for u, v in home["units"].items() if v is None],
                                                   "away": [u for u, v in away["units"].items() if v is None]}})
    return rows


def evaluate_archive(archive, schedule, season, now):
    completed = {r["game_id"]: r for r in schedule_complete(schedule, season, now)}
    for forecast in archive.get("predictions", {}).values():
        if forecast.get("settlement") or forecast["nflverse_game_id"] not in completed:
            continue
        if parse_time(forecast["forecast_as_of"]) >= parse_time(forecast["kickoff"]):
            continue
        r = completed[forecast["nflverse_game_id"]]
        hs, aws = int(float(r["home_score"])), int(float(r["away_score"]))
        margin, total = hs - aws, hs + aws
        outcome = "home" if margin > 0 else "away" if margin < 0 else "tie"
        p = forecast["outcome_probabilities"]
        brier = sum((p[k] - float(k == outcome)) ** 2 for k in ("home", "tie", "away"))
        forecast["settlement"] = {"home_score": hs, "away_score": aws,
            "actual_margin": margin, "actual_total": total, "outcome": outcome,
            "margin_error": margin - forecast["projection"]["margin"],
            "total_error": total - forecast["projection"]["total"],
            "three_outcome_brier": brier, "settled_at": now.isoformat().replace("+00:00", "Z")}
    settled = [r["settlement"] for r in archive.get("predictions", {}).values() if r.get("settlement")]
    n = len(settled)
    return {"prospective_predictions": len(archive.get("predictions", {})), "settled_predictions": n,
            "mean_three_outcome_brier": sum(x["three_outcome_brier"] for x in settled) / n if n else None,
            "margin_mae": sum(abs(x["margin_error"]) for x in settled) / n if n else None,
            "total_mae": sum(abs(x["total_error"]) for x in settled) / n if n else None,
            "calibration_status": "pending_separate_market_validation"}


def build_paper(dashboard, stats, plays, schedule, now, archive=None):
    season = int(stats["season"])
    if archive is None:
        archive = {"schema_version": 1, "season": season, "predictions": {}}
    if archive.get("season") != season:
        raise ValueError("Archive season does not match input")
    candidates = []
    for g in dashboard.get("games", []):
        r = match_row(g, schedule)
        if r and r.get("game_type") == "REG" and parse_time(g["kickoff"]) > now:
            candidates.append((g, r))
    if not candidates:
        return {"status": "waiting_for_slate", "forecasts": [], "evaluation": evaluate_archive(archive, schedule, season, now)}, archive, None
    # A prediction week must be later than all model training weeks. Do not train
    # on a partial prediction week, even if its Thursday game already finished.
    first_week = min(int(r["week"]) for _, r in candidates)
    earlier_schedule = [r for r in schedule if str(r.get("season")) == str(season) and int(r.get("week") or 0) < first_week]
    rows = training_rows(plays, earlier_schedule, season, now)
    if len(rows) < 48:
        report = {"status": "waiting_for_training_games", "training_games": len(rows), "forecasts": [],
                  "evaluation": evaluate_archive(archive, schedule, season, now)}
        return report, archive, None
    model = train(rows)
    digest = hashlib.sha256(json.dumps({k: model[k] for k in ("margin", "total")}, sort_keys=True).encode()).hexdigest()[:10]
    model_version = f"paper-{season}-w{model['training_max_week']}-{digest}"
    model["model_version"] = model_version
    model["feature_origin"] = "reconstructed_earlier_week_statistics_no_historical_injury_adjustments"
    forecasts = []
    ratings = score_league(stats["teams"], season)
    for g, r in candidates:
        home, away = ratings.get(g["home"]), ratings.get(g["away"])
        if not home or not away or home["status"] != "complete" or away["status"] != "complete":
            continue
        context = stats.get("scoring_context", {}).get(g["id"], {})
        # Use the expected units already computed by the dashboard; do not add
        # matchup/venue bonuses a second time to the point-model inputs.
        matchup = g.get("scoring", {})
        hu = matchup.get("home", {}).get("expected_lineup_units", home["units"])
        au = matchup.get("away", {}).get("expected_lineup_units", away["units"])
        game = {"season": season, "week": int(r["week"]), "as_of": now.isoformat(),
                "kickoff": g["kickoff"], "home": hu, "away": au,
                "neutral_site": context.get("neutral_site", r.get("location") != "Home"),
                "rest_days_difference": context.get("rest_days_difference", 0)}
        projection = predict(model, game)
        probabilities = probability(projection["margin"], model["margin"]["sigma"], 0)
        forecast = {"game_id": g["id"], "nflverse_game_id": r["game_id"],
                    "week": int(r["week"]),
                    "home": g["home"], "away": g["away"], "kickoff": g["kickoff"],
                    "forecast_as_of": now.isoformat().replace("+00:00", "Z"),
                    "statistics_as_of": stats["as_of"], "model_version": model_version,
                    "projection": projection,
                    "outcome_probabilities": {"home": probabilities["win"], "tie": probabilities["push"], "away": probabilities["loss"]},
                    "status": "paper_only_uncalibrated", "origin": "prospective",
                    "injury_adjustments_applied": bool(context.get("unit_adjustments"))}
        forecasts.append(forecast)
        # Freeze the first pregame forecast under the NFL game ID; provider event
        # ID changes must not create duplicate prospective observations.
        archive["predictions"].setdefault(r["game_id"], forecast)
        g["paper_forecast"] = forecast
    report = {"status": "paper_only_uncalibrated", "model_version": model_version,
              "forecast_count": len(forecasts), "forecasts": forecasts,
              "backtest": {"origin": "reconstructed_historical_features",
                           "training_games": model["training_games"], "validation_games": model["validation_games"],
                           "margin_rmse": model["margin"]["walk_forward_rmse"],
                           "total_rmse": model["total"]["walk_forward_rmse"],
                           "moneyline_three_outcome_brier": model["mean_three_outcome_moneyline_brier"]},
              "evaluation": evaluate_archive(archive, schedule, season, now),
              "detail": "Uncalibrated point forecasts; first pregame predictions are frozen for prospective evaluation. No bets recommended."}
    return report, archive, model


def run():
    now = datetime.now(timezone.utc)
    dashboard = json.loads(OUTPUT.read_text())
    stats = json.loads((ROOT / "data" / "free" / "stats.json").read_text())
    path = PAPER / f"{stats['season']}.json"
    archive = json.loads(path.read_text()) if path.exists() else None
    try:
        as_of = parse_time(stats.get("as_of"))
        if not as_of or not 0 <= (now - as_of).total_seconds() <= 48 * 3600:
            raise ValueError("Performance snapshot unavailable, future dated, or stale")
        raw = json.loads((CACHE / "performance_plays.json").read_text())
        report, archive, model = build_paper(dashboard, stats, raw["plays"], raw["schedule"], now, archive)
        PAPER.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(archive, indent=2, allow_nan=False) + "\n")
        if model:
            (PAPER / f"model_{stats['season']}.json").write_text(json.dumps(model, indent=2, allow_nan=False) + "\n")
    except (OSError, ValueError, KeyError, ImportError) as exc:
        report = {"status": "unavailable", "forecasts": [], "detail": f"Paper tracking unavailable: {type(exc).__name__}: {exc}"}
    dashboard["paper_tracking"] = report
    # Results survive missing training data and include completed games that
    # have already left the upcoming slate. Use the frozen archive, not the
    # latest model run, so later forecasts cannot rewrite the track record.
    dashboard["prediction_results"] = build_results(archive or {}, now)
    dashboard.setdefault("sources", {})["Paper predictions"] = {"status": "ok" if report["status"] == "paper_only_uncalibrated" else "unavailable",
          "checked_at": now.isoformat().replace("+00:00", "Z"), "detail": report.get("detail", report["status"])}
    OUTPUT.write_text(json.dumps(dashboard, indent=2, allow_nan=False) + "\n")
    print(f"Paper tracking: {report['status']}; {len(report['forecasts'])} forecasts")


if __name__ == "__main__":
    run()
