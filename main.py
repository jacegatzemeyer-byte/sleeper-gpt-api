import io
import csv
import time
import asyncio
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import PlainTextResponse
import httpx

# In-memory storage
PLAYER_CACHE: Dict[str, Any] = {"data": {}, "timestamp": 0}
TRENDING_CACHE: Dict[str, Any] = {"data": [], "timestamp": 0}

PLAYER_TTL = 86400   # 24 hours
TRENDING_TTL = 900   # 15 minutes

ACTIVE_NFL_TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "KC", "LV", "LAC", "LAR", "MIA",
    "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SF", "SEA", "TB",
    "TEN", "WAS"
}

DISALLOWED_POSITIONS = {
    "OL", "OT", "OG", "C", "DL", "DE", "DT", "LB", "DB", "CB", "S", "DEF"
}

HEADERS = {"User-Agent": "DynastyAdvisorMiddleware/1.5.1"}


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
    # Pre-warm the cache at startup so incoming GPT requests respond instantly
    asyncio.create_task(refresh_players())
    yield


app = FastAPI(
    title="Sleeper Dynasty Advisor API",
    version="1.5.1",
    lifespan=lifespan
)


async def get_cached_players(client: httpx.AsyncClient) -> Dict[str, Any]:
    now = time.time()
    # If empty or expired, refresh, but fall back to existing data on failure
    if not PLAYER_CACHE["data"] or (now - PLAYER_CACHE["timestamp"] > PLAYER_TTL):
        try:
            resp = await client.get("https://api.sleeper.app/v1/players/nfl")
            if resp.status_code == 200:
                PLAYER_CACHE["data"] = resp.json()
                PLAYER_CACHE["timestamp"] = now
        except Exception:
            if not PLAYER_CACHE["data"]:
                raise HTTPException(status_code=503, detail="Sleeper player catalog currently initializing. Retry in 10s.")
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
            pass  # Fail gracefully to existing cache or empty list
    return TRENDING_CACHE["data"]


@app.get("/")
async def root():
    return {
        "status": "healthy",
        "version": "1.5.1",
        "players_cached": len(PLAYER_CACHE["data"]),
        "message": "Sleeper Dynasty Advisor API is active."
    }


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

        for pid in pids:
            p_info = players.get(pid, {})
            slot = "STARTER" if pid in starter_ids else "BENCH"
            rows.append({
                "week": week,
                "side": side,
                "team_name": team_name,
                "slot": slot,
                "id": pid,
                "name": p_info.get("full_name") or f"{p_info.get('first_name', '')} {p_info.get('last_name', '')}".strip(),
                "pos": p_info.get("position", "NA"),
                "nfl_team": p_info.get("team", "FA"),
                "depth_chart": p_info.get("depth_chart_order") or "",
                "proj_or_actual_pts": m_data.get("custom_points") or 0.0
            })

    if format.lower() == "json":
        return rows

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["week", "side", "team_name", "slot", "id", "name", "pos", "nfl_team", "depth_chart", "proj_or_actual_pts"])
    writer.writeheader()
    writer.writerows(rows)
    return PlainTextResponse(output.getvalue(), media_type="text/csv")