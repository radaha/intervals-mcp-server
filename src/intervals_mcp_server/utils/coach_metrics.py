"""
Deterministic daily-condition metrics for the coaching loop.

Everything in this module is pure (no network, no clock): given wellness rows,
recent activities and today's planned events, it evaluates the numeric rows of
the GO / TRIM / SWAP / STOP decision table and returns the evidence for each rule.

Design rule: arithmetic lives here, interpretation lives in the LLM. A rule is
never silently skipped: it is reported as ``hit``, ``clear``, ``no_data`` (an input
is missing) or ``needs_input`` (the rule depends on something only the athlete can
tell, e.g. symptoms or the calendar).

Definitions that are easy to get wrong (all documented in ``DEFINITIONS``):

* All dates are JST calendar days.
* TSB = CTL - ATL at the end of the previous day (the evaluated day's own row
  already contains that day's planned/completed load).
* S1/S2 (illness watch) compare today's value against the 28 days *before today*.
* The 7-day-average rules (HRV, RHR) compare the 7-day mean ending on the
  evaluated day against a 28-day baseline that ends right *before* that 7-day
  window, so a recent dip is not absorbed into its own baseline.
* "High intensity day" means the planned workout's IF (``icu_intensity``, in
  percent) is at or above ``high_intensity_if_pct``.
* Feel uses the intervals.icu scale: 1 is the best side, so "poor" is >= 4.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

JST = timezone(timedelta(hours=9))

# --- thresholds (mirrors the decision table in coach-instructions.md) ---------
TSB_STOP = -30.0
TSB_TRIM = -25.0
SLEEP_SHORT_HOURS = 6.0
S1_RHR_DELTA_BPM = 3.0
S2_HRV_SD_MULT = 1.5
HRV_7D_SD_MULT = 0.5
RHR_7D_DELTA_BPM = 5.0
RPE_TRIM = 8
FEEL_POOR = 4
HIGH_INTENSITY_IF_PCT = 85.0

BASELINE_DAYS = 28
RECENT_DAYS = 7
# Minimum number of valid daily values before a mean / SD is trusted.
MIN_BASELINE_VALID = 14
MIN_RECENT_VALID = 4

COMMUTE_MIN_MOVING_SECS = 25 * 60
COMMUTE_MIN_LOAD = 15

MOTIVATION_LABELS = {1: "EXTREME", 2: "HIGH", 3: "AVG", 4: "LOW"}

DEFINITIONS = {
    "dates": "JST calendar days",
    "tsb": (
        "CTL - ATL at the end of the previous day (form going into the day); the "
        "evaluated day's own row already includes that day's planned/completed load "
        "and is reported only as tsb_target_day_row"
    ),
    "s1_s2_baseline": "28 days before today, today excluded",
    "seven_day_rules_baseline": (
        "28 days immediately before the 7-day window ending on the evaluated day"
    ),
    "high_intensity_day": "planned workout IF (icu_intensity, percent) >= threshold",
    "feel_scale": "1 = best, 5 = worst; poor means >= 4",
    "evaluable_devices": (
        "Garmin Edge 840, Zwift and TrainerRoad (device TRAINER_ROAD); Edge 530 is a commute bike"
    ),
    "yesterday_rule_session": (
        "highest-load evaluable session dated the previous JST day; rides of "
        f"<= {COMMUTE_MIN_MOVING_SECS // 60} min and load <= {COMMUTE_MIN_LOAD} are "
        "treated as commutes and ignored"
    ),
}


# --- small numeric helpers ----------------------------------------------------
def _num(value: Any) -> float | None:
    """Return value as float if it is a real number, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if math.isnan(value):
        return None
    return float(value)


def _round(value: float | None, ndigits: int = 1) -> float | None:
    return None if value is None else round(value, ndigits)


def _valid(values: list[float | None]) -> list[float]:
    return [v for v in values if v is not None]


def mean(values: list[float | None]) -> float | None:
    """Mean of the valid values, or None if there are none."""
    valid = _valid(values)
    return sum(valid) / len(valid) if valid else None


