"""Console + HTML + CSV output."""
from __future__ import annotations

import datetime as dt
import html

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from . import config
from .oddsmath import fmt_american

console = Console(width=160)


def _pct(x):
    return "-" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{100 * x:.1f}%"


def _signed(x, d=1):
    return "-" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:+.{d}f}"


def _time(ts):
    if ts is None or pd.isna(ts):
        return ""
    return pd.Timestamp(ts).tz_convert("America/New_York").strftime("%a %I:%M%p ET")


def _spread_txt(home, margin):
    """Expected home margin -> spread notation for the favourite."""
    if margin is None or np.isnan(margin):
        return "-"
    return f"{home} {-margin:+.1f}"


def value_bets(legs, min_edge=None):
    min_edge = config.MIN_EDGE_FLAG if min_edge is None else min_edge
    def need(l):
        return config.LONGSHOT_MIN_EDGE if l["market"] == "Moneyline" and l["odds"] >= config.LONGSHOT_ODDS else min_edge

    # a pick flagged "far from market: check news" is never listed as value: that gap is usually news
    v = [l for l in legs if l["ev"] > 0 and not l.get("flag") and (np.isnan(l["edge"]) or l["edge"] >= need(l))]
    return sorted(v, key=lambda l: -l["ev"])


def likely(legs, n=12, min_odds=-400):
    v = [l for l in legs if l["odds"] >= min_odds]
    return sorted(v, key=lambda l: -l["p_win"])[:n]


# ----------------------------------------------------------------------------- console
def print_sport(res: dict, bankroll: float | None = None):
    sport = res["sport"].upper()
    games = res["games"]
    if games.empty:
        return
    m = res["model"]
    console.rule(f"[bold]{sport}")
    if "mae_model" in m:
        console.print(f"[dim]Out-of-sample check ({m['n_oos']} games): model margin error {m['mae_model']:.2f} pts vs "
                      f"closing line {m['mae_market']:.2f}. Market blend: model weight {m['w_margin']:.0%} (sides), "
                      f"{m['w_total']:.0%} (totals).[/dim]")
    t = Table(title=f"{sport} games", show_lines=False, header_style="bold")
    for c in ("Game", "Time", "Model line", "Market", "Blended", "Home win", "Model tot", "Mkt tot", "Notes", "Top drivers (model, pts)"):
        t.add_column(c, overflow="fold")
    for g in games.itertuples():
        notes = []
        if sport == "NFL":
            notes.append(f"QB {g.qb_away} / {g.qb_home}")
            if g.indoor:
                notes.append("indoors")
            elif not np.isnan(g.temp if g.temp is not None else np.nan):
                notes.append(f"{g.temp:.0f}F wind {g.wind:.0f}mph" + (f" rain {g.precip:.1f}mm" if g.precip and g.precip > 0.5 else ""))
        elif g.b2b:
            notes.append("B2B: " + ",".join(g.b2b))
        drivers = "; ".join(f"{n} {v:+.1f}" for n, v in g.drivers[:3])
        t.add_row(g.game, _time(g.start), _spread_txt(g.home, g.model_margin), _spread_txt(g.home, g.mkt_margin),
                  _spread_txt(g.home, g.margin), _pct(g.p_home), f"{g.model_total:.1f}",
                  "-" if np.isnan(g.mkt_total) else f"{g.mkt_total:g}", " | ".join(notes), drivers)
    console.print(t)
    _legs_table(f"{sport} value bets (positive EV, edge >= {config.MIN_EDGE_FLAG:.0%} vs no-vig price)",
                value_bets([l for l in res["legs"] if l["market"] in ("Moneyline", "Spread", "Total")]), bankroll)
    _legs_table(f"{sport} most likely to win (odds no worse than -400)",
                likely([l for l in res["legs"] if l["market"] in ("Moneyline", "Spread", "Total")]), bankroll)
    props = [l for l in res["legs"] if l["market"] not in ("Moneyline", "Spread", "Total")]
    if props:
        _legs_table(f"{sport} prop value", value_bets(props)[:20], bankroll)
    elif not res["proj"].empty:
        _proj_table(res)


def _legs_table(title, legs, bankroll=None, n=15):
    if not legs:
        console.print(f"[dim]{title}: none today.[/dim]")
        return
    t = Table(title=title, header_style="bold")
    cols = ["Pick", "Game", "Odds", "Model %", "No-vig mkt %", "Edge", "EV/unit", "Kelly"]
    if bankroll:
        cols.append("Stake")
    for c in cols:
        t.add_column(c)
    for l in legs[:n]:
        row = [l["selection"] + (" [yellow](!)[/yellow]" if l.get("flag") else ""), l["game"], fmt_american(l["odds"]), _pct(l["p_cond"]), _pct(l["p_fair"]),
               _signed(100 * l["edge"]) + ("pp" if not np.isnan(l["edge"]) else ""), _signed(l["ev"], 3),
               _pct(l["kelly"])]
        if bankroll:
            row.append(f"${bankroll * l['kelly']:.0f}")
        t.add_row(*row)
    console.print(t)
    if any(l.get("flag") for l in legs[:n]):
        console.print("[yellow](!)[/yellow] [dim]model and market differ by more than a normal amount; usually news "
                      "the model has not seen (injury, QB change, weather). Check before betting; excluded from parlays.[/dim]")


