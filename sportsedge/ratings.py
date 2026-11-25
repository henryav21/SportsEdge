"""Opponent-adjusted, time-decayed team ratings.

For any per-game team stat y (points, EPA/play, pace, ...), at each point in time T we fit

    y[team, game] = mu + O[team] + D[opponent] + h * home

by weighted ridge regression on games strictly before T (weights halve every `halflife`).
O is the team's offensive effect, D the effect the team's defence has on opponents.
Because every game is explained by both sides, a team that has played weak defences gets
no credit for it: the ratings are opponent-adjusted by construction.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def opponent_adjusted(tg: pd.DataFrame, stats: list[str], eval_times, halflife: float, ridge: float,
                      min_weight: float = 0.02) -> pd.DataFrame:
    """tg columns: team, opp, home (1/-1/0), t, and each stat.

    Returns one row per (T, team) with  <stat>_o, <stat>_d, <stat>_mu  for every stat.
    """
    teams = sorted(set(tg["team"]) | set(tg["opp"]))
    idx = {t: i for i, t in enumerate(teams)}
    k = len(teams)
    ti = tg["team"].map(idx).to_numpy()
    oi = tg["opp"].map(idx).to_numpy()
    home = tg["home"].to_numpy(float)
    t = tg["t"].to_numpy(float)
    Y = tg[stats].to_numpy(float)

    n_all = len(tg)
    X_all = np.zeros((n_all, 2 * k + 2))
    X_all[:, 0] = 1
    X_all[:, 1] = home
    X_all[np.arange(n_all), 2 + ti] = 1
    X_all[np.arange(n_all), 2 + k + oi] = 1
    P = np.eye(2 * k + 2) * ridge
    P[0, 0] = P[1, 1] = 0.0

    out = []
    for T in sorted(set(np.asarray(eval_times, float))):
        prior = t < T
        w = np.where(prior, 0.5 ** ((T - t) / halflife), 0.0)
        rows = np.where(w > min_weight)[0]
        res = {"T": T, "team": teams}
        if len(rows) < k:  # not enough history yet
            for j, s in enumerate(stats):
                res[f"{s}_o"] = np.full(k, np.nan)
                res[f"{s}_d"] = np.full(k, np.nan)
                res[f"{s}_mu"] = np.full(k, np.nan)
            out.append(pd.DataFrame(res))
            continue
        X = X_all[rows]
        ww = w[rows]
        XtW = X.T * ww
        A = XtW @ X + P
        for j, s in enumerate(stats):
            y = Y[rows, j]
            ok = ~np.isnan(y)
            if ok.all():
                beta = np.linalg.solve(A, XtW @ y)
            else:
                Xo, wo = X[ok], ww[ok]
                XtWo = Xo.T * wo
                beta = np.linalg.solve(XtWo @ Xo + P, XtWo @ y[ok])
            O = beta[2:2 + k]
            D = beta[2 + k:]
            mu = beta[0] + O.mean() + D.mean()
            res[f"{s}_o"] = O - O.mean()
            res[f"{s}_d"] = D - D.mean()
            res[f"{s}_mu"] = np.full(k, mu)
        out.append(pd.DataFrame(res))
    return pd.concat(out, ignore_index=True)


def attach_matchup(games: pd.DataFrame, ratings: pd.DataFrame, stats: list[str]) -> pd.DataFrame:
    """games: one row per game with home, away, t. Adds, per stat:
    exp_h_<s> / exp_a_<s> (expected value for each side) plus diff_<s> and sum_<s>."""
    r = ratings.set_index(["T", "team"])
    g = games.copy()
    key_h = list(zip(g["t"].astype(float), g["home"]))
    key_a = list(zip(g["t"].astype(float), g["away"]))
    rh = r.reindex(key_h)
    ra = r.reindex(key_a)
    new = {}
    for s in stats:
        mu = rh[f"{s}_mu"].to_numpy()
        eh = rh[f"{s}_o"].to_numpy() + ra[f"{s}_d"].to_numpy()
        ea = ra[f"{s}_o"].to_numpy() + rh[f"{s}_d"].to_numpy()
        new[f"mu_{s}"] = mu
        new[f"exp_h_{s}"] = mu + eh
        new[f"exp_a_{s}"] = mu + ea
        new[f"diff_{s}"] = eh - ea
        new[f"sum_{s}"] = eh + ea
    return pd.concat([g.reset_index(drop=True), pd.DataFrame(new)], axis=1)
