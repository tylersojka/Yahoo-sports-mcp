"""MCP tools for reading and managing a Yahoo Fantasy Football team."""

from __future__ import annotations

import functools
import os
import urllib.parse
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import xmlutil
from .auth import Credentials, TokenStore
from .client import YahooAPIError, YahooClient

INSTRUCTIONS = """\
Manage the user's Yahoo Fantasy Football team.

Keys: a league key looks like `461.l.123456`, a team key like `461.l.123456.t.3`,
and a player key like `461.p.30123`. Most tools default to the user's own league
and team, so start with `get_my_teams` only when the user has several leagues.

Before any write tool (set_lineup, add_player, drop_player, propose_trade,
respond_to_trade, cancel_waiver_claim), tell the user exactly what will change
and get their confirmation. Every write tool accepts dry_run=true, which returns
the request it would send without sending it.
"""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

mcp = MCPServer("yahoo-fantasy-football", instructions=INSTRUCTIONS)


def tool(annotations: ToolAnnotations):
    """Register a tool, passing expected failures (missing login, bad input, Yahoo
    rejecting a move) to the model as readable errors instead of a generic crash."""

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except (YahooAPIError, RuntimeError, ValueError) as exc:
                raise ToolError(str(exc)) from exc

        mcp.tool(annotations=annotations)(wrapper)
        return fn

    return decorator

_client: YahooClient | None = None
_cache: dict[str, Any] = {}


def client() -> YahooClient:
    global _client
    if _client is None:
        _client = YahooClient(TokenStore(Credentials.from_env()))
    return _client


