import io
import csv
import time
import asyncio
import re
import unicodedata
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import PlainTextResponse
import httpx
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

# In-memory caches
PLAYER_CACHE: Dict[str, Any] = {"data": {}, "timestamp": 0}
TRENDING_CACHE: Dict[str, Any] = {"data": [], "timestamp": 0}
BORIS_CACHE: Dict[str, Any] = {"data": {}, "timestamp": 0}

PLAYER_TTL = 86400   # 24 hours
TRENDING_TTL = 900   # 15 minutes
BORIS_TTL = 7200     # 2 hours

ACTIVE_NFL_TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "KC", "LV", "LAC", "LAR", "MIA",
    "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SF", "SEA", "TB",
    "TEN", "WAS"
}

DISALLOWED_POSITIONS = {
    "OL", "OT", "OG", "C", "DL", "DE", "DT", "LB", "DB", "CB", "S", "DEF"
}

HEADERS = {"User-Agent": "DynastyAdvisorMiddleware/2.0.0"}

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
mcp = MCPServer(
    "Sleeper Dynasty Advisor",
    instructions=(
        "Read-only fantasy football tools for Sleeper league 1399229018905034752. "
        "Use Boris Chen PPR tiers as the primary weekly ranking baseline. "
        "The default team owner is jgatz12."
    ),
)