def _proj_table(res, n=25):
    p = res["proj"]
    p = p[p["status"] != "out"].copy()
    n_lines = len(res.get("prop_lines") or [])
    note = (f"{n_lines} sportsbook lines came without prices: see them in the dashboard, or pass --props-csv"
            if n_lines else "average simulated stat line")
    t = Table(title=f"{res['sport'].upper()} player projections ({note})", header_style="bold")
    if res["sport"] == "nfl":
        cols = [("player", "Player"), ("team", "Tm"), ("pos", "Pos"), ("touch_share", "Touch%"), ("tgt_share", "Tgt%"),
                ("pass_yds", "PassYd"), ("rush_yds", "RushYd"), ("receptions", "Rec"), ("rec_yds", "RecYd"),
                ("p_anytime_td", "TD%")]
        p["_y"] = p[["pass_yds", "rush_yds", "rec_yds"]].fillna(0).sum(axis=1) if "pass_yds" in p else 0
        p = p.sort_values("_y", ascending=False)
    else:
        cols = [("player", "Player"), ("team", "Tm"), ("min", "Min"), ("pts", "Pts"), ("reb", "Reb"), ("ast", "Ast"),
                ("fg3m", "3PM"), ("pra", "PRA")]
        p = p.sort_values("pra", ascending=False)
    for _, h in cols:
        t.add_column(h)
    for r in p.head(n).itertuples():
        vals = []
        for c, _ in cols:
            v = getattr(r, c, None)
            if c in ("touch_share", "tgt_share", "p_anytime_td"):
                vals.append(_pct(v))
            elif isinstance(v, float):
                vals.append("-" if np.isnan(v) else f"{v:.1f}")
            else:
                vals.append(str(v))
        t.add_row(*vals)
    console.print(t)


def print_parlays(parlays: dict):
    titles = {"value": "Best-value parlays (every leg +EV, separate games, standard parlay odds)",
              "safe": "Highest hit-rate parlays (pays at least +150)",
              "sgp": "Same-game combos the model sees as positively correlated (bet only at or above the fair price)"}
    for k, title in titles.items():
        df = parlays.get(k)
        if df is None or df.empty:
            console.print(f"[dim]{title}: none.[/dim]")
            continue
        t = Table(title=title, header_style="bold")
        for c in ("Legs", "Odds", "Model hit %", "Fair odds", "EV/unit" if k != "sgp" else "Correlation lift"):
            t.add_column(c, overflow="fold")
        for r in df.itertuples():
            last = _signed(r.ev, 3) if k != "sgp" else f"x{r.lift:.2f}"
            t.add_row(" + ".join(r.legs), fmt_american(r.odds) if k != "sgp" else "book SGP price",
                      _pct(r.p_hit), fmt_american(r.fair_odds), last)
        console.print(t)


# ----------------------------------------------------------------------------- files
def write_outputs(results: list[dict], parlays: dict, bankroll: float | None = None, track_line: str = "") -> dict:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    legs = [l for r in results for l in r["legs"]]
    csv_path = config.OUTPUT_DIR / f"bets_{stamp}.csv"
    if legs:
        df = pd.DataFrame([{k: v for k, v in l.items() if k != "result"} for l in legs])
        df["start"] = df["start"].astype(str)
        df.sort_values("ev", ascending=False).to_csv(csv_path, index=False)
    html_path = config.OUTPUT_DIR / f"slate_{stamp}.html"
    html_path.write_text(_html(results, parlays, bankroll, track_line), encoding="utf-8")
    return {"csv": csv_path if legs else None, "html": html_path}


def _h(x):
    return html.escape(str(x))


def _table(headers, rows, cls=""):
    th = "".join(f"<th>{_h(h)}</th>" for h in headers)
    trs = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="tw"><table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>'


