"""Tests the prop simulator against real closing prop lines for games already played.

For each recent game day the simulator is run as it would have been that morning (only earlier games are
used), then compared with the closing prop prices ESPN published for that day (DraftKings) and with what
each player actually did. From that it learns how much weight the simulator deserves next to the market:

    probability = w x simulator + (1 - w) x no-vig market

`w` is kept only if it beats the market alone when each day is predicted from the other days
(leave-one-day-out). Otherwise it is 0 and prop probabilities are the market's.

Are the stored lines really closing lines? The feed stamps most entries as updated after the game began,
which would be a problem if the numbers had moved in play. They had not: the stored lines miss the final
stat by as much as a pre-game number does (NBA points: 4.9 for the line, 5.1 for the simulator), and they
sit within about a point of their opening values. An in-play line would be far closer to the result.

One thing the replay knows that you would not always know in the morning: which regular players sat out.
An injury report gives most of that before tip-off, but late scratches are a real-life cost not counted here.

Only NBA has free prop *prices*. NFL lines come without a price, so the NFL test is weaker: it checks how
often the simulator's side of the real line won, taking the line itself as an even bet. A standard
-110 / -115 price needs 52.4% to 53.5% just to break even.
"""
from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pandas as pd

from . import config, markets, sources
from .oddsmath import american_to_decimal
from .util import norm_name

VERSION = {"nba": "2", "nfl": "3"}  # bump a sport's number when its prop simulator changes (rebuilds the replay)
NFL_WEEKS = 8      # weeks of real NFL lines the test looks back over
# The replay is not told who sat out: on game day the tool knows the injury report but not the healthy
# scratches, so this is slightly harder than real use. It does see which players had lines posted.
KNOW_INACTIVES = False
USE_LINES = True
NFL_STAT = {"pass_yds": ["passing_yards"], "pass_tds": ["passing_tds"], "pass_cmp": ["completions"],
            "pass_att": ["attempts"], "rush_yds": ["rushing_yards"], "rush_att": ["carries"],
            "rec_yds": ["receiving_yards"], "receptions": ["receptions"],
            "rush_rec_yds": ["rushing_yards", "receiving_yards"]}
REPLAY_SIMS = 4000
GRID = np.linspace(0, 1, 21)


COLS = ["date", "key", "player", "market", "line", "over_odds", "under_odds", "pc_raw", "pp", "actual", "mean", "med"]


def _dir():
    d = config.CACHE_DIR / "propcheck"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ============================================================================= replay (NBA)
def _nba_out(pl: pd.DataFrame, tg: pd.DataFrame, teams, D, played_ids: set) -> list[str]:
    """Regulars (5+ of the team's last 10 games) who did not play on day D."""
    hist = pl[pl["GAME_DATE"] < D]
    last = hist.sort_values("GAME_DATE").groupby("PLAYER_ID").tail(1)
    out = []
    for team in teams:
        rd = sorted(tg[(tg["team"] == team) & (tg["date"] < D)]["date"].unique())[-10:]
        if not rd:
            continue
        roster = last[(last["TEAM_ABBREVIATION"] == team) & (last["GAME_DATE"] >= rd[0])]["PLAYER_ID"]
        h = hist[hist["PLAYER_ID"].isin(roster) & hist["GAME_DATE"].isin(rd)]
        cnt = h.groupby("PLAYER_ID").size()
        miss = set(cnt[cnt >= 5].index) - played_ids
        out += [norm_name(n) for n in last[last["PLAYER_ID"].isin(miss)]["PLAYER_NAME"]]
    return out