async def refresh_players():
    """Fetches Sleeper's master player database safely in the background."""
    try:
        async with httpx.AsyncClient(timeout=30.0, headers=HEADERS) as client:
            resp = await client.get("https://api.sleeper.app/v1/players/nfl")
            if resp.status_code == 200:
                PLAYER_CACHE["data"] = resp.json()
                PLAYER_CACHE["timestamp"] = time.time()
                print(f"[Cache] Successfully loaded {len(PLAYER_CACHE['data'])} NFL players.")
    except Exception as e:
        print(f"[Cache Error] Failed to refresh players: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        asyncio.create_task(refresh_players())
        yield


app = FastAPI(
    title="Sleeper Dynasty Advisor API",
    version="2.0.0",
    lifespan=lifespan
)


def compute_alert_level(game_status: Optional[str], practice: Optional[str]) -> int:
    status_clean = (game_status or "").strip().upper()
    practice_clean = (practice or "").strip().upper()

    if status_clean in {"OUT", "IR", "PUP", "SUS", "DOUBTFUL"}:
        return 4
    if status_clean == "QUESTIONABLE" or practice_clean in {"DNP", "DID NOT PARTICIPATE"}:
        return 3
    if practice_clean in {"LIMITED", "LP"}:
        return 2
    if practice_clean in {"FULL", "FP"} or status_clean == "PROBABLE":
        return 1
    return 0


async def get_cached_players(client: httpx.AsyncClient) -> Dict[str, Any]:
    now = time.time()
    if not PLAYER_CACHE["data"] or (now - PLAYER_CACHE["timestamp"] > PLAYER_TTL):
        try:
            resp = await client.get("https://api.sleeper.app/v1/players/nfl")
            if resp.status_code == 200:
                PLAYER_CACHE["data"] = resp.json()
                PLAYER_CACHE["timestamp"] = now
        except Exception:
            if not PLAYER_CACHE["data"]:
                raise HTTPException(status_code=503, detail="Player catalog initializing. Retry in 10s.")
    return PLAYER_CACHE["data"]


async def get_cached_trending(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    now = time.time()
    if not TRENDING_CACHE["data"] or (now - TRENDING_CACHE["timestamp"] > TRENDING_TTL):
        try:
            resp = await client.get("https://api.sleeper.app/v1/players/nfl/trending/add?lookback_hours=24&limit=50")
            if resp.status_code == 200:
                TRENDING_CACHE["data"] = resp.json()
                TRENDING_CACHE["timestamp"] = now
        except Exception:
            pass
    return TRENDING_CACHE["data"]


@app.get("/")
async def root():
    return {
        "status": "healthy",
        "version": "2.0.0",
        "players_cached": len(PLAYER_CACHE["data"]),
        "message": "Sleeper Dynasty Advisor REST + MCP API is active.",
        "mcp_endpoint": "/mcp"
    }


@app.get("/injuries")
async def get_injuries(
    league_id: str = "1399229018905034752",
    username: str = "jgatz12",
    week: Optional[int] = None,
    scope: str = Query("matchup", enum=["matchup", "self"]),
    min_alert_level: int = Query(2, ge=0, le=5),
    format: str = "csv"
):
    """Audits injury designations and practice participation for matchup rosters."""
    async with httpx.AsyncClient(timeout=20.0, headers=HEADERS) as client:
        players = await get_cached_players(client)

        if week is None:
            state_resp = await client.get("https://api.sleeper.app/v1/state/nfl")
            week = state_resp.json().get("week", 1) if state_resp.status_code == 200 else 1

        users_resp = await client.get(f"https://api.sleeper.app/v1/league/{league_id}/users")
        rosters_resp = await client.get(f"https://api.sleeper.app/v1/league/{league_id}/rosters")
        matchups_resp = await client.get(f"https://api.sleeper.app/v1/league/{league_id}/matchups/{week}")

        if any(r.status_code != 200 for r in (users_resp, rosters_resp, matchups_resp)):
            raise HTTPException(status_code=502, detail=f"Failed to fetch matchup data for week {week}.")

        users = users_resp.json()
        rosters = rosters_resp.json()
        matchups = matchups_resp.json()

    user_id_to_name = {u["user_id"]: u.get("display_name", "Unknown") for u in users}
    roster_id_to_name = {r["roster_id"]: user_id_to_name.get(r.get("owner_id"), f"Team {r['roster_id']}") for r in rosters}

    target_user = next((u for u in users if u.get("display_name", "").lower() == username.lower()), None)
    if not target_user:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found in league.")

    target_roster = next((r for r in rosters if r.get("owner_id") == target_user["user_id"]), None)
    if not target_roster:
        raise HTTPException(status_code=404, detail=f"Roster not found for '{username}'.")

    target_roster_id = target_roster["roster_id"]
    user_matchup = next((m for m in matchups if m.get("roster_id") == target_roster_id), None)
    if not user_matchup:
        raise HTTPException(status_code=404, detail=f"No matchup scheduled for week {week}.")

    teams = [("self", user_matchup, roster_id_to_name.get(target_roster_id, username))]

    if scope == "matchup":
        matchup_id = user_matchup.get("matchup_id")
        opp_matchup = next((m for m in matchups if m.get("matchup_id") == matchup_id and m.get("roster_id") != target_roster_id), None)
        if opp_matchup:
            teams.append(("opponent", opp_matchup, roster_id_to_name.get(opp_matchup["roster_id"], "Opponent")))

    rows = []
    starter_counts = {"self_total": 0, "self_clear": 0, "opp_total": 0, "opp_clear": 0}

    for side, m_data, team_name in teams:
        starter_ids = set(m_data.get("starters") or [])
        all_ids = m_data.get("players") or []

        for pid in all_ids:
            p_info = players.get(pid, {})
            pos = p_info.get("position", "NA")
            is_starter = pid in starter_ids
            slot = "STARTER" if is_starter else "BENCH"

            # Filter non-skill positions for clean reporting
            if pos in DISALLOWED_POSITIONS:
                continue

            game_status = p_info.get("injury_status") or "ACTIVE"
            practice = p_info.get("practice_participation") or "FULL"
            body_part = p_info.get("injury_body_part") or "None"
            alert = compute_alert_level(game_status, practice)

            if is_starter:
                key_prefix = "self" if side == "self" else "opp"
                starter_counts[f"{key_prefix}_total"] += 1
                if alert < 2:
                    starter_counts[f"{key_prefix}_clear"] += 1

            if alert >= min_alert_level:
                rows.append({
                    "week": week,
                    "side": side,
                    "team_name": team_name,
                    "slot": slot,
                    "name": p_info.get("full_name") or f"{p_info.get('first_name', '')} {p_info.get('last_name', '')}".strip(),
                    "pos": pos,
                    "nfl_team": p_info.get("team", "FA"),
                    "alert_level": alert,
                    "game_status": game_status,
                    "practice": practice,
                    "body_part": body_part
                })

    rows.sort(key=lambda x: (x["side"], 0 if x["slot"] == "STARTER" else 1, -x["alert_level"]))

    if format.lower() == "json":
        return {
            "starter_clear_summary": f"Self: {starter_counts['self_clear']}/{starter_counts['self_total']} clear | Opp: {starter_counts['opp_clear']}/{starter_counts['opp_total']} clear",
            "flagged_players": rows
        }

    output = io.StringIO()
    output.write(f"# Starter Clearance: Self: {starter_counts['self_clear']}/{starter_counts['self_total']} clear | Opp: {starter_counts['opp_clear']}/{starter_counts['opp_total']} clear\n")
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "week", "side", "team_name", "slot", "name", "pos", "nfl_team",
            "alert_level", "game_status", "practice", "body_part"
        ]
    )
    writer.writeheader()
    writer.writerows(rows)
    return PlainTextResponse(output.getvalue(), media_type="text/csv")


