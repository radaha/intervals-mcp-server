"""
Intensity placement and distribution of the completed week (週次レビューの観測項目).

Pure functions (no network, no clock). This is an observation for the weekly review
only: it never changes the load-increase verdict of ``weekly_metrics``.

Sessions are the same evaluable sessions as the daily rules (Edge 840 / Zwift /
TrainerRoad recordings, commute-sized rides excluded). The intensity classes use the
same 85% IF line as the daily "high-intensity day":

* hard:     IF >= 85%
* moderate: 70% <= IF < 85%
* easy:     IF < 70%

Flags:

* ``back_to_back_hard``  - hard sessions on two consecutive JST days.
* ``hard_count_over_3``  - four or more hard sessions in the completed week.
* ``hr_power_mismatch``  - a session whose efficiency factor (NP / mean HR) is at least
  5% below the median EF of the same environment (indoor / outdoor) over the 28 days
  right before the completed week. Indoor ERG fixes NP, so a heart-rate drift shows up
  as a lower EF. Outdoor NP varies with wind and gradient, so outdoor flags are noisier.
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import median
from typing import Any

from intervals_mcp_server.utils.coach_metrics import (
    BASELINE_DAYS,
    COMMUTE_MIN_LOAD,
    COMMUTE_MIN_MOVING_SECS,
    HIGH_INTENSITY_IF_PCT,
    _is_short_ride,
    _num,
    _round,
    activity_local_date,
    is_commute_device,
    is_evaluable,
)

MODERATE_IF_PCT = 70.0
HARD_COUNT_MAX = 3
EF_DROP_PCT = 5.0
EF_MIN_MOVING_SECS = 30 * 60
EF_MIN_BASELINE_SESSIONS = 5
DEFINITIONS_VERSION = "v0.1"

DEFINITIONS = {
    "version": DEFINITIONS_VERSION,
    "sessions": (
        "evaluable sessions of the completed week (commute-sized rides excluded); "
        f"hard IF >= {HIGH_INTENSITY_IF_PCT:g}%, moderate {MODERATE_IF_PCT:g}-"
        f"{HIGH_INTENSITY_IF_PCT:g}%, easy < {MODERATE_IF_PCT:g}%"
    ),
    "time_share": "power zone time (icu_zone_times) of those sessions: Z1-2 / Z3 / Z4+",
    "back_to_back_hard": "hard sessions on two consecutive JST days",
    "hard_count_over_3": f"more than {HARD_COUNT_MAX} hard sessions in the completed week",
    "hr_power_mismatch": (
        f"session >= {EF_MIN_MOVING_SECS // 60} min with power and HR whose EF (NP / mean HR) "
        f"is >= {EF_DROP_PCT:g}% below the median EF of the same environment over the "
        f"{BASELINE_DAYS} days before the completed week (needs >= {EF_MIN_BASELINE_SESSIONS} "
        "baseline sessions)"
    ),
}


def _duration_secs(activity: dict[str, Any]) -> float:
    """Moving time, else elapsed time (TrainerRoad indoor sessions often lack moving_time)."""
    for key in ("moving_time", "elapsed_time"):
        value = _num(activity.get(key))
        if value is not None and value > 0:
            return value
    return 0.0


def _is_training_session(activity: dict[str, Any]) -> bool:
    return is_evaluable(activity) and not _is_short_ride(activity)


def _is_unconfirmed_commute(activity: dict[str, Any]) -> bool:
    """Commute-device ride too long/heavy to be a commute (the daily rules ask about it)."""
    if not is_commute_device(activity):
        return False
    moving = _num(activity.get("moving_time")) or 0.0
    load = _num(activity.get("icu_training_load")) or 0.0
    return moving > COMMUTE_MIN_MOVING_SECS and load > COMMUTE_MIN_LOAD


def _intensity_class(activity: dict[str, Any]) -> str:
    if_pct = _num(activity.get("icu_intensity"))
    if if_pct is None or if_pct <= 0:
        return "unclassified"
    if if_pct >= HIGH_INTENSITY_IF_PCT:
        return "hard"
    return "moderate" if if_pct >= MODERATE_IF_PCT else "easy"


def _environment(activity: dict[str, Any]) -> str:
    if activity.get("trainer") is True:
        return "indoor"
    kind = str(activity.get("type") or "").lower()
    device = str(activity.get("device_name") or "").lower().replace("_", "")
    if kind.startswith("virtual") or "zwift" in device or "trainerroad" in device:
        return "indoor"
    return "outdoor"


def _mean_hr(activity: dict[str, Any]) -> float | None:
    for key in ("average_heartrate", "icu_average_hr", "avgHr"):
        value = _num(activity.get(key))
        if value is not None and value > 0:
            return value
    return None


def efficiency_factor(activity: dict[str, Any]) -> float | None:
    """NP / mean HR, or None when power or HR is missing (HR 0 = not recorded)."""
    hr = _mean_hr(activity)
    if hr is None:
        return None
    np_watts = _num(activity.get("icu_weighted_avg_watts"))
    if np_watts is None or np_watts <= 0:
        return None
    return np_watts / hr


def _zone_seconds(activity: dict[str, Any]) -> list[float] | None:
    """Seconds per power zone, index 0 = Z1. Accepts [{id, secs}, ...] or plain numbers."""
    raw = activity.get("icu_zone_times")
    if not isinstance(raw, list) or not raw:
        return None
    secs: list[float] = []
    for item in raw:
        value = _num(item.get("secs")) if isinstance(item, dict) else _num(item)
        if isinstance(item, dict) and str(item.get("id") or "").upper() == "SS":
            continue  # sweet spot overlaps Z3/Z4 in intervals.icu; skip to avoid double counting
        secs.append(value or 0.0)
    return secs


def _time_share(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    buckets = {"z1_2": 0.0, "z3": 0.0, "z4_plus": 0.0}
    without_zones = 0.0
    for act in sessions:
        zones = _zone_seconds(act)
        if zones is None:
            without_zones += _duration_secs(act)
            continue
        buckets["z1_2"] += sum(zones[:2])
        buckets["z3"] += zones[2] if len(zones) > 2 else 0.0
        buckets["z4_plus"] += sum(zones[3:])
    total = sum(buckets.values())
    share: dict[str, Any] = {
        key: (_round(value / total, 3) if total else None) for key, value in buckets.items()
    }
    share["zone_seconds_total"] = round(total)
    share["excluded_secs_without_power_zones"] = round(without_zones)
    share["source"] = "icu_zone_times"
    return share


def _view(activity: dict[str, Any]) -> dict[str, Any]:
    day = activity_local_date(activity)
    return {
        "id": activity.get("id"),
        "name": activity.get("name"),
        "date": day.isoformat() if day else None,
        "if_pct": _round(_num(activity.get("icu_intensity")), 1),
    }


def compute_intensity_review(
    target: date, activities: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Intensity review of the 7 days ending the day before ``target``."""
    if activities is None:
        return {"status": "no_data", "reason": "activities could not be fetched"}

    week_start = target - timedelta(days=7)
    week_end = target - timedelta(days=1)
    base_start = week_start - timedelta(days=BASELINE_DAYS)
    base_end = week_start - timedelta(days=1)

    week: list[dict[str, Any]] = []
    baseline: list[dict[str, Any]] = []
    unconfirmed = 0
    for act in activities:
        day = activity_local_date(act)
        if day is None:
            continue
        if week_start <= day <= week_end:
            if _is_training_session(act):
                week.append(act)
            elif _is_unconfirmed_commute(act):
                unconfirmed += 1
        elif base_start <= day <= base_end and _is_training_session(act):
            baseline.append(act)
    week.sort(key=lambda a: str(a.get("start_date_local") or a.get("start_date") or ""))

    counts = dict.fromkeys(("hard", "moderate", "easy", "unclassified"), 0)
    hard_days: set[date] = set()
    hard_sessions: list[dict[str, Any]] = []
    for act in week:
        cls = _intensity_class(act)
        counts[cls] += 1
        if cls == "hard":
            hard_sessions.append(_view(act))
            day = activity_local_date(act)
            if day is not None:
                hard_days.add(day)

    consecutive = sorted(d for d in hard_days if d - timedelta(days=1) in hard_days)
    back_to_back = {
        "hit": bool(consecutive),
        "dates": [[(d - timedelta(days=1)).isoformat(), d.isoformat()] for d in consecutive],
    }
    hard_count = {
        "hit": counts["hard"] > HARD_COUNT_MAX,
        "count": counts["hard"],
        "max": HARD_COUNT_MAX,
    }

    # EF baseline per environment
    base_ef: dict[str, list[float]] = {"indoor": [], "outdoor": []}
    for act in baseline:
        ef = efficiency_factor(act)
        if ef is not None and _duration_secs(act) >= EF_MIN_MOVING_SECS:
            base_ef[_environment(act)].append(ef)
    baseline_view = {
        env: {
            "sessions": len(values),
            "median_ef": _round(median(values), 3) if values else None,
        }
        for env, values in base_ef.items()
    }

    mismatches: list[dict[str, Any]] = []
    no_hr = 0
    no_baseline = 0
    for act in week:
        if _duration_secs(act) < EF_MIN_MOVING_SECS:
            continue
        ef = efficiency_factor(act)
        if ef is None:
            if _mean_hr(act) is None:
                no_hr += 1
            continue
        env = _environment(act)
        values = base_ef[env]
        if len(values) < EF_MIN_BASELINE_SESSIONS:
            no_baseline += 1
            continue
        ref = median(values)
        diff_pct = (ef - ref) / ref * 100
        if diff_pct <= -EF_DROP_PCT:
            mismatches.append(
                {
                    **_view(act),
                    "environment": env,
                    "ef": _round(ef, 3),
                    "baseline_ef": _round(ref, 3),
                    "diff_pct": _round(diff_pct, 1),
                }
            )
    mismatch = {
        "hit": bool(mismatches),
        "sessions": mismatches,
        "no_hr_count": no_hr,
        "no_baseline_count": no_baseline,
    }

    hits = [
        name
        for name, flag in (
            ("back_to_back_hard", back_to_back),
            ("hard_count_over_3", hard_count),
            ("hr_power_mismatch", mismatch),
        )
        if flag["hit"]
    ]
    return {
        "status": "ok",
        "note": "observation only: does not change numeric_verdict",
        "sessions": {**counts, "unconfirmed_commute": unconfirmed},
        "hard_sessions": hard_sessions,
        "time_share": _time_share(week),
        "flags": {
            "back_to_back_hard": back_to_back,
            "hard_count_over_3": hard_count,
            "hr_power_mismatch": mismatch,
        },
        "flags_hit": hits,
        "ef_baseline": {
            "window": {"start": base_start.isoformat(), "end": base_end.isoformat()},
            **baseline_view,
        },
        "definitions": DEFINITIONS,
    }
