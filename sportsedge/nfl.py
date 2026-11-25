"""NFL data, game features, and game model.

Factors in the game model (all computed only from information available before kickoff):
  * opponent-adjusted team ratings: points, EPA/play, pass EPA/dropback, rush EPA/carry, yards per
    attempt / carry, sack rate, turnover rate, explosive-play rate, pace (plays), pass rate
  * starting-QB quality (EPA/play, shrunk for small samples) -> catches backup-QB starts
  * injuries: expected snap share of players listed Out/Doubtful (skill players, O-line, defence)
  * rest days (byes, short weeks), travel distance, time-zone change, neutral site, division game
  * weather: temperature, wind, precipitation, snow, dome/roof
"""
from __future__ import annotations

import datetime as dt

import nflreadpy as nfl
import numpy as np
import pandas as pd

from . import config, sources
from .gamemodel import GameModel
from .ratings import attach_matchup, opponent_adjusted
from .util import NFL_CANON, cached, haversine_km, nfl_site, norm_name, safe_div

STATS = ["points", "epa_play", "pass_epa_db", "rush_epa_c", "sack_rate", "to_rate", "explosive", "plays",
         "pass_rate", "pass_ypa", "rush_ypc"]

MARGIN_FEATS = ["diff_points", "diff_epa_play", "diff_pass_epa_db", "diff_rush_epa_c", "diff_sack_rate",
                "diff_to_rate", "diff_explosive", "diff_pass_ypa", "diff_rush_ypc", "qb_diff", "rest_diff",
                "travel_diff", "tz_diff", "home_field", "inj_skill_diff", "inj_ol_diff", "inj_def_diff"]
TOTAL_FEATS = ["sum_points", "sum_epa_play", "sum_pass_epa_db", "sum_rush_epa_c", "sum_explosive", "sum_plays",
               "sum_to_rate", "sum_sack_rate", "sum_pass_ypa", "sum_rush_ypc", "mu_points", "qb_sum",
               "cold", "wind", "precip", "snow", "indoor", "div_game", "inj_skill_sum", "inj_ol_sum", "inj_def_sum"]
LABELS = {
    "diff_points": "Scoring rating (opp-adj)", "diff_epa_play": "EPA/play rating", "diff_pass_epa_db": "Passing EPA",
    "diff_rush_epa_c": "Rushing EPA", "diff_sack_rate": "Sack rate", "diff_to_rate": "Turnover rate",
    "diff_explosive": "Explosive plays", "diff_pass_ypa": "Yards/attempt", "diff_rush_ypc": "Yards/carry",
    "qb_diff": "Starting QB quality", "rest_diff": "Rest days", "travel_diff": "Travel distance",
    "tz_diff": "Time-zone change", "home_field": "Home field", "inj_skill_diff": "Skill-player injuries",
    "inj_ol_diff": "O-line injuries", "inj_def_diff": "Defensive injuries",
    "sum_points": "Scoring ratings (both)", "sum_epa_play": "EPA/play (both)", "sum_pass_epa_db": "Passing EPA (both)",
    "sum_rush_epa_c": "Rushing EPA (both)", "sum_explosive": "Explosive plays (both)", "sum_plays": "Pace (plays)",
    "sum_to_rate": "Turnover rates", "sum_sack_rate": "Sack rates", "sum_pass_ypa": "Yards/attempt (both)",
    "sum_rush_ypc": "Yards/carry (both)", "mu_points": "League scoring level", "qb_sum": "QB quality (both)",
    "cold": "Cold (below 50F)", "wind": "Wind", "precip": "Rain", "snow": "Snow", "indoor": "Indoors",
    "div_game": "Division game", "inj_skill_sum": "Skill injuries (both)", "inj_ol_sum": "O-line injuries (both)",
    "inj_def_sum": "Defensive injuries (both)",
}

SKILL = {"RB", "WR", "TE", "FB", "HB"}
OLINE = {"T", "G", "C", "OL", "OT", "OG", "LT", "RT", "LG", "RG"}