@app.get("/boris-tiers")
async def get_boris_tiers(
    positions: Optional[str] = Query("QB,RB,WR,TE,FLX,K,DST", description="Comma-separated: QB,RB,WR,TE,FLX,K,DST"),
    scoring: str = Query("ppr", enum=["standard", "half_ppr", "ppr"]),
    format: str = "csv"
):
    now = time.time()
    cache_key = f"{scoring}_{positions}"

    if cache_key in BORIS_CACHE["data"] and (now - BORIS_CACHE["timestamp"] < BORIS_TTL):
        results = BORIS_CACHE["data"][cache_key]
    else:
        suffix = "-PPR" if scoring == "ppr" else ("-HALF" if scoring == "half_ppr" else "")
        requested_pos = [p.strip().upper() for p in positions.split(",") if p.strip()]
        results = []

        async with httpx.AsyncClient(timeout=15.0, headers=HEADERS) as client:
            current_week = 1
            try:
                state_resp = await client.get("https://api.sleeper.app/v1/state/nfl")
                if state_resp.status_code == 200:
                    current_week = state_resp.json().get("week", 1)
            except Exception:
                pass

            for pos in requested_pos:
                if pos in {"QB", "K", "DST"}:
                    file_name = f"text_{pos}.txt"
                elif pos == "FLX":
                    file_name = f"text_FLX{suffix}.txt"
                else:
                    file_name = f"text_{pos}{suffix}.txt"

                url = f"https://s3-us-west-1.amazonaws.com/fftiers/out/{file_name}"
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue

                    last_modified = resp.headers.get("last-modified", "Unknown")
                    lines = resp.text.strip().split("\n")
                    rank = 1
                    for line in lines:
                        line = line.strip()
                        if not line or ":" not in line:
                            continue
                        tier_part, players_part = line.split(":", 1)
                        tier_num = tier_part.replace("Tier", "").strip()
                        player_names = [p.strip() for p in players_part.split(",") if p.strip()]

                        for name in player_names:
                            results.append({
                                "week": current_week,
                                "updated_at": last_modified,
                                "position": pos,
                                "scoring": scoring.upper(),
                                "tier": int(tier_num) if tier_num.isdigit() else tier_num,
                                "rank": rank,
                                "name": name
                            })
                            rank += 1
                except Exception:
                    continue

        BORIS_CACHE["data"][cache_key] = results
        BORIS_CACHE["timestamp"] = now

    if format.lower() == "json":
        return results

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["week", "updated_at", "position", "scoring", "tier", "rank", "name"])
    writer.writeheader()
    writer.writerows(results)
    return PlainTextResponse(output.getvalue(), media_type="text/csv")


@app.get("/free-agents")
async def get_free_agents(
    league_id: str = "1399229018905034752",
    positions: Optional[str] = Query(None, description="Comma-separated: QB,RB,WR,TE,K"),
    active_teams_only: bool = True,
    trending_only: bool = False,
    sort_by: str = "trending_count",
    limit: int = 50,
    format: str = "csv"
):
    async with httpx.AsyncClient(timeout=20.0, headers=HEADERS) as client:
        players = await get_cached_players(client)
        trending_list = await get_cached_trending(client)
        trending_map = {item["player_id"]: item["count"] for item in trending_list}

        rosters_resp = await client.get(f"https://api.sleeper.app/v1/league/{league_id}/rosters")
        if rosters_resp.status_code != 200:
            raise HTTPException(status_code=rosters_resp.status_code, detail="Unable to retrieve league rosters.")
        rosters = rosters_resp.json()

    rostered_ids = {pid for r in rosters for pid in (r.get("players") or [])}
    pos_filter = set(positions.upper().split(",")) if positions else None
    results = []

    for pid, p in players.items():
        if pid in rostered_ids:
            continue

        team = p.get("team")
        pos = p.get("position")

        if active_teams_only and team not in ACTIVE_NFL_TEAMS:
            continue
        if pos in DISALLOWED_POSITIONS:
            continue
        if pos_filter and pos not in pos_filter:
            continue

        t_count = trending_map.get(pid, 0)
        if trending_only and t_count == 0:
            continue

        results.append({
            "id": pid,
            "name": p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
            "pos": pos or "NA",
            "team": team or "FA",
            "status": p.get("status") or "Active",
            "depth_chart": p.get("depth_chart_order") or "",
            "exp": p.get("years_exp", 0),
            "trending_count": t_count
        })

    if sort_by == "trending_count":
        results.sort(key=lambda x: x["trending_count"], reverse=True)

    results = results[:limit]

    if format.lower() == "json":
        return results

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["id", "name", "pos", "team", "status", "depth_chart", "exp", "trending_count"])
    writer.writeheader()
    writer.writerows(results)
    return PlainTextResponse(output.getvalue(), media_type="text/csv")


