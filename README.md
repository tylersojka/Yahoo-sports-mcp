# yahoo-fantasy-mcp

An MCP server that lets Claude run your Yahoo Fantasy Football team. It can read your
roster, matchups and the waiver wire, and it can **set your lineup, add/drop players,
put in FAAB waiver claims and propose or answer trades**.

## Tools

| Read | Write |
| --- | --- |
| `get_my_teams` – your leagues and team keys | `set_lineup` – move players between slots / bench |
| `get_league_settings` – scoring, roster slots, waiver & FAAB rules | `add_player` – free-agent add, add/drop, or waiver claim with FAAB bid |
| `get_standings` | `drop_player` |
| `get_roster` – lineup slots, injuries, byes | `propose_trade` |
| `get_matchup` – actual + projected points, win probability | `respond_to_trade` – accept / reject |
| `get_scoreboard` | `cancel_waiver_claim` – also withdraws a trade you proposed |
| `search_players` – with ownership in your league | |
| `get_free_agents` – by position, FA vs. waivers, sort by rank/points | |
| `get_player_stats` – weekly or season | |
| `get_transactions`, `get_pending_moves` | |

Every write tool takes `dry_run: true`, which returns the exact request without
sending it. The server tells Claude to confirm write actions with you first, and
write tools are marked as such, so Claude Desktop / Claude Code ask before running them.

Tools default to your team and the current week, so you rarely need to pass keys.

## Setup

### 1. Create a Yahoo app

1. Go to <https://developer.yahoo.com/apps/create/>.
2. **Application type:** Installed Application. **Redirect URI:** `oob`
   (if the form insists on a URL, use `https://localhost:8000` and set
   `YAHOO_REDIRECT_URI` to match).
3. **API Permissions:** Fantasy Sports → **Read/Write**. Read-only can't set lineups.
4. Copy the Client ID and Client Secret.

### 2. Install

```bash
git clone https://github.com/tylersojka/Yahoo-sports-mcp
cd Yahoo-sports-mcp
uv tool install .          # or: pipx install .   /   pip install .
```

### 3. Log in once

```bash
export YAHOO_CLIENT_ID=...
export YAHOO_CLIENT_SECRET=...
yahoo-fantasy-mcp auth
```

Open the printed URL, approve, and paste the code back. The token is saved to
`~/.config/yahoo-fantasy-mcp/token.json` (mode 600; override with `YAHOO_TOKEN_FILE`)
and refreshed automatically after that.

### 4. Connect to Claude

**Claude Code**

```bash
claude mcp add yahoo-ff \
  -e YAHOO_CLIENT_ID=... -e YAHOO_CLIENT_SECRET=... -e YAHOO_LEAGUE_ID=123456 \
  -- yahoo-fantasy-mcp
```

**Claude Desktop** (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "yahoo-ff": {
      "command": "yahoo-fantasy-mcp",
      "env": {
        "YAHOO_CLIENT_ID": "...",
        "YAHOO_CLIENT_SECRET": "...",
        "YAHOO_LEAGUE_ID": "123456"
      }
    }
  }
}
```

`YAHOO_LEAGUE_ID` is optional if you're in one Yahoo NFL league. It's the number in
your league URL: `football.fantasysports.yahoo.com/f1/<LEAGUE_ID>`.

## Things to ask

- "Set my optimal lineup for this week — check injuries and byes first."
- "Who are the best available RBs on waivers? Put in a $15 claim on the top one and drop my worst bench guy."
- "Find a trade that upgrades my TE without giving up my top 2 WRs, and send it with a friendly note."
- "Do I have any pending trade offers? Should I accept?"

## Limitations

- Yahoo's public API doesn't expose per-player weekly projections. Team-level projected
  points (in `get_matchup`) and stats/ranks are available; Claude can combine those with
  its own reasoning, or you can add another data source.
- Players whose game has started are locked; Yahoo rejects moves for them.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
pytest
```

Tests mock Yahoo's HTTP API and check the XML each write tool sends.
