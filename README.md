# Intervals.icu MCP Server

Model Context Protocol (MCP) server for connecting Claude and ChatGPT with the Intervals.icu API. It provides tools for retrieving activities, events, wellness data, power curves, and more.

If you find the MCP server useful, please consider supporting its continued development with a donation.

## Prerequisites

Before you begin you'll need your Intervals.icu credentials:

1. **API Key** — Log in to [Intervals.icu](https://intervals.icu), go to **Settings → API**, and generate a new API key.
2. **Athlete ID** — Visible in the URL when you're logged in, e.g. `https://intervals.icu/athlete/i12345/...` → `i12345`.

## Setup — Deploy to FastMCP Cloud

1. Sign in at [horizon.prefect.io](https://horizon.prefect.io) and connect your GitHub account.
2. Create a new server from your `intervals-mcp-server` fork/repo (branch `develop`). This repo's [`fastmcp.json`](fastmcp.json) already points it at the entrypoint (`src/intervals_mcp_server/server.py`) and dependencies (`pyproject.toml`/`uv.lock`) — leave **Requirements** blank in the deploy form.
3. Set environment variables:

   | Key | Value |
   |-----|-------|
   | `ATHLETE_ID` | Your Intervals.icu athlete ID (e.g. `i12345`) |
   | `API_KEY` | Your Intervals.icu API key |

   Don't set `MCP_CLIENT_ID`/`MCP_CLIENT_SECRET`/`MCP_SERVER_URL`/`MCP_TRANSPORT` — see note below.
4. Deploy. Your server URL is `https://<server-name>.fastmcp.app/mcp`.

<details>
<summary>OAuth details & troubleshooting</summary>

FastMCP Cloud puts its own OAuth gateway ("Horizon Authentication") in front of every deployment; on the free tier it can't be disabled. It fully replaces this app's own `SingleClientOAuthProvider`, so `MCP_CLIENT_ID`/`MCP_CLIENT_SECRET`/`MCP_SERVER_URL` are never reached and shouldn't be set here — that env-var trio is only for genuinely self-hosting this app elsewhere (see `auth.py`).

When connecting a client, use **"Register automatically" (DCR)** for Client OAuth, not "use your own OAuth client" — you'll be prompted to log into your Horizon account, which is the actual access gate on this platform.

**Connector shows no tools, or tool calls return 403 / JSON-RPC `-32603`:** almost always leftover `MCP_CLIENT_ID`/`MCP_CLIENT_SECRET`/`MCP_SERVER_URL` env vars causing a double auth layer (Horizon's gateway forwards its own token, which this app's own OAuth then rejects). Delete those three vars, redeploy, reconnect.

</details>

## Connecting Claude

1. Open Claude → **Settings** → **Integrations** (or **MCP Servers**) → **Add**
2. **Name:** `Intervals.icu`, **URL:** `https://<server-name>.fastmcp.app/mcp`
3. For Client OAuth, pick **"Register automatically" (DCR)** and log in with your Horizon account when prompted

Open a new conversation and ask "What MCP tools do you have available?" to confirm the connection.

## Connecting ChatGPT

1. In ChatGPT, open **Settings → Features → Custom MCP Connectors** → **Add**
2. Fill in:
   - **Name**: `Intervals.icu`
   - **MCP Server URL**: `https://your-server-name.fastmcp.app/mcp`

Save the connector and open a new chat.

## Available Tools

Once connected, the following tools are available:

**Activities**
- `get_activities` — Retrieve a list of activities
- `get_activity_details` — Get detailed information for a specific activity
- `get_activity_intervals` — Get interval data for a specific activity
- `get_activity_streams` — Get time-series stream data (power, HR, cadence, etc.)
- `get_activity_histogram` — Get a power, heart rate, pace, or gap histogram
- `get_activity_messages` — Get messages/comments on an activity
- `add_activity_message` — Add a message/comment to an activity
- `update_activity`, `delete_activity`, `create_manual_activity`, `bulk_create_manual_activities` — Edit, remove, or create activities

**Activity analysis**
- `get_activity_curve`, `get_activity_best_efforts`, `get_activity_segments`, `get_activity_interval_stats`, `get_activity_map`, `get_activity_power_vs_hr`, `get_activity_hr_load_model`, `get_activity_power_spike_model`, `get_activity_time_at_hr`, `get_activity_weather_summary`

**Activity search & interval editing**
- `search_activities`, `interval_search`, `get_activities_around`, `get_activities_by_ids`, `get_activity_tags`
- `update_activity_intervals`, `update_activity_interval`, `delete_activity_intervals`, `split_activity_interval`

**Events**
- `get_events` — Retrieve upcoming events (workouts, races, etc.)
- `get_event_by_id` — Get detailed information for a specific event
- `add_or_update_event` — Create or update an event
- `delete_event` — Delete a specific event
- `delete_events_by_date_range` — Delete events within a date range

**Wellness & Training**
- `get_wellness_data` — Fetch wellness data
- `get_daily_metrics` — Evaluate the numeric rows of a daily GO/TRIM/SWAP/STOP decision table in code (TSB, HRV/RHR trends, sleep, yesterday's RPE/Feel) and return per-rule evidence (read-only)
- `get_training_summary` — Get a training load summary
- `get_athlete_power_curves` — Get best power output curves for selected durations and time periods
- `get_athlete_zones` — Get athlete training zones (power, HR, pace, etc.)

**Training Plans**
- `get_training_plan`, `change_training_plan`, `apply_plan_changes`, `apply_plan_to_calendar`, `change_athlete_plans_bulk`

**Custom Items**
- `get_custom_items` — List custom items
- `get_custom_item_by_id` — Get a specific custom item
- `create_custom_item` — Create a new custom item
- `update_custom_item` — Update an existing custom item
- `delete_custom_item` — Delete a custom item

**Workout Library**
- `get_workout_folders` — Get workout library folder metadata (IDs, names, types)
- `list_workouts` — List workouts in the library, optionally filtered by folder
- `get_workout` — Get full workout detail including step-by-step structure
- `create_workout` — Create a new workout in a library folder
- `update_workout` — Update an existing library workout
- `schedule_workout` — Schedule a library workout onto the calendar

> **Structured workouts:** pass steps via `workout_doc`. The server renders them to Intervals.icu workout-builder text so the platform parses them and draws the step chart. Use renderable target units — power `%ftp`/`w`, HR `%hr`/`%lthr`, pace `%pace` or absolute pace (e.g. `4:30/km`); avoid `pace_zone`/`power_zone` for pace runs.

---

<details>
<summary><strong>Local Setup (alternative)</strong></summary>

If you prefer to run the server on your own machine instead of FastMCP Cloud, follow the steps below.

### Requirements

- Python 3.12 or higher
- [uv](https://github.com/astral-sh/uv) (recommended package manager)

### 1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Clone and install

```bash
git clone https://github.com/mvilanova/intervals-mcp-server.git
cd intervals-mcp-server
uv venv --python 3.12
source .venv/bin/activate   # Windows: .venv\Scripts\activate
uv sync
```

### 3. Set up environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in your credentials:

```
API_KEY=your_intervals_api_key_here
ATHLETE_ID=your_athlete_id_here
```

### Configure Claude Desktop

1. From the project directory, run:

   ```bash
   mcp install src/intervals_mcp_server/server.py --name "Intervals.icu" --with-editable . --env-file .env
   ```

2. Your `claude_desktop_config.json` should look like:

   ```json
   {
     "mcpServers": {
       "Intervals.icu": {
         "command": "/Users/<USERNAME>/.cargo/bin/uv",
         "args": [
           "run",
           "--with", "mcp[cli]",
           "--with-editable", "/path/to/intervals-mcp-server",
           "mcp", "run",
           "/path/to/intervals-mcp-server/src/intervals_mcp_server/server.py"
         ],
         "env": {
           "INTERVALS_API_BASE_URL": "https://intervals.icu/api/v1",
           "ATHLETE_ID": "<YOUR_ATHLETE_ID>",
           "API_KEY": "<YOUR_API_KEY>",
           "LOG_LEVEL": "INFO"
         }
       }
     }
   }
   ```

   Replace `/path/to/` with the actual path. If you see `spawn uv ENOENT` errors, use the full path from `which uv`.

3. Restart Claude Desktop.

### Configure ChatGPT (local)

1. Start the server in HTTP mode:

   ```bash
   export FASTMCP_HOST=127.0.0.1 FASTMCP_PORT=8765 MCP_TRANSPORT=http FASTMCP_LOG_LEVEL=INFO
   python src/intervals_mcp_server/server.py
   ```

2. ChatGPT needs a public URL, so forward the port (e.g. `ngrok http 8765`).

3. In ChatGPT, open **Settings → Features → Custom MCP Connectors** → **Add**:
   - **Name**: `Intervals.icu`
   - **MCP Server URL**: `https://<your-public-host>/mcp`

### Updating

```bash
git checkout main && git pull
source .venv/bin/activate
uv sync
```

If Claude Desktop fails after an update, delete the entry in `claude_desktop_config.json` and re-run the `mcp install` command above.

### Enabling debug logging

Modify `claude_desktop_config.json` to redirect stderr to a log file:

```json
{
  "mcpServers": {
    "Intervals.icu": {
      "command": "/bin/bash",
      "args": [
        "-c",
        "/Users/<USERNAME>/.local/bin/uv run --with 'mcp[cli]' --with-editable /path/to/intervals-mcp-server mcp run /path/to/intervals-mcp-server/src/intervals_mcp_server/server.py 2>> /path/to/intervals-mcp-server/mcp-server.log"
      ],
      "env": {
        "INTERVALS_API_BASE_URL": "https://intervals.icu/api/v1",
        "ATHLETE_ID": "<YOUR_ATHLETE_ID>",
        "API_KEY": "<YOUR_API_KEY>",
        "LOG_LEVEL": "INFO"
      }
    }
  }
}
```

Then tail the log:

```bash
tail -f /path/to/intervals-mcp-server/mcp-server.log
```

</details>

## Development and testing

Install development dependencies and run the test suite with:

```bash
uv sync --all-extras
pytest -v tests
```

### Running the server locally

```bash
mcp run src/intervals_mcp_server/server.py
```

## License

The GNU General Public License v3.0