# ============================================================================= data
def current_season() -> int:
    return int(nfl.get_current_season())


def _canon(df, cols):
    for c in cols:
        if c in df:
            df[c] = df[c].replace(NFL_CANON)
    return df


def load(name: str, first: int | None = None) -> pd.DataFrame:
    fns = {"schedules": nfl.load_schedules, "team_stats": nfl.load_team_stats, "player_stats": nfl.load_player_stats,
           "snap_counts": nfl.load_snap_counts, "injuries": nfl.load_injuries}
    first = first or config.NFL_FIRST_SEASON
    seasons = list(range(first, current_season() + 1))

    def fetch():
        df = fns[name](seasons).to_pandas()
        return _canon(df, ["home_team", "away_team", "team", "opponent_team", "opponent", "recent_team"])

    return cached(f"nfl_{name}_{first}", 12, fetch)


def load_ids() -> pd.DataFrame:
    return cached("nfl_ff_ids", 72, lambda: nfl.load_ff_playerids().to_pandas())


# ============================================================================= team games
def team_games(sched: pd.DataFrame, ts: pd.DataFrame) -> pd.DataFrame:
    s = sched[sched["home_score"].notna()].copy()
    neutral = s["location"].eq("Neutral").to_numpy()
    base = ["game_id", "season", "week"]
    h = s[base].assign(team=s["home_team"], opp=s["away_team"], home=np.where(neutral, 0, 1),
                       points=s["home_score"], pa=s["away_score"])
    a = s[base].assign(team=s["away_team"], opp=s["home_team"], home=np.where(neutral, 0, -1),
                       points=s["away_score"], pa=s["home_score"])
    tg = pd.concat([h, a], ignore_index=True)
    cols = ["game_id", "team", "attempts", "sacks_suffered", "carries", "passing_epa", "rushing_epa", "passing_yards",
            "rushing_yards", "passing_interceptions", "sack_fumbles_lost", "rushing_fumbles_lost",
            "receiving_fumbles_lost", "passing_20", "rushing_20", "passing_tds", "rushing_tds"]
    tg = tg.merge(ts[cols], on=["game_id", "team"], how="left")
    f = lambda c: tg[c].fillna(0).to_numpy(float)  # noqa: E731
    db = f("attempts") + f("sacks_suffered")
    plays = db + f("carries")
    tg["plays"] = np.where(plays > 0, plays, np.nan)
    tg["epa_play"] = safe_div(f("passing_epa") + f("rushing_epa"), plays)
    tg["pass_epa_db"] = safe_div(f("passing_epa"), db)
    tg["rush_epa_c"] = safe_div(f("rushing_epa"), f("carries"))
    tg["sack_rate"] = safe_div(f("sacks_suffered"), db)
    tg["to_rate"] = safe_div(f("passing_interceptions") + f("sack_fumbles_lost") + f("rushing_fumbles_lost")
                             + f("receiving_fumbles_lost"), plays)
    tg["explosive"] = safe_div(f("passing_20") + f("rushing_20"), plays)
    tg["pass_rate"] = safe_div(db, plays)
    tg["pass_ypa"] = safe_div(f("passing_yards"), f("attempts"))
    tg["rush_ypc"] = safe_div(f("rushing_yards"), f("carries"))
    tg["t"] = tg["season"] * config.NFL_SEASON_SLOTS + tg["week"]
    return tg


def team_ratings(tg: pd.DataFrame, eval_times) -> pd.DataFrame:
    key = f"nfl_ratings_{len(tg)}_{int(max(eval_times))}_{len(set(eval_times))}"
    return cached(key, 24, lambda: opponent_adjusted(tg, STATS, eval_times, config.NFL_HALFLIFE, config.NFL_RIDGE))


