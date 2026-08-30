"""
Ingests historic match results + closing odds from football-data.co.uk.

Why this source first: no API key, no anti-bot, no JS rendering — just
plain CSV files. It's the single most reliable free source for training
data (results back to the 1990s for major leagues, with Pinnacle/Bet365
closing odds included from ~2012 onward). This is what you backtest against.

Usage:
    python ingest/football_data_co_uk.py --league E0 --seasons 2223 2324 2425 2526

League codes (football-data.co.uk convention):
    E0 = Premier League      SP1 = La Liga        I1 = Serie A
    D1 = Bundesliga          F1 = Ligue 1          E1 = Championship
    (full list: https://www.football-data.co.uk/notes.txt)
"""
import argparse
import io
import sqlite3
import sys
from datetime import datetime, timezone

import pandas as pd
import requests

BASE_URL = "https://www.football-data.co.uk/mmz4281/{season}/{league}.csv"

# football-data.co.uk column -> our schema
COL_MAP = {
    "Date": "date", "HomeTeam": "home", "AwayTeam": "away",
    "FTHG": "home_goals", "FTAG": "away_goals",
    "HS": "home_shots", "AS": "away_shots",
    "HST": "home_shots_on_target", "AST": "away_shots_on_target",
    "HC": "home_corners", "AC": "away_corners",
    "HF": "home_fouls", "AF": "away_fouls",
    "HY": "home_yellow_cards", "AY": "away_yellow_cards",
    "HR": "home_red_cards", "AR": "away_red_cards",
    "Referee": "referee",
    # Pinnacle closing odds (the sharpest book — best proxy for "true" probability)
    "PSCH": "pinnacle_home", "PSCA": "pinnacle_away", "PSCD": "pinnacle_draw",
    # Bet365 as fallback (more historical coverage than Pinnacle)
    "B365H": "b365_home", "B365D": "b365_draw", "B365A": "b365_away",
}


def fetch_season_csv(league: str, season: str) -> pd.DataFrame:
    """season like '2526' for 2025-26."""
    url = BASE_URL.format(season=season, league=league)
    resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text), encoding_errors="ignore")
    df = df.rename(columns=COL_MAP)
    df["season"] = season
    df["league_code"] = league
    return df


def team_slug(name: str) -> str:
    return name.lower().strip().replace(" ", "_").replace("'", "").replace(".", "")


def upsert_team(conn: sqlite3.Connection, name: str) -> str:
    tid = team_slug(name)
    conn.execute(
        "INSERT OR IGNORE INTO teams (team_id, name) VALUES (?, ?)", (tid, name)
    )
    return tid


def ingest_dataframe(conn: sqlite3.Connection, df: pd.DataFrame, league_id: str):
    inserted, skipped = 0, 0
    for _, row in df.iterrows():
        if pd.isna(row.get("date")) or pd.isna(row.get("home")):
            skipped += 1
            continue
        home_id = upsert_team(conn, row["home"])
        away_id = upsert_team(conn, row["away"])
        try:
            dt = pd.to_datetime(row["date"], dayfirst=True).strftime("%Y-%m-%dT00:00:00")
        except Exception:
            skipped += 1
            continue
        match_id = f"fdcuk_{league_id}_{row['season']}_{home_id}_{away_id}_{dt[:10]}"

        conn.execute(
            """INSERT OR REPLACE INTO matches
               (match_id, league_id, season, date_utc, home_team_id, away_team_id,
                home_goals, away_goals, status, referee)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (match_id, league_id, row["season"], dt, home_id, away_id,
             _safe_int(row.get("home_goals")), _safe_int(row.get("away_goals")),
             "finished" if not pd.isna(row.get("home_goals")) else "scheduled",
             row.get("referee")),
        )

        conn.execute(
            """INSERT OR REPLACE INTO match_stats
               (match_id, home_shots, away_shots, home_shots_on_target, away_shots_on_target,
                home_corners, away_corners, home_fouls, away_fouls,
                home_yellow_cards, away_yellow_cards, home_red_cards, away_red_cards)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (match_id, _safe_int(row.get("home_shots")), _safe_int(row.get("away_shots")),
             _safe_int(row.get("home_shots_on_target")), _safe_int(row.get("away_shots_on_target")),
             _safe_int(row.get("home_corners")), _safe_int(row.get("away_corners")),
             _safe_int(row.get("home_fouls")), _safe_int(row.get("away_fouls")),
             _safe_int(row.get("home_yellow_cards")), _safe_int(row.get("away_yellow_cards")),
             _safe_int(row.get("home_red_cards")), _safe_int(row.get("away_red_cards"))),
        )

        # closing odds snapshot — prefer Pinnacle, fall back to Bet365
        now = datetime.now(timezone.utc).isoformat()
        for book_prefix, book_name in [("pinnacle", "pinnacle"), ("b365", "bet365")]:
            h, d, a = row.get(f"{book_prefix}_home"), row.get(f"{book_prefix}_draw"), row.get(f"{book_prefix}_away")
            if pd.isna(h) or pd.isna(d) or pd.isna(a):
                continue
            for outcome, val in [("home", h), ("draw", d), ("away", a)]:
                conn.execute(
                    """INSERT INTO odds_snapshots (match_id, bookmaker, captured_at_utc, market, outcome, odds_decimal)
                       VALUES (?,?,?,?,?,?)""",
                    (match_id, book_name, now, "1x2", outcome, float(val)),
                )
            break  # only store one book's closing line per match to avoid dupes on re-run

        inserted += 1
    conn.commit()
    return inserted, skipped


def _safe_int(v):
    try:
        if pd.isna(v):
            return None
        return int(v)
    except (ValueError, TypeError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", required=True, help="e.g. E0, SP1, I1, D1, F1")
    ap.add_argument("--league-id", default=None, help="our internal league_id; defaults to --league")
    ap.add_argument("--seasons", nargs="+", required=True, help="e.g. 2324 2425 2526")
    ap.add_argument("--db", default="football.db")
    args = ap.parse_args()

    league_id = args.league_id or args.league
    conn = sqlite3.connect(args.db)
    conn.execute("INSERT OR IGNORE INTO leagues (league_id, name, country) VALUES (?,?,?)",
                 (league_id, league_id, "unknown"))

    total_in, total_skip = 0, 0
    for season in args.seasons:
        print(f"Fetching {args.league} {season}...", file=sys.stderr)
        try:
            df = fetch_season_csv(args.league, season)
        except requests.HTTPError as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            continue
        n_in, n_skip = ingest_dataframe(conn, df, league_id)
        total_in += n_in
        total_skip += n_skip
        print(f"  {n_in} matches ingested, {n_skip} rows skipped", file=sys.stderr)

    conn.close()
    print(f"Done. {total_in} total matches ingested into {args.db}", file=sys.stderr)


if __name__ == "__main__":
    main()
