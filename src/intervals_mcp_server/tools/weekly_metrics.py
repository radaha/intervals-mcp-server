"""
Weekly load-progression metrics tool for Intervals.icu.

``get_weekly_progression_metrics`` fetches wellness, two weeks of activities and the
week's calendar events, then hands them to the pure functions in
``utils/weekly_metrics.py``. All arithmetic happens there; the LLM only interprets
the returned evidence and handles what needs the athlete's input.
"""

import asyncio
import json
from datetime import date, datetime, timedelta

from mcp.types import ToolAnnotations

from intervals_mcp_server.api.client import make_intervals_request
from intervals_mcp_server.config import get_config
from intervals_mcp_server.tools.daily_metrics import (
    WELLNESS_LOOKBACK_DAYS,
    _as_dict_list,
    _error_message,
    _error_payload,
    _wellness_by_date,
)
from intervals_mcp_server.utils.coach_metrics import JST, RECENT_DAYS
from intervals_mcp_server.utils.validation import resolve_athlete_id, validate_date
from intervals_mcp_server.utils.weekly_metrics import compute_weekly_progression

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import mcp  # noqa: F401

config = get_config()

# Completed week plus the week before it (for the weekly TSS comparison).
ACTIVITY_LOOKBACK_DAYS = 2 * RECENT_DAYS
ACTIVITY_LIMIT = 400


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Weekly Progression Metrics", read_only_hint=True, destructive_hint=False
    )
)
async def get_weekly_progression_metrics(
    athlete_id: str = "",
    api_key: str = "",
    target_date: str = "",
) -> str:
    """Evaluate the numeric criteria a-f of the weekly load-increase rule (増量判定).

    Returns JSON with each criterion's inputs, threshold and status (pass / fail /
    no_data), the provisional verdict (INCREASE_OK = 増量可, HOLD = 据え置き,
    REDUCE = 抑制), the week's decision log read from the [LOG] notes, weekly TSS and
    CTL rise against the guideline numbers, and a ``needs_input`` list of what only the
    athlete can decide (illness watch, previous weeks' increases, TrainerRoad block
    structure, approval). All numbers are computed in code: quote them as returned, do
    not recompute them.

    Day boundaries use Japan Standard Time (UTC+9). The completed week is the 7 days
    ending the day before the review day. Run it on the review day (Sunday).

    Args:
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        target_date: Review day in YYYY-MM-DD format (optional, defaults to today in JST)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return _error_payload(error_msg)

    if target_date:
        try:
            day = date.fromisoformat(validate_date(target_date))
        except ValueError as exc:
            return _error_payload(str(exc))
    else:
        day = datetime.now(JST).date()
    target = day.isoformat()

    base = f"/athlete/{athlete_id_to_use}"
    wellness_params = {
        "oldest": (day - timedelta(days=WELLNESS_LOOKBACK_DAYS)).isoformat(),
        "newest": target,
    }
    activity_params: dict[str, str | int] = {
        "oldest": (day - timedelta(days=ACTIVITY_LOOKBACK_DAYS)).isoformat(),
        "newest": (day - timedelta(days=1)).isoformat(),
        "limit": ACTIVITY_LIMIT,
    }
    event_params = {
        "oldest": (day - timedelta(days=RECENT_DAYS)).isoformat(),
        "newest": target,
    }

    wellness_result, activities_result, events_result = await asyncio.gather(
        make_intervals_request(url=f"{base}/wellness", api_key=api_key, params=wellness_params),
        make_intervals_request(url=f"{base}/activities", api_key=api_key, params=activity_params),
        make_intervals_request(url=f"{base}/events", api_key=api_key, params=event_params),
    )

    wellness_error = _error_message(wellness_result)
    if wellness_error:
        return _error_payload(f"Error fetching wellness data: {wellness_error}", target)

    activities_error = _error_message(activities_result)
    events_error = _error_message(events_result)
    metrics = compute_weekly_progression(
        day,
        _wellness_by_date(wellness_result),
        None if activities_error else _as_dict_list(activities_result),
        None if events_error else _as_dict_list(events_result),
    )
    data_errors: dict[str, str] = {}
    if activities_error:
        data_errors["activities"] = activities_error
    if events_error:
        data_errors["events"] = events_error
    if data_errors:
        metrics["data_errors"] = data_errors
    return json.dumps(metrics, ensure_ascii=False)
