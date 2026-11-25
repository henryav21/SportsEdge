"""NBA data, game features, and game model.

Factors (all known before tip-off):
  * opponent-adjusted ratings: points, offensive rating (pts/100 poss), pace, the four factors
    (eFG%, turnover %, offensive-rebound %, free-throw rate), 3-point attempt rate
  * rest: days off, back-to-backs, travel distance since last game, time-zone change, altitude
  * missing players: expected minutes x per-minute impact (game score) of rotation players who are out
    (history: absent from the box score; live: ESPN injury report Out/Doubtful)
"""
from __future__ import annotations

import datetime as dt
import time

import numpy as np
import pandas as pd

from . import config, sources
from .gamemodel import GameModel
from .ratings import attach_matchup, opponent_adjusted
from .util import NBA_SITES, cached, haversine_km, norm_name, safe_div

STATS = ["points", "ortg", "pace", "efg", "tov_pct", "orb_pct", "ftr", "fg3a_rate", "reb", "ast", "fg3m"]
MARGIN_FEATS = ["diff_points", "diff_ortg", "diff_efg", "diff_tov_pct", "diff_orb_pct", "diff_ftr", "diff_fg3a_rate",
                "rest_diff", "b2b_diff", "travel_diff", "tz_diff", "altitude", "home_field", "miss_val_diff",
                "miss_min_diff"]
TOTAL_FEATS = ["sum_points", "sum_pace", "sum_ortg", "sum_efg", "sum_tov_pct", "sum_orb_pct", "sum_ftr",
               "sum_fg3a_rate", "mu_points", "mu_pace", "b2b_sum", "rest_sum", "miss_val_sum", "miss_min_sum"]
LABELS = {
    "diff_points": "Scoring rating (opp-adj)", "diff_ortg": "Efficiency rating", "diff_efg": "Shooting (eFG%)",
    "diff_tov_pct": "Turnovers", "diff_orb_pct": "Off. rebounding", "diff_ftr": "Free-throw rate",
    "diff_fg3a_rate": "3PA rate", "rest_diff": "Rest days", "b2b_diff": "Back-to-back", "travel_diff": "Travel",
    "tz_diff": "Time-zone change", "altitude": "Altitude", "home_field": "Home court",
    "miss_val_diff": "Missing players (impact)", "miss_min_diff": "Missing players (minutes)",
    "sum_points": "Scoring ratings (both)", "sum_pace": "Pace (both)", "sum_ortg": "Offensive ratings (both)",
    "sum_efg": "Shooting (both)", "sum_tov_pct": "Turnovers (both)", "sum_orb_pct": "Off. rebounding (both)",
    "sum_ftr": "Free-throw rate (both)", "sum_fg3a_rate": "3PA rate (both)", "mu_points": "League scoring level",
    "mu_pace": "League pace", "b2b_sum": "Back-to-backs", "rest_sum": "Rest days (both)",
    "miss_val_sum": "Missing players (impact, both)", "miss_min_sum": "Missing players (minutes, both)",
}


# ============================================================================= data
def season_str(year: int) -> str:
    return f"{year}-{(year + 1) % 100:02d}"


def current_season_year(today: dt.date | None = None) -> int:
    today = today or dt.date.today()
    return today.year if today.month >= 9 else today.year - 1


def _gamelog(season: str, kind: str, stype: str) -> pd.DataFrame:
    from nba_api.stats.endpoints import leaguegamelog

    last = None
    for attempt in range(4):
        try:
            df = leaguegamelog.LeagueGameLog(season=season, player_or_team_abbreviation=kind,
                                             season_type_all_star=stype, timeout=60).get_data_frames()[0]
            time.sleep(0.6)
            return df
        except Exception as e:  # stats.nba.com is flaky; back off and retry
            last = e
            time.sleep(2 + 3 * attempt)
    raise RuntimeError(f"nba_api LeagueGameLog {season} {kind} {stype} failed: {last}")


def load_logs(kind: str) -> pd.DataFrame:
    cur = current_season_year()
    frames = []
    for y in range(cur - config.NBA_HISTORY_SEASONS, cur + 1):
        for stype in ("Regular Season", "Playoffs"):
            ttl = 4 if y == cur else None
            if y == cur and stype == "Playoffs" and dt.date.today().month < 4:
                continue
            df = cached(f"nba_log_{kind}_{y}_{stype[:3]}", ttl, lambda: _gamelog(season_str(y), kind, stype))
            if not df.empty:
                frames.append(df.assign(season=y, playoffs=stype == "Playoffs"))
    df = pd.concat(frames, ignore_index=True)
    df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"]).dt.date
    return df


def _t(season: int, date, season_start: dict) -> float:
    return (pd.Timestamp(date) - pd.Timestamp(season_start[season])).days + (season - 2000) * config.NBA_SEASON_DAYS