def _legs_html(legs, bankroll=None):
    if not legs:
        return '<p class="muted">None today.</p>'
    heads = ["Pick", "Game", "Odds", "Model %", "No-vig market %", "Edge", "EV / unit", "Kelly"] + (["Stake"] if bankroll else [])
    rows = []
    for l in legs:
        edge = "" if np.isnan(l["edge"]) else f'<span class="{"pos" if l["edge"] > 0 else "neg"}">{100 * l["edge"]:+.1f}pp</span>'
        flag = f" <span class='neg' title='{_h(l['flag'])}'>(!)</span>" if l.get("flag") else ""
        r = [f"<b>{_h(l['selection'])}</b>{flag}", _h(l["game"]), fmt_american(l["odds"]), _pct(l["p_cond"]), _pct(l["p_fair"]),
             edge, f'<span class="{"pos" if l["ev"] > 0 else "neg"}">{l["ev"]:+.3f}</span>', _pct(l["kelly"])]
        if bankroll:
            r.append(f"${bankroll * l['kelly']:.0f}")
        rows.append(r)
    return _table(heads, rows)


def _html(results, parlays, bankroll, track_line=""):
    parts = []
    for res in results:
        games = res["games"]
        if games.empty:
            continue
        sport = res["sport"].upper()
        m = res["model"]
        parts.append(f"<h2>{sport}</h2>")
        if "mae_model" in m:
            parts.append(f'<p class="note">Out-of-sample check on {m["n_oos"]:,} past games: model margin error '
                         f'<b>{m["mae_model"]:.2f}</b> pts vs closing line <b>{m["mae_market"]:.2f}</b>. '
                         f'Learned blend: model gets <b>{m["w_margin"]:.0%}</b> weight on sides and '
                         f'<b>{m["w_total"]:.0%}</b> on totals, the market gets the rest.</p>')
        rows = []
        for g in games.itertuples():
            notes = []
            if sport == "NFL":
                notes.append(f"QB: {_h(g.qb_away)} / {_h(g.qb_home)}")
                if g.indoor:
                    notes.append("Indoors")
                elif g.temp is not None and not np.isnan(g.temp):
                    notes.append(f"{g.temp:.0f}&deg;F, wind {g.wind:.0f} mph" + (f", rain {g.precip:.1f} mm" if g.precip and g.precip > 0.5 else ""))
            elif g.b2b:
                notes.append("Back-to-back: " + ", ".join(g.b2b))
            drivers = "<br>".join(f"{_h(n)} <span class='{'pos' if v > 0 else 'neg'}'>{v:+.1f}</span>" for n, v in g.drivers[:4])
            rows.append([f"<b>{_h(g.game)}</b><br><span class='muted'>{_time(g.start)}</span>",
                         _spread_txt(g.home, g.model_margin), _spread_txt(g.home, g.mkt_margin),
                         f"<b>{_spread_txt(g.home, g.margin)}</b>", _pct(g.p_home), f"{g.model_total:.1f}",
                         "-" if np.isnan(g.mkt_total) else f"{g.mkt_total:g}", "<br>".join(notes), drivers])
        parts.append("<h3>Games</h3>" + _table(["Game", "Model line", "Market line", "Blended", "Home win", "Model total",
                                                "Market total", "Context", "Biggest model drivers (pts to home)"], rows))
        gl = [l for l in res["legs"] if l["market"] in ("Moneyline", "Spread", "Total")]
        parts.append(f"<h3>Value bets</h3>" + _legs_html(value_bets(gl)[:15], bankroll))
        parts.append(f"<h3>Most likely to win</h3>" + _legs_html(likely(gl), bankroll))
        props = [l for l in res["legs"] if l["market"] not in ("Moneyline", "Spread", "Total")]
        if props:
            parts.append("<h3>Prop value</h3>" + _legs_html(value_bets(props)[:25], bankroll))
        if not res["proj"].empty:
            parts.append("<h3>Player projections</h3>" + _proj_html(res))
    titles = {"value": "Best-value parlays", "safe": "Highest hit-rate parlays (+150 or longer)",
              "sgp": "Correlated same-game combos"}
    blurbs = {"value": "Every leg is +EV on its own; legs are from different games so the standard parlay price applies.",
              "safe": "Most likely parlays that still pay at least +150. Hit rate comes from the simulation, not from multiplying.",
              "sgp": "Legs that win together more often than chance. Books reprice same-game parlays: only bet if the offered price is at or above the fair odds."}
    parts.append("<h2>Parlays</h2>")
    for k, title in titles.items():
        df = parlays.get(k)
        parts.append(f"<h3>{title}</h3><p class='muted'>{blurbs[k]}</p>")
        if df is None or df.empty:
            parts.append('<p class="muted">None.</p>')
            continue
        rows = [[" + ".join(f"<b>{_h(x)}</b>" for x in r.legs), fmt_american(r.odds) if k != "sgp" else "book price",
                 _pct(r.p_hit), fmt_american(r.fair_odds),
                 (f'<span class="{"pos" if r.ev > 0 else "neg"}">{r.ev:+.3f}</span>' if k != "sgp" else f"x{r.lift:.2f}")]
                for r in df.itertuples()]
        parts.append(_table(["Legs", "Odds", "Model hit %", "Fair odds", "EV / unit" if k != "sgp" else "Correlation lift"], rows))
    body = "\n".join(parts)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Slate {dt.date.today()}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#e4e1d8;--pos:#1a7f4b;--neg:#b3412f;--card:#fff}}