# --- helpers -----------------------------------------------------------------


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _get(d: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def summarize_player(p: dict) -> dict:
    out = {
        "player_key": p.get("player_key"),
        "name": _get(p, "name", "full"),
        "team": p.get("editorial_team_abbr"),
        "position": p.get("display_position"),
        "eligible_positions": _as_list(p.get("eligible_positions")),
        "selected_position": _get(p, "selected_position", "position"),
        "status": p.get("status"),
        "injury_note": p.get("injury_note"),
        "bye_week": _get(p, "bye_weeks", "week"),
        "percent_owned": _get(p, "percent_owned", "value"),
        "ownership": _get(p, "ownership", "ownership_type"),
        "owner_team": _get(p, "ownership", "owner_team_name"),
        "waiver_date": _get(p, "ownership", "waiver_date"),
        "points": _get(p, "player_points", "total"),
        "projected_points": _get(p, "player_projected_points", "total"),
    }
    stats = _get(p, "player_stats", "stats")
    if stats:
        out["stats"] = {s.get("stat_id"): s.get("value") for s in _as_list(stats)}
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def summarize_team(t: dict) -> dict:
    out = {
        "team_key": t.get("team_key"),
        "name": t.get("name"),
        "points": _get(t, "team_points", "total"),
        "projected_points": _get(t, "team_projected_points", "total"),
        "win_probability": t.get("win_probability"),
        "waiver_priority": t.get("waiver_priority"),
        "faab_balance": t.get("faab_balance"),
        "moves": t.get("number_of_moves"),
        "trades": t.get("number_of_trades"),
        "rank": _get(t, "team_standings", "rank"),
        "record": _record(t),
        "points_for": _get(t, "team_standings", "points_for"),
        "points_against": _get(t, "team_standings", "points_against"),
    }
    managers = _as_list(t.get("managers"))
    if managers:
        out["manager"] = _get(managers[0], "nickname")
    return {k: v for k, v in out.items() if v not in (None, "")}


def _record(t: dict) -> str | None:
    totals = _get(t, "team_standings", "outcome_totals")
    if not totals:
        return None
    rec = f"{totals.get('wins')}-{totals.get('losses')}"
    if totals.get("ties") not in (None, "0"):
        rec += f"-{totals['ties']}"
    return rec


async def nfl_game_key() -> str:
    if "game_key" not in _cache:
        data = await client().get("/game/nfl")
        _cache["game_key"] = data["game"]["game_key"]
    return _cache["game_key"]


async def my_teams() -> list[dict]:
    if "my_teams" not in _cache:
        data = await client().get("/users;use_login=1/games;game_keys=nfl/teams")
        teams = []
        for user in _as_list(data.get("users")):
            for game in _as_list(user.get("games")):
                for t in _as_list(game.get("teams")):
                    teams.append(t)
        _cache["my_teams"] = teams
    return _cache["my_teams"]


async def resolve_league(league_key: str | None) -> str:
    if league_key:
        return league_key
    league_id = os.environ.get("YAHOO_LEAGUE_ID")
    if league_id:
        return f"{await nfl_game_key()}.l.{league_id}"
    teams = await my_teams()
    leagues = sorted({t["team_key"].rsplit(".t.", 1)[0] for t in teams})
    if len(leagues) == 1:
        return leagues[0]
    if not leagues:
        raise ValueError("You have no Yahoo NFL fantasy teams this season.")
    raise ValueError(
        f"You're in several leagues ({', '.join(leagues)}). Pass league_key, "
        "or set YAHOO_LEAGUE_ID."
    )


async def resolve_team(team_key: str | None, league_key: str | None = None) -> str:
    if team_key:
        return team_key
    league = await resolve_league(league_key)
    for t in await my_teams():
        if t["team_key"].startswith(league + ".t."):
            return t["team_key"]
    raise ValueError(f"Couldn't find your team in league {league}.")


async def current_week(league_key: str) -> int:
    data = await client().get(f"/league/{league_key}")
    return int(data["league"]["current_week"])


def league_of(team_key: str) -> str:
    return team_key.rsplit(".t.", 1)[0]


def _players(container: Any) -> list[dict]:
    return [summarize_player(p) for p in _as_list(_get(container, "players"))]


def _dry_run(method: str, path: str, body: dict) -> dict:
    return {"dry_run": True, "method": method, "path": path, "body": xmlutil.build(body)}


# --- read tools --------------------------------------------------------------


@tool(READ)
async def get_my_teams() -> list[dict]:
    """List the user's Yahoo NFL fantasy teams this season, with league and team keys."""
    return [
        {**summarize_team(t), "league_key": league_of(t["team_key"])} for t in await my_teams()
    ]


@tool(READ)
async def get_league_settings(league_key: str | None = None) -> dict:
    """League rules: scoring type, roster slots, waiver/FAAB rules, trade deadline,
    current week, and the stat_id -> stat name map used by player stats."""
    lk = await resolve_league(league_key)
    league = (await client().get(f"/league/{lk}/settings"))["league"]
    s = league.get("settings", {})
    stats = _as_list(_get(s, "stat_categories", "stats"))
    modifiers = {
        m.get("stat_id"): m.get("value") for m in _as_list(_get(s, "stat_modifiers", "stats"))
    }
    return {
        "league_key": league.get("league_key"),
        "name": league.get("name"),
        "num_teams": league.get("num_teams"),
        "current_week": league.get("current_week"),
        "start_week": league.get("start_week"),
        "end_week": league.get("end_week"),
        "scoring_type": league.get("scoring_type"),
        "roster_positions": [
            {"position": r.get("position"), "count": r.get("count")}
            for r in _as_list(s.get("roster_positions"))
        ],
        "waiver_type": s.get("waiver_type"),
        "waiver_rule": s.get("waiver_rule"),
        "uses_faab": s.get("uses_faab"),
        "trade_end_date": s.get("trade_end_date"),
        "max_weekly_adds": s.get("max_weekly_adds"),
        "playoff_start_week": s.get("playoff_start_week"),
        "stat_categories": {
            st.get("stat_id"): {
                "name": st.get("display_name") or st.get("name"),
                "points_per": modifiers.get(st.get("stat_id")),
            }
            for st in stats
        },
    }


@tool(READ)
async def get_standings(league_key: str | None = None) -> list[dict]:
    """League standings: rank, record, points for/against, waiver priority and FAAB."""
    lk = await resolve_league(league_key)
    league = (await client().get(f"/league/{lk}/standings"))["league"]
    return [summarize_team(t) for t in _as_list(_get(league, "standings", "teams"))]


@tool(READ)
async def get_roster(team_key: str | None = None, week: int | None = None) -> dict:
    """A team's roster for a week (default: your team, current week). Shows each
    player's lineup slot (selected_position; BN = bench, IR = injured reserve),
    eligible positions, injury status and bye week."""
    tk = await resolve_team(team_key)
    wk = week or await current_week(league_of(tk))
    team = (await client().get(f"/team/{tk}/roster;week={wk}"))["team"]
    return {
        "team_key": tk,
        "team_name": team.get("name"),
        "week": wk,
        "players": _players(team.get("roster")),
    }


@tool(READ)
async def get_matchup(team_key: str | None = None, week: int | None = None) -> dict:
    """A team's head-to-head matchup for a week: both teams' actual and projected
    points and win probability."""
    tk = await resolve_team(team_key)
    wk = week or await current_week(league_of(tk))
    team = (await client().get(f"/team/{tk}/matchups;weeks={wk}"))["team"]
    matchups = _as_list(team.get("matchups"))
    if not matchups:
        return {"week": wk, "matchup": None}
    m = matchups[0]
    return {
        "week": m.get("week"),
        "status": m.get("status"),
        "is_playoffs": m.get("is_playoffs"),
        "teams": [summarize_team(t) for t in _as_list(m.get("teams"))],
    }


@tool(READ)
async def get_scoreboard(league_key: str | None = None, week: int | None = None) -> dict:
    """Every matchup in the league for a week."""
    lk = await resolve_league(league_key)
    path = f"/league/{lk}/scoreboard" + (f";week={week}" if week else "")
    league = (await client().get(path))["league"]
    sb = league.get("scoreboard") or {}
    return {
        "week": sb.get("week"),
        "matchups": [
            {
                "status": m.get("status"),
                "teams": [summarize_team(t) for t in _as_list(m.get("teams"))],
            }
            for m in _as_list(sb.get("matchups"))
        ],
    }


@tool(READ)
async def search_players(name: str, league_key: str | None = None) -> list[dict]:
    """Find players by name, with who owns them in your league (team, freeagents
    or waivers)."""
    lk = await resolve_league(league_key)
    league = (await client().get(f"/league/{lk}/players;search={urllib.parse.quote(name)}/ownership"))["league"]
    return _players(league)


@tool(READ)
async def get_free_agents(
    position: str | None = None,
    status: Literal["A", "FA", "W"] = "A",
    sort: Literal["AR", "OR", "PTS", "NAME"] = "AR",
    count: int = 25,
    start: int = 0,
    league_key: str | None = None,
) -> list[dict]:
    """Available players in your league.

    position: QB, RB, WR, TE, K, DEF, or a flex slot like W/R/T.
    status: A = all available, FA = free agents only, W = on waivers only.
    sort: AR = actual rank this season, OR = preseason rank, PTS = fantasy points.
    """
    lk = await resolve_league(league_key)
    params = f";status={status};sort={sort};count={min(count, 25)};start={start}"
    if position:
        params += f";position={position}"
    league = (await client().get(f"/league/{lk}/players{params}/percent_owned"))["league"]
    return _players(league)


@tool(READ)
async def get_player_stats(
    player_keys: list[str], week: int | None = None, league_key: str | None = None
) -> list[dict]:
    """Fantasy points and raw stats for players in your league's scoring.
    week omitted = season totals. Map stat ids with get_league_settings."""
    lk = await resolve_league(league_key)
    coverage = f"type=week;week={week}" if week else "type=season"
    keys = ",".join(player_keys)
    league = (await client().get(f"/league/{lk}/players;player_keys={keys}/stats;{coverage}"))[
        "league"
    ]
    return _players(league)


@tool(READ)
async def get_transactions(
    types: str = "add,drop,trade",
    count: int = 15,
    league_key: str | None = None,
) -> list[dict]:
    """Recent league transactions. types: comma-separated add, drop, commish, trade."""
    lk = await resolve_league(league_key)
    league = (await client().get(f"/league/{lk}/transactions;types={types};count={count}"))[
        "league"
    ]
    return [_summarize_transaction(t) for t in _as_list(league.get("transactions"))]


@tool(READ)
async def get_pending_moves(team_key: str | None = None) -> list[dict]:
    """Your pending waiver claims and trade offers (sent and received), with the
    transaction_key needed to cancel or respond."""
    tk = await resolve_team(team_key)
    lk = league_of(tk)
    out = []
    for kind in ("waiver", "pending_trade"):
        league = (await client().get(f"/league/{lk}/transactions;team_key={tk};type={kind}"))[
            "league"
        ]
        out.extend(_summarize_transaction(t) for t in _as_list(league.get("transactions")))
    return out


def _summarize_transaction(t: dict) -> dict:
    players = []
    for p in _as_list(t.get("players")):
        td = p.get("transaction_data") or {}
        players.append(
            {
                "player_key": p.get("player_key"),
                "name": _get(p, "name", "full"),
                "type": td.get("type"),
                "from": td.get("source_team_name") or td.get("source_type"),
                "to": td.get("destination_team_name") or td.get("destination_type"),
            }
        )
    out = {
        "transaction_key": t.get("transaction_key"),
        "type": t.get("type"),
        "status": t.get("status"),
        "timestamp": t.get("timestamp"),
        "faab_bid": t.get("faab_bid"),
        "waiver_priority": t.get("waiver_priority"),
        "trader_team_key": t.get("trader_team_key"),
        "tradee_team_key": t.get("tradee_team_key"),
        "trade_note": t.get("trade_note"),
        "players": players,
    }
    return {k: v for k, v in out.items() if v not in (None, "", [])}


# --- write tools -------------------------------------------------------------


@tool(WRITE)
async def set_lineup(
    moves: list[dict[str, str]],
    week: int | None = None,
    team_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Move players between lineup slots for a week.

    moves: [{"player_key": "461.p.30123", "position": "WR"}, ...]. Positions are
    roster slots like QB, RB, WR, TE, W/R/T, K, DEF, BN (bench), IR. Include every
    player whose slot changes, e.g. benching one player and starting another is
    two moves. Players whose game has started are locked and can't be moved.
    """
    tk = await resolve_team(team_key)
    wk = week or await current_week(league_of(tk))
    for m in moves:
        if not m.get("player_key") or not m.get("position"):
            raise ValueError(f"Each move needs player_key and position, got {m}")
    path = f"/team/{tk}/roster"
    body = {
        "roster": {
            "coverage_type": "week",
            "week": wk,
            "players": [{"player_key": m["player_key"], "position": m["position"]} for m in moves],
        }
    }
    if dry_run:
        return _dry_run("PUT", path, body)
    await client().put(path, body)
    return {"ok": True, "week": wk, "roster": (await get_roster(tk, wk))["players"]}


@tool(WRITE)
async def add_player(
    add_player_key: str,
    drop_player_key: str | None = None,
    faab_bid: int | None = None,
    team_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Add a free agent, or put in a waiver claim if the player is on waivers.

    drop_player_key: who to drop to make room (required when your roster is full).
    faab_bid: your FAAB bid in dollars, for waiver claims in FAAB leagues.
    """
    tk = await resolve_team(team_key)
    lk = league_of(tk)
    players = [
        {
            "player_key": add_player_key,
            "transaction_data": {"type": "add", "destination_team_key": tk},
        }
    ]
    if drop_player_key:
        players.append(
            {
                "player_key": drop_player_key,
                "transaction_data": {"type": "drop", "source_team_key": tk},
            }
        )
    transaction: dict[str, Any] = {"type": "add/drop" if drop_player_key else "add"}
    if faab_bid is not None:
        transaction["faab_bid"] = faab_bid
    transaction["players"] = players
    path = f"/league/{lk}/transactions"
    body = {"transaction": transaction}
    if dry_run:
        return _dry_run("POST", path, body)
    result = await client().post(path, body)
    return {"ok": True, "transaction": _summarize_transaction(result.get("transaction") or {})}


@tool(DESTRUCTIVE)
async def drop_player(player_key: str, team_key: str | None = None, dry_run: bool = False) -> dict:
    """Release a player from your roster. They may go on waivers for other teams."""
    tk = await resolve_team(team_key)
    path = f"/league/{league_of(tk)}/transactions"
    body = {
        "transaction": {
            "type": "drop",
            "players": [
                {
                    "player_key": player_key,
                    "transaction_data": {"type": "drop", "source_team_key": tk},
                }
            ],
        }
    }
    if dry_run:
        return _dry_run("POST", path, body)
    result = await client().post(path, body)
    return {"ok": True, "transaction": _summarize_transaction(result.get("transaction") or {})}


@tool(WRITE)
async def propose_trade(
    tradee_team_key: str,
    send_player_keys: list[str],
    receive_player_keys: list[str],
    note: str | None = None,
    team_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Offer a trade to another team. send_player_keys are yours; receive_player_keys
    are theirs. Find their team_key with get_standings."""
    tk = await resolve_team(team_key)
    players = [
        {
            "player_key": pk,
            "transaction_data": {
                "type": "pending_trade",
                "source_team_key": tk,
                "destination_team_key": tradee_team_key,
            },
        }
        for pk in send_player_keys
    ] + [
        {
            "player_key": pk,
            "transaction_data": {
                "type": "pending_trade",
                "source_team_key": tradee_team_key,
                "destination_team_key": tk,
            },
        }
        for pk in receive_player_keys
    ]
    path = f"/league/{league_of(tk)}/transactions"
    body = {
        "transaction": {
            "type": "pending_trade",
            "trader_team_key": tk,
            "tradee_team_key": tradee_team_key,
            "trade_note": note,
            "players": players,
        }
    }
    if dry_run:
        return _dry_run("POST", path, body)
    result = await client().post(path, body)
    return {"ok": True, "transaction": _summarize_transaction(result.get("transaction") or {})}


@tool(WRITE)
async def respond_to_trade(
    transaction_key: str,
    action: Literal["accept", "reject"],
    note: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Accept or reject a trade offered to you. Get transaction_key from get_pending_moves."""
    path = f"/transaction/{transaction_key}"
    body = {
        "transaction": {
            "transaction_key": transaction_key,
            "type": "pending_trade",
            "action": action,
            "trade_note": note,
        }
    }
    if dry_run:
        return _dry_run("PUT", path, body)
    await client().put(path, body)
    return {"ok": True, "transaction_key": transaction_key, "action": action}


@tool(DESTRUCTIVE)
async def cancel_waiver_claim(transaction_key: str, dry_run: bool = False) -> dict:
    """Cancel one of your pending waiver claims, or withdraw a trade you proposed.
    Get transaction_key from get_pending_moves."""
    path = f"/transaction/{transaction_key}"
    if dry_run:
        return {"dry_run": True, "method": "DELETE", "path": path}
    await client().delete(path)
    return {"ok": True, "cancelled": transaction_key}


def run() -> None:
    mcp.run("stdio")
