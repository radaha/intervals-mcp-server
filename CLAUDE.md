# CLAUDE.md — Intervals.icu MCP Server

## Project Overview

This is a **Model Context Protocol (MCP) server** that connects Claude and other MCP clients to the [Intervals.icu](https://intervals.icu) training platform API. It exposes MCP tools for reading and managing athlete data: activities, events, workouts, wellness metrics, and more.

- **Language**: Python 3.12+
- **Framework**: FastMCP (the standalone `fastmcp` package, not the `mcp` SDK's bundled `mcp.server.fastmcp.FastMCP` — required for FastMCP Cloud deployment). `mcp` (>=2.0,<3.0) is still a direct dependency for the OAuth authorization-server protocol used by `auth.py`.
- **HTTP client**: `httpx` (async)
- **Package manager**: `uv`
- **Linter/formatter**: `ruff`
- **Type checker**: `mypy`
- **Test runner**: `pytest` + `pytest-asyncio` + `pytest-mock`

---

## Repository Layout

```
src/intervals_mcp_server/
server.py          # Entry point; imports & re-exports all tools and resources
mcp_instance.py    # Shared FastMCP singleton (import this to register tools)
server_setup.py    # Transport selection & server startup logic
config.py          # Config dataclass + singleton loaded from env vars
api/
  client.py        # make_intervals_request() — all HTTP calls go here
tools/
  activities.py    # get_activities, get_activity_details, get_activity_intervals,
                   # get_activity_streams, get_activity_histogram, get_activity_messages,
                   # add_activity_message
  events.py        # get_events, get_races, get_event_by_id, add_or_update_event,
                   # delete_event, delete_events_by_date_range
  wellness.py      # get_wellness_data
  daily_metrics.py # get_daily_metrics (fetches data; arithmetic is in utils/coach_metrics.py)
  athlete.py       # get_athlete_zones
  power_curves.py  # get_athlete_power_curves
  training_summary.py # get_training_summary
  custom_items.py  # get_custom_items, get_custom_item_by_id, create_custom_item,
                   # update_custom_item, delete_custom_item
  workout_library.py  # get_workout_folders, list_workouts, get_workout,
                      # create_workout, update_workout, schedule_workout
resources/
  guide.py         # intervals-icu://guide MCP resource — usage guide for LLMs
utils/
  formatting.py    # Data formatting helpers (format_activity_summary, etc.)
  validation.py    # Input validation (athlete ID, dates, activity type)
  dates.py         # Date range utilities
  types.py         # Dataclasses & enums: Value, Step, WorkoutDoc, TransportAliases, etc.
  coach_metrics.py # Pure (no I/O) daily-condition rule engine behind get_daily_metrics

tests/
test_server.py                 # Main tool tests (monkeypatched API calls)
test_activities_date_filter.py # Date filter logic tests
test_add_or_update_event.py    # Event creation tests
test_formatting.py             # Formatting utility tests
test_make_intervals_request.py # API client tests
test_training_summary.py       # Training summary tests
test_validation.py             # Validation function tests
test_value.py                  # Value dataclass tests
test_workout_library.py        # Workout library tool tests
test_guide_resource.py         # MCP resource tests
test_tool_annotations.py       # Tool annotation tests
test_server_config.py          # Server config tests
sample_data.py                 # Shared mock data for tests
ressources/                    # Static test fixtures (JSON, text files)
```

---

## Development Environment

```bash
# Create and activate the virtual environment
uv venv --python 3.12
source .venv/bin/activate

# Install all dependencies including dev extras
uv sync --all-extras

# Copy env template and fill in your credentials
cp .env.example .env
# Edit .env: set API_KEY and ATHLETE_ID
```

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `API_KEY` | Yes | Your Intervals.icu API key |
| `ATHLETE_ID` | Yes | Your athlete ID: digits (e.g. `123456`) or `i`-prefixed (e.g. `i123456`) |
| `INTERVALS_API_BASE_URL` | No | Defaults to `https://intervals.icu/api/v1` |
| `MCP_TRANSPORT` | No | `stdio` (default), `sse`, `http`, or `streamable-http` |
| `FASTMCP_HOST` | No | Bind host for HTTP transport (default `127.0.0.1`) |
| `MCP_CLIENT_ID` | No | OAuth client ID. Must match what is entered in Claude.ai's connector settings. |
| `MCP_CLIENT_SECRET` | No | OAuth client secret / access token. Must match what is entered in Claude.ai's connector settings. |
| `MCP_SERVER_URL` | No | Public HTTPS URL of this server (default `http://localhost:8000`). Required for OAuth discovery when `MCP_CLIENT_ID`/`MCP_CLIENT_SECRET` are set. |

When `MCP_CLIENT_ID` and `MCP_CLIENT_SECRET` are both set the server enables OAuth 2.0 authorization code + PKCE protection on all HTTP endpoints. FastMCP publishes `/.well-known/oauth-authorization-server` so Claude.ai can auto-discover the token endpoint. stdio transport is unaffected.

---

## Running the Server

```bash
# Run via MCP CLI (stdio transport, recommended for development)
mcp run src/intervals_mcp_server/server.py

# Run directly
python src/intervals_mcp_server/server.py
```

### Docker

```bash
docker build -t intervals-mcp-server .
docker run -e API_KEY=... -e ATHLETE_ID=... intervals-mcp-server
```

---

## Code Quality — Required Before Every Commit

All three checks must pass:

```bash
ruff check .          # Linting (auto-fix available with --fix)
ruff format .         # Formatting
mypy src tests        # Static type checking
pytest                # Unit tests
```

Pre-commit hooks (`.pre-commit-config.yaml`) enforce ruff lint, ruff format, typo checking, and run pytest on `git push`. Install them with:

```bash
pip install pre-commit
pre-commit install && pre-commit install -t pre-push
```

---

## Key Architectural Patterns

### Tool Registration

Tools register themselves automatically via `@mcp.tool()` decorators when the module is imported. Each tool module imports the shared `mcp` singleton from `mcp_instance.py`:

```python
from intervals_mcp_server.mcp_instance import mcp

@mcp.tool(annotations=ToolAnnotations(title="...", readOnlyHint=True, destructiveHint=False))
async def my_tool(...) -> str:
  ...
```

`server.py` imports all tool modules to trigger registration, then re-exports the functions in `__all__` for test compatibility.

### All API Calls via `make_intervals_request()`

Every call to the Intervals.icu API goes through `api/client.py:make_intervals_request()`. Never use `httpx` directly in tool modules.

```python
result = await make_intervals_request(
    url=f"/athlete/{athlete_id}/activities",
    api_key=api_key,  # falls back to config.api_key if empty
    params={"oldest": start_date, "newest": end_date},
    method="GET",  # or "POST" / "PUT"
    data={"key": "value"},  # body for POST/PUT
)
```

Error responses always return `{"error": True, "message": "..."}`. Always check before using the result:

```python
if isinstance(result, dict) and "error" in result:
    return f"Error: {result.get('message', 'Unknown error')}"
```

### Configuration Singleton

Config is loaded once and cached in `config.py`:

```python
from intervals_mcp_server.config import get_config

config = get_config()
```

### WorkoutDoc / Step / Value Types

Structured workout data uses dataclasses in `utils/types.py`:
- `WorkoutDoc` — full workout with description, steps, zones, targets
- `Step` — individual workout step (duration/distance, intensity, power/HR/pace/cadence targets, repeats)
- `Value` — intensity target with units (`ValueUnits` enum) and optional range (`start`/`end`)

All three support `to_dict()` / `from_dict()` / `to_json()` / `from_json()` round-trips.

---

## Adding a New MCP Tool

1. Create the function in the appropriate module under `src/intervals_mcp_server/tools/` (or create a new module).
2. Decorate with `@mcp.tool(annotations=ToolAnnotations(...))`.
3. Use `resolve_athlete_id()` and `resolve_date_params()` from `utils/validation.py` for standard parameter handling.
4. Call `make_intervals_request()` for all API communication.
5. Return a formatted string (tools return `str`).
6. Import and re-export the function in `server.py` (`__all__` list).
7. Write tests in `tests/` — mock the HTTP client, test both success and error paths.

---

## Testing Conventions

- All tool functions are `async`; tests use `@pytest.mark.asyncio`.
- Mock HTTP by monkeypatching `make_intervals_request` or the `httpx_client`:
```python
import asyncio

monkeypatch.setattr(
    "intervals_mcp_server.server.make_intervals_request",
    lambda *a, **kw: asyncio.coroutine(lambda: mock_data)(),
)
```
- Set `API_KEY` and `ATHLETE_ID` env vars at the top of each test file before importing server modules:
```python
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")
```
- Shared realistic mock data lives in `tests/sample_data.py`.
- Test files are named `test_*.py`; test functions are named `test_*`.

---

## PR / Commit Conventions

- Commit messages: concise, imperative (`Add get_races tool`, `Fix date filter edge case`).
- PR title format: `[intervals-mcp-server] <brief description>`.
- PR description should mention whether `ruff`, `mypy`, and `pytest` passed, and any manual testing steps.
- Direct commits to `main` are blocked by pre-commit hook.

---

## MCP Resource

The server exposes one MCP resource at `intervals-icu://guide` (registered in `resources/guide.py`). It returns a plain-text usage guide describing key concepts (Activities vs Events vs Wellness), metric definitions (CTL, ATL, TSB), and recommended tool call sequences for common coaching workflows. LLM clients should load this resource at the start of coaching conversations.

## Operational Notes / Gotchas

### Structured workouts must be builder DSL
Intervals.icu parses/computes/renders steps only when sent as workout-builder DSL text in `description`. A raw `workout_doc` JSON posted to `/workouts` or `/events` is stored but never rendered (empty chart, null metrics); sending both, the raw
doc wins. Always emit steps via `str(WorkoutDoc)` into `description`. `_power`/`_pace` are resolved OUTPUT fields, never inputs.

### OAuth / HTTP transport
Activates only when `MCP_CLIENT_ID` + `MCP_CLIENT_SECRET` are set (`auth.py`, `mcp_instance.py`). `SingleClientOAuthProvider` subclasses `fastmcp.server.auth.OAuthProvider`, which itself subclasses `mcp.server.auth.provider.OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]` (all from `mcp.server.auth.provider`/`fastmcp.server.auth`, fixed generic params — do not
swap in ad hoc dataclasses for those three types or mypy's LSP check fails). Gotchas: IDs/secret must match the connector exactly (case-sensitive) and the connector URL must end in `/mcp`; `resource_base_url` must be set on the provider
(RFC 9728 metadata); the client must set `token_endpoint_auth_method="client_secret_post"` (mcp >= 1.23 defaults to None → 401 "Unsupported auth method"); `validate_scope` accepts empty scope; `mcp` is pinned `>=2.0,<3.0` (bumped from `<2.0` for
the `fastmcp` migration — the OAuth flow has been tested end-to-end against this line, see `git log auth.py`) so test OAuth against the latest SDK in that range before bumping further. FastMCP Cloud's scaling model is undocumented; the
in-memory authorization-code store in `auth.py` assumes a single long-lived process, so verify the full connector OAuth flow manually after any redeploy.

**FastMCP Cloud specifically:** confirmed by hands-on testing, not just docs — Horizon puts its own OAuth gateway (dashboard: Server → Access → Authentication → "Horizon Authentication") in front of every deployment, and it **cannot be disabled on the free tier** (requires a paid plan). That gateway serves `/oauth2/authorize`, `/oauth2/token`, `/oauth2/register` and fully supersedes this app's `/authorize`/`/token` routes, so `MCP_CLIENT_ID`/`MCP_CLIENT_SECRET`/`MCP_SERVER_URL` set as env vars on FastMCP Cloud are never reached by `SingleClientOAuthProvider` — don't set them there. Connectors must use DCR against Horizon's `registration_endpoint` instead of a fixed client id; see README's "Securing the endpoint on FastMCP Cloud". This app's own OAuth (`MCP_CLIENT_ID`/`MCP_CLIENT_SECRET`, the "own OAuth client" connector flow) only applies on Render/self-hosted, where there's no gateway in front.