# ============================================================================= quarterbacks
def qb_table(ps: pd.DataFrame) -> pd.DataFrame:
    q = ps[ps["attempts"].fillna(0) > 0].copy()
    q["plays"] = q["attempts"].fillna(0) + q["sacks_suffered"].fillna(0) + q["carries"].fillna(0)
    q["epa"] = q["passing_epa"].fillna(0) + q["rushing_epa"].fillna(0)
    q["t"] = q["season"] * config.NFL_SEASON_SLOTS + q["week"]
    q["key"] = q["player_display_name"].map(norm_name)
    return q[["player_id", "player_display_name", "key", "team", "season", "week", "t", "attempts", "plays", "epa"]]


class QBValues:
    def __init__(self, q: pd.DataFrame):
        self.q = q
        self.by = {pid: (g["t"].to_numpy(float), g["plays"].to_numpy(float), g["epa"].to_numpy(float))
                   for pid, g in q.groupby("player_id")}

    def value(self, pid, T: float) -> float:
        k0, prior = config.NFL_QB_PRIOR_PLAYS, config.NFL_QB_PRIOR_EPA
        if pid not in self.by:
            return prior
        t, plays, epa = self.by[pid]
        m = t < T
        w = 0.5 ** ((T - t[m]) / config.NFL_QB_HALFLIFE)
        return float(((w * epa[m]).sum() + k0 * prior) / ((w * plays[m]).sum() + k0))

    def expected_starter(self, team: str, T: float, last_starter, out_keys: set):
        """Last game's starter unless he is ruled out; otherwise the team's busiest other passer."""
        q = self.q
        if last_starter is not None and isinstance(last_starter, str):
            row = q[q["player_id"] == last_starter].tail(1)
            if row.empty or row["key"].iloc[0] not in out_keys:
                name = row["player_display_name"].iloc[0] if not row.empty else last_starter
                return last_starter, name
        recent = q[(q["team"] == team) & (q["t"] < T) & (q["t"] >= T - 40) & ~q["key"].isin(out_keys)]
        if recent.empty:
            return None, "unknown"
        best = recent.groupby(["player_id", "player_display_name"])["plays"].sum().idxmax()
        return best


# ============================================================================= injuries
def _group(pos) -> str | None:
    pos = str(pos).upper()
    if pos == "QB":
        return None
    if pos in SKILL:
        return "skill"
    if pos in OLINE:
        return "ol"
    return "def"


def expected_snap_shares(sched: pd.DataFrame, snaps: pd.DataFrame) -> dict:
    """Per team: DataFrame indexed by (season, week) of every player's expected snap share entering that game
    (mean of the previous 4 team games, 0 when he did not play)."""
    out = {}
    s = sched[["season", "week", "home_team", "away_team"]]
    for team in sorted(set(s["home_team"]) | set(s["away_team"])):
        seq = s[(s["home_team"] == team) | (s["away_team"] == team)][["season", "week"]].drop_duplicates()
        seq = seq.sort_values(["season", "week"])
        idx = pd.MultiIndex.from_frame(seq)
        sn = snaps[snaps["team"] == team]
        res = {}
        for col in ("offense_pct", "defense_pct"):
            pv = sn.pivot_table(index=["season", "week"], columns="pfr_player_id", values=col, aggfunc="max")
            pv = pv.reindex(idx).fillna(0.0)
            res[col] = pv.rolling(4, min_periods=1).mean().shift(1).fillna(0.0)
        names = sn.drop_duplicates("pfr_player_id", keep="last").set_index("pfr_player_id")["player"]
        res["names"] = {norm_name(n): pid for pid, n in names.items()}
        out[team] = res
    return out


def injury_impact_hist(shares: dict, inj: pd.DataFrame) -> pd.DataFrame:
    """Matched by name within team (the fantasy id map has no linemen), same as live ESPN matching."""
    inj = inj[inj["report_status"].isin(["Out", "Doubtful"])].copy()
    inj["key"] = inj["full_name"].map(norm_name)
    inj["grp"] = inj["position"].map(_group)
    rows = []
    for (team, season, week), grp in inj.groupby(["team", "season", "week"]):
        sh = shares.get(team)
        if sh is None or (season, week) not in sh["offense_pct"].index:
            continue
        players = [(sh["names"].get(k), gp) for k, gp in zip(grp["key"], grp["grp"])]
        rows.append({"team": team, "season": season, "week": week, **_impact(sh, season, week, players)})
    return pd.DataFrame(rows, columns=["team", "season", "week", "inj_skill", "inj_ol", "inj_def"])


