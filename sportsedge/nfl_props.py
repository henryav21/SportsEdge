"""NFL player-prop simulator.

Each simulation draws a game script (margin + total) from the game model, then builds the box score
from the top down:

  team plays      <- opponent-adjusted pace, scaled with the simulated total
  pass/rush split <- opponent-adjusted pass rate, shifted by the simulated score (trailing teams pass more)
  efficiency      <- opponent-adjusted yards/attempt & yards/carry, weather (wind, rain), simulated scoring
  player volume   <- target share / carry share (touch share), with injured teammates' shares redistributed
  player output   <- catch rate, yards per catch/carry, opponent's yards allowed to the position
  touchdowns      <- team TDs from simulated points, split by each player's TD-weighted opportunity share

Because every player on a team shares the same simulated game, correlations (QB yards with his WR's
yards, RB carries with a lead) come out naturally - which is what same-game parlays need.
All dispersion / script / weather coefficients are estimated from nflverse history.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config, sources
from .util import cached, norm_name, safe_div

MARKETS = ["pass_yds", "pass_tds", "pass_cmp", "pass_att", "rush_yds", "rush_att", "rec_yds", "receptions",
           "rush_rec_yds", "anytime_td"]
MARKET_LABEL = {"pass_yds": "Pass Yds", "pass_tds": "Pass TDs", "pass_cmp": "Completions", "pass_att": "Pass Att",
                "rush_yds": "Rush Yds", "rush_att": "Rush Att", "rec_yds": "Rec Yds", "receptions": "Receptions",
                "rush_rec_yds": "Rush+Rec Yds", "anytime_td": "Anytime TD"}


# ============================================================================= coefficients from history
def _expectations(tg: pd.DataFrame, ratings: pd.DataFrame) -> pd.DataFrame:
    r = ratings.set_index(["T", "team"])
    me = r.reindex(list(zip(tg["t"].astype(float), tg["team"])))
    op = r.reindex(list(zip(tg["t"].astype(float), tg["opp"])))
    out = tg.copy()
    for s in ("points", "plays", "pass_rate", "pass_ypa", "rush_ypc", "sack_rate"):
        out[f"exp_{s}"] = me[f"{s}_mu"].to_numpy() + me[f"{s}_o"].to_numpy() + op[f"{s}_d"].to_numpy()
        out[f"exp_opp_{s}"] = op[f"{s}_mu"].to_numpy() + op[f"{s}_o"].to_numpy() + me[f"{s}_d"].to_numpy()
    return out


def _ols(X, y):
    X = np.column_stack([np.ones(len(X)), X])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    return beta[1:], float(np.std(resid))


def coefficients(ctx: dict, games: pd.DataFrame) -> dict:
    tg, ratings, ps = ctx["tg"], ctx["ratings"], ctx["ps"]

    def fit():
        e = _expectations(tg, ratings).dropna(subset=["exp_points", "exp_plays", "pass_rate", "plays"])
        wx = games.set_index("game_id")[["wind", "precip", "indoor"]]
        e = e.join(wx, on="game_id")
        e[["wind", "precip"]] = e[["wind", "precip"]].fillna(0.0)
        margin = (e["points"] - e["pa"]).to_numpy(float)
        tot_res = (e["points"] + e["pa"] - e["exp_points"] - e["exp_opp_points"]).to_numpy(float)
        pts_res = (e["points"] - e["exp_points"]).to_numpy(float)
        c = {}
        (c["b_pass_rate"],), c["sd_pass_rate"] = _ols(margin[:, None], (e["pass_rate"] - e["exp_pass_rate"]).to_numpy())
        (c["c_plays"],), c["sd_plays"] = _ols(tot_res[:, None], (e["plays"] / e["exp_plays"] - 1).to_numpy())
        ok = e["pass_ypa"].notna() & (e["exp_pass_ypa"] > 0)
        X = np.column_stack([pts_res[ok], e.loc[ok, "wind"], e.loc[ok, "precip"]])
        (c["c_ypa"], c["d_wind"], c["d_precip"]), _ = _ols(X, (e.loc[ok, "pass_ypa"] / e.loc[ok, "exp_pass_ypa"] - 1).to_numpy())
        ok = e["rush_ypc"].notna() & (e["exp_rush_ypc"] > 0)
        (c["c_ypc"],), _ = _ols(pts_res[ok][:, None], (e.loc[ok, "rush_ypc"] / e.loc[ok, "exp_rush_ypc"] - 1).to_numpy())
        tds = tg["passing_tds"].fillna(0) + tg["rushing_tds"].fillna(0)
        c["td_per_pt"] = float(tds.sum() / tg["points"].sum())
        c["pass_td_frac"] = float(tg["passing_tds"].fillna(0).sum() / tds.sum())
        # ---- player-level dispersion
        p = ps[ps["season_type"] == "REG"]
        for col, key in (("targets", "r_tgt"), ("carries", "r_car")):
            grp = p[p[col].fillna(0) > 0].groupby(["player_id", "season"])[col]
            st = grp.agg(["mean", "var", "count"])
            st = st[(st["count"] >= 8) & (st["mean"] >= 3)]
            excess = (st["var"] - st["mean"]).clip(lower=1e-3)
            c[key] = float(np.clip(np.median(st["mean"] ** 2 / excess), 2, 50))
        c["k_rec"] = _gamma_shape(p, "receiving_yards", "receptions")
        c["k_cmp"] = _gamma_shape(p[p["position"] == "QB"], "passing_yards", "completions")
        q = p[p["carries"].fillna(0) > 0]
        ypc = q.groupby(["player_id", "season"])["rushing_yards"].transform("sum") / q.groupby(
            ["player_id", "season"])["carries"].transform("sum")
        avg = q["rushing_yards"] / q["carries"]
        c["sd_carry"] = float(np.sqrt((q["carries"] * (avg - ypc) ** 2).mean()))
        return c

    return cached(f"nfl_prop_coefs_{len(tg)}", 24, fit)


def _gamma_shape(p, yards, n):
    q = p[p[n].fillna(0) > 0]
    g = q.groupby(["player_id", "season"])
    mean = g[yards].transform("sum") / g[n].transform("sum")
    avg = q[yards] / q[n]
    return float(np.clip((mean**2).sum() / (q[n] * (avg - mean) ** 2).sum(), 0.3, 5))


# ============================================================================= player usage
# Who has a role this week? A player's share is his average over the games he played, which is the right
# number for a prop (a prop is void if he sits). But a fill-in keeps his big share after the starter returns,
# and the injury report does not list healthy scratches. Two signals settle it, both checked on a replay of
# real DraftKings lines (propcheck.py):
#   - sportsbooks post lines only for players with a role, so on a team with lines posted, a player
#     without one is held to a small share;
#   - before lines are posted, a runner who did not appear in the team's latest game has his carry share
#     discounted (for targets this made the replay worse, so it is not applied there).
# Counting missed games as zero instead was tried and was clearly worse: it punishes starters back from injury.
# Replay, 5,028 lines, not told who sat out: carries missed by 4.07 per player with no rule, 3.65 with these.
NOLINE_TGT_CAP = 0.04   # largest target share for a player with no posted line (team has lines)
NOLINE_CAR_CAP = 0.06   # same for carries
MIN_LINED = 4           # a team needs this many players with lines before the rule applies
MISSED_LAST = 0.4       # carry-share multiplier for a player absent from the team's latest game (no lines yet)


def usage_table(ps: pd.DataFrame, tg: pd.DataFrame, team: str, T: float) -> pd.DataFrame:
    """Recent usage + efficiency for every player who has played for `team` in its last few games."""
    p = ps[(ps["season_type"] == "REG") | (ps["season_type"] == "POST")].copy()
    p["t"] = p["season"] * config.NFL_SEASON_SLOTS + p["week"]
    p = p[p["t"] < T]
    team_games = tg[(tg["team"] == team) & (tg["t"] < T)].sort_values("t")
    if team_games.empty:
        return pd.DataFrame()
    recent_t = set(team_games["t"].tail(4))
    team_last_t = float(team_games["t"].max())
    roster = p[(p["team"] == team) & p["t"].isin(recent_t)]["player_id"].unique()
    tteam = tg.set_index(["game_id", "team"])[["carries", "attempts", "passing_tds", "rushing_tds"]]
    p = p[p["player_id"].isin(roster)].join(tteam, on=["game_id", "team"], rsuffix="_team")
    rows = []
    for pid, h in p.groupby("player_id"):
        h = h.sort_values("t")
        pos = h["position"].iloc[-1]
        on_team = h[h["team"] == team].tail(8)
        if on_team.empty:
            continue
        n = np.arange(len(on_team))[::-1]
        w = 0.5 ** (n / 3.0)  # usage: half-life 3 games
        tgt_share = np.nansum(w * on_team["target_share"].fillna(0)) / w.sum()
        car_share = np.nansum(w * safe_div(on_team["carries"].fillna(0), on_team["carries_team"])) / w.sum()
        hh = h.tail(24)  # efficiency: longer window, half-life 8 games, any team
        we = 0.5 ** (np.arange(len(hh))[::-1] / 8.0)
        s = lambda c: float(np.nansum(we * hh[c].fillna(0)))  # noqa: E731
        rows.append({
            "player_id": pid, "name": h["player_display_name"].iloc[-1], "position": pos, "team": team,
            "key": norm_name(h["player_display_name"].iloc[-1]),
            "tgt_share": tgt_share, "car_share": car_share,
            "w_targets": s("targets"), "w_rec": s("receptions"), "w_rec_yds": s("receiving_yards"),
            "w_carries": s("carries"), "w_rush_yds": s("rushing_yards"), "w_att": s("attempts"),
            "w_cmp": s("completions"), "w_pass_yds": s("passing_yards"),
            "w_rec_td": s("receiving_tds"), "w_rush_td": s("rushing_tds"),
            "w_team_pass_td": float(np.nansum(we * hh["passing_tds_team"].fillna(0))),
            "w_team_rush_td": float(np.nansum(we * hh["rushing_tds_team"].fillna(0))),
            "last_t": float(on_team["t"].iloc[-1]), "team_last_t": team_last_t,
        })
    return pd.DataFrame(rows)


def _shrunk(num, den, prior, k):
    return (num + prior * k) / (den + k)


POS_PRIOR = {  # league-typical efficiency used only as shrinkage targets for small samples
    "WR": {"catch": 0.63, "ypr": 12.8}, "TE": {"catch": 0.70, "ypr": 10.5}, "RB": {"catch": 0.77, "ypr": 7.6},
    "QB": {"catch": 0.6, "ypr": 10.0}, "FB": {"catch": 0.75, "ypr": 7.0},
}


def prepare_team(u: pd.DataFrame, out_keys: set, qb_name: str, lined: set | None = None) -> tuple[pd.DataFrame, list]:
    """Drop injured players, decide who has a role, rescale the shares to one team's worth, and set
    efficiency with small-sample shrinkage. lined: keys of players with a posted prop line (if known)."""
    if u.empty:
        return u, []
    u = u.copy()
    removed = u[u["key"].isin(out_keys)]["name"].tolist()
    u = u[~u["key"].isin(out_keys)]
    qb_key = norm_name(qb_name) if isinstance(qb_name, str) else None
    # only the expected starting QB throws; backups keep no pass share
    u["is_qb_starter"] = (u["position"] == "QB") & (u["key"] == qb_key)
    if qb_key and not u["is_qb_starter"].any():
        u = u[u["position"] != "QB"]
    # Each player's share is his average over the games he played, so across a roster they add up to more
    # than 100% (a backup's big games happened while the starter sat). The players who suit up split exactly
    # one team's worth of targets and carries, so the shares are rescaled to add up to 1.
    skill = ~u["is_qb_starter"]
    if lined is not None and u["key"].isin(lined).sum() >= MIN_LINED:
        no = skill & ~u["key"].isin(lined)
        u.loc[no, "tgt_share"] = np.minimum(u.loc[no, "tgt_share"], NOLINE_TGT_CAP)
        u.loc[no, "car_share"] = np.minimum(u.loc[no, "car_share"], NOLINE_CAR_CAP)
    elif "team_last_t" in u:
        missed = skill & (u["last_t"] < u["team_last_t"])
        u.loc[missed, "car_share"] = u.loc[missed, "car_share"] * MISSED_LAST
    if u["tgt_share"].sum() > 0:
        u["tgt_share"] = np.minimum(u["tgt_share"] / u["tgt_share"].sum(), 0.42)
    if u["car_share"].sum() > 0:
        u["car_share"] = np.minimum(u["car_share"] / u["car_share"].sum(), 0.85)
    pri = u["position"].map(lambda p: POS_PRIOR.get(p, POS_PRIOR["WR"]))
    u["catch_rate"] = _shrunk(u["w_rec"], u["w_targets"], pri.map(lambda d: d["catch"]), 25)
    u["ypr"] = _shrunk(u["w_rec_yds"], u["w_rec"], pri.map(lambda d: d["ypr"]), 15)
    u["ypc"] = _shrunk(u["w_rush_yds"], u["w_carries"], 4.2, 40)
    u["cmp_pct"] = _shrunk(u["w_cmp"], u["w_att"], 0.64, 120)
    u["y_per_cmp"] = _shrunk(u["w_pass_yds"], u["w_cmp"], 10.8, 80)
    u["rec_td_share"] = _shrunk(u["w_rec_td"], u["w_team_pass_td"], u["tgt_share"], 6)
    u["rush_td_share"] = _shrunk(u["w_rush_td"], u["w_team_rush_td"], u["car_share"], 6)
    for c in ("rec_td_share", "rush_td_share"):  # shares of one pool of team TDs cannot exceed 100%
        tot = u[c].sum()
        if tot > 1:
            u[c] = u[c] / tot
    return u.reset_index(drop=True), removed


# ============================================================================= simulation
def _negbin(rng, mean, r):
    lam = np.asarray(mean, float) * rng.gamma(r, 1.0 / r, size=np.shape(mean))
    return rng.poisson(np.maximum(lam, 0))


def simulate_team(rng, u: pd.DataFrame, side: dict, c: dict) -> dict:
    """side: exp_* values for this team in this game + simulated team_pts, team_margin, total arrays."""
    n = len(side["team_pts"])
    plays = side["exp_plays"] * (1 + c["c_plays"] * (side["total"] - side["mu_total"]))
    plays = np.maximum(plays * (1 + rng.normal(0, c["sd_plays"] * 0.8, n)), 35)
    pass_rate = np.clip(side["exp_pass_rate"] + c["b_pass_rate"] * (side["team_margin"] - side["mu_team_margin"])
                        + rng.normal(0, c["sd_pass_rate"] * 0.7, n), 0.3, 0.85)
    pass_att = plays * pass_rate * (1 - side["exp_sack_rate"])
    rush_att = plays * (1 - pass_rate)
    pts_res = side["team_pts"] - side["mu_team_pts"]
    eff_pass = np.clip(1 + c["c_ypa"] * pts_res + c["d_wind"] * side["wind"] + c["d_precip"] * side["precip"], 0.4, 2)
    eff_rush = np.clip(1 + c["c_ypc"] * pts_res, 0.4, 2)
    team_td = rng.poisson(np.maximum(side["team_pts"], 0) * c["td_per_pt"])
    pass_td = rng.binomial(team_td, side["pass_td_frac"])
    rush_td = team_td - pass_td

    out = {}
    for r in u.itertuples():
        s = {}
        tg = _negbin(rng, r.tgt_share * pass_att, c["r_tgt"]) if r.tgt_share > 0 else np.zeros(n, int)
        rec = rng.binomial(tg, np.clip(r.catch_rate, 0.05, 0.98))
        ypr = r.ypr * side["pos_mult"].get(r.position, 1.0) * side["opp_pass_mult"] * eff_pass
        s["receptions"] = rec
        s["rec_yds"] = np.where(rec > 0, rng.gamma(np.maximum(rec, 1) * c["k_rec"], ypr / c["k_rec"]), 0.0)
        car = _negbin(rng, r.car_share * rush_att, c["r_car"]) if r.car_share > 0 else np.zeros(n, int)
        s["rush_att"] = car
        s["rush_yds"] = car * r.ypc * side["opp_rush_mult"] * eff_rush + np.sqrt(car) * c["sd_carry"] * rng.normal(size=n)
        s["rush_rec_yds"] = s["rush_yds"] + s["rec_yds"]
        td = rng.binomial(pass_td, np.clip(r.rec_td_share, 0, 0.6)) + rng.binomial(rush_td, np.clip(r.rush_td_share, 0, 0.9))
        if r.is_qb_starter:
            att = np.round(pass_att * rng.normal(1, 0.04, n)).astype(int).clip(0)
            cmp_ = rng.binomial(att, np.clip(r.cmp_pct * side["opp_cmp_mult"], 0.3, 0.85))
            s["pass_att"] = att
            s["pass_cmp"] = cmp_
            s["pass_yds"] = np.where(cmp_ > 0, rng.gamma(np.maximum(cmp_, 1) * c["k_cmp"],
                                                         r.y_per_cmp * side["opp_pass_mult"] * eff_pass / c["k_cmp"]), 0.0)
            s["pass_tds"] = pass_td
            td = rng.binomial(rush_td, np.clip(r.rush_td_share, 0, 0.9))  # QB's own scoring = rushing TDs only
        s["anytime_td"] = td
        out[r.key] = {k: np.asarray(v, np.float32) for k, v in s.items()}
    return out


def opponent_position_mults(ps: pd.DataFrame, T: float) -> dict:
    """Defence-vs-position: yards per target allowed to WR/TE/RB relative to the same defence overall."""
    p = ps.copy()
    p["t"] = p["season"] * config.NFL_SEASON_SLOTS + p["week"]
    p = p[(p["t"] < T) & (p["t"] >= T - 40) & p["position"].isin(["WR", "TE", "RB"])]
    p["w"] = 0.5 ** ((T - p["t"]) / config.NFL_HALFLIFE)
    p["wy"] = p["w"] * p["receiving_yards"].fillna(0)
    p["wt"] = p["w"] * p["targets"].fillna(0)
    league = p.groupby("position")[["wy", "wt"]].sum()
    league_ypt = league["wy"] / league["wt"]
    d = p.groupby(["opponent_team", "position"])[["wy", "wt"]].sum()
    allp = p.groupby("opponent_team")[["wy", "wt"]].sum()
    out = {}
    for (team, pos), r in d.iterrows():
        k = 60.0
        pos_ypt = (r.wy + k * league_ypt[pos]) / (r.wt + k)
        tot = allp.loc[team]
        all_ypt = (tot.wy + k * (league["wy"].sum() / league["wt"].sum())) / (tot.wt + k)
        rel = (pos_ypt / league_ypt[pos]) / (all_ypt / (league["wy"].sum() / league["wt"].sum()))
        out.setdefault(team, {})[pos] = float(np.clip(rel, 0.8, 1.25))
    return out


def run(games: pd.DataFrame, sims: dict, ctx: dict, all_games: pd.DataFrame, rng,
        lined: set | None = None) -> tuple[dict, pd.DataFrame]:
    """games: upcoming rows (with mu_margin, mu_total). sims: game_id -> {'margin','total'} arrays.
    lined: keys of players who have a posted prop line this week (see the note above usage_table).
    Returns ({game_id: {player_key: {market: array}}}, projections table)."""
    c = coefficients(ctx, all_games)
    ratings = ctx["ratings"].set_index(["T", "team"])
    espn = ctx["espn_inj"]
    out_keys = set(espn.loc[espn["status"].map(sources.is_out), "key"]) if not espn.empty else set()
    q_keys = set(espn.loc[espn["status"].eq("Questionable"), "key"]) if not espn.empty else set()
    tg = ctx["tg"]
    pass_td_team = tg.sort_values("t").groupby("team").tail(10).groupby("team")[["passing_tds", "rushing_tds"]].sum()
    res, proj = {}, []
    for g in games.itertuples():
        T = float(g.t)
        pos_mult = opponent_position_mults(ctx["ps"], T)
        M, TOT = sims[g.game_id]["margin"], sims[g.game_id]["total"]
        res[g.game_id] = {}
        for side_name, team, opp, sign in (("h", g.home, g.away, 1), ("a", g.away, g.home, -1)):
            own = ratings.loc[(T, team)]
            ex = lambda s: float(getattr(g, f"exp_{side_name}_{s}"))  # noqa: E731
            base_ypa = own["pass_ypa_mu"] + own["pass_ypa_o"]
            base_ypc = own["rush_ypc_mu"] + own["rush_ypc_o"]
            ptd = pass_td_team.loc[team] if team in pass_td_team.index else None
            frac = c["pass_td_frac"] if ptd is None else float((ptd["passing_tds"] + 6 * c["pass_td_frac"]) / (ptd.sum() + 6))
            side = {
                "team_pts": (TOT + sign * M) / 2, "team_margin": sign * M, "total": TOT,
                "mu_team_pts": (g.mu_total + sign * g.mu_margin) / 2, "mu_team_margin": sign * g.mu_margin,
                "mu_total": g.mu_total, "exp_plays": ex("plays"), "exp_pass_rate": ex("pass_rate"),
                "exp_sack_rate": max(ex("sack_rate"), 0.02),
                "opp_pass_mult": float(np.clip(ex("pass_ypa") / base_ypa, 0.75, 1.3)) if base_ypa > 0 else 1.0,
                "opp_rush_mult": float(np.clip(ex("rush_ypc") / base_ypc, 0.75, 1.3)) if base_ypc > 0 else 1.0,
                "opp_cmp_mult": 1.0, "pos_mult": pos_mult.get(opp, {}), "pass_td_frac": frac,
                "wind": float(g.wind), "precip": float(g.precip),
            }
            u = usage_table(ctx["ps"], tg, team, T)
            qb_name = g.qb_name_h if side_name == "h" else g.qb_name_a
            u, removed = prepare_team(u, out_keys, qb_name, lined)
            if u.empty:
                continue
            # only players who can touch the ball are simulated: linemen and defenders are in the usage table
            # with zero shares, and 20,000 simulated games each for them is most of the app's memory
            u = u[(u["tgt_share"] >= 0.005) | (u["car_share"] >= 0.005) | u["is_qb_starter"]].reset_index(drop=True)
            if u.empty:
                continue
            sim = simulate_team(rng, u, side, c)
            res[g.game_id].update(sim)
            for r in u.itertuples():
                st = sim[r.key]
                if r.tgt_share < 0.05 and r.car_share < 0.05 and not r.is_qb_starter:
                    continue
                proj.append({"game_id": g.game_id, "team": team, "opp": opp, "player": r.name, "key": r.key,
                             "pos": r.position, "status": "Q" if r.key in q_keys else "",
                             "touch_share": r.tgt_share * side["exp_pass_rate"] + r.car_share * (1 - side["exp_pass_rate"]),
                             "tgt_share": r.tgt_share, "car_share": r.car_share,
                             **{f"{m}": float(np.mean(st[m])) for m in MARKETS if m in st and m != "anytime_td"},
                             "p_anytime_td": float(np.mean(st["anytime_td"] >= 1))})
            if removed:
                proj.append({"game_id": g.game_id, "team": team, "player": "OUT: " + ", ".join(removed), "key": "",
                             "pos": "", "status": "out"})
    return res, pd.DataFrame(proj)
