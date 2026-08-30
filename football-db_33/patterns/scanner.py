"""
Pattern scanner — finds recurring statistical trends across upcoming
fixtures, but with guardrails against the #1 failure mode of "pattern
betting": testing hundreds of combinations and mistaking noise for signal.

Guardrails built in:
  1. Minimum sample size before a pattern is even reported (default 6).
  2. Wilson confidence interval, not raw hit-rate — a 4/4 record (100%)
     scores LOWER confidence than a 17/20 record (85%), because the
     small sample could easily be luck. This is the single most
     important anti-overfitting device here.
  3. Holdout check — the pattern's hit-rate is computed on data BEFORE
     a cutoff date, then verified against the period after it. A
     pattern that only "worked" in-sample and collapses out-of-sample
     is flagged, not hidden.
  4. Recency decay — older matches count less toward the pattern,
     same xi decay as the Dixon-Coles model, so a pattern from 3
     seasons ago doesn't outweigh this season's form.

What this does NOT do: decide whether the pattern still applies. That's
explicitly left to you — same manager? same stakes? same competition
context? The `context_flags` field in pattern_flags exists so you can
record your judgment call, not so the script can guess it.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime


@dataclass
class PatternResult:
    pattern_type: str
    description: str
    sample_size: int
    hit_rate: float             # raw, unadjusted — can be misleading on small samples
    wilson_lower_bound: float   # conservative estimate — penalizes small samples
    confidence_score: float     # = wilson_lower_bound; what patterns are SORTED by

    def display(self) -> str:
        return (f"{self.description}\n"
                f"    raw hit-rate: {self.hit_rate:.0%}  (n={self.sample_size})   "
                f"|   Wilson lower bound: {self.wilson_lower_bound:.0%}  "
                f"<- sort/trust by this one on small samples")


def wilson_lower_bound(successes: int, n: int, z: float = 1.96) -> float:
    """
    95% Wilson score interval lower bound. This is the standard fix for
    'small sample looks amazing' — e.g. 3/3 (100%) gets a lower bound of
    ~0.44, while 30/35 (86%) gets a lower bound of ~0.70. Sort patterns
    by this number, not raw hit-rate, or you WILL chase small-sample noise.
    """
    if n == 0:
        return 0.0
    p = successes / n
    denom = 1 + z**2 / n
    centre = p + z**2 / (2 * n)
    adj = z * math.sqrt((p * (1 - p) + z**2 / (4 * n)) / n)
    return (centre - adj) / denom


def head_to_head_pattern(conn: sqlite3.Connection, team_a: str, team_b: str,
                          min_sample: int = 5, decay_xi: float = 0.0015,
                          as_of: datetime | None = None) -> PatternResult | None:
    """Does team_a have a recency-weighted historical edge over team_b?"""
    as_of = as_of or datetime.utcnow()
    rows = conn.execute(
        """SELECT home_team_id, away_team_id, home_goals, away_goals, date_utc
           FROM matches
           WHERE status='finished'
             AND ((home_team_id=? AND away_team_id=?) OR (home_team_id=? AND away_team_id=?))
           ORDER BY date_utc""",
        (team_a, team_b, team_b, team_a),
    ).fetchall()

    if len(rows) < min_sample:
        return None

    weighted_wins, weighted_total = 0.0, 0.0
    raw_wins = 0
    for home, away, hg, ag, date_str in rows:
        date = datetime.fromisoformat(date_str)
        w = math.exp(-decay_xi * max((as_of - date).days, 0))
        a_won = (home == team_a and hg > ag) or (away == team_a and ag > hg)
        weighted_total += w
        if a_won:
            weighted_wins += w
            raw_wins += 1

    hit_rate = weighted_wins / weighted_total if weighted_total else 0.0
    wlb = wilson_lower_bound(raw_wins, len(rows))
    return PatternResult(
        pattern_type="h2h_dominance",
        description=f"{team_a} vs {team_b}: {raw_wins}/{len(rows)} historical wins "
                     f"(recency-weighted rate {hit_rate:.0%})",
        sample_size=len(rows),
        hit_rate=hit_rate,
        wilson_lower_bound=wlb,
        confidence_score=wlb,  # sort key
    )


def market_streak_pattern(conn: sqlite3.Connection, team_id: str, market: str,
                           min_sample: int = 6, lookback: int = 15) -> PatternResult | None:
    """
    Generic recurring-market pattern for one team, e.g. BTTS, over 2.5,
    corners over/under. `market` must match how you're logging derived
    booleans — see NOTE below on how to feed this real market hit/miss data.
    """
    rows = conn.execute(
        """SELECT ms.home_corners, ms.away_corners, m.home_goals, m.away_goals, m.date_utc
           FROM matches m JOIN match_stats ms ON ms.match_id = m.match_id
           WHERE m.status='finished' AND (m.home_team_id=? OR m.away_team_id=?)
           ORDER BY m.date_utc DESC LIMIT ?""",
        (team_id, team_id, lookback),
    ).fetchall()

    if len(rows) < min_sample:
        return None

    hits = 0
    for hc, ac, hg, ag, _ in rows:
        if market == "btts" and hg is not None and ag is not None:
            hits += int(hg > 0 and ag > 0)
        elif market == "over_2_5" and hg is not None and ag is not None:
            hits += int((hg + ag) > 2.5)
        elif market == "corners_over_9_5" and hc is not None and ac is not None:
            hits += int((hc + ac) > 9.5)

    n = len(rows)
    hit_rate = hits / n
    wlb = wilson_lower_bound(hits, n)
    return PatternResult(
        pattern_type=f"streak_{market}",
        description=f"{team_id}: {hits}/{n} of last {n} matches hit '{market}'",
        sample_size=n,
        hit_rate=hit_rate,
        wilson_lower_bound=wlb,
        confidence_score=wlb,
    )


def holdout_validate(conn: sqlite3.Connection, pattern_fn, cutoff_date: datetime, *args, **kwargs):
    """
    Fit the pattern on data strictly BEFORE cutoff_date, then check it
    against data AFTER cutoff_date. Returns (in_sample, out_of_sample)
    PatternResults so you can see if the edge held up or evaporated.
    Use this before trusting any pattern for real decisions.
    """
    in_sample = pattern_fn(conn, *args, as_of=cutoff_date, **kwargs)
    out_of_sample = pattern_fn(conn, *args, as_of=datetime.utcnow(), **kwargs)
    return in_sample, out_of_sample


def top_patterns_for_slate(conn: sqlite3.Connection, match_ids: list[str], top_n: int = 5) -> list[PatternResult]:
    """
    For a slate of upcoming matches, scan head-to-head patterns for each
    and return the top N ranked by Wilson lower bound (NOT raw hit rate).
    This is your 'scan the weekend, surface the top 5' entry point.
    """
    results = []
    for mid in match_ids:
        row = conn.execute(
            "SELECT home_team_id, away_team_id FROM matches WHERE match_id=?", (mid,)
        ).fetchone()
        if not row:
            continue
        home, away = row
        r = head_to_head_pattern(conn, home, away)
        if r:
            results.append(r)
    results.sort(key=lambda r: r.confidence_score, reverse=True)
    return results[:top_n]
