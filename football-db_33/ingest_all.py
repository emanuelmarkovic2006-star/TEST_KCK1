"""
Ingests ALL leagues in config/leagues.csv for the last N seasons in one
command. Safe to stop and re-run — it uses INSERT OR REPLACE / OR IGNORE
throughout, so re-running never creates duplicates, and progress.json
tracks what's already done so a re-run skips completed work fast.

This is the script the autonomous loop (run_forever.sh) calls repeatedly.

Usage:
    python ingest_all.py --db football.db --seasons 5
    (seasons=5 means the 5 most recently completed/current seasons)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

sys.path.insert(0, "ingest")
from football_data_co_uk import fetch_season_csv, ingest_dataframe  # noqa: E402

PROGRESS_FILE = "progress.json"


def season_codes(n: int, end_year: int | None = None) -> list[str]:
    """
    football-data.co.uk season codes are 'YYZZ' e.g. '2526' = 2025-26.
    Returns the n most recent season codes ending at end_year (defaults
    to whatever season we're currently in, guessed from today's date —
    European seasons run Aug-May, so if it's before August we're still
    "in" the season that started last year).
    """
    today = datetime.now()
    current_start_year = today.year if today.month >= 7 else today.year - 1
    years = [current_start_year - i for i in range(n)]
    return [f"{str(y)[2:]}{str(y + 1)[2:]}" for y in years]


def load_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"completed": [], "failed": [], "last_run": None}


def save_progress(progress: dict):
    progress["last_run"] = datetime.now().isoformat()
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="football.db")
    ap.add_argument("--seasons", type=int, default=5, help="how many seasons back")
    ap.add_argument("--leagues-csv", default="config/leagues.csv")
    ap.add_argument("--retry-delay", type=int, default=30,
                     help="seconds to wait after a failure before continuing to the next job")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    with open(args.leagues_csv) as f:
        leagues = list(csv.DictReader(f))

    seasons = season_codes(args.seasons)
    progress = load_progress()
    completed = set(tuple(x) for x in progress["completed"])

    jobs = [(lg["league_id"], lg["fd_code"], lg["name"], lg["country"], season)
            for lg in leagues for season in seasons]
    remaining = [j for j in jobs if (j[0], j[4]) not in completed]

    print(f"{len(jobs)} total (league, season) jobs. {len(remaining)} remaining "
          f"({len(jobs) - len(remaining)} already done from a previous run).")

    for league_id, fd_code, name, country, season in remaining:
        conn.execute("INSERT OR IGNORE INTO leagues (league_id, name, country) VALUES (?,?,?)",
                     (league_id, name, country))
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {league_id} {season} ({fd_code})... ", end="", flush=True)
        try:
            df = fetch_season_csv(fd_code, season)
            n_in, n_skip = ingest_dataframe(conn, df, league_id)
            print(f"OK: {n_in} matches")
            progress["completed"].append([league_id, season])
            save_progress(progress)
        except Exception as e:
            print(f"FAILED: {type(e).__name__}: {str(e)[:150]}")
            progress["failed"].append({"league_id": league_id, "season": season,
                                        "error": str(e)[:300], "at": datetime.now().isoformat()})
            save_progress(progress)
            time.sleep(args.retry_delay)

    conn.close()

    total_matches = sqlite3.connect(args.db).execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    print()
    print(f"Run complete. {total_matches} total matches in database.")
    print(f"{len(progress['completed'])}/{len(jobs)} jobs done overall (across all runs).")
    if progress["failed"]:
        print(f"{len(progress['failed'])} failures logged in progress.json — re-run this "
              f"script and it'll retry them (failed jobs aren't marked completed).")


if __name__ == "__main__":
    main()
