import csv
import io
import time
from typing import Optional
from fastapi import FastAPI, Query, Response
from fastapi.responses import JSONResponse
import requests

app = FastAPI(
    title="Sleeper Free Agent & Roster API",
    version="1.4.0",
    description="Filtered free-agent lookup and team roster inspection for Sleeper fantasy leagues.",
)

PLAYERS_CACHE = {}
CACHE_TIMESTAMP = 0
CACHE_DURATION = 86400  # 24 hours

TRENDING_CACHE = {}
TRENDING_TIMESTAMP = 0
TRENDING_CACHE_DURATION = 900  # 15 minutes

ACTIVE_NFL_TEAMS = {
    "ARI",
    "ATL",
    "BAL",
    "BUF",
    "CAR",
    "CHI",
    "CIN",
    "CLE",
    "DAL",
    "DEN",
    "DET",
    "GB",
    "HOU",
    "IND",
    "JAX",
    "KC",
    "LAC",
    "LAR",
    "LV",
    "MIA",
    "MIN",
    "NE",
    "NO",
    "NYG",
    "NYJ",
    "PHI",
    "PIT",
    "SEA",
    "SF",
    "TB",
    "TEN",
    "WAS",
}

VALID_OFFENSIVE_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}

DISALLOWED_POSITIONS = {
    "OL",
    "OT",
    "OG",
    "C",
    "DL",
    "DE",
    "DT",
    "LB",
    "ILB",
    "OLB",
    "DB",
    "CB",
    "S",
    "FS",
    "SS",
}


def get_cached_players():
    global PLAYERS_CACHE, CACHE_TIMESTAMP
    now = time.time()
    if not PLAYERS_CACHE or (now - CACHE_TIMESTAMP) > CACHE_DURATION:
        res = requests.get("https://api.sleeper.app/v1/players/nfl")
        if res.status_code == 200:
            PLAYERS_CACHE = res.json()
            CACHE_TIMESTAMP = now
    return PLAYERS_CACHE


def get_cached_trending():
    global TRENDING_CACHE, TRENDING_TIMESTAMP
    now = time.time()
    if not TRENDING_CACHE or (now - TRENDING_TIMESTAMP) > TRENDING_CACHE_DURATION:
        res = requests.get(
            "https://api.sleeper.app/v1/players/nfl/trending/add?lookback_hours=24&limit=250"
        )
        if res.status_code == 200:
            TRENDING_CACHE = {
                str(item["player_id"]): item.get("count", 0)
                for item in res.json()
            }
            TRENDING_TIMESTAMP = now
    return TRENDING_CACHE


@app.get("/")
def health_check():
    return {
        "status": "ok",
        "version": "1.4.0",
        "message": "Sleeper Free Agent & Roster API is live",
    }


