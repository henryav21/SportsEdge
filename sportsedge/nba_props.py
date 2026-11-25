"""NBA player-prop simulator.

  minutes    <- recent minutes (games played), rescaled so the active roster fills 240 minutes
                (so an injured starter's minutes flow to the players who are actually available),
                reduced for starters / increased for bench in simulated blowouts (slope learned from data).
                A prop is void when the player does not play, so each player's own numbers use his minutes
                *if he plays* (min_cond). Bench players who only get in some nights are discounted for that
                only when sharing out the team's 240 minutes (min_proj).
  production <- each player's share of team production per minute (usage-style rate, shrunk for small
                samples) x the team's simulated output in this matchup (team points come straight from the
                game simulation, so opponent defence, pace and rest are already inside; rebounds, assists and
                threes use opponent-adjusted team ratings)
  noise      <- negative-binomial with dispersion estimated from game logs

Players on the same team share the simulated game, so PRA, teammates' points, and the game total are
correlated the way they really are.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import sources
from .util import cached, norm_name

MARKETS = ["pts", "reb", "ast", "fg3m", "stl", "blk", "pra", "pr", "pa", "ra"]
MARKET_LABEL = {"pts": "Points", "reb": "Rebounds", "ast": "Assists", "fg3m": "Threes", "stl": "Steals",
                "blk": "Blocks", "pra": "Pts+Reb+Ast", "pr": "Pts+Reb", "pa": "Pts+Ast", "ra": "Reb+Ast"}
BASE = {"pts": "PTS", "reb": "REB", "ast": "AST", "fg3m": "FG3M", "stl": "STL", "blk": "BLK"}
TEAM_STAT = {"pts": "points", "reb": "reb", "ast": "ast", "fg3m": "fg3m"}
# A player's per-minute share of team production is shrunk toward the team average as if he had played
# this many extra minutes at an average rate (matters only for small samples).
REL_PRIOR_MIN = 15.0  # chosen on a replay of recent games: lowest error in every market among 15 / 30 / 60


def coefficients(ctx: dict) -> dict:
    pl, tg = ctx["pl"], ctx["tg"]

    def fit():
        p = pl.copy()
        p["MIN"] = p["MIN"].astype(float)
        p = p.sort_values(["PLAYER_ID", "GAME_DATE"])
        p["exp_min"] = p.groupby("PLAYER_ID")["MIN"].transform(lambda s: s.ewm(halflife=4).mean().shift(1))
        mg = tg.set_index(["game_id", "team"])[["points", "pa"]]
        p = p.join(mg, on=["GAME_ID", "TEAM_ABBREVIATION"])
        p["blow"] = np.maximum(np.abs(p["points"] - p["pa"]) - 12, 0)
        c = {}
        for name, m in (("slope_starter", p["exp_min"] >= 28), ("slope_bench", (p["exp_min"] >= 8) & (p["exp_min"] < 22))):
            q = p[m].dropna(subset=["exp_min", "blow"])
            X = np.column_stack([np.ones(len(q)), q["blow"]])
            beta, *_ = np.linalg.lstsq(X, (q["MIN"] - q["exp_min"]).to_numpy(float), rcond=None)
            c[name] = float(beta[1])
        q = p[(p["exp_min"] >= 15) & (p["blow"] == 0)].dropna(subset=["exp_min"])
        c["sd_min"] = float(np.std(q["MIN"] - q["exp_min"]))
        q = p[p["MIN"] >= 20]
        for k, col in BASE.items():
            st = q.groupby(["PLAYER_ID", "season"])[col].agg(["mean", "var", "count"])
            st = st[(st["count"] >= 15) & (st["mean"] >= 0.5)]
            excess = (st["var"] - st["mean"]).clip(lower=1e-3)
            c[f"r_{k}"] = float(np.clip(np.median(st["mean"] ** 2 / excess), 1.5, 200))
        return c

    return cached(f"nba_prop_coefs_{len(pl)}", 24, fit)


def team_players(ctx: dict, team: str, asof, out_keys: set) -> tuple[pd.DataFrame, list]:
    pl, tg = ctx["pl"], ctx["tg"]
    p = pl[pl["GAME_DATE"] < asof].copy()
    p["MIN"] = p["MIN"].astype(float)
    last = p.sort_values("GAME_DATE").groupby("PLAYER_ID").tail(1)
    recent_dates = sorted(tg[(tg["team"] == team) & (tg["date"] < asof)]["date"].unique())[-10:]
    if not recent_dates:
        return pd.DataFrame(), []
    roster = last[(last["TEAM_ABBREVIATION"] == team) & (last["GAME_DATE"] >= recent_dates[0])]["PLAYER_ID"]
    p = p[p["PLAYER_ID"].isin(roster)]
    team_tot = tg.set_index(["game_id", "team"])[["points", "reb", "ast", "fg3m"]]
    p = p.join(team_tot, on=["GAME_ID", "TEAM_ABBREVIATION"])
    rows, removed = [], []
    for pid, h in p.groupby("PLAYER_ID"):
        h = h.sort_values("GAME_DATE").tail(30)
        name = h["PLAYER_NAME"].iloc[-1]
        key = norm_name(name)
        if key in out_keys:
            if h["MIN"].tail(5).mean() >= 12:
                removed.append(name)
            continue
        n = np.arange(len(h))[::-1]
        wm = 0.5 ** (n / 4.0)
        min_exp = float((wm * h["MIN"]).sum() / wm.sum())
        we = 0.5 ** (n / 12.0)
        M = float((we * h["MIN"]).sum())
        played_recent = h["GAME_DATE"].isin(recent_dates).sum()
        row = {"player_id": pid, "name": name, "key": key, "min_exp": min_exp, "games": len(h),
               "play_rate": played_recent / len(recent_dates)}
        for k, col in BASE.items():
            if k in TEAM_STAT:
                denom = float((we * h[TEAM_STAT[k]].fillna(0) * h["MIN"] / 240).sum())
                r = float((we * h[col]).sum()) / denom if denom > 0 else 1.0
                row[f"rel_{k}"] = (r * M + 1.0 * REL_PRIOR_MIN) / (M + REL_PRIOR_MIN)  # shrink to team-average rate
            else:
                row[f"rate_{k}"] = float((we * h[col]).sum()) / max(M, 1)
        rows.append(row)
    u = pd.DataFrame(rows)
    if u.empty:
        return u, removed
    u = u[u["min_exp"] >= 4].reset_index(drop=True)
    # Expected minutes. Core players (24+ min when they play) are assumed to play their normal minutes when
    # not on the injury report; bench minutes are scaled by how often the player actually got in recently.
    # Then the roster is fitted to 240 minutes: bench absorbs any excess, everyone grows if players are out.
    m = u["min_exp"].to_numpy(float, copy=True)
    core = m >= 24
    m[~core] *= u.loc[~core, "play_rate"].to_numpy(float)
    if m.sum() > 240:
        room = max(240 - m[core].sum(), 0.0)
        if m[~core].sum() > 0:
            m[~core] *= min(room / m[~core].sum(), 1.0)
        if m.sum() > 240:
            m *= 240 / m.sum()
    for _ in range(10):
        free = m < 42
        need = 240 - m.sum()
        if need < 0.5 or not free.any():
            break
        m[free] = np.minimum(m[free] * (1 + need / m[free].sum()), 42)
    u["min_proj"] = m
    # minutes if he plays: undo the "how often does he get in" discount, keep the injury / 240-minute scaling
    pr = np.where(core, 1.0, np.clip(u["play_rate"].to_numpy(float), 0.1, 1.0))
    u["min_cond"] = np.minimum(m / pr, np.maximum(u["min_exp"].to_numpy(float) * 1.35, 12.0)).clip(max=42)
    return u, removed


def simulate_team(rng, u: pd.DataFrame, side: dict, c: dict) -> dict:
    n = len(side["team_pts"])
    blow = np.maximum(np.abs(side["team_margin"]) - 12, 0)
    mins = {}
    for r in u.itertuples():
        slope = c["slope_starter"] if r.min_cond >= 28 else (c["slope_bench"] if r.min_cond < 22 else 0.0)
        sd = c["sd_min"] * min(r.min_cond / 30, 1.2)
        mins[r.key] = np.clip(r.min_cond + slope * blow + rng.normal(0, sd, n), 0, 48)
    team = {"pts": side["team_pts"],
            "reb": side["exp_reb"] * (1 + 0.5 * (side["total"] / side["mu_total"] - 1)),
            "ast": side["exp_ast"] * side["team_pts"] / side["mu_team_pts"],
            "fg3m": side["exp_fg3m"] * side["team_pts"] / side["mu_team_pts"]}
    norm = {k: float((u["min_proj"] / 240 * u[f"rel_{k}"]).sum()) for k in TEAM_STAT}
    out = {}
    for r in u.itertuples():
        s = {}
        for k in TEAM_STAT:
            mean = team[k] * (mins[r.key] / 240) * getattr(r, f"rel_{k}") / max(norm[k], 1e-6)
            s[k] = _negbin(rng, mean, c[f"r_{k}"])
        for k in ("stl", "blk"):
            s[k] = _negbin(rng, getattr(r, f"rate_{k}") * mins[r.key], c[f"r_{k}"])
        s["pra"] = s["pts"] + s["reb"] + s["ast"]
        s["pr"] = s["pts"] + s["reb"]
        s["pa"] = s["pts"] + s["ast"]
        s["ra"] = s["reb"] + s["ast"]
        s["min"] = mins[r.key]
        out[r.key] = {k: np.asarray(v, np.float32) for k, v in s.items()}
    return out


def _negbin(rng, mean, r):
    lam = np.maximum(np.asarray(mean, float), 0) * rng.gamma(r, 1.0 / r, size=np.shape(mean))
    return rng.poisson(lam)


def run(games: pd.DataFrame, sims: dict, ctx: dict, rng) -> tuple[dict, pd.DataFrame]:
    c = coefficients(ctx)
    espn = ctx["espn_inj"]
    out_keys = set(espn.loc[espn["status"].isin(["Out", "Doubtful"]), "key"]) if not espn.empty else set()
    q_keys = set(espn.loc[espn["status"].isin(["Day-To-Day", "Questionable"]), "key"]) if not espn.empty else set()
    res, proj = {}, []
    for g in games.itertuples():
        M, TOT = sims[g.game_id]["margin"], sims[g.game_id]["total"]
        res[g.game_id] = {}
        for sn, team, opp, sign in (("h", g.home, g.away, 1), ("a", g.away, g.home, -1)):
            u, removed = team_players(ctx, team, g.date, out_keys)
            if u.empty:
                continue
            side = {"team_pts": (TOT + sign * M) / 2, "team_margin": sign * M, "total": TOT,
                    "mu_total": g.mu_total, "mu_team_pts": (g.mu_total + sign * g.mu_margin) / 2,
                    "exp_reb": float(getattr(g, f"exp_{sn}_reb")), "exp_ast": float(getattr(g, f"exp_{sn}_ast")),
                    "exp_fg3m": float(getattr(g, f"exp_{sn}_fg3m"))}
            sim = simulate_team(rng, u, side, c)
            res[g.game_id].update(sim)
            for r in u.itertuples():
                if r.min_cond < 10:
                    continue
                st = sim[r.key]
                proj.append({"game_id": g.game_id, "team": team, "opp": opp, "player": r.name, "key": r.key,
                             "status": "DTD" if r.key in q_keys else "", "min": float(np.mean(st["min"])),
                             **{m: float(np.mean(st[m])) for m in MARKETS}})
            if removed:
                proj.append({"game_id": g.game_id, "team": team, "player": "OUT: " + ", ".join(removed), "key": "",
                             "status": "out"})
    return res, pd.DataFrame(proj)
