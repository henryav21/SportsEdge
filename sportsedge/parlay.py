"""Parlay search over simulated outcomes.

Every leg carries a per-simulation result (win / push / loss) drawn from the same simulated games, so a
parlay's hit rate is measured directly instead of multiplying probabilities. Legs from different games are
independent; legs from the same game keep their real correlation (e.g. a favourite covering and its QB's
passing yards going over).
"""
from __future__ import annotations

from itertools import combinations

import numpy as np
import pandas as pd

from . import config
from .oddsmath import decimal_to_american


def _payoff(leg) -> np.ndarray:
    r = leg["result"]
    return np.where(r == 1, leg["dec"], np.where(r == 0, 1.0, 0.0)).astype(np.float32)


def evaluate(legs: list[dict]) -> dict:
    """Price any combination of legs: standard parlay odds, model hit rate and EV. Legs from the same game
    keep the correlation measured in the shared simulation; legs from different games are independent."""
    groups = [l["group"] for l in legs]
    conflicts = sorted({g for g in groups if groups.count(g) > 1})
    dec = float(np.prod([l["dec"] for l in legs]))
    e_ind = float(np.prod([l["p_win"] * l["dec"] + l["p_push"] for l in legs]))
    nl_ind = float(np.prod([l["p_win"] + l["p_push"] for l in legs]))
    win_ind = float(np.prod([l["p_win"] for l in legs]))
    corr_e = corr_nl = corr_w = 1.0
    by_game = {}
    for l in legs:
        by_game.setdefault(l["game_id"], []).append(l)
    for gl in by_game.values():
        if len(gl) < 2 or any(l.get("result") is None for l in gl):
            continue
        P = np.stack([_payoff(l) for l in gl])
        W = np.stack([l["result"] == 1 for l in gl])
        corr_e *= float(P.prod(axis=0).mean()) / max(float(np.prod(P.mean(axis=1))), 1e-12)
        corr_nl *= float((P > 0).all(axis=0).mean()) / max(float(np.prod((P > 0).mean(axis=1))), 1e-12)
        corr_w *= float(W.all(axis=0).mean()) / max(float(np.prod(W.mean(axis=1))), 1e-12)
    p_hit = min(nl_ind * corr_nl, 1.0)
    p_win_all = min(win_ind * corr_w, p_hit)
    return {"n": len(legs), "dec": dec, "odds": decimal_to_american(dec) if dec > 1 else None,
            "p_hit": p_hit, "p_win_all": p_win_all,
            "fair_odds": decimal_to_american(1 / p_win_all) if 0 < p_win_all < 1 else None,
            "ev": e_ind * corr_e - 1, "lift": corr_nl, "same_game": len(by_game) < len(legs),
            "conflicts": conflicts}


def search(legs: list[dict], mode: str, max_legs: int | None = None, pool: int | None = None,
           show: int | None = None, min_dec: float = 2.5) -> pd.DataFrame:
    """mode:
       'value'  - legs with positive EV, independent games only; ranked by parlay EV at standard parlay odds
       'safe'   - highest hit-rate parlays paying at least `min_dec` (decimal), independent games only
       'sgp'    - same-game combinations ranked by correlation lift; shows the fair price to demand
    """
    max_legs = max_legs or config.PARLAY_MAX_LEGS
    pool = pool or config.PARLAY_POOL
    show = show or config.PARLAY_SHOW
    legs = [l for l in legs if not l.get("flag")]
    if mode == "value":
        cand = sorted([l for l in legs if l["ev"] > 0 and l["p_win"] >= 0.35
                       and (np.isnan(l["edge"]) or l["edge"] >= config.MIN_EDGE_FLAG)], key=lambda l: -l["ev"])[:pool]
    elif mode == "safe":
        cand = sorted([l for l in legs if l["ev"] > -0.04 and l["odds"] >= -400], key=lambda l: -l["p_win"])[:pool]
    else:
        cand = sorted([l for l in legs if l["ev"] > -0.03], key=lambda l: -l["ev"])[: pool * 2]
    if len(cand) < 2:
        return pd.DataFrame()
    P = np.stack([_payoff(l) for l in cand])
    n = len(cand)
    rows = []

    def ok(idx):
        groups = [cand[i]["group"] for i in idx]
        if len(set(groups)) < len(groups):
            return False
        games = [cand[i]["game_id"] for i in idx]
        if mode == "sgp":
            return len(set(games)) == 1
        return len(set(games)) == len(games)

    sim_e = P.mean(axis=1)         # simulated expected payoff per leg
    sim_nl = (P > 0).mean(axis=1)  # simulated P(not lost) per leg

    def record(idx, prod):
        """Per-leg probabilities come from the (market-anchored) legs; the simulation supplies only the
        correlation between legs of the same game. Legs from different games are independent."""
        same_game = len({cand[i]["game_id"] for i in idx}) < len(idx)
        dec = float(np.prod([cand[i]["dec"] for i in idx]))
        e_ind = float(np.prod([cand[i]["p_win"] * cand[i]["dec"] + cand[i]["p_push"] for i in idx]))
        nl_ind = float(np.prod([cand[i]["p_win"] + cand[i]["p_push"] for i in idx]))
        if same_game:
            corr_e = float(np.mean(prod)) / max(float(np.prod(sim_e[list(idx)])), 1e-12)
            lift = float(np.mean(prod > 0)) / max(float(np.prod(sim_nl[list(idx)])), 1e-12)
        else:
            corr_e = lift = 1.0
        hit = min(nl_ind * lift, 1.0)
        ev = e_ind * corr_e - 1
        rows.append({"legs": [cand[i]["selection"] for i in idx], "leg_keys": [cand[i]["key"] for i in idx], "games": sorted({cand[i]["game"] for i in idx}),
                     "n": len(idx), "dec": dec, "odds": decimal_to_american(dec), "p_hit": hit,
                     "fair_odds": decimal_to_american(1 / hit) if 0 < hit < 1 else None, "ev": ev,
                     "lift": lift, "idx": tuple(idx)})

    for size in range(2, max_legs + 1):
        for idx in combinations(range(n), size):
            if not ok(idx):
                continue
            prod = P[idx[0]].copy()
            for i in idx[1:]:
                prod *= P[i]
            record(idx, prod)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    if mode == "value":
        df = df.sort_values("ev", ascending=False)
    elif mode == "safe":
        df = df[df["dec"] >= min_dec].sort_values(["p_hit", "ev"], ascending=False)
    else:
        df = df[df["lift"] > 1.03].sort_values(["lift", "p_hit"], ascending=False)
    # diversity: a single leg may appear in at most two listed parlays
    used, keep = {}, []
    for r in df.itertuples():
        if all(used.get(i, 0) < 2 for i in r.idx):
            keep.append(r.Index)
            for i in r.idx:
                used[i] = used.get(i, 0) + 1
        if len(keep) >= show:
            break
    return df.loc[keep].drop(columns=["idx"]).reset_index(drop=True)
