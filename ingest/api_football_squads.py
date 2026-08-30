"""
Pulls full squads (every player, every club) from API-Football via
RapidAPI. Free tier = 100 requests/day, 1 request per team's squad, so
this is deliberately paced and resumable — same progress.json pattern
as ingest_all.py, so it just picks up tomorrow where today's quota ran out.

SETUP (one-time, ~2 minutes, free):
    1. Go to https://rapidapi.com/api-sports/api/api-football
    2. Sign up (free), subscribe to the free "Basic" plan (0 cost)
    3. Copy your API key from the RapidAPI dashboard
    4. Set it as an environment variable: API_FOOTBALL_KEY=xxxx
       (in GitHub Actions: add it as a repo secret, see workflow file)

Usage:
    export API_FOOTBALL_KEY=your_key_here
    python ingest/api_football_squads.py --db football.db --season 2025

This needs team_ids to already exist. It maps our team_id -> API-Football's
numeric team id the first time it sees a team, by searching on name, and
caches that mapping in teams.source_ids so it's a one-time lookup per team.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests

BASE_URL = "https://api-football-v1.p.rapidapi.com/v3"
PROGRESS_FILE = "player_progress.json"
DAILY_LIMIT = 95  # leave a small buffer under the 100/day free cap


def api_get(path: str, params: dict, api_key: str) -> dict:
    headers = {
        "x-rapidapi-host": "api-football-v1.p.rapidapi.com",
        "x-rapidapi-key": api_key,
    }
    resp = requests.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"API error: {data['errors']}")
    return data


def find_team_api_id(conn, team_id: str, team_name: str, api_key: str) -> int | None:
    row = conn.execute("SELECT source_ids FROM teams WHERE team_id=?", (team_id,)).fetchone()
    if row and row[0]:
        ids = json.loads(row[0])
        if "api_football" in ids:
            return ids["api_football"]

    data = api_get("/teams", {"search": team_name}, api_key)
    results = data.get("response", [])
    if not results:
        return None
    api_id = results[0]["team"]["id"]

    existing = json.loads(row[0]) if row and row[0] else {}
    existing["api_football"] = api_id
    conn.execute("UPDATE teams SET source_ids=? WHERE team_id=?", (json.dumps(existing), team_id))
    conn.commit()
    return api_id


def player_slug(name: str, team_id: str) -> str:
    base = name.lower().strip().replace(" ", "_").replace("'", "").replace(".", "")
    return f"{team_id}_{base}"


def ingest_squad(conn, team_id: str, api_team_id: int, season: int, api_key: str) -> int:
    data = api_get("/players/squads", {"team": api_team_id}, api_key)
    response = data.get("response", [])
    if not response:
        return 0
    players = response[0].get("players", [])

    for p in players:
        pid = player_slug(p["name"], team_id)
        photo = p.get("photo")
        conn.execute(
            """INSERT INTO players (player_id, name, nationality, position, photo_url, source_ids)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(player_id) DO UPDATE SET
                 position=excluded.position, photo_url=excluded.photo_url""",
            (pid, p["name"], None, p.get("position"), photo,
             json.dumps({"api_football": p.get("id")})),
        )
        conn.execute(
            """INSERT INTO player_team_history (player_id, team_id, season, squad_number, is_current)
               VALUES (?,?,?,?,1)
               ON CONFLICT(player_id, team_id, season) DO UPDATE SET squad_number=excluded.squad_number""",
            (pid, team_id, str(season), p.get("number")),
        )
    conn.commit()
    return len(players)


def load_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"completed_teams": [], "failed": [], "requests_used_today": 0, "date": None}


def save_progress(p: dict):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(p, f, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="football.db")
    ap.add_argument("--season", type=int, default=datetime.now().year)
    args = ap.parse_args()

    api_key = os.environ.get("API_FOOTBALL_KEY")
    if not api_key:
        print("ERROR: set the API_FOOTBALL_KEY environment variable first. See the setup "
              "instructions at the top of this file.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    teams = conn.execute("SELECT team_id, name FROM teams ORDER BY team_id").fetchall()

    progress = load_progress()
    today = datetime.now(timezone.utc).date().isoformat()
    if progress.get("date") != today:
        progress["requests_used_today"] = 0
        progress["date"] = today

    remaining_teams = [t for t in teams if t[0] not in progress["completed_teams"]]
    print(f"{len(teams)} total teams. {len(remaining_teams)} still need squads pulled.")
    print(f"Today's quota used so far: {progress['requests_used_today']}/{DAILY_LIMIT}")

    for team_id, team_name in remaining_teams:
        if progress["requests_used_today"] >= DAILY_LIMIT:
            print(f"\nHit today's quota ({DAILY_LIMIT} requests). Stopping cleanly — "
                  f"re-run this script tomorrow (or let the scheduled workflow do it) "
                  f"and it continues from here. {len(remaining_teams)} teams still remaining.")
            break

        try:
            api_id = find_team_api_id(conn, team_id, team_name, api_key)
            progress["requests_used_today"] += 1
            if api_id is None:
                print(f"  {team_id}: no API-Football match found for '{team_name}', skipping")
                progress["failed"].append({"team_id": team_id, "reason": "no match found"})
                progress["completed_teams"].append(team_id)  # don't retry forever
                save_progress(progress)
                continue

            n = ingest_squad(conn, team_id, api_id, args.season, api_key)
            progress["requests_used_today"] += 1
            print(f"  {team_id}: {n} players")
            progress["completed_teams"].append(team_id)
            save_progress(progress)
            time.sleep(1)  # be polite to the API

        except Exception as e:
            print(f"  {team_id}: FAILED - {type(e).__name__}: {str(e)[:150]}")
            progress["failed"].append({"team_id": team_id, "error": str(e)[:300],
                                        "at": datetime.now(timezone.utc).isoformat()})
            save_progress(progress)
            time.sleep(5)

    conn.close()
    total_players = sqlite3.connect(args.db).execute("SELECT COUNT(*) FROM players").fetchone()[0]
    print(f"\n{total_players} total players in database. "
          f"{len(progress['completed_teams'])}/{len(teams)} teams done overall.")


if __name__ == "__main__":
    main()