def _impact(sh, season, week, players) -> dict:
    tot = {"inj_skill": 0.0, "inj_ol": 0.0, "inj_def": 0.0}
    off, de = sh["offense_pct"].loc[(season, week)], sh["defense_pct"].loc[(season, week)]
    for pfr, grp in players:
        if not isinstance(grp, str) or not isinstance(pfr, str):
            continue
        src = de if grp == "def" else off
        if pfr in src.index:
            tot[f"inj_{grp}"] += float(src[pfr])
    return tot


def injury_impact_live(shares: dict, espn: pd.DataFrame, team: str, season: int, week: int) -> dict:
    sh = shares.get(team)
    if sh is None or (season, week) not in sh["offense_pct"].index or espn.empty:
        return {"inj_skill": 0.0, "inj_ol": 0.0, "inj_def": 0.0}
    e = espn[(espn["team"] == team) & espn["status"].isin(["Out", "Doubtful"])]
    players = [(sh["names"].get(k), _group(p)) for k, p in zip(e["key"], e["position"])]
    return _impact(sh, season, week, players)


# ============================================================================= weather
def _kickoff_utc(gameday, gametime) -> pd.Timestamp:
    ts = pd.Timestamp(f"{gameday} {gametime or '13:00'}").tz_localize("America/New_York")
    return ts.tz_convert("UTC")


def historical_precip(sched: pd.DataFrame) -> pd.DataFrame:
    sites = {}
    for _, r in sched[["home_team", "season"]].drop_duplicates().iterrows():
        site = nfl_site(r.home_team, r.season)
        if site:
            sites.setdefault((site[0], site[1]), site)
    start = dt.date(config.NFL_FIRST_SEASON, 8, 1)
    end = dt.date.today() - dt.timedelta(days=6)
    frames = []
    for (lat, lon), site in sites.items():
        try:
            w = sources.weather_history(site, start, end)
        except RuntimeError:
            continue
        frames.append(w.assign(lat=lat, lon=lon))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["date", "precip_mm", "snow_cm", "lat", "lon"])


