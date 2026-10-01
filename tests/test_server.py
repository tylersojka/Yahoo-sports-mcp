import time
import xml.etree.ElementTree as ET

import httpx
import pytest
import respx

from yahoo_fantasy_mcp import server, xmlutil
from yahoo_fantasy_mcp.auth import Credentials, TokenStore
from yahoo_fantasy_mcp.client import BASE_URL, YahooAPIError, YahooClient

NS = 'xmlns="http://fantasysports.yahooapis.com/fantasy/v2/base.rng"'

USER_TEAMS = f"""<?xml version="1.0"?>
<fantasy_content {NS}>
 <users count="1"><user><guid>ABC</guid>
  <games count="1"><game><game_key>461</game_key><code>nfl</code>
   <teams count="1"><team>
    <team_key>461.l.1234.t.3</team_key><name>Ditto Dynasty</name>
    <managers><manager><nickname>Tyler</nickname></manager></managers>
   </team></teams>
  </game></games>
 </user></users>
</fantasy_content>"""

LEAGUE = f"""<?xml version="1.0"?>
<fantasy_content {NS}><league><league_key>461.l.1234</league_key>
<current_week>5</current_week></league></fantasy_content>"""

ROSTER = f"""<?xml version="1.0"?>
<fantasy_content {NS}><team><team_key>461.l.1234.t.3</team_key><name>Ditto Dynasty</name>
<roster><coverage_type>week</coverage_type><week>5</week>
<players count="2">
 <player><player_key>461.p.1</player_key><name><full>Ja'Marr Chase</full></name>
  <editorial_team_abbr>Cin</editorial_team_abbr><display_position>WR</display_position>
  <eligible_positions><position>WR</position></eligible_positions>
  <selected_position><coverage_type>week</coverage_type><position>WR</position></selected_position>
  <bye_weeks><week>10</week></bye_weeks>
 </player>
 <player><player_key>461.p.2</player_key><name><full>Bench Guy</full></name>
  <display_position>RB</display_position><status>Q</status>
  <eligible_positions><position>RB</position><position>W/R/T</position></eligible_positions>
  <selected_position><position>BN</position></selected_position>
 </player>
</players></roster></team></fantasy_content>"""


def test_parse_lists_and_dicts():
    data = xmlutil.parse(ROSTER)
    players = data["team"]["roster"]["players"]
    assert len(players) == 2
    assert players[0]["eligible_positions"] == ["WR"]
    assert players[1]["eligible_positions"] == ["RB", "W/R/T"]
    summary = server.summarize_player(players[1])
    assert summary == {
        "player_key": "461.p.2",
        "name": "Bench Guy",
        "position": "RB",
        "eligible_positions": ["RB", "W/R/T"],
        "selected_position": "BN",
        "status": "Q",
    }


def test_parse_empty_collection():
    data = xmlutil.parse(f'<fantasy_content {NS}><league><players count="0"/></league></fantasy_content>')
    assert data["league"]["players"] == []


def test_build_nests_plural_lists():
    body = xmlutil.build(
        {"roster": {"week": 5, "players": [{"player_key": "461.p.1", "position": "BN"}]}}
    )
    root = ET.fromstring(body.split("\n", 1)[1])
    assert root.find("roster/week").text == "5"
    assert root.find("roster/players/player/position").text == "BN"


def test_build_escapes_text():
    body = xmlutil.build({"transaction": {"trade_note": "you & me <3"}})
    assert ET.fromstring(body.split("\n", 1)[1]).find("transaction/trade_note").text == "you & me <3"


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.delenv("YAHOO_LEAGUE_ID", raising=False)
    store = TokenStore(Credentials("id", "secret"), tmp_path / "token.json")
    store._token = {"access_token": "tok", "refresh_token": "r", "expires_at": time.time() + 3600}
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        server._client = YahooClient(store, httpx.AsyncClient(base_url=BASE_URL))
        server._cache.clear()
        mock.get("/users;use_login=1/games;game_keys=nfl/teams").respond(text=USER_TEAMS)
        mock.get("/league/461.l.1234").respond(text=LEAGUE)
        mock.get("/team/461.l.1234.t.3/roster;week=5").respond(text=ROSTER)
        yield mock
    server._client = None


async def test_get_roster_defaults_to_my_team_and_current_week(api):
    roster = await server.get_roster()
    assert roster["team_key"] == "461.l.1234.t.3"
    assert roster["week"] == 5
    assert [p["name"] for p in roster["players"]] == ["Ja'Marr Chase", "Bench Guy"]