@app.get("/roster")
def get_roster(
    league_id: str = Query(
        "1399229018905034752", description="Sleeper League ID"
    ),
    username: str = Query(
        "jgatz12",
        description="Sleeper username or display name to fetch roster for",
    ),
    format: str = Query("csv", description="Output format: 'csv' or 'json'"),
):
    # 1. Map users to identify owner_id
    users_res = requests.get(
        f"https://api.sleeper.app/v1/league/{league_id}/users"
    )
    if users_res.status_code != 200:
        return Response(
            content="Invalid League ID",
            status_code=400,
            media_type="text/plain",
        )

    target_user_id = None
    clean_username = username.strip().lower()
    for u in users_res.json():
        uname = (u.get("username") or "").lower()
        dname = (u.get("display_name") or "").lower()
        tname = (u.get("metadata", {}).get("team_name") or "").lower()
        if clean_username in [uname, dname, tname]:
            target_user_id = u.get("user_id")
            break

    # 2. Fetch rosters
    roster_res = requests.get(
        f"https://api.sleeper.app/v1/league/{league_id}/rosters"
    )
    if roster_res.status_code != 200:
        return Response(
            content="Could not fetch rosters",
            status_code=400,
            media_type="text/plain",
        )

    target_roster = None
    for r in roster_res.json():
        if r.get("owner_id") == target_user_id:
            target_roster = r
            break

    if not target_roster:
        return Response(
            content=f"Roster not found for user: {username}",
            status_code=404,
            media_type="text/plain",
        )

    all_players = get_cached_players()
    starters = set(target_roster.get("starters") or [])
    taxi = set(target_roster.get("taxi") or [])
    reserve = set(target_roster.get("reserve") or [])
    all_rostered = target_roster.get("players") or []

    roster_list = []
    for pid in all_rostered:
        str_pid = str(pid)
        info = all_players.get(str_pid, {})

        # Slot classification
        if str_pid in starters:
            slot = "STARTER"
        elif str_pid in taxi:
            slot = "TAXI"
        elif str_pid in reserve:
            slot = "IR"
        else:
            slot = "BENCH"

        roster_list.append(
            {
                "id": str_pid,
                "name": info.get("full_name")
                or f"{info.get('first_name', '')} {info.get('last_name', '')}".strip()
                or str_pid,
                "pos": "/".join(info.get("fantasy_positions") or ["DEF"]),
                "team": info.get("team") or "FA",
                "slot": slot,
                "depth_chart": info.get("depth_chart_order") or 99,
                "exp": info.get("years_exp") or 0,
            }
        )

    # Sort: Starters first, then Bench, Taxi, IR
    slot_order = {"STARTER": 1, "BENCH": 2, "TAXI": 3, "IR": 4}
    roster_list.sort(key=lambda x: (slot_order.get(x["slot"], 5), x["pos"]))

    if format.lower() == "json":
        return JSONResponse(content=roster_list)

    output = io.StringIO()
    fieldnames = ["id", "name", "pos", "team", "slot", "depth_chart", "exp"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(roster_list)

    return Response(content=output.getvalue(), media_type="text/csv")


@app.get("/free-agents")
def get_free_agents(
    league_id: str = Query(..., description="Sleeper League ID"),
    positions: Optional[str] = Query(
        None, description="Comma-separated list (e.g. RB,WR,TE)"
    ),
    active_teams_only: bool = Query(
        True,
        description="Require player to belong to one of the 32 active NFL teams",
    ),
    trending_only: bool = Query(
        False, description="Only return players actively trending as adds"
    ),
    sort_by: str = Query(
        "trending_count",
        description="Sort field: trending_count, depth_chart, name, or exp",
    ),
    limit: int = Query(
        75, le=200, description="Max players to return (max 200)"
    ),
    format: str = Query("csv", description="Response format: 'csv' or 'json'"),
):
    roster_res = requests.get(
        f"https://api.sleeper.app/v1/league/{league_id}/rosters"
    )
    if roster_res.status_code != 200:
        return Response(
            content="Invalid League ID",
            status_code=400,
            media_type="text/plain",
        )

    rostered_ids = set()
    for team in roster_res.json():
        if team.get("players"):
            for pid in team["players"]:
                rostered_ids.add(str(pid))

    all_players = get_cached_players()
    trending_map = get_cached_trending()

    if positions:
        target_positions = {
            pos.strip().upper() for pos in positions.split(",") if pos.strip()
        }
    else:
        target_positions = VALID_OFFENSIVE_POSITIONS

    candidates = []
    for pid, info in all_players.items():
        str_pid = str(pid)

        if str_pid in rostered_ids:
            continue

        raw_positions = set(info.get("fantasy_positions") or [])
        pos_str = info.get("position") or ""
        team = (info.get("team") or "").upper().strip()
        status = info.get("status")

        if raw_positions.intersection(DISALLOWED_POSITIONS) or (
            pos_str in DISALLOWED_POSITIONS
        ):
            continue

        matched_positions = raw_positions.intersection(target_positions)
        if not matched_positions:
            continue

        if active_teams_only and team not in ACTIVE_NFL_TEAMS:
            continue

        if status in ["Inactive", "Retired", None]:
            continue

        trend_count = trending_map.get(str_pid, 0)
        if trending_only and trend_count == 0:
            continue

        candidates.append(
            {
                "id": str_pid,
                "name": info.get("full_name")
                or f"{info.get('first_name', '')} {info.get('last_name', '')}".strip(),
                "pos": "/".join(matched_positions),
                "team": team,
                "status": status,
                "depth_chart": info.get("depth_chart_order") or 99,
                "exp": info.get("years_exp") or 0,
                "trending_count": trend_count,
            }
        )

    if sort_by == "trending_count":
        candidates.sort(
            key=lambda x: (x["trending_count"], -x["depth_chart"]), reverse=True
        )
    elif sort_by == "depth_chart":
        candidates.sort(key=lambda x: (x["depth_chart"], -x["trending_count"]))
    elif sort_by == "exp":
        candidates.sort(key=lambda x: x["exp"], reverse=True)
    elif sort_by == "name":
        candidates.sort(key=lambda x: x["name"].lower())

    results = candidates[:limit]

    if format.lower() == "json":
        return JSONResponse(content=results)

    output = io.StringIO()
    fieldnames = [
        "id",
        "name",
        "pos",
        "team",
        "status",
        "depth_chart",
        "exp",
        "trending_count",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(results)

    return Response(content=output.getvalue(), media_type="text/csv")