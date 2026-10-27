"""
Tests for the weekly load-increase criteria (utils/weekly_metrics.py) and the
get_weekly_progression_metrics tool wiring.
"""

import asyncio
import json
import os
import pathlib
import sys
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.server import get_weekly_progression_metrics  # noqa: E402
from intervals_mcp_server.utils.weekly_metrics import (  # noqa: E402
    compute_weekly_progression,
    find_illness_tags,
    parse_log_verdicts,
)
from tests.test_coach_metrics import TARGET, alt, make_wellness  # noqa: E402

WEEK = [TARGET - timedelta(days=i) for i in range(7, 0, -1)]  # 10/20 .. 10/26


def log(day: date, code: str = "GO", description: str = "") -> dict:
    return {
        "id": int(day.strftime("%m%d")) + 1000,
        "category": "NOTE",
        "name": f"[LOG] {code}",
        "description": description,
        "start_date_local": f"{day}T00:00:00",
    }


def planned(day: date, event_id: int, minutes: int = 60, name: str = "Workout") -> dict:
    return {
        "id": event_id,
        "category": "WORKOUT",
        "name": name,
        "moving_time": minutes * 60,
        "start_date_local": f"{day}T00:00:00",
    }


def ride(day: date, event_id: int | None, minutes: int = 60, load: float = 50.0) -> dict:
    act = {
        "id": f"i{day:%m%d}",
        "start_date_local": f"{day}T06:00:00",
        "moving_time": minutes * 60,
        "icu_training_load": load,
        "device_name": "Garmin Edge 840",
    }
    if event_id is not None:
        act["paired_event_id"] = event_id
    return act


def all_go() -> list[dict]:
    return [log(d) for d in WEEK]


def run(wellness=None, activities=None, events=None) -> dict:
    return compute_weekly_progression(
        TARGET,
        make_wellness() if wellness is None else wellness,
        [] if activities is None else activities,
        all_go() if events is None else events,
    )


def crit(result: dict, crit_id: str) -> dict:
    return next(c for c in result["criteria"] if c["id"] == crit_id)


# --- verdict -----------------------------------------------------------------------------------
def test_all_criteria_pass_gives_increase_ok():
    result = run()
    assert result["numeric_verdict"] == "INCREASE_OK"
    assert result["numeric_verdict_ja"] == "増量可"
    assert [c["id"] for c in result["criteria"]] == list("abcdef")
    assert result["failed_criteria"] == []
    assert result["completed_week"] == {"start": "2026-10-20", "end": "2026-10-26"}


def test_one_failed_criterion_holds():
    wellness = make_wellness(sleep=lambda a: 6.0)
    result = run(wellness)
    assert result["failed_criteria"] == ["e"]
    assert result["numeric_verdict"] == "HOLD"
    assert result["numeric_verdict_ja"] == "据え置き"


def test_two_failed_criteria_reduce():
    wellness = make_wellness(sleep=lambda a: 6.0, rhr=lambda a: 50.0 if a <= 6 else 46.0)
    result = run(wellness)
    assert result["failed_criteria"] == ["c", "e"]
    assert result["numeric_verdict"] == "REDUCE"
    assert result["reduce_reason"] == "two_or_more_criteria_failed"


def test_ctl_rise_over_seven_reduces_even_if_all_pass():
    # CTL rises 8 over the week up to the day before the review day.
    wellness = make_wellness(ctl=lambda a: 44.0 - (a - 1) * 8.0 / 7 if 1 <= a <= 8 else 36.0)
    result = run(wellness)
    assert result["load"]["ctl_rise_7d"] is not None and result["load"]["ctl_rise_7d"] > 7
    assert result["numeric_verdict"] == "REDUCE"
    assert result["reduce_reason"] == "ctl_rise_over_7"


def test_missing_data_blocks_an_increase_but_not_a_certain_reduce():
    wellness = make_wellness(hrv=lambda a: None)
    result = run(wellness)
    assert crit(result, "b")["status"] == "no_data"
    assert result["numeric_verdict"] is None
    assert "b" in result["provisional_reasons"][0]

    two_fails = make_wellness(
        hrv=lambda a: None, sleep=lambda a: 6.0, rhr=lambda a: 50.0 if a < 7 else 46.0
    )
    assert run(two_fails)["numeric_verdict"] == "REDUCE"


# --- a: decision log -------------------------------------------------------------------------
def test_log_verdicts_are_parsed_from_note_names():
    events = [log(WEEK[0], "swap"), {**log(WEEK[1]), "name": "Roan High"}, log(WEEK[2], "TRIM")]
    verdicts = parse_log_verdicts(events, WEEK[0], WEEK[-1])
    assert verdicts == {WEEK[0]: "SWAP", WEEK[2]: "TRIM"}