# ============================================================================= feature table
def build(upcoming: pd.DataFrame | None = None, verbose=True) -> tuple[pd.DataFrame, dict]:
    """All games (completed + upcoming) with features. `upcoming` = ESPN slate rows for live odds/weather."""
    log = verbose if callable(verbose) else (print if verbose else (lambda *a, **k: None))
    log("NFL: loading nflverse data ...")
    sched = load("schedules")
    ts = load("team_stats")
    ps = load("player_stats")
    snaps = load("snap_counts")
    inj = load("injuries")

    sched = sched.copy()
    sched["t"] = sched["season"] * config.NFL_SEASON_SLOTS + sched["week"]
    cur = current_season()
    # keep completed games + current-season upcoming games
    sched = sched[(sched["home_score"].notna()) | (sched["season"] == cur)].reset_index(drop=True)

    tg = team_games(sched, ts)
    log("NFL: fitting opponent-adjusted ratings ...")
    ratings = team_ratings(tg, sched["t"].unique())

    g = sched.rename(columns={"home_team": "home", "away_team": "away"})
    g["completed"] = g["home_score"].notna()
    g["margin"] = g["home_score"] - g["away_score"]
    g["total"] = g["home_score"] + g["away_score"]
    g["mkt_margin"] = g["spread_line"]
    g["mkt_total"] = g["total_line"]
    g = attach_matchup(g, ratings, STATS)

    # ---- quarterbacks
    q = qb_table(ps)
    qbv = QBValues(q)
    espn_inj = sources.espn_injuries("nfl") if upcoming is not None else pd.DataFrame(
        columns=["team", "key", "status", "position"])
    out_keys = set(espn_inj.loc[espn_inj["status"].map(sources.is_out), "key"]) if not espn_inj.empty else set()
    last_qb = _last_starters(g)
    qb_h, qb_a, qbn_h, qbn_a = [], [], [], []
    for r in g.itertuples():
        for side, store, names in (("home", qb_h, qbn_h), ("away", qb_a, qbn_a)):
            pid = getattr(r, f"{side}_qb_id")
            name = getattr(r, f"{side}_qb_name")
            if not r.completed and (not isinstance(pid, str) or not pid):
                pid, name = qbv.expected_starter(getattr(r, side), r.t, last_qb.get((getattr(r, side), r.t)), out_keys)
            store.append(qbv.value(pid, r.t))
            names.append(name)
    g["qb_h"], g["qb_a"] = qb_h, qb_a
    g["qb_name_h"], g["qb_name_a"] = qbn_h, qbn_a
    g["qb_diff"] = g["qb_h"] - g["qb_a"]
    g["qb_sum"] = g["qb_h"] + g["qb_a"]

    # ---- rest / travel / site
    neutral = g["location"].eq("Neutral")
    g["home_field"] = (~neutral).astype(float)
    hr = g["home_rest"].fillna(7).clip(4, 14)
    ar = g["away_rest"].fillna(7).clip(4, 14)
    g["rest_diff"] = hr - ar
    th, ta, tzh, tza = [], [], [], []
    for r in g.itertuples():
        hs, as_ = nfl_site(r.home, r.season), nfl_site(r.away, r.season)
        if r.location == "Neutral" or hs is None or as_ is None:
            th.append(0.0), ta.append(0.0), tzh.append(0.0), tza.append(0.0)
            continue
        th.append(0.0)
        ta.append(haversine_km(as_, hs) / 1000)
        tzh.append(0.0)
        tza.append(abs(hs[2] - as_[2]))
    g["travel_diff"] = np.array(th) - np.array(ta)
    g["tz_diff"] = np.array(tzh) - np.array(tza)
    g["div_game"] = g["div_game"].fillna(0).astype(float)

    # ---- injuries
    log("NFL: injury impact ...")
    shares = cached(f"nfl_snapshares_{len(snaps)}_{len(sched)}", 12, lambda: expected_snap_shares(sched, snaps))
    ih = injury_impact_hist(shares, inj).set_index(["team", "season", "week"])
    for side in ("home", "away"):
        key = list(zip(g[side], g["season"], g["week"]))
        vals = ih.reindex(key)
        for c in ("inj_skill", "inj_ol", "inj_def"):
            g[f"{c}_{side[0]}"] = vals[c].fillna(0.0).to_numpy()

    # ---- weather (history)
    log("NFL: weather ...")
    roof = g["roof"].fillna("outdoors").str.lower()
    g["indoor"] = roof.isin(["dome", "closed"]).astype(float)
    px = historical_precip(sched)
    px_map = {(round(r.lat, 3), round(r.lon, 3), r.date): (r.precip_mm, r.snow_cm) for r in px.itertuples()}
    pr, sn = [], []
    for r in g.itertuples():
        site = nfl_site(r.home, r.season)
        d = pd.Timestamp(r.gameday).date()
        v = px_map.get((round(site[0], 3), round(site[1], 3), d)) if site and r.location != "Neutral" else None
        pr.append(v[0] if v else 0.0)
        sn.append(v[1] if v else 0.0)
    g["precip_raw"], g["snow_raw"] = pr, sn
    g["temp_f"] = g["temp"]
    g["wind_mph"] = g["wind"]

    # ---- live overrides for upcoming games (odds, weather, roof, injuries)
    g["event_id"] = pd.Series([None] * len(g), dtype=object)
    g["start"] = pd.Series(pd.NaT, index=g.index, dtype="datetime64[ns, UTC]")
    if upcoming is not None and not upcoming.empty:
        _apply_live(g, upcoming, shares, espn_inj)

    out = g["indoor"] > 0
    g["cold"] = np.where(out, 0.0, (50 - g["temp_f"].fillna(60)).clip(lower=0))
    g["wind"] = np.where(out, 0.0, g["wind_mph"].fillna(5).clip(0, 40))
    g["precip"] = np.where(out, 0.0, np.minimum(g["precip_raw"].fillna(0), 25))
    g["snow"] = np.where(out, 0.0, np.minimum(g["snow_raw"].fillna(0), 15))
    for c in ("inj_skill", "inj_ol", "inj_def"):
        g[f"{c}_diff"] = g[f"{c}_h"] - g[f"{c}_a"]
        g[f"{c}_sum"] = g[f"{c}_h"] + g[f"{c}_a"]

    feats = sorted(set(MARGIN_FEATS + TOTAL_FEATS))
    g = g[~g[["diff_points", "sum_points", "mu_points"]].isna().any(axis=1)].reset_index(drop=True)
    g[feats] = g[feats].astype(float).fillna(0.0)
    ctx = {"sched": sched, "ps": ps, "snaps": snaps, "tg": tg, "ratings": ratings, "shares": shares,
           "espn_inj": espn_inj, "qbv": qbv}
    return g, ctx


