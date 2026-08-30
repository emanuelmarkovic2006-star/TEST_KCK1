"""
Backtest the Dixon-Coles model: fit on data up to a cutoff, predict the
matches AFTER the cutoff, then score those predictions against what
actually happened. This is the only real way to know if the model is
worth anything — not eyeballing a few games, but scoring hundreds.

Two scores, both "lower is better":
  - Log loss: heavily punishes confident wrong predictions.
  - Brier score: mean squared error between predicted probability and
    outcome (0 or 1). More forgiving than log loss, easier to interpret.

Also reports the SAME scores for the market's own closing odds (implied
probability, de-vigged). If your model's log loss is close to or better
than the market's, you're onto something real. If it's much worse, the
model needs more work before you trust it for anything.

Usage:
    python model/backtest.py --db football.db --league E0 \
        --train-through 2024-06-01 --test-through 2025-06-01
"""
from __future__ import annotations

import argparse
import math
import sqlite3
from datetime import datetime

import numpy as np

from dixon_coles import DixonColesModel


def load_matches(conn, league_id, before: datetime | None = None, after: datetime | None = None):
    q = """SELECT home_team_id, away_team_id, home_goals, away_goals, date_utc, match_id
           FROM matches WHERE league_id=? AND status='finished'"""
    params = [league_id]
    if before:
        q += " AND date_utc < ?"
        params.append(before.isoformat())
    if after:
        q += " AND date_utc >= ?"
        params.append(after.isoformat())
    q += " ORDER BY date_utc"
    rows = conn.execute(q, params).fetchall()
    return [
        {"home": r[0], "away": r[1], "home_goals": r[2], "away_goals": r[3],
         "date": datetime.fromisoformat(r[4]), "match_id": r[5]}
        for r in rows
    ]


def devig_1x2(odds_h, odds_d, odds_a):
    """Remove the bookmaker's margin (overround) to get 'fair' implied probabilities."""
    inv = [1 / odds_h, 1 / odds_d, 1 / odds_a]
    overround = sum(inv)
    return [x / overround for x in inv]


def get_closing_odds(conn, match_id):
    rows = conn.execute(
        "SELECT outcome, odds_decimal FROM odds_snapshots WHERE match_id=? AND market='1x2'",
        (match_id,),
    ).fetchall()
    d = {outcome: odds for outcome, odds in rows}
    if {"home", "draw", "away"} <= d.keys():
        return devig_1x2(d["home"], d["draw"], d["away"])
    return None


def log_loss(y_true_idx: int, probs: list[float]) -> float:
    p = max(min(probs[y_true_idx], 1 - 1e-15), 1e-15)
    return -math.log(p)


def brier_score(y_true_idx: int, probs: list[float]) -> float:
    y = [1.0 if i == y_true_idx else 0.0 for i in range(len(probs))]
    return sum((p - yt) ** 2 for p, yt in zip(probs, y))


def outcome_idx(hg: int, ag: int) -> int:
    if hg > ag:
        return 0  # home
    if hg == ag:
        return 1  # draw
    return 2      # away


def run_backtest(db_path: str, league_id: str, train_through: str, test_through: str | None):
    conn = sqlite3.connect(db_path)
    train_cutoff = datetime.fromisoformat(train_through)
    test_cutoff = datetime.fromisoformat(test_through) if test_through else None

    train = load_matches(conn, league_id, before=train_cutoff)
    test = load_matches(conn, league_id, after=train_cutoff, before=test_cutoff)

    if len(train) < 50:
        print(f"WARNING: only {len(train)} training matches — fit will be unstable.")
    if not test:
        print("No test matches in the requested window. Nothing to score.")
        return

    print(f"Training on {len(train)} matches (through {train_through})")
    print(f"Testing on {len(test)} matches...")

    model = DixonColesModel()
    model.fit(train, as_of=train_cutoff)

    model_ll, model_bs = [], []
    market_ll, market_bs = [], []
    n_with_odds = 0

    for m in test:
        if m["home"] not in model.teams or m["away"] not in model.teams:
            continue  # promoted/relegated team with no training history — skip fairly
        probs = model.match_probs(m["home"], m["away"])
        p_list = [probs["p_home_win"], probs["p_draw"], probs["p_away_win"]]
        y = outcome_idx(m["home_goals"], m["away_goals"])
        model_ll.append(log_loss(y, p_list))
        model_bs.append(brier_score(y, p_list))

        market_probs = get_closing_odds(conn, m["match_id"])
        if market_probs:
            n_with_odds += 1
            market_ll.append(log_loss(y, market_probs))
            market_bs.append(brier_score(y, market_probs))

    print()
    print(f"Scored {len(model_ll)} matches (model), {n_with_odds} had closing odds available.")
    print()
    print(f"{'Metric':<20} {'Model':>10} {'Market':>10}   (lower = better)")
    print(f"{'Log loss':<20} {np.mean(model_ll):>10.4f} {np.mean(market_ll) if market_ll else float('nan'):>10.4f}")
    print(f"{'Brier score':<20} {np.mean(model_bs):>10.4f} {np.mean(market_bs) if market_bs else float('nan'):>10.4f}")
    print()
    if market_ll:
        gap = np.mean(model_ll) - np.mean(market_ll)
        if gap < 0.02:
            print("Model is roughly matching the market's closing line. That's a genuinely good sign.")
        elif gap < 0.1:
            print("Model is somewhat behind the market — usable, but there's room to improve"
                  " (more data, better team strength priors, or injury/lineup features).")
        else:
            print("Model is well behind the market. Don't trust it for real decisions yet —"
                  " needs more training data or a feature the market is pricing that the model isn't.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="football.db")
    ap.add_argument("--league", required=True)
    ap.add_argument("--train-through", required=True, help="YYYY-MM-DD")
    ap.add_argument("--test-through", default=None, help="YYYY-MM-DD, omit for 'through today'")
    args = ap.parse_args()
    run_backtest(args.db, args.league, args.train_through, args.test_through)


if __name__ == "__main__":
    main()
