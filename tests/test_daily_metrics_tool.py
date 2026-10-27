"""
Tests for the get_daily_metrics MCP tool (API fetching and JSON assembly).

The rule arithmetic itself is covered in test_coach_metrics.py; here the HTTP layer
is mocked and only the wiring is checked.
"""

import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.server import get_daily_metrics  # noqa: E402
from intervals_mcp_server.tools.daily_metrics import _wellness_by_date  # noqa: E402
from tests.test_coach_metrics import (  # noqa: E402
    TARGET,
    YESTERDAY,
    make_wellness,
    session,
    workout,
)


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
        "intervals_mcp_server.tools.daily_metrics.make_intervals_request", fake_request
    )
    return calls


def _call(**kwargs) -> dict:
    return json.loads(asyncio.run(get_daily_metrics(target_date=TARGET.isoformat(), **kwargs)))


def test_returns_json_with_all_rules_and_a_verdict(monkeypatch):
    calls = _patch(
        monkeypatch,
        wellness=list(make_wellness().values()),
        activities=[session(icu_rpe=4, feel=2)],
        events=[workout(60.0)],
    )
    result = _call()
    assert result["date"] == "2026-10-27"
    assert result["numeric_verdict"] == "GO"
    assert [r["id"] for r in result["rules"]] == [f"R{i:02d}" for i in range(1, 11)]
    assert result["yesterday_session"]["date"] == YESTERDAY

    urls = {url.rsplit("/", 1)[-1]: params for url, params in calls}
    assert urls["wellness"]["newest"] == "2026-10-27"
    assert urls["wellness"]["oldest"] <= "2026-09-21"  # reaches back far enough for baselines
    assert urls["events"] == {"oldest": "2026-10-27", "newest": "2026-10-27"}


def test_accepts_date_keyed_wellness_dict(monkeypatch):
    _patch(monkeypatch, wellness=make_wellness(), events=[workout(60.0)])
    assert _call()["numeric_verdict"] == "GO"


def test_wellness_by_date_handles_list_and_dict():
    rows = make_wellness(days=3)
    assert set(_wellness_by_date(list(rows.values()))) == set(rows)
    assert set(_wellness_by_date(rows)) == set(rows)
    assert _wellness_by_date("nonsense") == {}


def test_wellness_error_yields_no_verdict(monkeypatch):
    _patch(monkeypatch, wellness={"error": True, "message": "boom"})
    result = _call()
    assert result["numeric_verdict"] is None
    assert "boom" in result["error"]


def test_activity_error_is_reported_and_blocks_the_verdict(monkeypatch):
    async def fake_request(*_args, **kwargs):
        if kwargs["url"].endswith("/wellness"):
            return list(make_wellness().values())
        if kwargs["url"].endswith("/activities"):
            return {"error": True, "message": "activities down"}
        return [workout(60.0)]

    monkeypatch.setattr(
        "intervals_mcp_server.tools.daily_metrics.make_intervals_request", fake_request
    )
    result = _call()
    assert result["data_errors"] == {"activities": "activities down"}
    assert result["numeric_verdict"] is None


def test_invalid_date_is_rejected():
    result = json.loads(asyncio.run(get_daily_metrics(target_date="2026/10/27")))
    assert result["numeric_verdict"] is None
    assert "YYYY-MM-DD" in result["error"]


def test_high_intensity_threshold_can_be_overridden(monkeypatch):
    wellness = make_wellness(ctl=lambda a: 40.0, atl=lambda a: 65.2)  # TSB -25.2
    _patch(monkeypatch, wellness=list(wellness.values()), events=[workout(80.0)])
    default = _call()  # IF 80 is below the default threshold of 85
    assert default["high_intensity_day"] is False
    assert default["numeric_verdict"] == "GO"
    result = _call(high_intensity_if_pct=75.0)
    assert result["high_intensity_day"] is True
    assert result["numeric_verdict"] == "TRIM"