def sample_sd(values: list[float | None]) -> float | None:
    """Sample standard deviation (n - 1) of the valid values; None if fewer than 2."""
    valid = _valid(values)
    if len(valid) < 2:
        return None
    avg = sum(valid) / len(valid)
    return math.sqrt(sum((v - avg) ** 2 for v in valid) / (len(valid) - 1))


@dataclass(frozen=True)
class Baseline:
    """Mean / SD of a baseline window and how many days contributed."""

    mean: float
    sd: float
    n: int


def make_baseline(
    values: list[float | None], min_valid: int = MIN_BASELINE_VALID
) -> Baseline | None:
    """Build a baseline, or None if there are too few valid days."""
    valid = _valid(values)
    sd = sample_sd(values)
    if len(valid) < min_valid or sd is None:
        return None
    return Baseline(mean=sum(valid) / len(valid), sd=sd, n=len(valid))


def daily_values(
    wellness: dict[str, dict[str, Any]], key: str, start: date, end: date
) -> list[float | None]:
    """Values of ``key`` for every day from start to end inclusive (None when missing)."""
    out: list[float | None] = []
    for offset in range((end - start).days + 1):
        entry = wellness.get((start + timedelta(days=offset)).isoformat())
        out.append(_num(entry.get(key)) if entry else None)
    return out


def sleep_hours(entry: dict[str, Any] | None) -> float | None:
    """Sleep duration in hours from a wellness row."""
    if not entry:
        return None
    secs = _num(entry.get("sleepSecs"))
    if secs is not None:
        return secs / 3600
    return _num(entry.get("sleepHours"))


def tsb_of(entry: dict[str, Any] | None) -> float | None:
    """TSB (form) as CTL - ATL."""
    if not entry:
        return None
    ctl, atl = _num(entry.get("ctl")), _num(entry.get("atl"))
    if ctl is None or atl is None:
        return None
    return ctl - atl


# --- rule result --------------------------------------------------------------
@dataclass
class RuleResult:
    """Outcome of one row of the decision table."""

    id: str
    label: str
    outcome: str  # STOP / SWAP / TRIM / TRIM_OR_SWAP
    status: str  # hit / clear / no_data / needs_input
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "outcome": self.outcome,
            "status": self.status,
            "detail": self.detail,
        }