@media (prefers-color-scheme: dark){{:root{{--bg:#161615;--fg:#ecebe6;--muted:#9a988f;--line:#2e2d2a;--pos:#5fc48f;--neg:#ec7a66;--card:#1e1e1c}}}}
body{{font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--fg);max-width:1200px;margin:0 auto;padding:24px}}
h1{{font-size:24px;margin:0 0 4px}} h4{{font-size:13px;margin:14px 0 4px;color:var(--muted)}} h2{{font-size:20px;margin:32px 0 8px;border-bottom:1px solid var(--line);padding-bottom:6px}} h3{{font-size:15px;margin:20px 0 6px}}
.muted{{color:var(--muted)}} .note{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}}
.tw{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--line);border-radius:8px}}
th,td{{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top;font-variant-numeric:tabular-nums}}
th{{font-size:12px;color:var(--muted);font-weight:600}} .pos{{color:var(--pos)}} .neg{{color:var(--neg)}}
</style></head><body>
<h1>Betting slate</h1><p class="muted">Generated {dt.datetime.now():%Y-%m-%d %H:%M}. Probabilities are model estimates, not guarantees.
Edge = model win probability minus the sportsbook's no-vig probability. EV = expected profit per 1 unit staked. Kelly = quarter-Kelly fraction, capped at {config.KELLY_CAP:.0%}.</p>
{f'<p class="note">{_h(track_line)}</p>' if track_line else ''}
{body}
<p class="muted" style="margin-top:32px">Sportsbooks build in a margin, closing lines are efficient, and even a well-calibrated model loses often. Track results, bet only what you can afford to lose. Problem gambling help: 1-800-GAMBLER.</p>
</body></html>"""


def _proj_html(res, n=40):
    p = res["proj"]
    p = p[p["status"] != "out"].copy()
    outs = res["proj"][res["proj"]["status"] == "out"]["player"].tolist()
    if res["sport"] == "nfl":
        groups = [("Quarterbacks", p[p["pos"] == "QB"].sort_values("pass_yds", ascending=False),
                   [("player", "Player"), ("team", "Team"), ("opp", "Opp"), ("pass_att", "Att"), ("pass_cmp", "Cmp"),
                    ("pass_yds", "Pass yds"), ("pass_tds", "Pass TD"), ("rush_yds", "Rush yds"), ("p_anytime_td", "Anytime TD")]),
                  ("Rushing (by carry share)", p[p["car_share"] >= 0.15].sort_values("car_share", ascending=False),
                   [("player", "Player"), ("team", "Team"), ("pos", "Pos"), ("touch_share", "Touch share"),
                    ("car_share", "Carry share"), ("rush_att", "Carries"), ("rush_yds", "Rush yds"),
                    ("rec_yds", "Rec yds"), ("p_anytime_td", "Anytime TD")]),
                  ("Receiving (by target share)", p[p["tgt_share"] >= 0.08].sort_values("tgt_share", ascending=False),
                   [("player", "Player"), ("team", "Team"), ("pos", "Pos"), ("tgt_share", "Target share"),
                    ("receptions", "Rec"), ("rec_yds", "Rec yds"), ("p_anytime_td", "Anytime TD")])]
        html_out = ""
        for title, df, cols in groups:
            html_out += f"<h4>{title}</h4>" + _rows_html(df.head(n), cols)
        extra = f"<p class='muted'>{'; '.join(_h(o) for o in outs)}</p>" if outs else ""
        return html_out + extra
    cols = [("player", "Player"), ("team", "Team"), ("min", "Min"), ("pts", "Pts"), ("reb", "Reb"),
            ("ast", "Ast"), ("fg3m", "3PM"), ("pra", "PRA")]
    p = p.sort_values("pra", ascending=False)
    extra = f"<p class='muted'>{'; '.join(_h(o) for o in outs)}</p>" if outs else ""
    return _rows_html(p.head(n), cols) + extra


def _rows_html(p, cols):
    rows = []
    for r in p.itertuples():
        vals = []
        for c, _ in cols:
            v = getattr(r, c, None)
            if c in ("touch_share", "tgt_share", "car_share", "p_anytime_td"):
                vals.append(_pct(v))
            elif isinstance(v, float):
                vals.append("-" if np.isnan(v) else f"{v:.1f}")
            else:
                vals.append(_h(v) + (" <span class='neg'>(Q)</span>" if c == "player" and getattr(r, "status", "") in ("Q", "DTD") else ""))
        rows.append(vals)
    return _table([h for _, h in cols], rows)
