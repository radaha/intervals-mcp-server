"""
Unit tests for the deterministic daily-condition rules in utils/coach_metrics.py.

Each test builds synthetic wellness rows where ``ago`` is the number of days before
the evaluated day (0 = that day), so the window arithmetic is easy to read.
"""

import os
import pathlib
import sys
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.utils.coach_metrics import (  # noqa: E402
    activity_local_date,
    compute_daily_metrics,
    make_baseline,
    mean,
    sample_sd,
    summarize_planned,
)

TARGET = date(2026, 10, 27)
YESTERDAY = (TARGET - timedelta(days=1)).isoformat()


def alt(ago: int) -> float:
    """HRV that alternates 50 / 46 (mean 48, sample SD about 2.04 over 28 days)."""
    return 50.0 if ago % 2 else 46.0


def make_wellness(
    *,
    hrv=alt,
    rhr=lambda ago: 46.0,
    sleep=lambda ago: 7.0,
    ctl=lambda ago: 44.0,
    atl=lambda ago: 44.0,
    days: int = 40,
    target: date = TARGET,
) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for ago in range(days):
        day = (target - timedelta(days=ago)).isoformat()
        sleep_hours = sleep(ago)
        rows[day] = {
            "id": day,
            "hrv": hrv(ago),
            "restingHR": rhr(ago),
            "sleepSecs": None if sleep_hours is None else sleep_hours * 3600,
            "ctl": ctl(ago),
            "atl": atl(ago),
        }
    return rows


def workout(if_pct: float | None = 60.0, name: str = "Z2", category: str = "WORKOUT") -> dict:
    return {"id": 1, "name": name, "category": category, "icu_intensity": if_pct}


def session(day: str = YESTERDAY, device: str = "Garmin Edge 840", **overrides) -> dict:
    base = {
        "id": "i1",
        "name": "Ride",
        "device_name": device,
        "start_date_local": f"{day}T08:37:16",
        "icu_rpe": 8,
        "feel": 4,
        "icu_intensity": 75.7,
        "icu_training_load": 124,
        "moving_time": 7775,
    }
    base.update(overrides)
    return base


def run(wellness=None, activities=None, events=None, **kwargs) -> dict:
    return compute_daily_metrics(
        TARGET,
        make_wellness() if wellness is None else wellness,
        [] if activities is None else activities,
        [workout()] if events is None else events,
        **kwargs,
    )


def rule(result: dict, rule_id: str) -> dict:
    return next(r for r in result["rules"] if r["id"] == rule_id)


# --- helpers ---------------------------------------------------------------------
def test_mean_and_sd_ignore_missing_values():
    assert mean([1.0, None, 3.0]) == 2.0
    assert mean([None, None]) is None
    assert sample_sd([2.0, 4.0]) == (2.0**0.5)
    assert sample_sd([5.0]) is None


def test_baseline_needs_enough_valid_days():
    assert make_baseline([46.0, 50.0] * 6) is None  # 12 valid days
    base = make_baseline([46.0, 50.0] * 7)  # 14 valid days
    assert base is not None and base.n == 14


def test_activity_date_prefers_local_and_converts_utc_to_jst():
    assert activity_local_date({"start_date_local": "2026-10-24T08:37:16"}) == date(2026, 10, 24)
    # 23:37 UTC on the 23rd is 08:37 JST on the 24th
    assert activity_local_date({"start_date": "2026-10-23T23:37:16Z"}) == date(2026, 10, 24)
    assert activity_local_date({}) is None


def test_planned_summary_ignores_notes_and_skipped():
    planned = summarize_planned(
        [
            workout(70.0, "Z2"),
            workout(None, "[LOG] GO", category="NOTE"),
            workout(95.0, "[SKIPPED] VO2", category="WORKOUT"),
            workout(88.0, "Sweet Spot"),
        ]
    )
    assert planned["count"] == 2
    assert planned["if_pct_max"] == 88.0


# --- TSB rules --------------------------------------------------------------------
def test_tsb_stop_boundary_is_strictly_below_minus_30():
    at_limit = run(make_wellness(ctl=lambda a: 40.0, atl=lambda a: 70.0))
    assert rule(at_limit, "R02")["status"] == "clear"
    below = run(make_wellness(ctl=lambda a: 40.0, atl=lambda a: 70.1))
    assert rule(below, "R02")["status"] == "hit"
    assert below["numeric_verdict"] == "STOP"


