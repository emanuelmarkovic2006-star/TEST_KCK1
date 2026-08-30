"""
Dixon-Coles model for football match prediction.

Why not plain independent Poisson: two teams' goal counts aren't actually
independent — low-scoring results (0-0, 1-0, 0-1, 1-1) are more correlated
in reality than a naive Poisson x Poisson model predicts. Dixon & Coles
(1997) fixed this with a small correction (rho) applied only to
low-scoring cells, plus a time-decay weight so recent form matters more
than results from a year ago.

Each team gets two learned numbers:
    attack[team]  — how many goals they tend to score, relative to average
    defense[team] — how many goals they tend to concede, relative to average
Plus one global home-advantage constant.

Expected home goals    = exp(attack[home] + defense[away] + home_adv)
Expected away goals    = exp(attack[away] + defense[home])

Your `user_ratings` table nudges attack/defense AFTER fitting — a
deliberate, visible adjustment layered on top of the objective fit,
not mixed into it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from scipy.optimize import minimize
from scipy.stats import poisson


@dataclass
class DixonColesModel:
    teams: list[str] = field(default_factory=list)
    attack: dict[str, float] = field(default_factory=dict)
    defense: dict[str, float] = field(default_factory=dict)
    home_adv: float = 0.25
    rho: float = -0.1          # low-score correlation term, fit from data
    xi: float = 0.0018         # time-decay rate (per day); ~half-life of 1 year

    # -- fitting -----------------------------------------------------------

    def fit(self, matches: list[dict], as_of: datetime | None = None):
        """
        matches: list of dicts with keys
            home, away, home_goals, away_goals, date (datetime)
        """
        as_of = as_of or max(m["date"] for m in matches)
        self.teams = sorted({m["home"] for m in matches} | {m["away"] for m in matches})
        n = len(self.teams)
        idx = {t: i for i, t in enumerate(self.teams)}

        weights = np.array([
            math.exp(-self.xi * max((as_of - m["date"]).days, 0)) for m in matches
        ])

        def unpack(params):
            attack = params[:n]
            defense = params[n:2 * n]
            home_adv, rho = params[2 * n], params[2 * n + 1]
            return attack, defense, home_adv, rho

        def neg_log_likelihood(params):
            attack, defense, home_adv, rho = unpack(params)
            ll = 0.0
            for w, m in zip(weights, matches):
                hi, ai = idx[m["home"]], idx[m["away"]]
                lam_h = math.exp(attack[hi] + defense[ai] + home_adv)
                lam_a = math.exp(attack[ai] + defense[hi])
                hg, ag = m["home_goals"], m["away_goals"]
                p = poisson.pmf(hg, lam_h) * poisson.pmf(ag, lam_a)
                p *= _dc_adjustment(hg, ag, lam_h, lam_a, rho)
                p = max(p, 1e-10)
                ll += w * math.log(p)
            return -ll

        x0 = np.zeros(2 * n + 2)
        x0[2 * n] = self.home_adv
        x0[2 * n + 1] = self.rho

        # constrain avg attack = 0 so params are identifiable
        constraints = [{"type": "eq", "fun": lambda p, n=n: np.mean(p[:n])}]
        result = minimize(neg_log_likelihood, x0, method="SLSQP", constraints=constraints,
                           options={"maxiter": 300, "ftol": 1e-8})

        attack, defense, home_adv, rho = unpack(result.x)
        self.attack = {t: float(attack[idx[t]]) for t in self.teams}
        self.defense = {t: float(defense[idx[t]]) for t in self.teams}
        self.home_adv = float(home_adv)
        self.rho = float(rho)
        return result

    # -- prediction ----------------------------------------------------------

    def expected_goals(self, home: str, away: str, attack_adj: dict[str, float] | None = None,
                        defense_adj: dict[str, float] | None = None) -> tuple[float, float]:
        attack_adj = attack_adj or {}
        defense_adj = defense_adj or {}
        a_h = self.attack.get(home, 0.0) + attack_adj.get(home, 0.0)
        a_a = self.attack.get(away, 0.0) + attack_adj.get(away, 0.0)
        d_h = self.defense.get(home, 0.0) + defense_adj.get(home, 0.0)
        d_a = self.defense.get(away, 0.0) + defense_adj.get(away, 0.0)
        lam_h = math.exp(a_h + d_a + self.home_adv)
        lam_a = math.exp(a_a + d_h)
        return lam_h, lam_a

    def score_matrix(self, home: str, away: str, max_goals: int = 10, **kwargs) -> np.ndarray:
        lam_h, lam_a = self.expected_goals(home, away, **kwargs)
        m = np.zeros((max_goals + 1, max_goals + 1))
        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                p = poisson.pmf(i, lam_h) * poisson.pmf(j, lam_a)
                p *= _dc_adjustment(i, j, lam_h, lam_a, self.rho)
                m[i, j] = p
        m /= m.sum()  # renormalize after truncation + DC correction
        return m

    def match_probs(self, home: str, away: str, **kwargs) -> dict:
        m = self.score_matrix(home, away, **kwargs)
        p_home = float(np.tril(m, -1).sum())
        p_draw = float(np.trace(m))
        p_away = float(np.triu(m, 1).sum())
        n = m.shape[0]
        total_goals = np.add.outer(np.arange(n), np.arange(n))
        p_over_2_5 = float(m[total_goals > 2.5].sum())
        p_btts = float(m[1:, 1:].sum())
        lam_h, lam_a = self.expected_goals(home, away, **kwargs)
        return {
            "p_home_win": p_home, "p_draw": p_draw, "p_away_win": p_away,
            "p_over_2_5": p_over_2_5, "p_btts_yes": p_btts,
            "exp_home_goals": lam_h, "exp_away_goals": lam_a,
        }

    @staticmethod
    def prob_to_fair_odds(p: float) -> float:
        return round(1 / p, 2) if p > 0 else float("inf")


def _dc_adjustment(hg: int, ag: int, lam_h: float, lam_a: float, rho: float) -> float:
    """Dixon-Coles low-score correlation correction (tau function)."""
    if hg == 0 and ag == 0:
        return 1 - lam_h * lam_a * rho
    elif hg == 0 and ag == 1:
        return 1 + lam_h * rho
    elif hg == 1 and ag == 0:
        return 1 + lam_a * rho
    elif hg == 1 and ag == 1:
        return 1 - rho
    return 1.0