def _nba_day(D, g: pd.DataFrame, ctx: dict, model, rng) -> pd.DataFrame:
    from . import nba_props

    cols = COLS
    lines = sources.espn_props_history("nba", [D])
    lines = lines[lines["over_odds"].notna() & lines["under_odds"].notna()] if not lines.empty else lines
    gd = g[(g["date"] == D) & g["completed"]].copy()
    if lines.empty or gd.empty:
        return pd.DataFrame(columns=cols)
    gd = model.predict(gd).dropna(subset=["mu_margin", "mu_total"]).reset_index(drop=True)
    if gd.empty:
        return pd.DataFrame(columns=cols)
    sims = {}
    for r in gd.itertuples():
        dm, dt_ = model.dists(r.mu_margin, r.mu_total)
        sims[r.game_id] = {"margin": dm.sample(REPLAY_SIMS, rng), "total": dt_.sample(REPLAY_SIMS, rng)}
    pl, tg = ctx["pl"], ctx["tg"]
    played = pl[pl["GAME_DATE"] == D].copy()
    played["key"] = played["PLAYER_NAME"].map(norm_name)
    out = _nba_out(pl, tg, pd.unique(gd[["home", "away"]].to_numpy().ravel()), D, set(played["PLAYER_ID"]))
    res, _ = nba_props.run(gd, sims, dict(ctx, espn_inj=pd.DataFrame({"key": out, "status": "Out"})), rng)
    where = markets._locate(res)
    act = played.drop_duplicates("key").set_index("key")
    rows = []
    for r in lines.itertuples():
        gid = where.get(r.key)
        if gid is None or r.key not in act.index or r.market not in res[gid][r.key]:
            continue  # did not play (the bet is void) or not simulated
        a = act.loc[r.key]
        base = {k: float(a[c]) for k, c in nba_props.BASE.items()}
        base.update(pra=base["pts"] + base["reb"] + base["ast"], pr=base["pts"] + base["reb"],
                    pa=base["pts"] + base["ast"], ra=base["reb"] + base["ast"])
        x = res[gid][r.key][r.market]
        pp = float(np.mean(x == r.line))
        rows.append({"date": D, "key": r.key, "player": r.player, "market": r.market, "line": float(r.line),
                     "over_odds": float(r.over_odds), "under_odds": float(r.under_odds),
                     "pc_raw": float(np.mean(x > r.line)) / max(1 - pp, 1e-9), "pp": pp, "actual": base[r.market],
                     "mean": float(np.mean(x)), "med": float(np.median(x))})
    return pd.DataFrame(rows, columns=cols)


# ============================================================================= replay (NFL, lines without prices)
def _nfl_week(gw: pd.DataFrame, g: pd.DataFrame, ctx: dict, model, lines: pd.DataFrame, rng) -> pd.DataFrame:
    from . import nfl_props

    gd = model.predict(gw.copy()).dropna(subset=["mu_margin", "mu_total"]).reset_index(drop=True)
    if gd.empty or lines.empty:
        return pd.DataFrame(columns=COLS)
    sims = {}
    for r in gd.itertuples():
        dm, dt_ = model.dists(r.mu_margin, r.mu_total)
        sims[r.game_id] = {"margin": dm.sample(REPLAY_SIMS, rng), "total": dt_.sample(REPLAY_SIMS, rng)}
    ps, tg = ctx["ps"], ctx["tg"]
    season, week = int(gd["season"].iloc[0]), int(gd["week"].iloc[0])
    wk = ps[(ps["season"] == season) & (ps["week"] == week)].copy()
    wk["key"] = wk["player_display_name"].map(norm_name)
    played = set(wk["key"])
    out = []  # players in a team's recent usage who did not play this week = the inactive list
    for r in gd.itertuples():
        for team in (r.home, r.away):
            u = nfl_props.usage_table(ps, tg, team, float(r.t))
            if not u.empty and KNOW_INACTIVES:
                out += [k for k in u["key"] if k not in played]
    res, _ = nfl_props.run(gd, sims, dict(ctx, espn_inj=pd.DataFrame({"key": out, "status": "Out"})), g, rng,
                           set(lines["key"]) if USE_LINES else None)
    where = markets._locate(res)
    act = wk.drop_duplicates("key").set_index("key")
    rows = []
    for r in lines.itertuples():
        gid = where.get(r.key)
        if gid is None or r.key not in act.index or r.market not in NFL_STAT or r.market not in res[gid][r.key]:
            continue
        a = act.loc[r.key]
        y = float(sum(0.0 if pd.isna(a[c]) else float(a[c]) for c in NFL_STAT[r.market]))
        x = res[gid][r.key][r.market]
        pp = float(np.mean(x == r.line))
        rows.append({"date": r.date, "key": r.key, "player": r.player, "market": r.market, "line": float(r.line),
                     "over_odds": np.nan, "under_odds": np.nan,
                     "pc_raw": float(np.mean(x > r.line)) / max(1 - pp, 1e-9), "pp": pp, "actual": y,
                     "mean": float(np.mean(x)), "med": float(np.median(x)), "slate": f"{season}w{week:02d}"})
    return pd.DataFrame(rows, columns=COLS + ["slate"])