@app.get("/roster")
async def get_roster(
    league_id: str = "1399229018905034752",
    username: str = "jgatz12",
    format: str = "csv"
):
    async with httpx.AsyncClient(timeout=20.0, headers=HEADERS) as client:
        players = await get_cached_players(client)
        users_resp = await client.get(f"https://api.sleeper.app/v1/league/{league_id}/users")
        rosters_resp = await client.get(f"https://api.sleeper.app/v1/league/{league_id}/rosters")

        if users_resp.status_code != 200 or rosters_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to fetch league data from Sleeper.")

        users = users_resp.json()
        rosters = rosters_resp.json()

    user = next((u for u in users if u.get("display_name", "").lower() == username.lower()), None)
    if not user:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found in league.")

    roster = next((r for r in rosters if r.get("owner_id") == user["user_id"]), None)
    if not roster:
        raise HTTPException(status_code=404, detail=f"Roster not found for user '{username}'.")

    starter_ids = set(roster.get("starters") or [])
    taxi_ids = set(roster.get("taxi") or [])
    reserve_ids = set(roster.get("reserve") or [])
    all_player_ids = roster.get("players") or []

    rows = []
    for pid in all_player_ids:
        p_info = players.get(pid, {})
        slot = "STARTER" if pid in starter_ids else ("TAXI" if pid in taxi_ids else ("IR" if pid in reserve_ids else "BENCH"))

        rows.append({
            "id": pid,
            "name": p_info.get("full_name") or f"{p_info.get('first_name', '')} {p_info.get('last_name', '')}".strip(),
            "pos": p_info.get("position", "NA"),
            "team": p_info.get("team", "FA"),
            "slot": slot,
            "depth_chart": p_info.get("depth_chart_order") or "",
            "exp": p_info.get("years_exp", 0)
        })

    slot_order = {"STARTER": 1, "BENCH": 2, "TAXI": 3, "IR": 4}
    rows.sort(key=lambda x: (slot_order.get(x["slot"], 5), x["pos"], x["name"]))

    if format.lower() == "json":
        return rows

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["id", "name", "pos", "team", "slot", "depth_chart", "exp"])
    writer.writeheader()
    writer.writerows(rows)
    return PlainTextResponse(output.getvalue(), media_type="text/csv")


