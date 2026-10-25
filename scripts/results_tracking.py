"""Publish the original pregame forecasts and their verified final outcomes."""
from __future__ import annotations

import math
from datetime import datetime, timezone


def timestamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else None
    except (TypeError, ValueError):
        return None


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def build_results(archive, now=None):
    """Use frozen prospective records only; never reconstruct past predictions.

    Final scores come exclusively from the archive's final-score settlement.
    A tied game is reported separately and excluded from winner accuracy.
    """
    now = now or datetime.now(timezone.utc)
    records = []
    for game_id, forecast in (archive.get("predictions") or {}).items():
        if not isinstance(forecast, dict) or forecast.get("origin") != "prospective":
            continue
        saved, kick = timestamp(forecast.get("forecast_as_of")), timestamp(forecast.get("kickoff"))
        if not saved or not kick or not saved < kick or saved > now:
            continue
        projection = forecast.get("projection") or {}
        home_points, away_points = (number(projection.get("projected_" + side + "_points"))
                                    for side in ("home", "away"))
        margin, total = number(projection.get("margin")), number(projection.get("total"))
        if None in (home_points, away_points, margin, total) or not all(forecast.get(k) for k in ("home", "away")):
            continue
        probabilities = forecast.get("outcome_probabilities") or {}
        hp, ap = number(probabilities.get("home")), number(probabilities.get("away"))
        if hp is not None and ap is not None:
            predicted = "home" if hp > ap else "away" if ap > hp else None
        else:
            predicted = "home" if margin > 0 else "away" if margin < 0 else None
        parts = str(game_id).split("_")
        week = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else forecast.get("week")
        row = {"id": game_id, "season": archive.get("season"), "week": week,
               "away": forecast["away"], "home": forecast["home"],
               "kickoff": forecast["kickoff"], "forecast_as_of": forecast["forecast_as_of"],
               "model_version": forecast.get("model_version"),
               "projected_home_score": home_points, "projected_away_score": away_points,
               "projected_margin": margin, "projected_total": total,
               "predicted_side": predicted, "predicted_winner": forecast.get(predicted) if predicted else None,
               "winner_probability": hp if predicted == "home" else ap if predicted == "away" else None,
               "status": "upcoming" if kick > now else "awaiting_final",
               "winner_result": "pending", "final_home_score": None, "final_away_score": None,
               "actual_total": None, "margin_error": None, "total_error": None,
               "settled_at": None}
        settlement = forecast.get("settlement") or {}
        hs, aws = number(settlement.get("home_score")), number(settlement.get("away_score"))
        if kick <= now and hs is not None and aws is not None and hs >= 0 and aws >= 0 and hs.is_integer() and aws.is_integer():
            actual_side = "home" if hs > aws else "away" if aws > hs else "tie"
            result = "no_pick" if predicted is None else "tie" if actual_side == "tie" else "win" if predicted == actual_side else "loss"
            row.update(status="final", winner_result=result, final_home_score=int(hs), final_away_score=int(aws),
                       actual_total=int(hs + aws), margin_error=hs - aws - margin,
                       total_error=hs + aws - total, settled_at=settlement.get("settled_at"))
        records.append(row)
    records.sort(key=lambda r: r["kickoff"], reverse=True)
    finals = [r for r in records if r["status"] == "final"]
    wins = sum(r["winner_result"] == "win" for r in finals)
    losses = sum(r["winner_result"] == "loss" for r in finals)
    return {"season": archive.get("season"), "updated_at": now.isoformat().replace("+00:00", "Z"),
            "basis": "first_saved_pregame_forecast", "records": records,
            "summary": {"tracked": len(records), "final": len(finals), "wins": wins, "losses": losses,
                        "ties": sum(r["final_home_score"] == r["final_away_score"] for r in finals),
                        "winner_accuracy": wins / (wins + losses) if wins + losses else None,
                        "margin_mae": sum(abs(r["margin_error"]) for r in finals) / len(finals) if finals else None,
                        "total_mae": sum(abs(r["total_error"]) for r in finals) / len(finals) if finals else None}}