def _replay_nfl(g: pd.DataFrame, ctx: dict, model, log) -> pd.DataFrame:
    today = dt.date.today()
    done = g[g["completed"] & (g["season"] == g["season"].max())]
    weeks = sorted(done["t"].unique())[-NFL_WEEKS:]
    rng = np.random.default_rng(config.SEED)
    parts, lines, fresh = [], None, 0
    for t in weeks:
        gw = done[done["t"] == t]
        season, week = int(gw["season"].iloc[0]), int(gw["week"].iloc[0])
        path = _dir() / f"nfl_{season}w{week:02d}_v{VERSION['nfl']}.pkl"
        if path.exists() and not config.REFRESH:
            parts.append(pd.read_pickle(path))
            continue
        if fresh == 0:
            log("NFL: testing the prop simulator on past DraftKings lines (first time only) ...")
        if lines is None:  # every game day in the window, read once
            days = [today - dt.timedelta(days=i) for i in range(1, NFL_WEEKS * 7 + 4)]
            lines = sources.espn_props_history("nfl", days)
        pairs = set(zip(gw["home"], gw["away"]))
        ln = lines[[hp in pairs for hp in zip(lines["home"], lines["away"])]] if not lines.empty else lines
        if ln.empty:
            continue  # nothing published for this week (not cached, so it is retried later)
        try:
            df = _nfl_week(gw, g, ctx, model, ln, rng)
        except Exception as e:
            log(f"NFL: prop replay skipped week {week}: {e!r}")
            continue
        df.to_pickle(path)
        parts.append(df)
        fresh += 1
    parts = [p for p in parts if not p.empty]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def replay(sport: str, g: pd.DataFrame, ctx: dict, model, log=print, days: int | None = None) -> pd.DataFrame:
    """One row per real closing line on the last `days` game days. Each day is computed once and cached."""
    if sport == "nfl":
        return _replay_nfl(g, ctx, model, log)
    if sport != "nba":
        return pd.DataFrame()
    days = days or config.PROP_CHECK_DAYS
    today = dt.date.today()
    dates = sorted(d for d in g.loc[g["completed"], "date"].unique() if d < today)[-days:]
    rng = np.random.default_rng(config.SEED)
    parts, fresh = [], 0
    for D in dates:
        path = _dir() / f"{sport}_{D:%Y%m%d}_v{VERSION[sport]}.pkl"
        if path.exists() and not config.REFRESH:
            parts.append(pd.read_pickle(path))
            continue
        if fresh == 0:
            log(f"{sport.upper()}: testing the prop simulator on past closing lines (first time only) ...")
        try:
            df = _nba_day(D, g, ctx, model, rng)
        except Exception as e:  # one bad day must not stop the slate
            log(f"{sport.upper()}: prop replay skipped {D}: {e!r}")
            continue
        df.to_pickle(path)
        parts.append(df)
        fresh += 1
    parts = [p for p in parts if not p.empty]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


