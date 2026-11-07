"""
Tests for the weekly intensity review (utils/intensity_review.py).
"""

import os
import pathlib
import sys
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.utils.intensity_review import (  # noqa: E402
    compute_intensity_review,
    efficiency_factor,
)
from intervals_mcp_server.utils.weekly_metrics import compute_weekly_progression  # noqa: E402

TARGET = date(2026, 10, 27)  # review day; completed week is 10/20 .. 10/26
WEEK = [TARGET - timedelta(days=i) for i in range(7, 0, -1)]


def session(
    day: date,
    if_pct: float = 65.0,
    minutes: int = 60,
    np_watts: float | None = 150.0,
    hr: float | None = 130.0,
    device: str = "TRAINER_ROAD",
    hour: int = 6,
    zones: list | None = None,
    load: float = 50.0,
    **extra,
) -> dict:
    act = {
        "id": f"i{day:%m%d}{hour:02d}",
        "name": "Session",
        "start_date_local": f"{day}T{hour:02d}:00:00",
        "moving_time": minutes * 60,
        "icu_training_load": load,
        "icu_intensity": if_pct,
        "device_name": device,
        "icu_weighted_avg_watts": np_watts,
        "average_heartrate": hr,
    }
    if zones is not None:
        act["icu_zone_times"] = zones
    act.update(extra)
    return act


def indoor_baseline(ef: float = 150 / 130, count: int = 5) -> list[dict]:
    first = TARGET - timedelta(days=30)
    return [session(first + timedelta(days=i), np_watts=ef * 130, hr=130) for i in range(count)]


# --- classification and counts -------------------------------------------------------------
def test_classifies_sessions_by_if():
    acts = [session(WEEK[0], 90), session(WEEK[2], 75), session(WEEK[4], 60), session(WEEK[5], 0)]
    review = compute_intensity_review(TARGET, acts)
    assert review["sessions"] == {
        "hard": 1,
        "moderate": 1,
        "easy": 1,
        "unclassified": 1,
        "unconfirmed_commute": 0,
    }
    assert review["flags_hit"] == []


def test_if_exactly_85_is_hard_and_70_is_moderate():
    review = compute_intensity_review(TARGET, [session(WEEK[0], 85.0), session(WEEK[2], 70.0)])
    assert review["sessions"]["hard"] == 1
    assert review["sessions"]["moderate"] == 1


def test_commute_sized_rides_and_outside_week_are_excluded():
    acts = [
        session(WEEK[0], 95, minutes=20, load=10),  # commute-sized on a training device
        session(TARGET, 95),  # review day itself is not in the completed week
        session(WEEK[0] - timedelta(days=1), 95),  # before the week (baseline window)
        session(WEEK[1], 60, device="Garmin Edge 530", minutes=20, load=8),  # commute
    ]
    review = compute_intensity_review(TARGET, acts)
    assert sum(v for k, v in review["sessions"].items() if k != "unconfirmed_commute") == 0


def test_long_commute_device_ride_is_counted_as_unconfirmed():
    acts = [session(WEEK[1], 70, device="Garmin Edge 530", minutes=60, load=40)]
    review = compute_intensity_review(TARGET, acts)
    assert review["sessions"]["unconfirmed_commute"] == 1
    assert review["sessions"]["easy"] == 0


# --- back to back / hard count -------------------------------------------------------------
def test_back_to_back_hard_days_flagged():
    review = compute_intensity_review(TARGET, [session(WEEK[2], 90), session(WEEK[3], 88)])
    flag = review["flags"]["back_to_back_hard"]
    assert flag["hit"] is True
    assert flag["dates"] == [[WEEK[2].isoformat(), WEEK[3].isoformat()]]


def test_two_hard_sessions_same_day_are_not_back_to_back():
    acts = [session(WEEK[2], 90, hour=6), session(WEEK[2], 90, hour=18)]
    review = compute_intensity_review(TARGET, acts)
    assert review["flags"]["back_to_back_hard"]["hit"] is False
    assert review["sessions"]["hard"] == 2


def test_jst_day_boundary_uses_local_date():
    # 23:30 and next day 00:30 local are two consecutive JST days
    acts = [
        session(WEEK[2], 90, hour=23),
        session(WEEK[3], 90, hour=0),
    ]
    review = compute_intensity_review(TARGET, acts)
    assert review["flags"]["back_to_back_hard"]["hit"] is True


