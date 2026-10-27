# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `get_daily_metrics` tool (read-only): computes the numeric rules of a daily
  GO/TRIM/SWAP/STOP decision table in code and returns per-rule evidence
  (`hit` / `clear` / `no_data` / `needs_input`) with a provisional numeric verdict.
  Arithmetic lives in `utils/coach_metrics.py`; day boundaries are JST, TSB is the
  previous day's end value, and Feel uses the 1 = best scale.
- OAuth 2.0 (authorization code + PKCE) for the HTTP transport. Optional; activates
  only when `MCP_CLIENT_ID` and `MCP_CLIENT_SECRET` are set (`MCP_SERVER_URL` required
  for discovery). stdio transport is unaffected.
- Activity write tools: `update_activity`, `delete_activity`, `create_manual_activity`,
  `bulk_create_manual_activities`; `"gap"` histogram type.
- Activity analysis tools: `get_activity_curve`, `get_activity_best_efforts`,
  `get_activity_segments`, `get_activity_interval_stats`, `get_activity_map`,
  `get_activity_power_vs_hr`, `get_activity_hr_load_model`,
  `get_activity_power_spike_model`, `get_activity_time_at_hr`,
  `get_activity_weather_summary`.
- Activity search tools: `search_activities`, `interval_search`,
  `get_activities_around`, `get_activities_by_ids`, `get_activity_tags`.
- Activity interval editing tools: `update_activity_intervals`,
  `update_activity_interval`, `delete_activity_intervals`, `split_activity_interval`.
- Training plan tools: `get_training_plan`, `change_training_plan`,
  `apply_plan_changes`, `apply_plan_to_calendar`, `change_athlete_plans_bulk`.

### Fixed

- Structured workouts created via `create_workout`/`update_workout`/`schedule_workout`
  now render in Intervals.icu: steps are sent as workout-builder DSL text in
  `description` (a raw `workout_doc` JSON is stored but never parsed/rendered).
- OAuth token exchange on mcp >= 1.23 (set `token_endpoint_auth_method`), protected
  resource metadata (RFC 9728), and acceptance of an empty `scope=` from clients.

## [0.1.0] - 2025-06-01

### Added

- Initial release of Intervals.icu MCP Server.
- Activity tools: `get_activities`, `get_activity_details`, `get_activity_intervals`,
  `get_activity_streams`, `get_activity_histogram`, `get_activity_messages`,
  `add_activity_message`.
- Event tools: `get_events`, `get_event_by_id`, `add_or_update_event`, `delete_event`,
  `delete_events_by_date_range`.
- Wellness & training tools: `get_wellness_data`, `get_training_summary`,
  `get_athlete_power_curves`, `get_athlete_zones`.
- Custom-item tools: `get_custom_items`, `get_custom_item_by_id`, `create_custom_item`,
  `update_custom_item`, `delete_custom_item`.
- Docker support with Render deployment guide.
- Local setup via `uv` and `mcp` CLI.