def test_tsb_trim_needs_high_intensity_day():
    wellness = make_wellness(ctl=lambda a: 40.0, atl=lambda a: 65.2)  # TSB -25.2
    high = run(wellness, events=[workout(85.0)])  # IF at threshold counts as high
    assert rule(high, "R08")["status"] == "hit"
    assert high["numeric_verdict"] == "TRIM"
    low = run(wellness, events=[workout(84.9)])
    assert rule(low, "R08")["status"] == "clear"
    unknown = run(wellness, events=[])
    assert rule(unknown, "R08")["status"] == "no_data"


def test_tsb_exactly_minus_25_is_not_a_trim():
    wellness = make_wellness(ctl=lambda a: 40.0, atl=lambda a: 65.0)
    assert rule(run(wellness, events=[workout(95.0)]), "R08")["status"] == "clear"


# --- illness watch (S1 / S2) ---------------------------------------------------------
def test_s1_threshold_is_baseline_plus_3_inclusive():
    hit = run(make_wellness(rhr=lambda a: 49.0 if a == 0 else 46.0))
    assert rule(hit, "R04")["detail"]["s1"]["hit"] is True
    clear = run(make_wellness(rhr=lambda a: 48.9 if a == 0 else 46.0))
    assert rule(clear, "R04")["detail"]["s1"]["hit"] is False


def test_s2_threshold_uses_28_days_before_today():
    baseline = [alt(a) for a in range(1, 29)]
    limit = mean(baseline) - 1.5 * sample_sd(baseline)
    low = run(make_wellness(hrv=lambda a: limit - 0.1 if a == 0 else alt(a)))
    assert rule(low, "R04")["detail"]["s2"]["hit"] is True
    ok = run(make_wellness(hrv=lambda a: limit + 0.1 if a == 0 else alt(a)))
    assert rule(ok, "R04")["detail"]["s2"]["hit"] is False


def test_s1_and_s2_together_are_a_swap_and_one_alone_is_not():
    both = run(
        make_wellness(
            hrv=lambda a: 30.0 if a == 0 else alt(a),
            rhr=lambda a: 50.0 if a == 0 else 46.0,
        )
    )
    assert rule(both, "R04")["status"] == "hit"
    assert both["numeric_verdict"] == "SWAP"

    only_s1 = run(make_wellness(rhr=lambda a: 50.0 if a == 0 else 46.0))
    assert rule(only_s1, "R04")["status"] == "clear"
    assert only_s1["observations"]["illness_watch_signals"] == ["S1"]
    assert only_s1["observations"]["illness_question_needed"] is True


def test_todays_extreme_value_does_not_enter_its_own_baseline():
    result = run(make_wellness(rhr=lambda a: 80.0 if a == 0 else 46.0))
    assert rule(result, "R04")["detail"]["s1"]["baseline_mean"] == 46.0


# --- 7-day mean rules ----------------------------------------------------------------
def test_hrv_seven_day_rule_needs_two_consecutive_days_below():
    sustained = run(make_wellness(hrv=lambda a: 40.0 if a <= 7 else alt(a)))
    assert rule(sustained, "R06")["status"] == "hit"
    assert sustained["numeric_verdict"] == "SWAP"

    one_day_dip = run(make_wellness(hrv=lambda a: 30.0 if a == 0 else alt(a)))
    detail = rule(one_day_dip, "R06")["detail"]
    assert detail["today"]["below"] is True
    assert detail["yesterday"]["below"] is False
    assert rule(one_day_dip, "R06")["status"] == "clear"


def test_hrv_baseline_excludes_the_recent_week():
    # Today's 7-day window is 0-6 days ago; its baseline is 7-34 days ago.
    result = run(make_wellness(hrv=lambda a: 40.0 if a <= 6 else alt(a)))
    detail = rule(result, "R06")["detail"]["today"]
    assert detail["mean_7d"] == 40.0
    # The dip is not absorbed into its own baseline: 14 x 50 and 14 x 46.
    assert detail["baseline_mean"] == 48.0


