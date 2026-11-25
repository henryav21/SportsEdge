"""Walk-forward backtest against real closing lines.

Every prediction here was made by a model trained only on earlier seasons. Two betting strategies are
replayed at the closing price: the raw model, and the model blended with the market (what `slate` uses).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from rich.table import Table

from .markets import _cond
from .oddsmath import american_to_decimal, devig, implied
from .report import console


def _settle(result, dec):
    return dec - 1 if result > 0 else (0.0 if result == 0 else -1.0)


def run(sport: str, min_edge: float = 0.02):
    mod = __import__(f"sportsedge.{sport}", fromlist=["build"])
    g, _ = mod.build(None, verbose=True)
    model = mod.fit_model(g)
    o = model.oos.copy()
    if o.empty:
        console.print("Not enough history for a walk-forward test.")
        return
    has = o.dropna(subset=["mkt_margin", "mkt_total"]).copy()
    console.rule(f"[bold]{sport.upper()} walk-forward backtest")
    console.print(f"{len(o):,} out-of-sample games ({o['season'].min()}-{o['season'].max()}), "
                  f"{len(has):,} with closing lines.")

    # ------------------------------------------------------------------ accuracy
    t = Table(title="Prediction error (lower is better)", header_style="bold")
    for c in ("Quantity", "Model", "Closing line", "Blend (used live)"):
        t.add_column(c)
    bm = model.blend(has["pm"], has["mkt_margin"], model.w_margin)
    bt = model.blend(has["pt"], has["mkt_total"], model.w_total)
    t.add_row("Margin MAE", f"{np.abs(has.margin - has.pm).mean():.2f}", f"{np.abs(has.margin - has.mkt_margin).mean():.2f}",
              f"{np.abs(has.margin - bm).mean():.2f}")
    t.add_row("Total MAE", f"{np.abs(has.total - has.pt).mean():.2f}", f"{np.abs(has.total - has.mkt_total).mean():.2f}",
              f"{np.abs(has.total - bt).mean():.2f}")
    console.print(t)
    for k, v in model.cv.items():
        console.print(f"{k.title()}: blend weight fit {v['w_fit']:.0%}; leave-one-season-out error change "
                      f"{-v['cv_mse_gain_pct']:+.2f}% -> model weight used live: "
                      f"{(model.w_margin if k == 'margin' else model.w_total):.0%}")

    # ------------------------------------------------------------------ moneyline calibration
    ml_h = "home_moneyline" if "home_moneyline" in has else "ml_home"
    ml_a = "away_moneyline" if "away_moneyline" in has else "ml_away"
    mlg = has.dropna(subset=[ml_h, ml_a])
    mlg = mlg[mlg["margin"] != 0]
    sd_model_m = float(np.std(o["margin"] - o["pm"]))
    sd_model_t = float(np.std(o["total"] - o["pt"]))
    if len(mlg) > 50:
        y = (mlg["margin"] > 0).to_numpy(float)
        p_mkt = np.array([devig([implied(a), implied(b)])[0] for a, b in zip(mlg[ml_h], mlg[ml_a])])
        p_mod = np.array([_cond(model.dist_margin(m, sd_model_m, exclude_season=s), "gt", 0)
                          for m, s in zip(mlg["pm"], mlg["season"])])
        mu_b = model.blend(mlg["pm"], mlg["mkt_margin"], model.w_margin)
        p_bl = np.array([_anchor(f, model.dist_margin(b, None, exclude_season=s), model.dist_margin(k, None, exclude_season=s), "gt", 0)
                         for f, b, k, s in zip(p_mkt, mu_b, mlg["mkt_margin"], mlg["season"])])
        t = Table(title=f"Win probability quality ({len(mlg):,} games with moneylines; lower is better)", header_style="bold")
        for c in ("Source", "Log loss", "Brier"):
            t.add_column(c)
        for name, p in (("Model only (raw)", p_mod), ("No-vig closing moneyline", p_mkt), ("Live (market-anchored)", p_bl)):
            p = np.clip(p, 1e-4, 1 - 1e-4)
            t.add_row(name, f"{-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)):.4f}", f"{np.mean((p - y) ** 2):.4f}")
        console.print(t)
        cal = pd.DataFrame({"p": p_bl, "y": y})
        cal["bin"] = pd.cut(cal["p"], [0, .2, .35, .5, .65, .8, 1])
        c = cal.groupby("bin", observed=True).agg(n=("y", "size"), predicted=("p", "mean"), actual=("y", "mean"))
        t = Table(title="Calibration of live home-win probability", header_style="bold")
        for col in ("Bucket", "Games", "Predicted", "Actual"):
            t.add_column(col)
        for b, r in c.iterrows():
            t.add_row(str(b), f"{int(r.n):,}", f"{r.predicted:.1%}", f"{r.actual:.1%}")
        console.print(t)

    # ------------------------------------------------------------------ betting replay at closing prices
    # Same calculation as the live tool: no-vig closing price moved by the model's point difference.
    rows = []
    for label, wm, wt, sdm, sdt in (("Model only", 1.0, 1.0, sd_model_m, sd_model_t),
                                    ("Blend (live)", model.w_margin, model.w_total, None, None)):
        for r in has.itertuples():
            mu_m = wm * r.pm + (1 - wm) * r.mkt_margin
            mu_t = wt * r.pt + (1 - wt) * r.mkt_total
            dB_m = model.dist_margin(mu_m, sdm, exclude_season=r.season)
            dK_m = model.dist_margin(r.mkt_margin, sdm, exclude_season=r.season)
            dB_t = model.dist_total(mu_t, sdt, exclude_season=r.season)
            dK_t = model.dist_total(r.mkt_total, sdt, exclude_season=r.season)
            L = -r.mkt_margin  # home spread
            ho, ao = _odds(r, "home_spread_odds"), _odds(r, "away_spread_odds")
            fair_h = devig([implied(ho), implied(ao)])[0]
            res_h = np.sign(r.margin + L)
            for kind, odds, fair, res in (("gt", ho, fair_h, res_h), ("lt", ao, 1 - fair_h, -res_h)):
                _maybe_bet(rows, label, "Spread", r.season, _anchor(fair, dB_m, dK_m, kind, -L), dB_m.p_eq(-L),
                           odds, fair, res, min_edge)
            X = r.mkt_total
            oo, uo = _odds(r, "over_odds"), _odds(r, "under_odds")
            fair_o = devig([implied(oo), implied(uo)])[0]
            res_o = np.sign(r.total - X)
            for kind, odds, fair, res in (("gt", oo, fair_o, res_o), ("lt", uo, 1 - fair_o, -res_o)):
                _maybe_bet(rows, label, "Total", r.season, _anchor(fair, dB_t, dK_t, kind, X), dB_t.p_eq(X),
                           odds, fair, res, min_edge)
    bets = pd.DataFrame(rows)
    t = Table(title=f"Betting replay at closing prices (bet when edge >= {min_edge:.0%} and EV > 0)", header_style="bold")
    for c in ("Strategy", "Market", "Bets", "Win %", "Units", "ROI"):
        t.add_column(c)
    if not bets.empty:
        for (s, m), b in bets.groupby(["strategy", "market"]):
            wins = (b["pnl"] > 0).mean()
            t.add_row(s, m, f"{len(b):,}", f"{wins:.1%}", f"{b['pnl'].sum():+.1f}", f"{b['pnl'].mean():+.1%}")
    console.print(t)
    if not bets.empty:
        t = Table(title="Replay ROI by season", header_style="bold")
        t.add_column("Strategy")
        t.add_column("Market")
        seasons = sorted(bets["season"].unique())
        for s in seasons:
            t.add_column(str(s))
        for (s, m), b in bets.groupby(["strategy", "market"]):
            by = b.groupby("season")["pnl"]
            t.add_row(s, m, *[f"{by.mean().get(x, np.nan):+.1%} ({by.size().get(x, 0)})" for x in seasons])
        console.print(t)
    console.print("[dim]Closing lines are the hardest prices to beat; lines available earlier in the week are softer, "
                  "and props are softer still. A break-even ROI against closing lines is already a strong result.[/dim]")
    return bets


def _odds(r, col):
    v = getattr(r, col, -110)
    return -110.0 if v is None or np.isnan(v) else float(v)


def _anchor(fair, dB, dK, kind, x):
    return float(np.clip(fair + _cond(dB, kind, x) - _cond(dK, kind, x), 0.005, 0.995))


def _maybe_bet(rows, label, market, season, p_cond, p_push, odds, fair, res, min_edge):
    dec = american_to_decimal(odds)
    p_win = p_cond * (1 - p_push)
    if p_win * (dec - 1) - (1 - p_win - p_push) > 0 and p_cond - fair >= min_edge:
        rows.append({"strategy": label, "market": market, "season": season, "pnl": _settle(res, dec)})