def test_four_hard_sessions_flagged():
    acts = [session(WEEK[i], 90) for i in (0, 2, 4, 6)]
    review = compute_intensity_review(TARGET, acts)
    assert review["flags"]["hard_count_over_3"]["hit"] is True
    assert review["flags"]["back_to_back_hard"]["hit"] is False
    acts3 = [session(WEEK[i], 90) for i in (0, 2, 4)]
    assert compute_intensity_review(TARGET, acts3)["flags"]["hard_count_over_3"]["hit"] is False


# --- EF / HR-power mismatch ----------------------------------------------------------------
def test_efficiency_factor_missing_hr_or_power():
    assert efficiency_factor(session(WEEK[0], hr=0)) is None
    assert efficiency_factor(session(WEEK[0], hr=None)) is None
    assert efficiency_factor(session(WEEK[0], np_watts=None)) is None
    assert efficiency_factor(session(WEEK[0], np_watts=150, hr=125)) == 150 / 125


def test_hr_power_mismatch_flagged_at_5_percent_drop():
    base = indoor_baseline()
    drifted = session(WEEK[3], np_watts=150, hr=137)  # EF 1.095 vs 1.154 -> -5.1%
    ok = session(WEEK[4], np_watts=150, hr=134)  # -3.0%
    review = compute_intensity_review(TARGET, base + [drifted, ok])
    flag = review["flags"]["hr_power_mismatch"]
    assert flag["hit"] is True
    assert [s["date"] for s in flag["sessions"]] == [WEEK[3].isoformat()]
    assert flag["sessions"][0]["environment"] == "indoor"
    assert review["ef_baseline"]["indoor"]["sessions"] == 5


def test_hr_power_mismatch_needs_baseline_and_hr():
    review = compute_intensity_review(
        TARGET,
        indoor_baseline(count=4) + [session(WEEK[3], np_watts=150, hr=150), session(WEEK[4], hr=0)],
    )
    flag = review["flags"]["hr_power_mismatch"]
    assert flag["hit"] is False
    assert flag["no_baseline_count"] == 1
    assert flag["no_hr_count"] == 1


def test_outdoor_session_not_compared_with_indoor_baseline():
    outdoor = session(WEEK[3], np_watts=150, hr=150, device="Garmin Edge 840")
    review = compute_intensity_review(TARGET, indoor_baseline() + [outdoor])
    assert review["flags"]["hr_power_mismatch"]["hit"] is False
    assert review["flags"]["hr_power_mismatch"]["no_baseline_count"] == 1


def test_short_sessions_skipped_for_ef():
    short = session(WEEK[3], minutes=29, np_watts=150, hr=160)
    review = compute_intensity_review(TARGET, indoor_baseline() + [short])
    assert review["flags"]["hr_power_mismatch"]["hit"] is False


# --- time share ---------------------------------------------------------------------------
def test_time_share_from_zone_times_skips_sweet_spot():
    zones = [
        {"id": "Z1", "secs": 600},
        {"id": "Z2", "secs": 1800},
        {"id": "Z3", "secs": 600},
        {"id": "Z4", "secs": 600},
        {"id": "Z5", "secs": 0},
        {"id": "SS", "secs": 900},
    ]
    acts = [session(WEEK[0], zones=zones), session(WEEK[1])]  # second has no zones
    share = compute_intensity_review(TARGET, acts)["time_share"]
    assert share["z1_2"] == round(2400 / 3600, 3)
    assert share["z3"] == round(600 / 3600, 3)
    assert share["z4_plus"] == round(600 / 3600, 3)
    assert share["excluded_secs_without_power_zones"] == 3600


def test_no_activities_gives_no_data():
    assert compute_intensity_review(TARGET, None)["status"] == "no_data"


# --- wiring -------------------------------------------------------------------------------
def test_weekly_progression_includes_review_without_changing_verdict():
    result_without = compute_weekly_progression(TARGET, {}, [], [])
    acts = [session(WEEK[i], 90) for i in (0, 1, 2, 3)]
    result_with = compute_weekly_progression(TARGET, {}, acts, [])
    assert result_with["intensity_review"]["flags_hit"] == [
        "back_to_back_hard",
        "hard_count_over_3",
    ]
    assert result_with["numeric_verdict"] == result_without["numeric_verdict"]
    assert result_with["no_data_criteria"] == result_without["no_data_criteria"]


def test_trainerroad_session_without_moving_time_uses_elapsed_time():
    base = indoor_baseline()
    tr = session(WEEK[3], np_watts=150, hr=140, minutes=60, trainer=True)
    tr["moving_time"] = None
    tr["elapsed_time"] = 3600
    flag = compute_intensity_review(TARGET, base + [tr])["flags"]["hr_power_mismatch"]
    assert flag["hit"] is True
