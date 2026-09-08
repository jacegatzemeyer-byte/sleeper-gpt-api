import csv
import io
import time
from typing import Optional
from fastapi import FastAPI, Query, Response
from fastapi.responses import JSONResponse
import requests

app = FastAPI(
    title="Sleeper Free Agent API",
    version="1.2.1",
    description="Filtered free-agent lookup for Sleeper fantasy leagues.",
)

PLAYERS_CACHE = {}
CACHE_TIMESTAMP = 0
CACHE_DURATION = 86400  # 24 hours

TRENDING_CACHE = {}
TRENDING_TIMESTAMP = 0
TRENDING_CACHE_DURATION = 900  # 15 minutes

# Strictly allowed offensive fantasy positions (excludes OL, DL, LB, DB, etc.)
VALID_FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}


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
            "https://api.sleeper.app/v1/players/nfl/trending/add?lookback_hours=24&limit=200"
        )
        if res.status_code == 200:
            TRENDING_CACHE = {
                item["player_id"]: item.get("count", 0) for item in res.json()
            }
            TRENDING_TIMESTAMP = now
    return TRENDING_CACHE


@app.get("/")
def health_check():
    return {"status": "ok", "message": "Sleeper Free Agent API is live"}


@app.get("/free-agents")
def get_free_agents(
    league_id: str = Query(..., description="Sleeper League ID"),
    positions: Optional[str] = Query(
        None, description="Comma-separated list (e.g. RB,WR,TE)"
    ),
    active_teams_only: bool = Query(
        True,
        description="Require player to be signed to an active NFL franchise (filters out FAs and retired players)",
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
            rostered_ids.update(team["players"])

    all_players = get_cached_players()
    trending_map = get_cached_trending()

    # Determine allowed positions
    if positions:
        target_positions = {
            pos.strip().upper() for pos in positions.split(",") if pos.strip()
        }
    else:
        target_positions = VALID_FANTASY_POSITIONS

    candidates = []
    for pid, info in all_players.items():
        if pid in rostered_ids:
            continue

        raw_positions = set(info.get("fantasy_positions") or [])
        status = info.get("status")
        team = info.get("team")

        # 1. Hard filter: Must match an offensive fantasy skill position
        matched_positions = raw_positions.intersection(target_positions)
        if not matched_positions:
            continue

        # 2. Hard filter: Exclude non-active / retired statuses
        if status in ["Inactive", "Retired", None]:
            continue

        # 3. Hard filter: Exclude unsigned players (removes Bell, Gallup, free agents)
        if active_teams_only and (not team or team in ["FA", "FREE_AGENT"]):
            continue

        trend_count = trending_map.get(pid, 0)
        if trending_only and trend_count == 0:
            continue

        candidates.append(
            {
                "id": str(pid),
                "name": info.get("full_name")
                or f"{info.get('first_name', '')} {info.get('last_name', '')}".strip(),
                "pos": "/".join(matched_positions),
                "team": team or "FA",
                "status": status,
                "depth_chart": info.get("depth_chart_order") or 99,
                "exp": info.get("years_exp") or 0,
                "trending_count": trend_count,
            }
        )

    # Sorting logic
    if sort_by == "trending_count":
        candidates.sort(key=lambda x: x["trending_count"], reverse=True)
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