def test_swap_or_stop_fails_a_and_one_trim_is_allowed():
    swap = run(events=[log(WEEK[0], "SWAP")] + [log(d) for d in WEEK[1:]])
    assert crit(swap, "a")["status"] == "fail"
    one_trim = run(events=[log(WEEK[0], "TRIM")] + [log(d) for d in WEEK[1:]])
    assert crit(one_trim, "a")["status"] == "pass"
    two_trim = run(events=[log(d, "TRIM") for d in WEEK[:2]] + [log(d) for d in WEEK[2:]])
    assert crit(two_trim, "a")["status"] == "fail"


def test_missing_log_day_is_no_data_unless_a_failure_is_already_certain():
    missing = run(events=[log(d) for d in WEEK[1:]])
    assert crit(missing, "a")["status"] == "no_data"
    assert crit(missing, "a")["detail"]["missing_log_days"] == [WEEK[0].isoformat()]
    certain = run(events=[log(WEEK[1], "STOP")])
    assert crit(certain, "a")["status"] == "fail"


# --- b, c, d, e: wellness --------------------------------------------------------------------------
def test_hrv_seven_day_mean_below_baseline_fails_b():
    result = run(make_wellness(hrv=lambda a: 40.0 if a <= 6 else alt(a)))
    assert crit(result, "b")["status"] == "fail"


def test_rhr_must_stay_strictly_below_plus_three():
    at_limit = run(make_wellness(rhr=lambda a: 49.0 if a <= 6 else 46.0))
    assert crit(at_limit, "c")["status"] == "fail"  # 49.0 == 46 + 3, not below
    below = run(make_wellness(rhr=lambda a: 48.9 if a <= 6 else 46.0))
    assert crit(below, "c")["status"] == "pass"


def test_tsb_boundary_uses_the_previous_day():
    ok = run(make_wellness(ctl=lambda a: 40.0, atl=lambda a: 60.0))  # -20.0
    assert crit(ok, "d")["status"] == "pass"
    low = run(make_wellness(ctl=lambda a: 40.0, atl=lambda a: 60.1))
    assert crit(low, "d")["status"] == "fail"
    assert crit(low, "d")["detail"]["basis_date"] == "2026-10-26"


def test_sleep_boundary():
    assert crit(run(make_wellness(sleep=lambda a: 6.5)), "e")["status"] == "pass"
    assert crit(run(make_wellness(sleep=lambda a: 6.4)), "e")["status"] == "fail"
    sparse = run(make_wellness(sleep=lambda a: 7.0 if a == 0 else None))
    assert crit(sparse, "e")["status"] == "no_data"


# --- f: planned sessions ----------------------------------------------------------------------------
def test_completed_sessions_pass_f():
    events = all_go() + [planned(WEEK[0], 1), planned(WEEK[2], 2)]
    activities = [ride(WEEK[0], 1), ride(WEEK[2], 2, minutes=55)]
    detail = crit(run(activities=activities, events=events), "f")["detail"]
    assert detail["completed"] == 2 and detail["disrupted"] == 0


def test_two_disrupted_sessions_fail_f_and_one_is_allowed():
    events = all_go() + [planned(WEEK[0], 1), planned(WEEK[2], 2, name="[SKIPPED] Z2")]
    one = crit(run(activities=[ride(WEEK[0], 1)], events=events), "f")
    assert one["status"] == "pass" and one["detail"]["cancelled"][0]["reason"] == "skipped"

    events += [planned(WEEK[4], 3, minutes=120)]
    two = crit(run(activities=[ride(WEEK[0], 1), ride(WEEK[4], 3, minutes=80)], events=events), "f")
    assert two["status"] == "fail"
    assert two["detail"]["shortened"][0]["planned_min"] == 120
    assert two["detail"]["shortened"][0]["actual_min"] == 80


def test_unpaired_event_is_cancelled_but_a_duplicate_on_a_paired_day_is_ignored():
    events = all_go() + [planned(WEEK[0], 1), planned(WEEK[0], 9, name="Workout [Edge]")]
    result = run(activities=[ride(WEEK[0], 1)], events=events)
    detail = crit(result, "f")["detail"]
    assert detail["ignored_duplicates"][0]["id"] == 9
    assert detail["cancelled"] == []

    lone = run(activities=[], events=all_go() + [planned(WEEK[3], 5)])
    assert crit(lone, "f")["detail"]["cancelled"][0]["reason"] == "no paired activity"