@app.get("/matchup")
async def get_matchup(
    league_id: str = "1399229018905034752",
    username: str = "jgatz12",
    week: Optional[int] = None,
    starters_only: bool = False,
    format: str = "csv"
):
    async with httpx.AsyncClient(timeout=20.0, headers=HEADERS) as client:
        players = await get_cached_players(client)

        if week is None:
            state_resp = await client.get("https://api.sleeper.app/v1/state/nfl")
            week = state_resp.json().get("week", 1) if state_resp.status_code == 200 else 1

        users_resp = await client.get(f"https://api.sleeper.app/v1/league/{league_id}/users")
        rosters_resp = await client.get(f"https://api.sleeper.app/v1/league/{league_id}/rosters")
        matchups_resp = await client.get(f"https://api.sleeper.app/v1/league/{league_id}/matchups/{week}")

        if any(r.status_code != 200 for r in (users_resp, rosters_resp, matchups_resp)):
            raise HTTPException(status_code=502, detail=f"Failed to fetch matchup data for week {week}.")

        users = users_resp.json()
        rosters = rosters_resp.json()
        matchups = matchups_resp.json()

    user_id_to_name = {u["user_id"]: u.get("display_name", "Unknown") for u in users}
    roster_id_to_name = {r["roster_id"]: user_id_to_name.get(r.get("owner_id"), f"Team {r['roster_id']}") for r in rosters}

    target_user = next((u for u in users if u.get("display_name", "").lower() == username.lower()), None)
    if not target_user:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found in league.")

    target_roster = next((r for r in rosters if r.get("owner_id") == target_user["user_id"]), None)
    if not target_roster:
        raise HTTPException(status_code=404, detail=f"Roster not found for '{username}'.")

    target_roster_id = target_roster["roster_id"]
    user_matchup = next((m for m in matchups if m.get("roster_id") == target_roster_id), None)
    if not user_matchup:
        raise HTTPException(status_code=404, detail=f"No matchup scheduled for week {week}.")

    matchup_id = user_matchup.get("matchup_id")
    opp_matchup = next((m for m in matchups if m.get("matchup_id") == matchup_id and m.get("roster_id") != target_roster_id), None)

    teams = [("USER", user_matchup, roster_id_to_name.get(target_roster_id, username))]
    if opp_matchup:
        teams.append(("OPPONENT", opp_matchup, roster_id_to_name.get(opp_matchup["roster_id"], "Opponent")))

    rows = []
    for side, m_data, team_name in teams:
        starter_ids = set(m_data.get("starters") or [])
        pids = starter_ids if starters_only else (m_data.get("players") or [])

        team_points = m_data.get("points") or 0.0
        custom_points = m_data.get("custom_points") or ""
        players_points = m_data.get("players_points") or {}
        r_id = m_data.get("roster_id", "")
        m_id = m_data.get("matchup_id", "")

        for pid in pids:
            p_info = players.get(pid, {})
            slot = "STARTER" if pid in starter_ids else "BENCH"
            player_pts = players_points.get(pid, 0.0)

            rows.append({
                "week": week,
                "side": side,
                "team_name": team_name,
                "roster_id": r_id,
                "matchup_id": m_id,
                "team_points": team_points,
                "custom_points": custom_points,
                "slot": slot,
                "id": pid,
                "name": p_info.get("full_name") or f"{p_info.get('first_name', '')} {p_info.get('last_name', '')}".strip(),
                "pos": p_info.get("position", "NA"),
                "nfl_team": p_info.get("team", "FA"),
                "player_points": player_pts
            })

    if format.lower() == "json":
        return rows

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "week", "side", "team_name", "roster_id", "matchup_id",
            "team_points", "custom_points", "slot", "id", "name",
            "pos", "nfl_team", "player_points"
        ]
    )
    writer.writeheader()
    writer.writerows(rows)
    return PlainTextResponse(output.getvalue(), media_type="text/csv")

# ---------------------------------------------------------------------------
# MCP tool layer
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    """Normalize player names so Sleeper names match Boris Chen text files."""
    value = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    value = value.lower().replace("’", "'")
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"\b(jr|sr|ii|iii|iv|v)\.?\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


@mcp.tool(
    name="getRoster",
    title="Get Sleeper roster",
    description="Read a Sleeper roster for this dynasty league, including starters, bench, taxi, and IR.",
    annotations=READ_ONLY,
)
async def mcp_get_roster(
    username: str = "jgatz12",
    league_id: str = "1399229018905034752",
) -> List[Dict[str, Any]]:
    return await get_roster(league_id=league_id, username=username, format="json")


@mcp.tool(
    name="getMatchup",
    title="Get weekly matchup",
    description="Read the user's Sleeper matchup, team scores, starters, bench players, and individual player points.",
    annotations=READ_ONLY,
)
async def mcp_get_matchup(
    username: str = "jgatz12",
    league_id: str = "1399229018905034752",
    week: Optional[int] = None,
    starters_only: bool = False,
) -> List[Dict[str, Any]]:
    return await get_matchup(
        league_id=league_id,
        username=username,
        week=week,
        starters_only=starters_only,
        format="json",
    )


@mcp.tool(
    name="getFreeAgents",
    title="Get available Sleeper players",
    description=(
        "Return players who are actually unrostered in the specified Sleeper league. "
        "Filter by position and optionally limit to players trending as adds."
    ),
    annotations=READ_ONLY,
)
async def mcp_get_free_agents(
    positions: Optional[str] = None,
    league_id: str = "1399229018905034752",
    active_teams_only: bool = True,
    trending_only: bool = False,
    sort_by: str = "trending_count",
    limit: int = 50,
) -> List[Dict[str, Any]]:
    return await get_free_agents(
        league_id=league_id,
        positions=positions,
        active_teams_only=active_teams_only,
        trending_only=trending_only,
        sort_by=sort_by,
        limit=limit,
        format="json",
    )


