import csv
import io
import time
from typing import Optional
from fastapi import FastAPI, Query, Response
from fastapi.responses import JSONResponse
import requests

app = FastAPI(
    title="Sleeper Free Agent API",
    version="1.3.0",
    description="Filtered free-agent lookup for Sleeper fantasy leagues.",
)

PLAYERS_CACHE = {}
CACHE_TIMESTAMP = 0
CACHE_DURATION = 86400  # 24 hours

TRENDING_CACHE = {}
TRENDING_TIMESTAMP = 0
TRENDING_CACHE_DURATION = 900  # 15 minutes

# The 32 active NFL franchise codes (strictly blocks FA, None, and defunct teams)
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

# Strictly standard offensive fantasy positions
VALID_OFFENSIVE_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}

# Positions that disqualify a player from standard fantasy pools
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
        "version": "1.3.0",
        "message": "Sleeper Free Agent API is live",
    }


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
    # 1. Fetch rosters
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

    # Determine allowed positions
    if positions:
        target_positions = {
            pos.strip().upper() for pos in positions.split(",") if pos.strip()
        }
    else:
        target_positions = VALID_OFFENSIVE_POSITIONS

    candidates = []
    for pid, info in all_players.items():
        str_pid = str(pid)

        # Exclude rostered players
        if str_pid in rostered_ids:
            continue

        raw_positions = set(info.get("fantasy_positions") or [])
        pos_str = info.get("position") or ""
        team = (info.get("team") or "").upper().strip()
        status = info.get("status")

        # Reject IDP / Linemen
        if raw_positions.intersection(DISALLOWED_POSITIONS) or (
            pos_str in DISALLOWED_POSITIONS
        ):
            continue

        # Match against target offensive skill positions
        matched_positions = raw_positions.intersection(target_positions)
        if not matched_positions:
            continue

        # Require an active 32-team franchise
        if active_teams_only and team not in ACTIVE_NFL_TEAMS:
            continue

        # Exclude inactive / retired
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

    # Sorting
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