# ============================================================================= scoring
def _centre(df: pd.DataFrame) -> pd.Series:
    """The same per-slate centring the live pricing uses (markets.prop_centre), one slate per day."""
    d = (markets._logit(df["fair_over"]) - markets._logit(df["pc_raw"])).clip(-1.5, 1.5)
    n_day = d.groupby(df["date"]).transform("size")
    pooled = d.groupby(df["date"]).transform("mean")
    grp = [df["date"], df["market"]]
    shift = (d.groupby(grp).transform("sum") + pooled * markets.CENTRE_PRIOR) / (d.groupby(grp).transform("size") + markets.CENTRE_PRIOR)
    shift = shift.where(n_day >= markets.CENTRE_MIN_LINES, 0.0)
    return markets._sigmoid(markets._logit(df["pc_raw"]) + shift)


YARDS = ("pass_yds", "rush_yds", "rec_yds", "rush_rec_yds")


def _lean(gap, y, fo, mask=None) -> dict:
    """Record of the simulator's side where it disagreed with the market by a strong-lean amount."""
    m = (np.abs(gap) >= markets.PROP_LEAN) & (np.abs(gap) < markets.PROP_GAP_FLAG)
    if mask is not None:
        m = m & mask
    if not m.any():
        return {"n": 0, "won": None, "market": None}
    won = np.where(gap > 0, y, 1 - y)[m]
    return {"n": int(m.sum()), "won": float(won.mean()), "market": float(np.where(gap > 0, fo, 1 - fo)[m].mean()),
            "lo": markets.PROP_LEAN, "hi": markets.PROP_GAP_FLAG}


def _logloss(p, y) -> float:
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _best_w(pc, fo, y) -> float:
    return float(GRID[int(np.argmin([_logloss(w * pc + (1 - w) * fo, y) for w in GRID]))])


def _bets(df: pd.DataFrame, w: pd.Series) -> pd.DataFrame:
    """Every over and under as a bet at its real closing price, with the tool's probability at weight w."""
    out = []
    for side, sgn in (("over", 1), ("under", -1)):
        fair = df["fair_over"] if sgn > 0 else 1 - df["fair_over"]
        sim = df["pc"] if sgn > 0 else 1 - df["pc"]
        p_cond = w * sim + (1 - w) * fair
        dec = df[f"{side}_odds"].map(american_to_decimal)
        p_win = p_cond * (1 - df["pp"])
        won = (df["actual"] > df["line"]) if sgn > 0 else (df["actual"] < df["line"])
        push = df["actual"] == df["line"]
        out.append(pd.DataFrame({"date": df["date"], "market": df["market"], "side": side, "p_cond": p_cond,
                                 "fair": fair, "edge": p_cond - fair, "ev": p_win * dec + df["pp"] - 1,
                                 "flag": (df["pc"] - df["fair_over"]).abs() >= markets.PROP_GAP_FLAG,
                                 "pnl": np.where(push, 0.0, np.where(won, dec - 1, -1.0)),
                                 "won": won & ~push, "push": push}))
    return pd.concat(out, ignore_index=True)


def _record(b: pd.DataFrame) -> dict:
    if b.empty:
        return {"n": 0, "win": None, "roi": None, "se": None, "units": 0.0, "expected": None}
    wl = b[~b["push"]]
    return {"n": int(len(b)), "win": float(wl["won"].mean()) if len(wl) else None, "roi": float(b["pnl"].mean()),
            "se": float(b["pnl"].std(ddof=1) / np.sqrt(len(b))) if len(b) > 1 else None,
            "units": float(b["pnl"].sum()), "expected": float(wl["p_cond"].mean()) if len(wl) else None}