# ============================================================================= team games
def team_games(tl: pd.DataFrame) -> pd.DataFrame:
    t = tl.copy()
    t["is_home"] = t["MATCHUP"].str.contains(" vs. ")
    o = t[["GAME_ID", "TEAM_ABBREVIATION", "PTS", "FGA", "FTA", "OREB", "DREB", "TOV"]].rename(
        columns=lambda c: c if c == "GAME_ID" else f"OPP_{c}")
    t = t.merge(o, on="GAME_ID")
    t = t[t["TEAM_ABBREVIATION"] != t["OPP_TEAM_ABBREVIATION"]]
    poss = t["FGA"] + 0.44 * t["FTA"] - t["OREB"] + t["TOV"]
    opp_poss = t["OPP_FGA"] + 0.44 * t["OPP_FTA"] - t["OPP_OREB"] + t["OPP_TOV"]
    game_poss = (poss + opp_poss) / 2
    out = pd.DataFrame({
        "game_id": t["GAME_ID"], "date": t["GAME_DATE"], "season": t["season"], "team": t["TEAM_ABBREVIATION"],
        "opp": t["OPP_TEAM_ABBREVIATION"], "home": np.where(t["is_home"], 1, -1), "points": t["PTS"],
        "pa": t["OPP_PTS"], "pace": game_poss, "ortg": safe_div(100 * t["PTS"], game_poss),
        "efg": safe_div(t["FGM"] + 0.5 * t["FG3M"], t["FGA"]), "tov_pct": safe_div(t["TOV"], game_poss),
        "orb_pct": safe_div(t["OREB"], t["OREB"] + t["OPP_DREB"]), "ftr": safe_div(t["FTA"], t["FGA"]),
        "fg3a_rate": safe_div(t["FG3A"], t["FGA"]), "reb": t["REB"], "ast": t["AST"], "fg3m": t["FG3M"],
    })
    return out.reset_index(drop=True)


# ============================================================================= missing players
def player_quality(pl: pd.DataFrame) -> pd.DataFrame:
    p = pl.copy()
    p["MIN"] = p["MIN"].astype(float)
    p["GS"] = (p["PTS"] + 0.4 * p["FGM"] - 0.7 * p["FGA"] - 0.4 * (p["FTA"] - p["FTM"]) + 0.7 * p["OREB"]
               + 0.3 * p["DREB"] + p["STL"] + 0.7 * p["AST"] + 0.7 * p["BLK"] - 0.4 * p["PF"] - p["TOV"])
    return p


def missing_players(tg: pd.DataFrame, pl: pd.DataFrame) -> tuple[pd.DataFrame, dict, float]:
    """History: per team-game, expected minutes and impact of rotation players absent from the box score.
    Also returns the latest per-team player table (for live injuries) and the replacement level."""
    p = player_quality(pl)
    rot = p[p["MIN"] >= 15]
    repl = float(np.percentile(rot["GS"] / rot["MIN"], 20))  # replacement level = 20th pct rotation player
    prior_min = 150.0
    # next appearance team (to tell trades from injuries)
    p = p.sort_values(["PLAYER_ID", "GAME_DATE"])
    by_player = {pid: (g["GAME_DATE"].to_numpy(), g["TEAM_ABBREVIATION"].to_numpy()) for pid, g in p.groupby("PLAYER_ID")}
    rows, latest = [], {}
    tgs = tg.sort_values("date")
    for team, games in tgs.groupby("team"):
        gids = games["game_id"].tolist()
        dates = games["date"].to_numpy()
        pt = p[p["TEAM_ABBREVIATION"] == team]
        mins = pt.pivot_table(index="GAME_ID", columns="PLAYER_ID", values="MIN", aggfunc="sum").reindex(gids)
        gs = pt.pivot_table(index="GAME_ID", columns="PLAYER_ID", values="GS", aggfunc="sum").reindex(gids)
        played = mins.notna()
        exp_min = mins.fillna(0).ewm(halflife=5).mean()
        s_sum = gs.ewm(halflife=10, ignore_na=True).sum()
        m_sum = mins.ewm(halflife=10, ignore_na=True).sum()
        gspm = ((s_sum.fillna(0) + repl * prior_min) / (m_sum.fillna(0) + prior_min))
        val = (gspm - repl) * exp_min
        # as-of (entering each game) = previous row
        exp_prev, val_prev = exp_min.shift(1).fillna(0), val.shift(1).fillna(0)
        cand = (exp_prev >= 8) & ~played
        for i in np.where(cand.to_numpy().any(axis=1))[0]:
            mm, vv = 0.0, 0.0
            for pid in cand.columns[cand.iloc[i].to_numpy()]:
                d_all, t_all = by_player[pid]
                j = np.searchsorted(d_all, dates[i], side="right")
                if j < len(d_all) and t_all[j] != team:
                    continue  # traded away, not injured
                if j >= len(d_all) and d_all[-1] < dates[i] and (dates[i] - d_all[-1]).days > 60:
                    continue  # long gone
                mm += exp_prev.iloc[i][pid]
                vv += val_prev.iloc[i][pid]
            rows.append({"game_id": gids[i], "team": team, "miss_min": mm, "miss_val": vv})
        last = pd.DataFrame({"exp_min": exp_min.iloc[-1], "val": val.iloc[-1], "gspm": gspm.iloc[-1]})
        names = pt.drop_duplicates("PLAYER_ID", keep="last").set_index("PLAYER_ID")["PLAYER_NAME"]
        last["name"] = names.reindex(last.index)
        last["key"] = last["name"].map(norm_name)
        latest[team] = last[last["exp_min"] > 1]
    return pd.DataFrame(rows, columns=["game_id", "team", "miss_min", "miss_val"]), latest, repl