# --- activity helpers ---------------------------------------------------------
def activity_local_date(activity: dict[str, Any]) -> date | None:
    """JST calendar day of an activity (start_date_local first, else UTC converted)."""
    local = activity.get("start_date_local")
    if isinstance(local, str) and len(local) >= 10:
        try:
            return date.fromisoformat(local[:10])
        except ValueError:
            pass
    utc = activity.get("start_date")
    if isinstance(utc, str) and len(utc) >= 10:
        try:
            parsed = datetime.fromisoformat(utc.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(JST).date()
    return None


def _device(activity: dict[str, Any]) -> str:
    return str(activity.get("device_name") or "")


def is_evaluable(activity: dict[str, Any]) -> bool:
    """Training sessions are Edge 840, Zwift or TrainerRoad (virtual ride) recordings.

    Indoor sessions synced from TrainerRoad/Zwift arrive with ``device_name``
    ``TRAINER_ROAD``; they are the athlete's real workouts, not commutes.
    """
    device = _device(activity).lower().replace("_", "").replace(" ", "")
    return "edge840" in device or "zwift" in device or "trainerroad" in device


def is_commute_device(activity: dict[str, Any]) -> bool:
    return "edge 530" in _device(activity).lower()


def _is_short_ride(activity: dict[str, Any]) -> bool:
    """Commute-sized ride (short and light) even if recorded on a training device."""
    moving = _num(activity.get("moving_time")) or 0.0
    load = _num(activity.get("icu_training_load")) or 0.0
    return moving <= COMMUTE_MIN_MOVING_SECS and load <= COMMUTE_MIN_LOAD


def _rpe(activity: dict[str, Any]) -> float | None:
    for key in ("perceived_exertion", "icu_rpe"):
        value = _num(activity.get(key))
        if value is not None and value > 0:
            return value
    return None


def summarize_session(activity: dict[str, Any]) -> dict[str, Any]:
    """Compact, rule-relevant view of one activity."""
    day = activity_local_date(activity)
    feel = _num(activity.get("feel"))
    return {
        "id": activity.get("id"),
        "name": activity.get("name"),
        "date": day.isoformat() if day else None,
        "device": _device(activity) or None,
        "rpe": _rpe(activity),
        "feel": int(feel) if feel is not None else None,
        "if_pct": _round(_num(activity.get("icu_intensity")), 1),
        "load": _round(_num(activity.get("icu_training_load")), 0),
    }


def _sort_key(activity: dict[str, Any]) -> str:
    return str(activity.get("start_date_local") or activity.get("start_date") or "")


# --- planned workout ----------------------------------------------------------
def summarize_planned(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Planned workouts of the day (notes and skipped entries excluded)."""
    workouts = [
        e
        for e in events
        if str(e.get("category") or "").upper() != "NOTE"
        and not str(e.get("name") or "").startswith("[SKIPPED]")
    ]
    intensities = [v for v in (_num(e.get("icu_intensity")) for e in workouts) if v is not None]
    return {
        "count": len(workouts),
        "names": [e.get("name") for e in workouts],
        "event_ids": [e.get("id") for e in workouts],
        "if_pct_max": _round(max(intensities), 1) if intensities else None,
    }


# --- the rules ----------------------------------------------------------------
def _no_data(rule: RuleResult, reason: str) -> RuleResult:
    rule.status = "no_data"
    rule.detail["reason"] = reason
    return rule


def _rule_tsb_stop(tsb: float | None) -> RuleResult:
    rule = RuleResult("R02", "TSB < -30", "STOP", "clear", {"threshold": TSB_STOP})
    if tsb is None:
        return _no_data(rule, "CTL/ATL missing for the day")
    rule.detail["tsb"] = _round(tsb)
    rule.status = "hit" if tsb < TSB_STOP else "clear"
    return rule


def _rule_illness_signals(
    wellness: dict[str, dict[str, Any]], target: date
) -> tuple[RuleResult, dict[str, Any]]:
    """S1 (RHR up) and S2 (HRV down); both together -> SWAP."""
    rule = RuleResult("R04", "S1 and S2 both flagged", "SWAP", "clear", {})
    base_start, base_end = target - timedelta(days=BASELINE_DAYS), target - timedelta(days=1)
    today = wellness.get(target.isoformat()) or {}
    rhr_today, hrv_today = _num(today.get("restingHR")), _num(today.get("hrv"))
    rhr_base = make_baseline(daily_values(wellness, "restingHR", base_start, base_end))
    hrv_base = make_baseline(daily_values(wellness, "hrv", base_start, base_end))

    s1: bool | None = None
    s2: bool | None = None
    if rhr_today is not None and rhr_base is not None:
        s1 = rhr_today >= rhr_base.mean + S1_RHR_DELTA_BPM
        rule.detail["s1"] = {
            "rhr": rhr_today,
            "baseline_mean": _round(rhr_base.mean),
            "threshold": _round(rhr_base.mean + S1_RHR_DELTA_BPM),
            "hit": s1,
        }
    if hrv_today is not None and hrv_base is not None:
        limit = hrv_base.mean - S2_HRV_SD_MULT * hrv_base.sd
        s2 = hrv_today < limit
        rule.detail["s2"] = {
            "hrv": hrv_today,
            "baseline_mean": _round(hrv_base.mean),
            "baseline_sd": _round(hrv_base.sd, 2),
            "threshold": _round(limit),
            "hit": s2,
        }
    signals = {"S1": s1, "S2": s2}
    if s1 is False or s2 is False:
        rule.status = "clear"
    elif s1 is None or s2 is None:
        _no_data(rule, "RHR or HRV (today or baseline) missing")
    else:
        rule.status = "hit"
    return rule, signals


def _rule_short_sleep(
    wellness: dict[str, dict[str, Any]], target: date, high_intensity: bool | None
) -> RuleResult:
    rule = RuleResult(
        "R05", "Sleep < 6h two days running and high-intensity day", "SWAP", "clear", {}
    )
    today = sleep_hours(wellness.get(target.isoformat()))
    yesterday = sleep_hours(wellness.get((target - timedelta(days=1)).isoformat()))
    rule.detail.update(
        {
            "sleep_hours_today": _round(today, 2),
            "sleep_hours_yesterday": _round(yesterday, 2),
            "threshold_hours": SLEEP_SHORT_HOURS,
        }
    )
    if today is None or yesterday is None:
        if (today is not None and today >= SLEEP_SHORT_HOURS) or (
            yesterday is not None and yesterday >= SLEEP_SHORT_HOURS
        ):
            return rule  # one of the two nights is long enough, so the rule cannot hit
        return _no_data(rule, "sleep missing for today or yesterday")
    if not (today < SLEEP_SHORT_HOURS and yesterday < SLEEP_SHORT_HOURS):
        return rule
    if high_intensity is None:
        return _no_data(rule, "short sleep confirmed, but planned IF is unknown")
    rule.detail["high_intensity_day"] = high_intensity
    rule.status = "hit" if high_intensity else "clear"
    return rule


def _seven_day_vs_baseline(
    wellness: dict[str, dict[str, Any]], key: str, day: date
) -> tuple[float | None, Baseline | None]:
    """7-day mean ending on ``day`` and the 28-day baseline right before that window."""
    window_start = day - timedelta(days=RECENT_DAYS - 1)
    recent = daily_values(wellness, key, window_start, day)
    base_end = window_start - timedelta(days=1)
    base_start = base_end - timedelta(days=BASELINE_DAYS - 1)
    base = make_baseline(daily_values(wellness, key, base_start, base_end))
    valid_recent = _valid(recent)
    recent_mean = mean(recent) if len(valid_recent) >= MIN_RECENT_VALID else None
    return recent_mean, base


def _rule_hrv_trend(wellness: dict[str, dict[str, Any]], target: date) -> RuleResult:
    rule = RuleResult(
        "R06", "HRV 7-day mean below baseline - 0.5 SD, two days running", "SWAP", "clear", {}
    )
    flags: list[bool] = []
    for label, day in (("today", target), ("yesterday", target - timedelta(days=1))):
        recent_mean, base = _seven_day_vs_baseline(wellness, "hrv", day)
        if recent_mean is None or base is None:
            return _no_data(rule, f"HRV 7-day mean or baseline missing for {label}")
        limit = base.mean - HRV_7D_SD_MULT * base.sd
        flags.append(recent_mean < limit)
        rule.detail[label] = {
            "mean_7d": _round(recent_mean),
            "baseline_mean": _round(base.mean),
            "baseline_sd": _round(base.sd, 2),
            "threshold": _round(limit),
            "below": recent_mean < limit,
        }
    rule.status = "hit" if all(flags) else "clear"
    return rule


def _rule_rhr_trend(wellness: dict[str, dict[str, Any]], target: date) -> RuleResult:
    rule = RuleResult("R07", "RHR 7-day mean >= baseline + 5 bpm", "SWAP", "clear", {})
    recent_mean, base = _seven_day_vs_baseline(wellness, "restingHR", target)
    if recent_mean is None or base is None:
        return _no_data(rule, "RHR 7-day mean or baseline missing")
    limit = base.mean + RHR_7D_DELTA_BPM
    rule.detail.update(
        {
            "mean_7d": _round(recent_mean),
            "baseline_mean": _round(base.mean),
            "threshold": _round(limit),
        }
    )
    rule.status = "hit" if recent_mean >= limit else "clear"
    return rule


def _rule_tsb_trim(tsb: float | None, high_intensity: bool | None) -> RuleResult:
    rule = RuleResult("R08", "TSB < -25 and high-intensity day", "TRIM", "clear", {})
    rule.detail["threshold"] = TSB_TRIM
    if tsb is None:
        return _no_data(rule, "CTL/ATL missing for the day")
    rule.detail["tsb"] = _round(tsb)
    if tsb >= TSB_TRIM:
        return rule
    if high_intensity is None:
        return _no_data(rule, "TSB is low, but planned IF is unknown")
    rule.detail["high_intensity_day"] = high_intensity
    rule.status = "hit" if high_intensity else "clear"
    return rule


def _rule_yesterday_effort(
    yesterday_session: dict[str, Any] | None, activities_available: bool
) -> RuleResult:
    rule = RuleResult("R09", "Yesterday RPE >= 8 and Feel >= 4", "TRIM", "clear", {})
    rule.detail.update({"rpe_threshold": RPE_TRIM, "feel_threshold": FEEL_POOR})
    if not activities_available:
        return _no_data(rule, "activities could not be fetched")
    if yesterday_session is None:
        rule.detail["note"] = "no evaluable session yesterday"
        return rule
    rpe, feel = yesterday_session["rpe"], yesterday_session["feel"]
    rule.detail["session"] = yesterday_session
    if rpe is None or feel is None:
        return _no_data(rule, "RPE or Feel not recorded for yesterday's session")
    rule.status = "hit" if (rpe >= RPE_TRIM and feel >= FEEL_POOR) else "clear"
    return rule


def _needs_input_rules() -> dict[str, RuleResult]:
    return {
        "R01": RuleResult(
            "R01", "Fever or below-the-neck symptoms reported", "STOP", "needs_input", {}
        ),
        "R03": RuleResult(
            "R03", "Above-the-neck symptoms only reported", "SWAP", "needs_input", {}
        ),
        "R10": RuleResult(
            "R10", "Calendar squeezes the workout", "TRIM_OR_SWAP", "needs_input", {}
        ),
    }


# --- observations (do not change the verdict) ----------------------------------
def _observations(
    wellness: dict[str, dict[str, Any]], target: date, signals: dict[str, bool | None]
) -> dict[str, Any]:
    obs: dict[str, Any] = {}

    motivation = [
        _num((wellness.get((target - timedelta(days=i)).isoformat()) or {}).get("motivation"))
        for i in range(3)
    ]
    today_motivation = motivation[0]
    obs["motivation_today"] = (
        f"{int(today_motivation)} ({MOTIVATION_LABELS.get(int(today_motivation), '?')})"
        if today_motivation is not None
        else None
    )
    obs["motivation_low_3_days"] = all(v is not None and v >= 4 for v in motivation)

    ctl_today = _num((wellness.get(target.isoformat()) or {}).get("ctl"))
    ctl_week_ago = _num((wellness.get((target - timedelta(days=7)).isoformat()) or {}).get("ctl"))
    if ctl_today is not None and ctl_week_ago is not None:
        rise = ctl_today - ctl_week_ago
        obs["ctl_rise_7d"] = _round(rise)
        obs["ctl_rise_over_7"] = rise > 7
    else:
        obs["ctl_rise_7d"] = None
        obs["ctl_rise_over_7"] = None

    room_temp = _num((wellness.get(target.isoformat()) or {}).get("RoomTemp"))
    obs["room_temp_c"] = room_temp
    obs["room_temp_over_28"] = None if room_temp is None else room_temp > 28

    flagged = [name for name, hit in signals.items() if hit]
    obs["illness_watch_signals"] = flagged
    obs["illness_question_needed"] = bool(flagged)
    return obs


# --- main entry point -----------------------------------------------------------
def compute_daily_metrics(
    target: date,
    wellness: dict[str, dict[str, Any]],
    activities: list[dict[str, Any]] | None,
    planned_events: list[dict[str, Any]] | None,
    high_intensity_if_pct: float = HIGH_INTENSITY_IF_PCT,
) -> dict[str, Any]:
    """Evaluate the numeric rows of the decision table for ``target`` (a JST day).

    ``activities`` / ``planned_events`` are ``None`` when they could not be fetched
    (which is different from "fetched, but empty").
    """
    today_entry = wellness.get(target.isoformat())
    latest_date = max(wellness) if wellness else None
    wellness_status = {
        "status": "ok" if today_entry else "stale_or_missing",
        "target_date": target.isoformat(),
        "latest_available_date": latest_date,
    }

    # Morning decision: use form going into the day (previous day's CTL - ATL). The
    # target day's own row already contains that day's planned or completed load.
    prev_entry = wellness.get((target - timedelta(days=1)).isoformat())
    tsb = tsb_of(prev_entry)
    tsb_day_row = tsb_of(today_entry)
    planned = summarize_planned(planned_events) if planned_events is not None else None
    high_intensity: bool | None = None
    if planned is not None and planned["if_pct_max"] is not None:
        high_intensity = planned["if_pct_max"] >= high_intensity_if_pct

    # Sessions
    yesterday_session: dict[str, Any] | None = None
    latest_session: dict[str, Any] | None = None
    commute_checks: list[dict[str, Any]] = []
    todays_sessions: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    if activities is not None:
        recent: list[dict[str, Any]] = []
        for act in sorted(activities, key=_sort_key):
            day = activity_local_date(act)
            if day is None:
                continue
            age = (target - day).days
            if age == 0 and is_evaluable(act):
                todays_sessions.append(summarize_session(act))
            if not 1 <= age <= RECENT_DAYS:
                continue
            if is_evaluable(act) and _is_short_ride(act):
                ignored.append(summarize_session(act))  # commute-sized ride on a training device
            elif is_evaluable(act):
                recent.append(act)
                if age == 1 and (
                    yesterday_session is None
                    or (_num(act.get("icu_training_load")) or 0.0)
                    >= (yesterday_session["load"] or 0.0)
                ):
                    yesterday_session = summarize_session(act)  # highest load of the day wins
            elif is_commute_device(act):
                moving = _num(act.get("moving_time")) or 0
                load = _num(act.get("icu_training_load")) or 0
                if moving > COMMUTE_MIN_MOVING_SECS and load > COMMUTE_MIN_LOAD:
                    commute_checks.append(summarize_session(act))
            else:
                ignored.append(summarize_session(act))
        if recent:
            latest_session = summarize_session(recent[-1])

    illness_rule, signals = _rule_illness_signals(wellness, target)
    needs = _needs_input_rules()
    rules = [
        needs["R01"],
        _rule_tsb_stop(tsb),
        needs["R03"],
        illness_rule,
        _rule_short_sleep(wellness, target, high_intensity),
        _rule_hrv_trend(wellness, target),
        _rule_rhr_trend(wellness, target),
        _rule_tsb_trim(tsb, high_intensity),
        _rule_yesterday_effort(yesterday_session, activities is not None),
        needs["R10"],
    ]

    verdict, provisional = _decide(rules, wellness_status["status"] == "ok")

    today_view = {
        "ctl": _round(_num((today_entry or {}).get("ctl"))),
        "atl": _round(_num((today_entry or {}).get("atl"))),
        "tsb": _round(tsb),
        "tsb_basis_date": (target - timedelta(days=1)).isoformat(),
        "tsb_target_day_row": _round(tsb_day_row),
        "hrv": _num((today_entry or {}).get("hrv")),
        "rhr": _num((today_entry or {}).get("restingHR")),
        "sleep_hours": _round(sleep_hours(today_entry), 2),
        "weight_kg": _num((today_entry or {}).get("weight")),
    }
    return {
        "date": target.isoformat(),
        "timezone": "Asia/Tokyo (UTC+9)",
        "wellness_status": wellness_status,
        "wellness_today": today_view,
        "planned_workout": planned,
        "high_intensity_if_pct_threshold": high_intensity_if_pct,
        "high_intensity_day": high_intensity,
        "yesterday_session": yesterday_session,
        "latest_session_7d": latest_session,
        "todays_sessions": todays_sessions,
        "commute_confirmation_needed": commute_checks,
        "ignored_activities": ignored,
        "rules": [r.to_dict() for r in rules],
        "numeric_verdict": verdict,
        "provisional_reasons": provisional,
        "observations": _observations(wellness, target, signals),
        "definitions": DEFINITIONS,
    }


def _decide(rules: list[RuleResult], wellness_ok: bool) -> tuple[str | None, list[str]]:
    """First hit in table order; explains why the verdict may still change."""
    provisional: list[str] = []
    if not wellness_ok:
        return None, ["wellness row for the target date is missing: no verdict"]

    verdict: str | None = None
    for rule in rules:
        if rule.status == "needs_input":
            provisional.append(f"{rule.id} needs athlete input ({rule.label})")
        elif rule.status == "no_data":
            provisional.append(f"{rule.id} has no data ({rule.detail.get('reason', '')})")
        elif rule.status == "hit" and verdict is None:
            verdict = rule.outcome
            # Rules below the first hit cannot change the verdict.
            break
    if verdict is None:
        no_data = [r for r in rules if r.status == "no_data"]
        if no_data:
            return None, provisional + ["no verdict: at least one rule lacks data"]
        verdict = "GO"
    return verdict, provisional
