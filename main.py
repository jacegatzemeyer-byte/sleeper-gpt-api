import csv
import io
import os
import time
from typing import Optional
from fastapi import FastAPI, Query, Response
import requests

app = FastAPI(title="Sleeper Free Agent Tool")

PLAYERS_CACHE = {}
CACHE_TIMESTAMP = 0
CACHE_DURATION = 86400  # 24 hours


def get_cached_players():
    global PLAYERS_CACHE, CACHE_TIMESTAMP
    now = time.time()
    if not PLAYERS_CACHE or (now - CACHE_TIMESTAMP) > CACHE_DURATION:
        res = requests.get("https://api.sleeper.app/v1/players/nfl")
        PLAYERS_CACHE = res.json()
        CACHE_TIMESTAMP = now
    return PLAYERS_CACHE


@app.get("/")
def health_check():
    return {"status": "ok", "message": "Sleeper Free Agent API is live"}


@app.get("/free-agents")
def get_free_agents(
    league_id: str,
    position: Optional[str] = Query(None, description="QB, RB, WR, TE, K, DEF"),
    limit: int = Query(75, description="Max players to return"),
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
    available = []

    for pid, info in all_players.items():
        if pid in rostered_ids:
            continue

        pos_list = info.get("fantasy_positions") or []
        status = info.get("status")

        if not pos_list or status in ["Inactive", None]:
            continue

        if position and position.upper() not in pos_list:
            continue

        available.append(
            {
                "id": pid,
                "name": info.get("full_name"),
                "pos": "/".join(pos_list),
                "team": info.get("team") or "FA",
                "status": status,
                "exp": info.get("years_exp", 0),
            }
        )

        if len(available) >= limit:
            break

    output = io.StringIO()
    writer = csv.DictWriter(
        output, fieldnames=["id", "name", "pos", "team", "status", "exp"]
    )
    writer.writeheader()
    writer.writerows(available)

    return Response(content=output.getvalue(), media_type="text/csv")