def test_exactly_at_the_shortened_ratio_is_not_shortened():
    events = all_go() + [planned(WEEK[0], 1, minutes=100)]
    detail = crit(run(activities=[ride(WEEK[0], 1, minutes=85)], events=events), "f")["detail"]
    assert detail["shortened"] == []


# --- load numbers and flags ----------------------------------------------------------------------------
def test_weekly_tss_includes_all_activities_and_guideline():
    prev = TARGET - timedelta(days=9)
    activities = [
        ride(WEEK[0], None, load=100),
        ride(WEEK[3], None, load=200),
        ride(prev, None, load=80),
    ]
    load = run(activities=activities)["load"]
    assert load["last_week_tss"] == 300
    assert load["previous_week_tss"] == 80
    assert load["max_next_week_tss_at_guideline"] == 330


def test_illness_tags_are_reported_for_the_athlete_to_judge():
    events = all_go()
    events[2] = log(WEEK[2], "GO", "備考 [WATCH:家族発症] S1× S2×")
    tags = find_illness_tags(events, WEEK[0], WEEK[-1])
    assert tags == [{"date": WEEK[2].isoformat(), "tag": "[WATCH:家族発症]"}]
    result = run(events=events)
    assert result["illness_tags"] == tags
    assert any("illness tags" in r for r in result["provisional_reasons"])
    assert result["numeric_verdict"] == "INCREASE_OK"  # numbers only; Claude applies the watch rule


def test_events_not_fetched_gives_no_data_for_a_and_f():
    result = compute_weekly_progression(TARGET, make_wellness(), [], None)
    assert crit(result, "a")["status"] == "no_data"
    assert crit(result, "f")["status"] == "no_data"
    assert result["numeric_verdict"] is None


# --- tool wiring ------------------------------------------------------------------------------------------
def _patch(monkeypatch, *, wellness, activities=None, events=None):
    calls: list[tuple[str, dict]] = []

    async def fake_request(*_args, **kwargs):
        url = kwargs["url"]
        calls.append((url, kwargs.get("params") or {}))
        if url.endswith("/wellness"):
            return wellness
        if url.endswith("/activities"):
            return [] if activities is None else activities
        if url.endswith("/events"):
            return [] if events is None else events
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(
        "intervals_mcp_server.tools.weekly_metrics.make_intervals_request", fake_request
    )
    return calls


def _call(**kwargs) -> dict:
    return json.loads(
        asyncio.run(get_weekly_progression_metrics(target_date=TARGET.isoformat(), **kwargs))
    )


def test_tool_returns_json_and_requests_the_right_windows(monkeypatch):
    calls = _patch(monkeypatch, wellness=list(make_wellness().values()), events=all_go())
    result = _call()
    assert result["numeric_verdict"] == "INCREASE_OK"
    params = {url.rsplit("/", 1)[-1]: p for url, p in calls}
    assert params["activities"]["newest"] == "2026-10-26"
    assert params["activities"]["oldest"] == "2026-10-13"
    assert params["events"] == {"oldest": "2026-10-20", "newest": "2026-10-27"}
    assert params["wellness"]["newest"] == "2026-10-27"


def test_tool_wellness_error_yields_no_verdict(monkeypatch):
    _patch(monkeypatch, wellness={"error": True, "message": "boom"})
    result = _call()
    assert result["numeric_verdict"] is None and "boom" in result["error"]


def test_tool_event_error_is_reported(monkeypatch):
    async def fake_request(*_args, **kwargs):
        if kwargs["url"].endswith("/wellness"):
            return list(make_wellness().values())
        if kwargs["url"].endswith("/events"):
            return {"error": True, "message": "events down"}
        return []

    monkeypatch.setattr(
        "intervals_mcp_server.tools.weekly_metrics.make_intervals_request", fake_request
    )
    result = _call()
    assert result["data_errors"] == {"events": "events down"}
    assert result["numeric_verdict"] is None


def test_tool_rejects_an_invalid_date():
    result = json.loads(asyncio.run(get_weekly_progression_metrics(target_date="2026/10/27")))
    assert result["numeric_verdict"] is None and "YYYY-MM-DD" in result["error"]


def test_verdict_range_shows_both_extremes_when_a_criterion_lacks_data():
    result = run(make_wellness(hrv=lambda a: None))
    assert result["numeric_verdict"] is None
    assert result["verdict_range"] == {
        "if_missing_all_pass": "INCREASE_OK",
        "if_missing_all_fail": "HOLD",
    }
    assert run()["verdict_range"] is None