def evaluate(sport: str, df: pd.DataFrame) -> dict | None:
    """Learn the simulator weight and replay the tool's value picks at real closing prices."""
    if df is None or df.empty:
        return None
    df = df.copy()
    fair = [markets._fair_pair(o, u)[0] for o, u in zip(df["over_odds"], df["under_odds"])]
    df["fair_over"] = fair
    df = df.dropna(subset=["fair_over", "pc_raw"]).reset_index(drop=True)
    if len(df) < 200:
        return None
    df["pc"] = _centre(df)
    live = df[df["actual"] != df["line"]]
    y = (live["actual"] > live["line"]).to_numpy(float)
    pc, fo = live["pc"].to_numpy(float), live["fair_over"].to_numpy(float)
    w_fit = _best_w(pc, fo, y)
    # leave-one-day-out: each day is priced with a weight learned from the other days
    w_day = {}
    for D in df["date"].unique():
        m = (live["date"] != D).to_numpy()
        w_day[D] = _best_w(pc[m], fo[m], y[m]) if m.sum() > 100 else 0.0
    w_live = live["date"].map(w_day).to_numpy(float)
    ll_market, ll_sim = _logloss(fo, y), _logloss(pc, y)
    ll_cv = _logloss(w_live * pc + (1 - w_live) * fo, y)
    keeps = ll_cv < ll_market
    w_used = w_fit if keeps else 0.0

    b = _bets(df, df["date"].map(w_day).astype(float))
    val = b[(b["ev"] > 0) & (b["edge"] >= config.MIN_EDGE_FLAG)]
    by_market = []
    for m, d in live.groupby("market"):
        yy = (d["actual"] > d["line"]).to_numpy(float)
        by_market.append({"market": m, "n": int(len(d)), "over_rate": float(yy.mean()),
                          "market_over": float(d["fair_over"].mean()), "sim_over_raw": float(d["pc_raw"].mean()),
                          "ll_market": _logloss(d["fair_over"].to_numpy(float), yy),
                          "ll_sim": _logloss(d["pc"].to_numpy(float), yy)})
    # does a bigger disagreement with the market mean a better bet? (simulator alone, centred)
    gap = (live["pc"] - live["fair_over"]).to_numpy(float)
    side_won = np.where(gap > 0, y, 1 - y)
    buckets = []
    for lo, hi in ((0, 0.03), (0.03, 0.06), (0.06, 0.10), (0.10, 0.15), (0.15, 1.0)):
        m = (np.abs(gap) >= lo) & (np.abs(gap) < hi)
        if m.sum():
            fair_side = np.where(gap > 0, fo, 1 - fo)[m]
            buckets.append({"lo": lo, "hi": hi, "n": int(m.sum()), "won": float(side_won[m].mean()),
                            "market": float(fair_side.mean()), "sim": float(np.where(gap > 0, pc, 1 - pc)[m].mean())})
    return {"sport": sport, "version": VERSION[sport], "priced": True, "updated": dt.datetime.now().isoformat(timespec="seconds"),
            "days": int(df["date"].nunique()), "first": str(min(df["date"])), "last": str(max(df["date"])),
            "n_lines": int(len(df)), "n_players": int(df["key"].nunique()),
            "ll_market": ll_market, "ll_sim": ll_sim, "ll_blend": ll_cv, "w_fit": w_fit, "beats_market": bool(keeps),
            "w_used": float(w_used),
            "value": _record(val[~val["flag"]]), "flagged": _record(val[val["flag"]]),
            "all_bets": _record(b), "lean": _lean(gap, y, fo), "by_market": by_market, "gap_buckets": buckets}