def _last_starters(g: pd.DataFrame) -> dict:
    """(team, T) -> QB id of the team's most recent completed game before T."""
    done = g[g["completed"]].sort_values("t")
    hist = {}
    for r in done.itertuples():
        hist.setdefault(r.home, []).append((r.t, r.home_qb_id))
        hist.setdefault(r.away, []).append((r.t, r.away_qb_id))
    out = {}
    for r in g[~g["completed"]].itertuples():
        for team in (r.home, r.away):
            prev = [pid for t, pid in hist.get(team, []) if t < r.t]
            out[(team, r.t)] = prev[-1] if prev else None
    return out


def _apply_live(g: pd.DataFrame, up: pd.DataFrame, shares: dict, espn_inj: pd.DataFrame):
    for _, u in up.iterrows():
        m = (~g["completed"]) & (g["home"] == u.home) & (g["away"] == u.away)
        if not m.any():
            continue
        i = g.index[m][0]
        g.at[i, "event_id"] = u.event_id
        g.at[i, "start"] = u.start
        if not np.isnan(u.spread_home):
            g.at[i, "mkt_margin"] = -u.spread_home
        if not np.isnan(u.total):
            g.at[i, "mkt_total"] = u.total
        if u.indoor is True:
            g.at[i, "indoor"] = 1.0
        elif u.indoor is False:
            g.at[i, "indoor"] = 0.0
        site = nfl_site(u.home)
        if g.at[i, "location"] == "Neutral":
            site = None
        if site and g.at[i, "indoor"] == 0:
            wx = sources.weather_forecast(site, u.start)
            if wx:
                g.at[i, "temp_f"] = wx["temp"]
                g.at[i, "wind_mph"] = wx["wind"]
                g.at[i, "precip_raw"] = wx["precip_mm"]
                g.at[i, "snow_raw"] = wx["snow_cm"]
        season, week = int(g.at[i, "season"]), int(g.at[i, "week"])
        for side, team in (("h", u.home), ("a", u.away)):
            imp = injury_impact_live(shares, espn_inj, team, season, week)
            for c, v in imp.items():
                g.at[i, f"{c}_{side}"] = v


def shape_history() -> pd.DataFrame:
    """Long history of (margin, total, closing line) used to learn key numbers."""
    s = load("schedules", first=config.NFL_KEYNUM_FIRST_SEASON)
    s = s[s["home_score"].notna()]
    return pd.DataFrame({"season": s["season"], "margin": s["home_score"] - s["away_score"],
                         "total": s["home_score"] + s["away_score"], "mkt_margin": s["spread_line"],
                         "mkt_total": s["total_line"]})


def fit_model(g: pd.DataFrame) -> GameModel:
    return GameModel("nfl", MARGIN_FEATS, TOTAL_FEATS, LABELS).fit(g, shape_history())