def test_rhr_seven_day_rule_threshold_is_baseline_plus_5():
    hit = run(make_wellness(rhr=lambda a: 51.0 if a <= 6 else 46.0))
    assert rule(hit, "R07")["status"] == "hit"
    clear = run(make_wellness(rhr=lambda a: 50.9 if a <= 6 else 46.0))
    assert rule(clear, "R07")["status"] == "clear"


# --- sleep ------------------------------------------------------------------------------
def test_short_sleep_two_days_and_high_intensity_is_a_swap():
    wellness = make_wellness(sleep=lambda a: 5.5 if a <= 1 else 7.0)
    assert rule(run(wellness, events=[workout(88.0)]), "R05")["status"] == "hit"
    assert rule(run(wellness, events=[workout(70.0)]), "R05")["status"] == "clear"
    assert rule(run(wellness, events=[]), "R05")["status"] == "no_data"


def test_one_long_night_clears_the_sleep_rule_even_without_planned_if():
    wellness = make_wellness(sleep=lambda a: 5.5 if a == 1 else 7.0)
    assert rule(run(wellness, events=[]), "R05")["status"] == "clear"


def test_missing_sleep_with_no_long_night_is_no_data():
    wellness = make_wellness(sleep=lambda a: None)
    assert rule(run(wellness), "R05")["status"] == "no_data"


# --- yesterday's effort (RPE / Feel) ------------------------------------------------------
def test_rpe_8_with_poor_feel_is_a_trim():
    result = run(activities=[session(icu_rpe=8, feel=4)])
    assert rule(result, "R09")["status"] == "hit"
    assert result["numeric_verdict"] == "TRIM"


def test_good_feel_does_not_trigger_the_rule():
    # intervals.icu Feel: 1 = strong, 2 = good ... 5 = weak
    for feel in (1, 2, 3):
        result = run(activities=[session(icu_rpe=9, feel=feel)])
        assert rule(result, "R09")["status"] == "clear", feel


def test_rpe_below_threshold_is_clear_even_with_poor_feel():
    assert rule(run(activities=[session(icu_rpe=7, feel=5)]), "R09")["status"] == "clear"


def test_missing_feel_or_rpe_is_no_data():
    assert rule(run(activities=[session(feel=None)]), "R09")["status"] == "no_data"
    assert rule(run(activities=[session(icu_rpe=None)]), "R09")["status"] == "no_data"


def test_commute_bike_is_not_a_training_session_but_may_need_confirmation():
    result = run(activities=[session(device="Garmin Edge 530", icu_rpe=9, feel=5)])
    assert result["yesterday_session"] is None
    assert rule(result, "R09")["status"] == "clear"
    assert len(result["commute_confirmation_needed"]) == 1


def test_short_commute_needs_no_confirmation():
    short = session(device="Garmin Edge 530", moving_time=1200, icu_training_load=10)
    assert run(activities=[short])["commute_confirmation_needed"] == []


def test_zwift_counts_and_the_latest_yesterday_session_wins():
    early = session(device="ZWIFT", icu_rpe=9, feel=5, start_date_local=f"{YESTERDAY}T06:00:00")
    late = session(icu_rpe=4, feel=2, start_date_local=f"{YESTERDAY}T18:00:00")
    result = run(activities=[late, early])
    assert result["yesterday_session"]["rpe"] == 4
    assert rule(result, "R09")["status"] == "clear"


def test_utc_only_timestamp_is_assigned_to_the_jst_day():
    act = session(icu_rpe=8, feel=4)
    del act["start_date_local"]
    act["start_date"] = "2026-10-25T23:37:16Z"  # 08:37 JST on 10-26 = yesterday
    assert rule(run(activities=[act]), "R09")["status"] == "hit"


def test_session_older_than_yesterday_only_feeds_the_latest_session_field():
    old = session(day=(TARGET - timedelta(days=3)).isoformat())
    result = run(activities=[old])
    assert result["yesterday_session"] is None
    assert result["latest_session_7d"]["date"] == (TARGET - timedelta(days=3)).isoformat()


def test_unavailable_activities_make_the_rule_no_data():
    result = compute_daily_metrics(TARGET, make_wellness(), None, [workout()])
    assert rule(result, "R09")["status"] == "no_data"
    assert result["numeric_verdict"] is None


