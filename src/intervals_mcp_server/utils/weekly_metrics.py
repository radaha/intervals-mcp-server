"""
Deterministic weekly load-progression metrics (the "増量判定" of the weekly review).

Pure functions (no network, no clock), built on the same helpers and definitions as
``coach_metrics``. Given wellness rows, the last two weeks of activities and the
week's calendar events, they evaluate criteria a-f of the load-increase rule and
return the evidence for each, plus a provisional verdict:

* ``INCREASE_OK`` - all of a-f pass.
* ``HOLD``        - exactly one of a-f fails (run the plan as is, no add-on).
* ``REDUCE``      - two or more fail, or CTL rose by more than 7 in the week.

Time windows (all JST days, ``target`` is the day of the review):

* "Completed week": the 7 days ending the day before ``target``. Used for the decision
  log (a), the planned sessions (f) and the weekly TSS.
* Wellness criteria (b, c, e) use the same 7-day windows as the daily rules, ending on
  ``target`` itself, and the baseline is the 28 days right before that 7-day window.
* TSB (d) is the previous day's end value, as in the daily rules.

What only the athlete can tell (illness watch / recovery period, whether the last two
weeks were increases, the TrainerRoad block structure, approval) is reported as
``needs_input`` and never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from intervals_mcp_server.utils.coach_metrics import (
    HRV_7D_SD_MULT,
    MIN_RECENT_VALID,
    RECENT_DAYS,
    _num,
    _round,
    _seven_day_vs_baseline,
    activity_local_date,
    sleep_hours,
    tsb_of,
)

# --- thresholds (criteria a-f and the guidance numbers of the load-increase rule) --------
MAX_SWAP_STOP_DAYS = 0
MAX_TRIM_DAYS = 1
RHR_7D_DELTA_BPM = 3.0
TSB_MIN = -20.0
SLEEP_7D_MIN_HOURS = 6.5
MAX_DISRUPTED_SESSIONS = 1
# A planned session counts as shortened when it was ridden for less than this share of the
# planned moving time. The rule text does not give a number; this is an adjustable default.
SHORTENED_RATIO = 0.85
REDUCE_FAILED_CRITERIA = 2
CTL_RISE_REDUCE = 7.0
CTL_RISE_USUAL_RANGE = (2.0, 4.0)
CTL_RISE_STREAK_FLAG = 5.0
TSS_INCREASE_MAX_PCT = 10.0

VERDICT_LABELS_JA = {"INCREASE_OK": "増量可", "HOLD": "据え置き", "REDUCE": "抑制"}

_LOG_RE = re.compile(r"^\s*\[LOG\]\s*(GO|TRIM|SWAP|STOP)\b", re.IGNORECASE)
_ILLNESS_TAG_RE = re.compile(r"\[(?:WATCH|ILL):[^\]]+\]")

DEFINITIONS = {
    "dates": "JST calendar days",
    "completed_week": "the 7 days ending the day before the review day",
    "a_decisions": "daily verdicts are read from the [LOG] NOTE events of the completed week",
    "b_c_baseline": (
        "7-day mean ending on the review day vs the 28 days right before that 7-day window"
    ),
    "d_tsb": "CTL - ATL at the end of the day before the review day",
    "e_sleep": "mean sleep hours over the 7 days ending on the review day",
    "f_sessions": (
        "planned WORKOUT events of the completed week; cancelled = [SKIPPED] or no paired "
        f"activity; shortened = ridden < {int(SHORTENED_RATIO * 100)}% of planned moving time; "
        "an unpaired event on a day that has another paired event is a duplicate and ignored"
    ),
    "weekly_tss": "sum of icu_training_load of all activities (commutes included)",
    "ctl_rise_7d": "CTL at the end of the day before the review day minus CTL 7 days earlier",
}


@dataclass
class Criterion:
    """One of the criteria a-f: ``pass``, ``fail`` or ``no_data``."""

    id: str
    label: str
    status: str = "pass"
    detail: dict[str, Any] = field(default_factory=dict)

    def no_data(self, reason: str) -> Criterion:
        self.status = "no_data"
        self.detail["reason"] = reason
        return self

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "status": self.status, "detail": self.detail}


# --- events helpers -----------------------------------------------------------------------
def _event_date(event: dict[str, Any]) -> date | None:
    start = event.get("start_date_local") or event.get("start_date")
    if isinstance(start, str) and len(start) >= 10:
        try:
            return date.fromisoformat(start[:10])
        except ValueError:
            return None
    return None


def _category(event: dict[str, Any]) -> str:
    return str(event.get("category") or "").upper()


def parse_log_verdicts(events: list[dict[str, Any]], start: date, end: date) -> dict[date, str]:
    """Daily verdicts from ``[LOG] <code>`` NOTE events (first one per day wins)."""
    verdicts: dict[date, str] = {}
    for event in sorted(events, key=lambda e: str(e.get("start_date_local") or "")):
        if _category(event) != "NOTE":
            continue
        day = _event_date(event)
        match = _LOG_RE.match(str(event.get("name") or ""))
        if day is None or match is None or not start <= day <= end:
            continue
        verdicts.setdefault(day, match.group(1).upper())
    return verdicts


def find_illness_tags(events: list[dict[str, Any]], start: date, end: date) -> list[dict[str, str]]:
    """[WATCH:...] / [ILL:...] tags written into the decision logs of the window."""
    found: list[dict[str, str]] = []
    for event in events:
        if _category(event) != "NOTE":
            continue
        day = _event_date(event)
        if day is None or not start <= day <= end:
            continue
        text = f"{event.get('name') or ''} {event.get('description') or ''}"
        for tag in _ILLNESS_TAG_RE.findall(text):
            found.append({"date": day.isoformat(), "tag": tag})
    return sorted(found, key=lambda item: (item["date"], item["tag"]))


# --- criteria -------------------------------------------------------------------------------
def _criterion_a(verdicts: dict[date, str], days: list[date]) -> Criterion:
    crit = Criterion("a", "SWAP/STOP 0 days (TRIM at most 1 day) in the completed week")
    counts = dict.fromkeys(("GO", "TRIM", "SWAP", "STOP"), 0)
    for day in days:
        if day in verdicts:
            counts[verdicts[day]] += 1
    missing = [d.isoformat() for d in days if d not in verdicts]
    crit.detail.update({"counts": counts, "missing_log_days": missing})
    if counts["SWAP"] + counts["STOP"] > MAX_SWAP_STOP_DAYS or counts["TRIM"] > MAX_TRIM_DAYS:
        crit.status = "fail"  # a failure is certain even with missing days
    elif missing:
        crit.no_data(f"no [LOG] for {len(missing)} day(s)")
    return crit


def _criterion_b(wellness: dict[str, dict[str, Any]], target: date) -> Criterion:
    crit = Criterion("b", "HRV 7-day mean >= baseline - 0.5 SD")
    recent, base = _seven_day_vs_baseline(wellness, "hrv", target)
    if recent is None or base is None:
        return crit.no_data("HRV 7-day mean or baseline missing")
    limit = base.mean - HRV_7D_SD_MULT * base.sd
    crit.detail.update(
        {
            "mean_7d": _round(recent),
            "baseline_mean": _round(base.mean),
            "baseline_sd": _round(base.sd, 2),
            "threshold": _round(limit),
        }
    )
    crit.status = "pass" if recent >= limit else "fail"
    return crit


def _criterion_c(wellness: dict[str, dict[str, Any]], target: date) -> Criterion:
    crit = Criterion("c", f"RHR 7-day mean < baseline + {RHR_7D_DELTA_BPM:g} bpm")
    recent, base = _seven_day_vs_baseline(wellness, "restingHR", target)
    if recent is None or base is None:
        return crit.no_data("RHR 7-day mean or baseline missing")
    limit = base.mean + RHR_7D_DELTA_BPM
    crit.detail.update(
        {
            "mean_7d": _round(recent),
            "baseline_mean": _round(base.mean),
            "threshold": _round(limit),
        }
    )
    crit.status = "pass" if recent < limit else "fail"
    return crit


def _criterion_d(wellness: dict[str, dict[str, Any]], target: date) -> Criterion:
    crit = Criterion("d", f"TSB >= {TSB_MIN:g}")
    basis = target - timedelta(days=1)
    tsb = tsb_of(wellness.get(basis.isoformat()))
    crit.detail.update({"tsb": _round(tsb), "basis_date": basis.isoformat(), "threshold": TSB_MIN})
    if tsb is None:
        return crit.no_data("CTL/ATL missing for the basis day")
    crit.status = "pass" if tsb >= TSB_MIN else "fail"
    return crit


def _criterion_e(wellness: dict[str, dict[str, Any]], target: date) -> Criterion:
    crit = Criterion("e", f"Sleep 7-day mean >= {SLEEP_7D_MIN_HOURS:g} h")
    hours = [
        sleep_hours(wellness.get((target - timedelta(days=i)).isoformat()))
        for i in range(RECENT_DAYS)
    ]
    valid = [h for h in hours if h is not None]
    crit.detail["threshold_hours"] = SLEEP_7D_MIN_HOURS
    if len(valid) < MIN_RECENT_VALID:
        return crit.no_data("fewer than 4 days with sleep data")
    avg = sum(valid) / len(valid)
    crit.detail.update({"mean_hours": _round(avg, 2), "days_with_data": len(valid)})
    crit.status = "pass" if avg >= SLEEP_7D_MIN_HOURS else "fail"
    return crit


def _criterion_f(
    activities: list[dict[str, Any]] | None,
    events: list[dict[str, Any]] | None,
    days: list[date],
) -> Criterion:
    crit = Criterion("f", f"Cancelled or shortened planned sessions <= {MAX_DISRUPTED_SESSIONS}")
    if activities is None or events is None:
        return crit.no_data("activities or events could not be fetched")

    day_set = set(days)
    planned = [e for e in events if _category(e) == "WORKOUT" and (_event_date(e) in day_set)]
    paired: dict[str, dict[str, Any]] = {}
    for activity in activities:
        event_id = activity.get("paired_event_id")
        if event_id is not None:
            paired[str(event_id)] = activity
    paired_days = {_event_date(e) for e in planned if str(e.get("id")) in paired}

    cancelled: list[dict[str, Any]] = []
    shortened: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    completed = 0
    for event in planned:
        view = {"id": event.get("id"), "name": event.get("name"), "date": str(_event_date(event))}
        name = str(event.get("name") or "")
        act = paired.get(str(event.get("id")))
        if name.startswith("[SKIPPED]"):
            cancelled.append({**view, "reason": "skipped"})
        elif act is not None:
            planned_secs = _num(event.get("moving_time"))
            actual_secs = _num(act.get("moving_time"))
            if (
                planned_secs
                and actual_secs is not None
                and actual_secs < SHORTENED_RATIO * planned_secs
            ):
                shortened.append(
                    {
                        **view,
                        "planned_min": round(planned_secs / 60),
                        "actual_min": round(actual_secs / 60),
                    }
                )
            else:
                completed += 1
        elif _event_date(event) in paired_days:
            duplicates.append(view)  # replaced or duplicated by a paired event that day
        else:
            cancelled.append({**view, "reason": "no paired activity"})

    disrupted = len(cancelled) + len(shortened)
    crit.detail.update(
        {
            "planned_sessions": len(planned) - len(duplicates),
            "completed": completed,
            "cancelled": cancelled,
            "shortened": shortened,
            "ignored_duplicates": duplicates,
            "disrupted": disrupted,
            "shortened_ratio": SHORTENED_RATIO,
        }
    )
    crit.status = "fail" if disrupted > MAX_DISRUPTED_SESSIONS else "pass"
    return crit


# --- weekly load numbers ----------------------------------------------------------------------
def _tss(activities: list[dict[str, Any]], days: list[date]) -> float:
    day_set = set(days)
    return sum(
        _num(a.get("icu_training_load")) or 0.0
        for a in activities
        if activity_local_date(a) in day_set
    )


def _ctl_rise(wellness: dict[str, dict[str, Any]], target: date) -> float | None:
    basis = target - timedelta(days=1)
    now = _num((wellness.get(basis.isoformat()) or {}).get("ctl"))
    before = _num((wellness.get((basis - timedelta(days=7)).isoformat()) or {}).get("ctl"))
    if now is None or before is None:
        return None
    return now - before


def _classify(failed_count: int, reduce_by_ctl: bool) -> str:
    if failed_count >= REDUCE_FAILED_CRITERIA or reduce_by_ctl:
        return "REDUCE"
    return "HOLD" if failed_count else "INCREASE_OK"


# --- main entry point ---------------------------------------------------------------------------
def compute_weekly_progression(
    target: date,
    wellness: dict[str, dict[str, Any]],
    activities: list[dict[str, Any]] | None,
    events: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Evaluate criteria a-f of the load-increase rule for the review on ``target``.

    ``activities`` / ``events`` are ``None`` when they could not be fetched (different
    from "fetched, but empty").
    """
    week = [target - timedelta(days=i) for i in range(RECENT_DAYS, 0, -1)]
    previous_week = [d - timedelta(days=RECENT_DAYS) for d in week]
    start, end = week[0], week[-1]

    verdicts = parse_log_verdicts(events, start, end) if events is not None else {}
    criteria = [
        _criterion_a(verdicts, week)
        if events is not None
        else Criterion("a", "SWAP/STOP 0 days (TRIM at most 1 day)").no_data("events not fetched"),
        _criterion_b(wellness, target),
        _criterion_c(wellness, target),
        _criterion_d(wellness, target),
        _criterion_e(wellness, target),
        _criterion_f(activities, events, week),
    ]

    failed = [c.id for c in criteria if c.status == "fail"]
    missing = [c.id for c in criteria if c.status == "no_data"]
    ctl_rise = _ctl_rise(wellness, target)
    reduce_by_ctl = ctl_rise is not None and ctl_rise > CTL_RISE_REDUCE

    provisional: list[str] = []
    verdict: str | None = _classify(len(failed), reduce_by_ctl)
    verdict_range: dict[str, str] | None = None
    if missing and verdict != "REDUCE":
        # The verdict depends on the missing criteria: show both extremes.
        verdict_range = {
            "if_missing_all_pass": _classify(len(failed), reduce_by_ctl),
            "if_missing_all_fail": _classify(len(failed) + len(missing), reduce_by_ctl),
        }
        verdict = None
        provisional.append(f"criteria without data: {', '.join(missing)}")
    if ctl_rise is None:
        provisional.append("CTL rise could not be computed")

    tags = find_illness_tags(events, start, end) if events is not None else []
    needs_input = [
        "illness watch / return-to-training period (-> HOLD regardless of the numbers)",
        "whether the previous two weeks were both increases (3rd week: hold or lighter week)",
        "TrainerRoad block structure (load / recovery weeks take priority)",
        "approval by the athlete before any change is applied",
    ]
    if tags:
        provisional.append("illness tags found in the week's logs: check watch/return rules")

    last_tss = _tss(activities, week) if activities is not None else None
    prev_tss = _tss(activities, previous_week) if activities is not None else None
    load = {
        "last_week_tss": _round(last_tss, 0),
        "previous_week_tss": _round(prev_tss, 0),
        "max_next_week_tss_at_guideline": (
            _round(last_tss * (1 + TSS_INCREASE_MAX_PCT / 100), 0) if last_tss else None
        ),
        "tss_increase_guideline_pct": TSS_INCREASE_MAX_PCT,
        "ctl_rise_7d": _round(ctl_rise),
        "ctl_rise_usual_range": list(CTL_RISE_USUAL_RANGE),
        "ctl_rise_over_5": None if ctl_rise is None else ctl_rise > CTL_RISE_STREAK_FLAG,
        "ctl_rise_over_7": None if ctl_rise is None else reduce_by_ctl,
    }

    return {
        "date": target.isoformat(),
        "timezone": "Asia/Tokyo (UTC+09:00)",
        "completed_week": {"start": start.isoformat(), "end": end.isoformat()},
        "criteria": [c.to_dict() for c in criteria],
        "failed_criteria": failed,
        "no_data_criteria": missing,
        "numeric_verdict": verdict,
        "numeric_verdict_ja": VERDICT_LABELS_JA.get(verdict) if verdict else None,
        "verdict_range": verdict_range,
        "reduce_reason": (
            "ctl_rise_over_7"
            if reduce_by_ctl
            else ("two_or_more_criteria_failed" if len(failed) >= REDUCE_FAILED_CRITERIA else None)
        ),
        "decision_log": {d.isoformat(): verdicts.get(d) for d in week},
        "illness_tags": tags,
        "load": load,
        "needs_input": needs_input,
        "provisional_reasons": provisional,
        "definitions": DEFINITIONS,
    }
