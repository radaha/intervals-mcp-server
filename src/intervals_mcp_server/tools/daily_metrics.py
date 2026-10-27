"""
Daily-condition metrics tool for Intervals.icu.

``get_daily_metrics`` fetches wellness, recent activities and the day's planned
events, then hands them to the pure functions in ``utils/coach_metrics.py``. All
arithmetic happens there; the LLM only interprets the returned evidence.
"""

import asyncio
import json
from datetime import date, datetime, timedelta
from typing import Any

from mcp.types import ToolAnnotations

from intervals_mcp_server.api.client import make_intervals_request
from intervals_mcp_server.config import get_config
from intervals_mcp_server.utils.coach_metrics import (
    BASELINE_DAYS,
    HIGH_INTENSITY_IF_PCT,
    JST,
    RECENT_DAYS,
    compute_daily_metrics,
)
from intervals_mcp_server.utils.validation import resolve_athlete_id, validate_date

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import mcp  # noqa: F401

config = get_config()

# The 7-day-average rules need a 28-day baseline that ends before the 7-day window,
# evaluated for today and yesterday: 1 + 7 + 28 days back from yesterday.
WELLNESS_LOOKBACK_DAYS = RECENT_DAYS + BASELINE_DAYS + 1
ACTIVITY_LOOKBACK_DAYS = RECENT_DAYS + 1
ACTIVITY_LIMIT = 200


def _error_payload(message: str, target: str = "") -> str:
    """JSON error: no verdict is ever produced from partial wellness data."""
    return json.dumps(
        {"date": target or None, "error": message, "numeric_verdict": None},
        ensure_ascii=False,
    )


def _wellness_by_date(result: Any) -> dict[str, dict[str, Any]]:
    """Index wellness rows (list or date-keyed dict) by ISO date."""
    rows: list[dict[str, Any]] = []
    if isinstance(result, dict):
        for date_str, data in result.items():
            if isinstance(data, dict):
                rows.append({**data, "id": data.get("id") or data.get("date") or date_str})
    elif isinstance(result, list):
        rows = [r for r in result if isinstance(r, dict)]
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("id") or row.get("date")
        if isinstance(key, str) and len(key) >= 10:
            indexed[key[:10]] = row
    return indexed


def _as_dict_list(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    return []


def _error_message(result: Any) -> str | None:
    """Message of an API error response, or None if the call succeeded."""
    if isinstance(result, dict) and "error" in result:
        return str(result.get("message") or "Unknown error")
    return None


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Daily Metrics", read_only_hint=True, destructive_hint=False
    )
)
async def get_daily_metrics(
    athlete_id: str = "",
    api_key: str = "",
    target_date: str = "",
    high_intensity_if_pct: float = HIGH_INTENSITY_IF_PCT,
) -> str:
    """Evaluate the numeric rows of the daily GO/TRIM/SWAP/STOP decision table.

    Returns JSON with, for every rule, its inputs, threshold and status (hit / clear /
    no_data / needs_input), plus a provisional ``numeric_verdict`` (first hit in table
    order). Rules that depend on what only the athlete can tell (symptoms, calendar)
    are reported as needs_input and are not evaluated. All numbers are computed in
    code: quote them as returned, do not recompute them.

    Day boundaries use Japan Standard Time (UTC+9). TSB is CTL - ATL at the end of the
    previous day, because the target day's own row already includes that day's load.
    A high-intensity day is a planned workout whose IF (icu_intensity, percent) is at
    or above ``high_intensity_if_pct``. Feel uses the intervals.icu scale where 1 is
    the best, so poor means >= 4. If the wellness row for the date is missing, no
    verdict is returned.

    Args:
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        target_date: Day to evaluate in YYYY-MM-DD format (optional, defaults to today in JST)
        high_intensity_if_pct: IF threshold in percent for a high-intensity day (optional, defaults to 85)
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
        "newest": target,
        "limit": ACTIVITY_LIMIT,
    }
    event_params = {"oldest": target, "newest": target}

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
    activities = None if activities_error else _as_dict_list(activities_result)
    events = None if events_error else _as_dict_list(events_result)

    metrics = compute_daily_metrics(
        day,
        _wellness_by_date(wellness_result),
        activities,
        events,
        high_intensity_if_pct=high_intensity_if_pct,
    )
    data_errors: dict[str, str] = {}
    if activities_error:
        data_errors["activities"] = activities_error
    if events_error:
        data_errors["events"] = events_error
    if data_errors:
        metrics["data_errors"] = data_errors
    return json.dumps(metrics, ensure_ascii=False)