# ============================================================================= feature table
def build(upcoming: pd.DataFrame | None = None, verbose=True) -> tuple[pd.DataFrame, dict]:
    log = verbose if callable(verbose) else (print if verbose else (lambda *a, **k: None))
    log("NBA: loading game logs (stats.nba.com) ...")
    tl = load_logs("T")
    pl = load_logs("P")
    tg = team_games(tl)
    season_start = tg.groupby("season")["date"].min().to_dict()
    cur = current_season_year()
    season_start.setdefault(cur, dt.date(cur, 10, 21))
    tg["t"] = [_t(s, d, season_start) for s, d in zip(tg["season"], tg["date"])]

    # ---- games: completed (home rows) + upcoming slate
    h = tg[tg["home"] == 1]
    g = pd.DataFrame({"game_id": h["game_id"], "date": h["date"], "season": h["season"], "home": h["team"],
                      "away": h["opp"], "home_pts": h["points"], "away_pts": h["pa"], "t": h["t"],
                      "completed": True, "event_id": None, "start": pd.NaT, "neutral": False})
    if upcoming is not None and not upcoming.empty:
        up = upcoming[upcoming["state"] == "pre"].copy()
        up["date"] = up["start"].dt.tz_convert("America/New_York").dt.date
        u = pd.DataFrame({"game_id": "E" + up["event_id"].astype(str), "date": up["date"], "season": cur,
                          "home": up["home"], "away": up["away"], "home_pts": np.nan, "away_pts": np.nan,
                          "t": [_t(cur, d, season_start) for d in up["date"]], "completed": False,
                          "event_id": up["event_id"], "start": up["start"], "neutral": up["neutral"]})
        g = pd.concat([g, u], ignore_index=True)
    g = g.sort_values(["date", "game_id"]).reset_index(drop=True)
    g["margin"] = g["home_pts"] - g["away_pts"]
    g["total"] = g["home_pts"] + g["away_pts"]

    log("NBA: fitting opponent-adjusted ratings ...")
    key = f"nba_ratings_{len(tg)}_{len(g)}_{int(g['t'].max())}"
    ratings = cached(key, 12, lambda: opponent_adjusted(tg, STATS, g["t"].unique(), config.NBA_HALFLIFE, config.NBA_RIDGE))
    g = attach_matchup(g, ratings, STATS)

    # ---- schedule context: rest, back-to-back, travel, time zones, altitude
    sched = pd.concat([g[["game_id", "date", "home", "home"]].set_axis(["game_id", "date", "team", "site"], axis=1),
                       g[["game_id", "date", "away", "home"]].set_axis(["game_id", "date", "team", "site"], axis=1)])
    sched = sched.sort_values(["team", "date"])
    sched["prev_date"] = sched.groupby("team")["date"].shift(1)
    sched["prev_site"] = sched.groupby("team")["site"].shift(1)
    sched["rest"] = [min((d - pd_).days - 1, 3) if isinstance(pd_, dt.date) else 3
                     for d, pd_ in zip(sched["date"], sched["prev_date"])]
    sched["travel"] = [haversine_km(NBA_SITES.get(a), NBA_SITES.get(b)) / 1000 if isinstance(a, str) else 0.0
                       for a, b in zip(sched["prev_site"], sched["site"])]
    sched["tz"] = [abs(NBA_SITES[a][2] - NBA_SITES[b][2]) if isinstance(a, str) and a in NBA_SITES and b in NBA_SITES
                   else 0.0 for a, b in zip(sched["prev_site"], sched["site"])]
    sc = sched.set_index(["game_id", "team"])
    for side in ("home", "away"):
        v = sc.reindex(list(zip(g["game_id"], g[side])))
        g[f"rest_{side[0]}"] = v["rest"].fillna(3).to_numpy()
        g[f"travel_{side[0]}"] = v["travel"].fillna(0).to_numpy()
        g[f"tz_{side[0]}"] = v["tz"].fillna(0).to_numpy()
    g["b2b_h"] = (g["rest_h"] == 0).astype(float)
    g["b2b_a"] = (g["rest_a"] == 0).astype(float)
    g["rest_diff"] = g["rest_h"] - g["rest_a"]
    g["rest_sum"] = g["rest_h"] + g["rest_a"]
    g["b2b_diff"] = g["b2b_h"] - g["b2b_a"]
    g["b2b_sum"] = g["b2b_h"] + g["b2b_a"]
    g["travel_diff"] = g["travel_h"] - g["travel_a"]
    g["tz_diff"] = g["tz_h"] - g["tz_a"]
    g["altitude"] = [NBA_SITES.get(h_, (0, 0, 0, 0))[3] / 1000 * (NBA_SITES.get(a_, (0, 0, 0, 0))[3] < 1000)
                     for h_, a_ in zip(g["home"], g["away"])]
    g["home_field"] = (~g["neutral"].fillna(False).astype(bool)).astype(float)

    # ---- missing players
    log("NBA: missing-player impact ...")
    miss, latest, repl = cached(f"nba_missing_{len(pl)}", 12, lambda: missing_players(tg, pl))
    mi = miss.set_index(["game_id", "team"])
    for side in ("home", "away"):
        v = mi.reindex(list(zip(g["game_id"], g[side])))
        g[f"miss_min_{side[0]}"] = v["miss_min"].fillna(0).to_numpy()
        g[f"miss_val_{side[0]}"] = v["miss_val"].fillna(0).to_numpy()
    espn_inj = pd.DataFrame(columns=["team", "key", "status", "position", "name"])
    if upcoming is not None:
        espn_inj = sources.espn_injuries("nba")
        for i in g.index[~g["completed"]]:
            for side in ("home", "away"):
                team = g.at[i, side]
                lt = latest.get(team)
                if lt is None or espn_inj.empty:
                    continue
                outk = set(espn_inj[(espn_inj["team"] == team) & espn_inj["status"].isin(["Out", "Doubtful"])]["key"])
                hit = lt[lt["key"].isin(outk)]
                g.at[i, f"miss_min_{side[0]}"] = float(hit["exp_min"].sum())
                g.at[i, f"miss_val_{side[0]}"] = float(hit["val"].sum())
    g["miss_val_diff"] = g["miss_val_h"] - g["miss_val_a"]
    g["miss_val_sum"] = g["miss_val_h"] + g["miss_val_a"]
    g["miss_min_diff"] = g["miss_min_h"] - g["miss_min_a"]
    g["miss_min_sum"] = g["miss_min_h"] + g["miss_min_a"]

    # ---- market lines (closing lines for history, live lines for the slate)
    log("NBA: closing lines (ESPN) ...")
    first = cur - config.NBA_ODDS_SEASONS
    dates = sorted(set(d for d, s, c in zip(g["date"], g["season"], g["completed"]) if c and s >= first))
    lines = sources.nba_closing_lines(dates)
    g["mkt_margin"] = np.nan
    g["mkt_total"] = np.nan
    g["ml_home"] = np.nan
    g["ml_away"] = np.nan
    if not lines.empty:
        li = lines.drop_duplicates(["date", "home", "away"]).set_index(["date", "home", "away"])
        v = li.reindex(list(zip(g["date"], g["home"], g["away"])))
        g["mkt_margin"] = -v["spread_home"].to_numpy()
        g["mkt_total"] = v["total"].to_numpy()
        g["ml_home"] = v["ml_home"].to_numpy()
        g["ml_away"] = v["ml_away"].to_numpy()
    if upcoming is not None and not upcoming.empty:
        uu = upcoming.set_index("event_id")
        for i in g.index[~g["completed"]]:
            u = uu.loc[g.at[i, "event_id"]]
            g.at[i, "mkt_margin"] = -u["spread_home"] if not np.isnan(u["spread_home"]) else np.nan
            g.at[i, "mkt_total"] = u["total"]

    feats = sorted(set(MARGIN_FEATS + TOTAL_FEATS))
    g = g[~g[["diff_points", "sum_points", "mu_points"]].isna().any(axis=1)].reset_index(drop=True)
    g[feats] = g[feats].astype(float).fillna(0.0)
    ctx = {"tg": tg, "pl": pl, "ratings": ratings, "latest": latest, "espn_inj": espn_inj, "repl": repl,
           "season_start": season_start}
    return g, ctx


def fit_model(g: pd.DataFrame) -> GameModel:
    return GameModel("nba", MARGIN_FEATS, TOTAL_FEATS, LABELS).fit(g)