async def test_set_lineup_sends_roster_put(api):
    put = api.put("/team/461.l.1234.t.3/roster").respond(text="")
    await server.set_lineup(
        [
            {"player_key": "461.p.1", "position": "BN"},
            {"player_key": "461.p.2", "position": "WR"},
        ]
    )
    sent = ET.fromstring(put.calls.last.request.content)
    assert sent.find("roster/coverage_type").text == "week"
    assert sent.find("roster/week").text == "5"
    moves = [(p.find("player_key").text, p.find("position").text) for p in sent.iter("player")]
    assert moves == [("461.p.1", "BN"), ("461.p.2", "WR")]
    assert put.calls.last.request.headers["Authorization"] == "Bearer tok"


async def test_add_drop_with_faab(api):
    post = api.post("/league/461.l.1234/transactions").respond(
        text=f"<fantasy_content {NS}><transaction><transaction_key>461.l.1234.w.c.9</transaction_key>"
        "<type>waiver</type><status>pending</status></transaction></fantasy_content>"
    )
    result = await server.add_player("461.p.50", drop_player_key="461.p.2", faab_bid=12)
    assert result["transaction"]["status"] == "pending"
    sent = ET.fromstring(post.calls.last.request.content)
    t = sent.find("transaction")
    assert t.find("type").text == "add/drop"
    assert t.find("faab_bid").text == "12"
    data = [
        (p.find("player_key").text, p.find("transaction_data/type").text)
        for p in t.iter("player")
    ]
    assert data == [("461.p.50", "add"), ("461.p.2", "drop")]
    assert t.find("players/player/transaction_data/destination_team_key").text == "461.l.1234.t.3"


async def test_propose_trade_directions(api):
    result = await server.propose_trade(
        "461.l.1234.t.7", ["461.p.1"], ["461.p.99"], note="fair?", dry_run=True
    )
    assert result["dry_run"] is True
    t = ET.fromstring(result["body"].split("\n", 1)[1]).find("transaction")
    assert t.find("trader_team_key").text == "461.l.1234.t.3"
    assert t.find("tradee_team_key").text == "461.l.1234.t.7"
    flows = [
        (
            p.find("player_key").text,
            p.find("transaction_data/source_team_key").text,
            p.find("transaction_data/destination_team_key").text,
        )
        for p in t.iter("player")
    ]
    assert flows == [
        ("461.p.1", "461.l.1234.t.3", "461.l.1234.t.7"),
        ("461.p.99", "461.l.1234.t.7", "461.l.1234.t.3"),
    ]


async def test_dry_run_sends_nothing(api):
    put = api.put("/team/461.l.1234.t.3/roster").respond(text="")
    result = await server.set_lineup([{"player_key": "461.p.1", "position": "BN"}], dry_run=True)
    assert result["method"] == "PUT"
    assert not put.called


async def test_api_error_surfaces_yahoo_description(api):
    api.put("/team/461.l.1234.t.3/roster").respond(
        400,
        text=f'<error {NS}><description>Player is locked</description></error>',
    )
    with pytest.raises(YahooAPIError, match="Player is locked"):
        await server.set_lineup([{"player_key": "461.p.1", "position": "BN"}])


async def test_refreshes_and_retries_on_401(api, monkeypatch):
    refreshed = []

    def fake_refresh():
        refreshed.append(True)
        server._client.tokens._token["access_token"] = "new"

    monkeypatch.setattr(server._client.tokens, "refresh", fake_refresh)
    route = api.get("/game/nfl")
    route.side_effect = [
        httpx.Response(401, text="expired"),
        httpx.Response(200, text=f"<fantasy_content {NS}><game><game_key>461</game_key></game></fantasy_content>"),
    ]
    assert await server.nfl_game_key() == "461"
    assert refreshed == [True]
    assert route.calls.last.request.headers["Authorization"] == "Bearer new"


async def test_tools_registered():
    names = {t.name for t in await server.mcp.list_tools()}
    assert {
        "get_roster",
        "get_free_agents",
        "set_lineup",
        "add_player",
        "drop_player",
        "propose_trade",
        "respond_to_trade",
        "cancel_waiver_claim",
    } <= names


async def test_tool_errors_reach_the_model(api):
    api.put("/team/461.l.1234.t.3/roster").respond(
        400, text=f"<error {NS}><description>Player is locked</description></error>"
    )
    with pytest.raises(Exception, match="Player is locked"):
        await server.mcp.call_tool(
            "set_lineup", {"moves": [{"player_key": "461.p.1", "position": "BN"}]}
        )