def evaluate_lines(sport: str, df: pd.DataFrame) -> dict | None:
    """Lines without prices: how often did the simulator's side of the real line win? The line is taken as
    an even bet, so the weight is learned against a flat 50% instead of a no-vig price."""
    if df is None or df.empty:
        return None
    df = df.copy()
    df["fair_over"] = 0.5
    df["date"] = df["slate"]  # one slate per week for the centring
    df = df.dropna(subset=["pc_raw"]).reset_index(drop=True)
    if len(df) < 200:
        return None
    df["pc"] = _centre(df)
    live = df[df["actual"] != df["line"]]
    y = (live["actual"] > live["line"]).to_numpy(float)
    pc, fo = live["pc"].to_numpy(float), live["fair_over"].to_numpy(float)
    w_fit = _best_w(pc, fo, y)
    w_day = {}
    for D in df["date"].unique():
        m = (live["date"] != D).to_numpy()
        w_day[D] = _best_w(pc[m], fo[m], y[m]) if m.sum() > 100 else 0.0
    w_live = live["date"].map(w_day).to_numpy(float)
    ll_market, ll_sim = _logloss(fo, y), _logloss(pc, y)
    ll_cv = _logloss(w_live * pc + (1 - w_live) * fo, y)
    keeps = ll_cv < ll_market
    by_market = []
    for m, d in live.groupby("market"):
        yy = (d["actual"] > d["line"]).to_numpy(float)
        by_market.append({"market": m, "n": int(len(d)), "over_rate": float(yy.mean()), "market_over": 0.5,
                          "sim_over_raw": float(d["pc_raw"].mean()), "line": float(d["line"].mean()),
                          "sim_mean": float(d["mean"].mean()), "sim_med": float(d["med"].mean()),
                          "actual_mean": float(d["actual"].mean()),
                          "ll_market": _logloss(np.full(len(d), 0.5), yy), "ll_sim": _logloss(d["pc"].to_numpy(float), yy)})
    gap = (live["pc"] - 0.5).to_numpy(float)
    side_won = np.where(gap > 0, y, 1 - y)
    buckets = []
    for lo, hi in ((0, 0.03), (0.03, 0.06), (0.06, 0.10), (0.10, 0.15), (0.15, 1.0)):
        m = (np.abs(gap) >= lo) & (np.abs(gap) < hi)
        if m.sum():
            buckets.append({"lo": lo, "hi": hi, "n": int(m.sum()), "won": float(side_won[m].mean()), "market": 0.5,
                            "sim": float(np.where(gap > 0, pc, 1 - pc)[m].mean())})
    yards = live["market"].isin(YARDS).to_numpy()
    return {"sport": sport, "version": VERSION[sport], "priced": False,
            "updated": dt.datetime.now().isoformat(timespec="seconds"),
            "days": int(df["date"].nunique()), "first": str(min(df["date"])), "last": str(max(df["date"])),
            "n_lines": int(len(df)), "n_players": int(df["key"].nunique()),
            "ll_market": ll_market, "ll_sim": ll_sim, "ll_blend": ll_cv, "w_fit": w_fit, "beats_market": bool(keeps),
            "w_used": float(w_fit if keeps else 0.0),
            "lean": _lean(gap, y, fo), "lean_yards": _lean(gap, y, fo, yards),
            "by_market": by_market, "gap_buckets": buckets}


# ============================================================================= entry points
def _summary_path(sport: str):
    return _dir() / f"summary_{sport}.json"


def update(sport: str, g: pd.DataFrame, ctx: dict, model, log=print) -> dict | None:
    """Replay any new game days and refresh the learned weight. Called while building the slate."""
    df = replay(sport, g, ctx, model, log)
    s = evaluate_lines(sport, df) if sport == "nfl" else evaluate(sport, df)
    if s is None:
        return None
    _summary_path(sport).write_text(json.dumps(s, indent=1), encoding="utf-8")
    return s


def summary(sport: str) -> dict | None:
    p = _summary_path(sport.lower())
    if not p.exists():
        return None
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return s if s.get("version") == VERSION.get(sport.lower()) else None


def learned_weight(sport: str | None) -> float | None:
    """Simulator weight learned from real closing lines, or None when this sport has not been tested."""
    s = summary(sport) if sport else None
    return None if s is None else float(s["w_used"])