@mcp.tool(
    name="getInjuries",
    title="Audit matchup injuries",
    description=(
        "Audit Sleeper injury and practice statuses for the user's roster and, optionally, the opponent. "
        "Returns starter-clearance counts plus flagged players."
    ),
    annotations=READ_ONLY,
)
async def mcp_get_injuries(
    username: str = "jgatz12",
    league_id: str = "1399229018905034752",
    week: Optional[int] = None,
    scope: str = "matchup",
    min_alert_level: int = 2,
) -> Dict[str, Any]:
    if scope not in {"matchup", "self"}:
        raise ValueError("scope must be 'matchup' or 'self'")
    if not 0 <= min_alert_level <= 5:
        raise ValueError("min_alert_level must be between 0 and 5")
    return await get_injuries(
        league_id=league_id,
        username=username,
        week=week,
        scope=scope,
        min_alert_level=min_alert_level,
        format="json",
    )


@mcp.tool(
    name="getBorisTiers",
    title="Get Boris Chen tiers",
    description=(
        "Fetch current Boris Chen weekly tiers and ranks. Use scoring='ppr' for this league. "
        "positions accepts comma-separated QB,RB,WR,TE,FLX,K,DST."
    ),
    annotations=READ_ONLY,
)
async def mcp_get_boris_tiers(
    positions: str = "QB,RB,WR,TE,FLX,K,DST",
    scoring: str = "ppr",
) -> List[Dict[str, Any]]:
    if scoring not in {"standard", "half_ppr", "ppr"}:
        raise ValueError("scoring must be standard, half_ppr, or ppr")
    return await get_boris_tiers(positions=positions, scoring=scoring, format="json")


@mcp.tool(
    name="getWaiverAnalysis",
    title="Rank available players with Boris Chen",
    description=(
        "Join the league's actual Sleeper free-agent pool to current Boris Chen PPR rankings. "
        "Use this as the first-pass waiver-wire scan before comparing candidates with the user's roster."
    ),
    annotations=READ_ONLY,
)
async def mcp_get_waiver_analysis(
    positions: str = "RB,WR,TE",
    league_id: str = "1399229018905034752",
    scoring: str = "ppr",
    limit: int = 75,
    include_unranked: bool = False,
) -> List[Dict[str, Any]]:
    if scoring not in {"standard", "half_ppr", "ppr"}:
        raise ValueError("scoring must be standard, half_ppr, or ppr")
    if not 1 <= limit <= 250:
        raise ValueError("limit must be between 1 and 250")

    requested_positions = [p.strip().upper() for p in positions.split(",") if p.strip()]
    if not requested_positions:
        raise ValueError("positions must include at least one position")

    free_agents = await get_free_agents(
        league_id=league_id,
        positions=",".join(requested_positions),
        active_teams_only=True,
        trending_only=False,
        sort_by="trending_count",
        limit=1000,
        format="json",
    )

    boris_positions = list(dict.fromkeys(
        requested_positions + (["FLX"] if any(p in {"RB", "WR", "TE"} for p in requested_positions) else [])
    ))
    boris_rows = await get_boris_tiers(
        positions=",".join(boris_positions),
        scoring=scoring,
        format="json",
    )

    by_pos_name: Dict[tuple, Dict[str, Any]] = {}
    flex_by_name: Dict[str, Dict[str, Any]] = {}
    for row in boris_rows:
        key = _normalize_name(row.get("name", ""))
        if not key:
            continue
        if row.get("position") == "FLX":
            flex_by_name[key] = row
        else:
            by_pos_name[(row.get("position"), key)] = row

    enriched = []
    for player in free_agents:
        key = _normalize_name(player.get("name", ""))
        positional = by_pos_name.get((player.get("pos"), key))
        flex = flex_by_name.get(key)
        if not include_unranked and not positional and not flex:
            continue

        enriched.append({
            **player,
            "boris_position_tier": positional.get("tier") if positional else None,
            "boris_position_rank": positional.get("rank") if positional else None,
            "boris_flex_tier": flex.get("tier") if flex else None,
            "boris_flex_rank": flex.get("rank") if flex else None,
            "boris_week": (flex or positional or {}).get("week"),
            "boris_updated_at": (flex or positional or {}).get("updated_at"),
            "boris_scoring": scoring.upper(),
        })

    def rank_key(row: Dict[str, Any]):
        rank = row.get("boris_flex_rank") or row.get("boris_position_rank")
        return (
            rank is None,
            rank if rank is not None else 10**9,
            -(row.get("trending_count") or 0),
            row.get("name") or "",
        )

    enriched.sort(key=rank_key)
    return enriched[:limit]


_mcp_http_app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
app.mount("/", _mcp_http_app)