# --- verdict assembly -----------------------------------------------------------------
def test_all_clear_gives_go_and_lists_rules_needing_athlete_input():
    result = run()
    assert result["numeric_verdict"] == "GO"
    statuses = {r["id"]: r["status"] for r in result["rules"]}
    assert statuses["R01"] == statuses["R03"] == statuses["R10"] == "needs_input"
    assert any("R01" in reason for reason in result["provisional_reasons"])


def test_first_hit_in_table_order_wins():
    # STOP (R02) and SWAP (R06) both hit; STOP comes first in the table.
    wellness = make_wellness(
        ctl=lambda a: 40.0,
        atl=lambda a: 75.0,
        hrv=lambda a: 40.0 if a <= 7 else alt(a),
    )
    result = run(wellness)
    assert rule(result, "R06")["status"] == "hit"
    assert result["numeric_verdict"] == "STOP"


def test_no_verdict_when_a_rule_lacks_data_and_nothing_hit():
    result = run(make_wellness(hrv=lambda a: None))
    assert rule(result, "R06")["status"] == "no_data"
    assert result["numeric_verdict"] is None


def test_a_hit_is_reported_even_if_a_later_rule_lacks_data():
    wellness = make_wellness(ctl=lambda a: 40.0, atl=lambda a: 75.0, hrv=lambda a: None)
    assert run(wellness)["numeric_verdict"] == "STOP"


def test_stale_wellness_gives_no_verdict():
    stale = make_wellness(target=TARGET - timedelta(days=1))
    result = run(stale)
    assert result["wellness_status"]["status"] == "stale_or_missing"
    assert result["wellness_status"]["latest_available_date"] == YESTERDAY
    assert result["numeric_verdict"] is None


# --- observations -------------------------------------------------------------------------
def test_observations_motivation_ctl_rise_and_room_temp():
    wellness = make_wellness(ctl=lambda a: 44.0 - a)  # +7 over a week
    for ago in range(3):
        wellness[(TARGET - timedelta(days=ago)).isoformat()]["motivation"] = 4
    wellness[TARGET.isoformat()]["RoomTemp"] = 29.0
    obs = run(wellness)["observations"]
    assert obs["motivation_today"] == "4 (LOW)"
    assert obs["motivation_low_3_days"] is True
    assert obs["ctl_rise_7d"] == 7.0
    assert obs["ctl_rise_over_7"] is False  # exactly 7 is not "over 7"
    assert obs["room_temp_over_28"] is True


# --- replay-driven regressions (real account data, Oct 2026) ------------------------
def test_tsb_uses_end_of_previous_day_not_the_target_day_row():
    # The target day's row already carries that day's load; only the previous day counts.
    wellness = make_wellness(ctl=lambda a: 40.0, atl=lambda a: 70.0 if a == 0 else 50.0)
    result = run(wellness)
    assert result["wellness_today"]["tsb"] == -10.0
    assert result["wellness_today"]["tsb_target_day_row"] == -30.0
    assert result["wellness_today"]["tsb_basis_date"] == YESTERDAY
    assert rule(result, "R02")["status"] == "clear"


def test_trainerroad_device_counts_as_a_training_session():
    result = run(activities=[session(device="TRAINER_ROAD", icu_rpe=8, feel=4)])
    assert result["yesterday_session"]["device"] == "TRAINER_ROAD"
    assert rule(result, "R09")["status"] == "hit"


def test_short_edge_840_ride_is_a_commute_not_yesterdays_session():
    commute = session(
        icu_rpe=None, feel=None, icu_training_load=7, moving_time=976,
        start_date_local=f"{YESTERDAY}T15:19:00",
    )  # fmt: skip
    real = session(icu_rpe=4, feel=2, start_date_local=f"{YESTERDAY}T05:44:00")
    result = run(activities=[real, commute])
    assert result["yesterday_session"]["rpe"] == 4.0
    assert rule(result, "R09")["status"] == "clear"
    assert len(result["ignored_activities"]) == 1


def test_highest_load_session_of_the_day_is_the_yesterday_session():
    big = session(
        icu_rpe=8, feel=4, icu_training_load=118, start_date_local=f"{YESTERDAY}T05:44:00"
    )
    small = session(
        icu_rpe=2, feel=2, icu_training_load=49, start_date_local=f"{YESTERDAY}T20:53:00"
    )
    result = run(activities=[big, small])
    assert result["yesterday_session"]["load"] == 118
    assert rule(result, "R09")["status"] == "hit"
