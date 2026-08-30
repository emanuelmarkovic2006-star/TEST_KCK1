"""
The day-to-day driver. Fits the model on everything in the database up
to today, then predicts every scheduled match in the next N days —
folding in your manual rating adjustments from user_ratings, and
surfacing the top pattern-flagged matches for you to sanity-check.

Usage:
    python predict.py --db football.db --league premier_league --days 3
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone

import sys
sys.path.insert(0, "model")
sys.path.insert(0, "patterns")
from dixon_coles import DixonColesModel   # noqa: E402
from scanner import top_patterns_for_slate  # noqa: E402


def load_finished(conn, league_id):
    rows = conn.execute(
        """SELECT home_team_id, away_team_id, home_goals, away_goals, date_utc
           FROM matches WHERE league_id=? AND status='finished'""",
        (league_id,),
    ).fetchall()
    return [
        {"home": r[0], "away": r[1], "home_goals": r[2], "away_goals": r[3],
         "date": datetime.fromisoformat(r[4])}
        for r in rows
    ]


def load_upcoming(conn, league_id, days):
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    return conn.execute(
        """SELECT match_id, home_team_id, away_team_id, date_utc
           FROM matches
           WHERE league_id=? AND status='scheduled' AND date_utc BETWEEN ? AND ?
           ORDER BY date_utc""",
        (league_id, now.isoformat(), horizon.isoformat()),
    ).fetchall()


def load_user_ratings(conn):
    """Most recent adjustment per team, if you've set any."""
    rows = conn.execute(
        """SELECT team_id, attack_adj, defense_adj FROM user_ratings ur
           WHERE as_of_date = (SELECT MAX(as_of_date) FROM user_ratings WHERE team_id = ur.team_id)"""
    ).fetchall()
    attack_adj = {r[0]: r[1] for r in rows}
    defense_adj = {r[0]: r[2] for r in rows}
    return attack_adj, defense_adj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="football.db")
    ap.add_argument("--league", required=True, help="internal league_id, e.g. premier_league")
    ap.add_argument("--days", type=int, default=3)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)

    finished = load_finished(conn, args.league)
    if len(finished) < 30:
        print(f"Only {len(finished)} finished matches for '{args.league}' — need more history "
              f"for a stable fit. Run the ingest script for a few more seasons first.")
        return

    model = DixonColesModel()
    model.fit(finished, as_of=datetime.now(timezone.utc))
    attack_adj, defense_adj = load_user_ratings(conn)

    upcoming = load_upcoming(conn, args.league, args.days)
    if not upcoming:
        print(f"No scheduled matches for '{args.league}' in the next {args.days} days in the DB.\n"
              f"(Ingest scripts currently only pull finished results — add a fixtures source to "
              f"populate 'scheduled' rows, e.g. the ESPN or API-Football fixtures endpoint.)")
        return

    print(f"{'Match':<45} {'1':>6} {'X':>6} {'2':>6} {'O2.5':>6} {'BTTS':>6}")
    print("-" * 80)
    match_ids = []
    for match_id, home, away, date_str in upcoming:
        match_ids.append(match_id)
        if home not in model.teams or away not in model.teams:
            print(f"{home} vs {away:<20} -- insufficient history, skipped")
            continue
        p = model.match_probs(home, away, attack_adj=attack_adj, defense_adj=defense_adj)
        label = f"{home} vs {away}"
        print(f"{label:<45} {p['p_home_win']:>6.0%} {p['p_draw']:>6.0%} {p['p_away_win']:>6.0%} "
              f"{p['p_over_2_5']:>6.0%} {p['p_btts_yes']:>6.0%}")

    print()
    print("Top pattern-flagged matches — showing BOTH numbers, you decide which to weight:")
    patterns = top_patterns_for_slate(conn, match_ids, top_n=5)
    if not patterns:
        print("  (none met the minimum sample size)")
    for p in patterns:
        print(f"  {p.display()}")
        print(f"         -> now check yourself: same manager? same stakes? "
              f"squad changed much since these games?")


if __name__ == "__main__":
    main()
