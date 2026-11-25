"""Results tracker.

Every `slate` run logs each priced pick (and each listed parlay) to history/. `grade` settles picks whose
games are final, using official results (nflverse scores + box scores, stats.nba.com game logs), and
records closing-line value (did the price you were shown beat the line the game closed at?).

The graded history answers the questions a backtest cannot for props: are the probabilities honest, does
the tool beat the no-vig market, and how much should the prop simulator be trusted. Once enough props are
graded, that last answer replaces the default 50/50 prop blend automatically.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
from rich.table import Table

from . import config, sources
from .oddsmath import devig, implied
from .util import cached, norm_name

GAME_MARKETS = ("Moneyline", "Spread", "Total")
PICK_COLS = ["key", "logged_at", "sport", "game_id", "date", "start", "game", "home", "away", "market",
             "market_key", "side", "player_key", "subject", "selection", "line", "odds", "dec", "p_win", "p_push",
             "p_cond", "p_model", "p_fair", "edge", "ev", "kelly", "proj", "flag", "book", "in_value", "in_likely",
             "status", "actual", "pnl", "close_line", "close_p", "clv", "graded_at"]
PARLAY_COLS = ["key", "logged_at", "mode", "date", "legs", "legs_json", "n", "odds", "dec", "p_hit", "fair_odds",
               "ev", "lift", "status", "pnl", "graded_at"]
LEG_FIELDS = ["key", "sport", "game_id", "date", "home", "away", "market_key", "side", "player_key", "line", "dec"]
EXPIRE_DAYS = 10  # an open pick with no official result after this long is voided
# p_fair and book are kept so that later line movement can be measured against the number the bet was made at
BET_LEG_EXTRA = ["selection", "subject", "market", "odds", "p_cond", "p_push", "proj", "start", "p_fair", "book"]

NFL_STAT = {"pass_yds": ["passing_yards"], "pass_tds": ["passing_tds"], "pass_cmp": ["completions"],
            "pass_att": ["attempts"], "rush_yds": ["rushing_yards"], "rush_att": ["carries"],
            "rec_yds": ["receiving_yards"], "receptions": ["receptions"],
            "rush_rec_yds": ["rushing_yards", "receiving_yards"],
            "anytime_td": ["rushing_tds", "receiving_tds", "special_teams_tds"]}
NBA_STAT = {"pts": ["PTS"], "reb": ["REB"], "ast": ["AST"], "fg3m": ["FG3M"], "stl": ["STL"], "blk": ["BLK"],
            "pra": ["PTS", "REB", "AST"], "pr": ["PTS", "REB"], "pa": ["PTS", "AST"], "ra": ["REB", "AST"]}


# ============================================================================= storage
def history_dir() -> Path:
    d = Path(os.getenv("SPORTSEDGE_HISTORY") or (config.ROOT / "history"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(name: str, cols: list[str]) -> pd.DataFrame:
    p = history_dir() / name
    if not p.exists():
        return pd.DataFrame(columns=cols)
    df = pd.read_csv(p, dtype={"key": str, "id": str, "game_id": str, "date": str, "player_key": str, "side": str,
                               "market_key": str, "status": str, "flag": str, "book": str})
    for c in cols:
        if c not in df:
            df[c] = np.nan
    for c in ("player_key", "flag", "book", "side", "market_key"):
        if c in df:
            df[c] = df[c].fillna("")
    for c in ("in_value", "in_likely"):
        if c in df:
            df[c] = df[c].astype(str).str.lower() == "true"
    # fixed dtypes so settled values can be written into columns that start out empty
    for c in ("status", "graded_at", "logged_at", "start", "legs_json", "legs", "id", "placed_at", "note"):
        if c in df:
            df[c] = df[c].astype(object)
    for c in ("actual", "pnl", "close_line", "close_p", "clv", "line", "dec", "p_fair", "p_model", "p_push",
              "stake", "odds"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return df[cols]


def _write(df: pd.DataFrame, name: str):
    df.to_csv(history_dir() / name, index=False)


def load_picks() -> pd.DataFrame:
    return _read("picks.csv", PICK_COLS)


def load_parlays() -> pd.DataFrame:
    return _read("parlays.csv", PARLAY_COLS)


def _merge_latest(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Keep the latest pre-game snapshot of each bet; never touch a bet that is already settled."""
    settled = set(old.loc[old["status"] != "open", "key"])
    new = new[~new["key"].isin(settled)]
    both = pd.concat([old, new], ignore_index=True) if not old.empty else new
    return both.drop_duplicates("key", keep="last").reset_index(drop=True)


# ============================================================================= logging
def log_slate(results: list[dict], parlays: dict) -> int:
    from .report import likely, value_bets

    legs = [l for r in results for l in r["legs"]]
    if not legs:
        return 0
    value_keys = {l["key"] for l in value_bets([l for l in legs if l["market"] in GAME_MARKETS])}
    value_keys |= {l["key"] for l in value_bets([l for l in legs if l["market"] not in GAME_MARKETS])}
    likely_keys = {l["key"] for l in likely([l for l in legs if l["market"] in GAME_MARKETS])}
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    rows = []
    for l in legs:
        r = {c: l.get(c) for c in PICK_COLS if c in l}
        r.update({"logged_at": now, "start": str(l.get("start")), "in_value": l["key"] in value_keys,
                  "in_likely": l["key"] in likely_keys, "status": "open", "book": l.get("book") or ""})
        rows.append(r)
    new = pd.DataFrame(rows).reindex(columns=PICK_COLS)
    _write(_merge_latest(load_picks(), new), "picks.csv")

    by_key = {l["key"]: l for l in legs}
    prow = []
    for mode, df in (parlays or {}).items():
        if df is None or df.empty:
            continue
        for r in df.itertuples():
            legs_info = [{f: _jsonable(by_key[k].get(f)) for f in LEG_FIELDS} for k in r.leg_keys]
            prow.append({"key": mode + "#" + "+".join(sorted(r.leg_keys)), "logged_at": now, "mode": mode,
                         "date": min(x["date"] for x in legs_info), "legs": " + ".join(r.legs),
                         "legs_json": json.dumps(legs_info), "n": r.n, "odds": r.odds, "dec": r.dec,
                         "p_hit": r.p_hit, "fair_odds": r.fair_odds, "ev": r.ev, "lift": r.lift, "status": "open"})
    if prow:
        _write(_merge_latest(load_parlays(), pd.DataFrame(prow).reindex(columns=PARLAY_COLS)), "parlays.csv")
    return len(rows)


def _jsonable(v):
    """One field of a pick in a form that can be written as JSON. NFL kick-off times arrive as timestamps,
    NBA ones as text: both are stored as ISO text."""
    if v is None or v is pd.NaT:
        return None
    if isinstance(v, (pd.Timestamp, dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.floating, float)):
        return None if np.isnan(v) else float(v)
    if isinstance(v, np.integer):
        return int(v)
    return v


# ============================================================================= settlement (pure functions)
def _status(x: float) -> str:
    return "won" if x > 0 else ("lost" if x < 0 else "push")


def settle_game(market_key: str, side: str, line, margin: float, total: float) -> tuple[str, float]:
    """margin/total are final (home minus away, combined). line is from the bet side's perspective."""
    if market_key == "ml":
        m = margin if side == "home" else -margin
        return _status(m), margin
    if market_key == "spread":
        m = margin if side == "home" else -margin
        return _status(m + float(line)), margin
    if market_key == "total":
        d = total - float(line)
        return _status(d if side == "over" else -d), total
    raise ValueError(market_key)


def settle_prop(side: str, line, value: float) -> str:
    d = float(value) - float(line)
    return _status(d if side in ("over", "yes") else -d)


def pnl(status: str, dec: float) -> float:
    return float(dec) - 1 if status == "won" else (-1.0 if status == "lost" else 0.0)


# ============================================================================= official results
class Resolver:
    """Looks up final scores, player stat lines and closing lines for the picks being graded."""

    def __init__(self, legs: pd.DataFrame):
        self.nfl_sched = self.nfl_ps = None
        self.nba_team = self.nba_player = None
        self.nba_close = None
        self._prop_close: dict = {}  # (sport, date) -> closing prop lines for that day's finished games
        sports = set(legs["sport"].str.upper())
        if "NFL" in sports:
            self._load_nfl(legs[legs["sport"].str.upper() == "NFL"])
        if "NBA" in sports:
            self._load_nba(legs[legs["sport"].str.upper() == "NBA"])

    # ---------------------------------------------------------------- loading
    def _load_nfl(self, legs):
        import nflreadpy as nfl

        seasons = sorted({int(str(g).split("_")[0]) for g in legs["game_id"]})
        key = "_".join(map(str, seasons))
        self.nfl_sched = cached(f"grade_nfl_sched_{key}", 1, lambda: nfl.load_schedules(seasons).to_pandas())
        ps = cached(f"grade_nfl_ps_{key}", 1, lambda: nfl.load_player_stats(seasons).to_pandas())
        ps = ps.copy()
        ps["pkey"] = ps["player_display_name"].map(norm_name)
        self.nfl_ps = ps

    def _load_nba(self, legs):
        from . import nba

        dates = pd.to_datetime(legs["date"]).dt.date
        years = sorted({nba.current_season_year(d) for d in dates})
        tl, pl = [], []
        for y in years:
            for stype in ("Regular Season", "Playoffs", "PlayIn"):
                if stype != "Regular Season" and not any(d.month in (4, 5, 6) for d in dates):
                    continue
                for kind, store in (("T", tl), ("P", pl)):
                    try:
                        df = cached(f"grade_nba_{kind}_{y}_{stype[:3]}", 1,
                                    lambda: nba._gamelog(nba.season_str(y), kind, stype))
                        store.append(df)
                    except RuntimeError:
                        pass
        self.nba_team = pd.concat(tl, ignore_index=True) if tl else pd.DataFrame()
        self.nba_player = pd.concat(pl, ignore_index=True) if pl else pd.DataFrame()
        if not self.nba_player.empty:
            self.nba_player["pkey"] = self.nba_player["PLAYER_NAME"].map(norm_name)
        past = sorted({d for d in dates if d <= dt.date.today()})
        try:
            self.nba_close = sources.nba_closing_lines(past) if past else pd.DataFrame()
        except Exception:
            self.nba_close = pd.DataFrame()

    # ---------------------------------------------------------------- lookups
    def game(self, l) -> tuple[float, float] | None:
        if l["sport"].upper() == "NFL":
            if self.nfl_sched is None:  # that sport's results were not needed/loaded in this pass
                return None
            s = self.nfl_sched
            row = s[s["game_id"] == l["game_id"]]
            if row.empty or pd.isna(row["home_score"].iloc[0]):
                return None
            r = row.iloc[0]
            return float(r.home_score - r.away_score), float(r.home_score + r.away_score)
        t = self.nba_team
        if t is None or t.empty:
            return None
        day = t[t["GAME_DATE"].astype(str).str[:10] == l["date"]]
        h = day[(day["TEAM_ABBREVIATION"] == l["home"]) & day["MATCHUP"].str.contains(l["away"])]
        a = day[(day["TEAM_ABBREVIATION"] == l["away"]) & day["MATCHUP"].str.contains(l["home"])]
        if h.empty or a.empty:
            return None
        hp, ap = float(h["PTS"].iloc[0]), float(a["PTS"].iloc[0])
        return hp - ap, hp + ap

    def player_value(self, l):
        """Stat value, 'void' (player did not play) or None (box score not published yet)."""
        if l["sport"].upper() == "NFL":
            if self.nfl_ps is None:
                return None
            g = self.nfl_ps[self.nfl_ps["game_id"] == l["game_id"]]
            cols = NFL_STAT.get(l["market_key"])
        else:
            p = self.nba_player
            if p is None or p.empty:
                return None
            g = p[(p["GAME_DATE"].astype(str).str[:10] == l["date"]) & p["TEAM_ABBREVIATION"].isin([l["home"], l["away"]])]
            cols = NBA_STAT.get(l["market_key"])
        if g.empty or cols is None:
            return None
        row = g[g["pkey"] == l["player_key"]]
        if row.empty:
            return "void"
        return float(sum(float(row[c].fillna(0).iloc[0]) if c in row else 0.0 for c in cols))

    def closing(self, l) -> dict | None:
        if l["sport"].upper() == "NFL":
            if self.nfl_sched is None:
                return None
            row = self.nfl_sched[self.nfl_sched["game_id"] == l["game_id"]]
            if row.empty:
                return None
            r = row.iloc[0]
            return {"spread_home": -r.spread_line if pd.notna(r.spread_line) else np.nan, "total": r.total_line,
                    "ml_home": r.home_moneyline, "ml_away": r.away_moneyline}
        c = self.nba_close
        if c is None or c.empty:
            return None
        row = c[(c["date"].astype(str) == l["date"]) & (c["home"] == l["home"]) & (c["away"] == l["away"])]
        if row.empty:
            return None
        r = row.iloc[0]
        return {"spread_home": r.spread_home, "total": r.total, "ml_home": r.ml_home, "ml_away": r.ml_away}

    def prop_closing(self, l) -> dict | None:
        """The sportsbook's last pre-game line and prices for this player and stat, if it posted one."""
        k = (str(l["sport"]).lower(), str(l["date"]))
        if k not in self._prop_close:
            try:
                self._prop_close[k] = sources.espn_props_history(k[0], [dt.date.fromisoformat(k[1])])
            except Exception:  # no closing lines is not a reason to leave a pick ungraded
                self._prop_close[k] = pd.DataFrame()
        c = self._prop_close[k]
        if c is None or c.empty:
            return None
        row = c[(c["key"] == l["player_key"]) & (c["market"] == l["market_key"])
                & (c["home"] == l["home"]) & (c["away"] == l["away"])]
        if row.empty:
            return None
        r = row.iloc[0]
        return {"line": r["line"], "over_odds": r["over_odds"], "under_odds": r["under_odds"]}

    # ---------------------------------------------------------------- settle one leg
    def settle(self, l) -> dict | None:
        mk = l["market_key"]
        if mk in ("ml", "spread", "total"):
            res = self.game(l)
            if res is None:
                return None
            status, actual = settle_game(mk, l["side"], l["line"], *res)
            out = {"status": status, "actual": actual}
            out.update(_clv(l, self.closing(l)))
            return out
        v = self.player_value(l)
        if v is None:
            return None
        if v == "void":
            return {"status": "void", "actual": np.nan}
        out = {"status": settle_prop(l["side"], l["line"], v), "actual": v}
        out.update(_prop_clv(l, self.prop_closing(l)))
        return out


def _clv(l, close: dict | None) -> dict:
    """Closing-line value from the bet side's perspective: + means the bet beat the closing line.
    Spreads and totals in points; moneylines in no-vig probability (percentage points / 100)."""
    if not close:
        return {}
    mk, side = l["market_key"], l["side"]
    try:
        if mk == "spread" and pd.notna(close["spread_home"]):
            cl = close["spread_home"] if side == "home" else -close["spread_home"]
            return {"close_line": cl, "clv": float(l["line"]) - cl}
        if mk == "total" and pd.notna(close["total"]):
            d = float(close["total"]) - float(l["line"])
            return {"close_line": close["total"], "clv": d if side == "over" else -d}
        if mk == "ml" and pd.notna(close["ml_home"]) and pd.notna(close["ml_away"]):
            ph, pa = devig([implied(close["ml_home"]), implied(close["ml_away"])])
            cp = float(ph if side == "home" else pa)
            pf = l.get("p_fair")
            return {"close_p": cp, "clv": cp - float(pf)} if pf is not None and pd.notna(pf) else {"close_p": cp}
    except (TypeError, ValueError):
        pass
    return {}


def _prop_clv(l, close: dict | None) -> dict:
    """Closing-line value for a prop, from the bet side's perspective: + means the bet beat the closing line.

    If the sportsbook's closing line differs from the pick's line, the value is that move in the stat's own
    units, signed so that a higher closing line is good for an over and a lower one is good for an under.
    If the line is the same, it is the change in the no-vig chance of the pick's side (0.02 = 2 points).
    The sign always means the same thing, so 'how often did picks beat the close' can be counted across both."""
    if not close:
        return {}
    try:
        line, cl, side = float(l["line"]), float(close["line"]), l["side"]
    except (TypeError, ValueError):
        return {}
    if np.isnan(cl):
        return {}
    out = {"close_line": cl}
    o, u = close.get("over_odds"), close.get("under_odds")
    has_o, has_u = o is not None and pd.notna(o), u is not None and pd.notna(u)
    cp = np.nan
    if has_o and has_u:
        po, pu = devig([implied(o), implied(u)])
        cp = float(pu if side == "under" else po)
    elif has_o and side != "under":
        cp = float(implied(o))  # one-sided market (anytime TD): the vigged price, as when it was picked
    if not np.isnan(cp):
        out["close_p"] = cp
    if side != "yes" and not np.isnan(line) and abs(cl - line) > 1e-9:
        out["clv"] = (cl - line) * (-1.0 if side == "under" else 1.0)
    else:
        pf = l.get("p_fair")
        if not np.isnan(cp) and pf is not None and pd.notna(pf):
            out["clv"] = cp - float(pf)
    return out


def backfill_prop_clv(picks: pd.DataFrame, res: "Resolver | None" = None) -> int:
    """Add closing-line value to props that were settled before their closing line was available.
    Changes `picks` in place and returns how many rows gained one."""
    todo = picks[(~picks["market"].isin(GAME_MARKETS)) & picks["status"].isin(["won", "lost", "push"])
                 & picks["close_line"].isna()]
    cutoff = (dt.date.today() - dt.timedelta(days=EXPIRE_DAYS)).isoformat()
    todo = todo[todo["date"].astype(str) >= cutoff]
    if todo.empty:
        return 0
    res = res or Resolver(pd.DataFrame(columns=LEG_FIELDS).assign(sport=pd.Series(dtype=str)))
    n = 0
    for i in todo.index:
        out = _prop_clv(picks.loc[i], res.prop_closing(picks.loc[i]))
        for k, v in out.items():
            picks.at[i, k] = v
        n += bool(out)
    return n


# ============================================================================= grading
def _due(df: pd.DataFrame) -> pd.Series:
    start = pd.to_datetime(df["start"], utc=True, errors="coerce")
    now = pd.Timestamp.now(tz="UTC")
    return (df["status"] == "open") & ((start.isna()) | (start < now - pd.Timedelta(hours=3)))


def _settle_combo(res: "Resolver", legs: list[dict]):
    """Settle a parlay / placed bet. Returns None while any leg is pending, else (status, factor) where the
    payout is stake x total decimal x factor (a pushed or void leg drops out of the parlay, as at a book)."""
    stats, factor = [], 1.0
    for leg in legs:
        out = res.settle(pd.Series(leg))
        if out is None:
            return None
        stats.append(out["status"])
        if out["status"] == "lost":
            factor = 0.0
        elif out["status"] in ("push", "void"):
            factor /= float(leg["dec"])
    if "lost" in stats:
        return "lost", 0.0
    if all(s in ("push", "void") for s in stats):
        return "push", factor
    return "won", factor


def grade(verbose: bool = True) -> dict:
    picks, parlays, bets = load_picks(), load_parlays(), load_bets()
    due = picks[_due(picks)]
    today_s = dt.date.today().isoformat()
    open_parlays = parlays[(parlays["status"] == "open") & (parlays["date"].astype(str) <= today_s)]
    open_bets = bets[(bets["status"] == "open") & (bets["date"].astype(str) <= today_s)]
    # legs inside parlays / placed bets still need results even if the pick row was replaced
    extra = []
    for js in list(open_parlays["legs_json"]) + list(open_bets["legs_json"]):
        extra += [leg for leg in json.loads(js) if str(leg.get("date")) <= today_s]
    legs_needed = pd.concat([due[LEG_FIELDS], pd.DataFrame(extra, columns=LEG_FIELDS)], ignore_index=True)
    if legs_needed.empty:
        if verbose:
            print(f"Nothing to grade yet ({int((picks['status'] == 'open').sum())} picks waiting for their games).")
        return {"settled": 0, "parlays": 0, "bets": 0}
    if verbose:
        print(f"Grading {len(due)} picks, {len(open_parlays)} parlays and {len(open_bets)} placed bets ...")
    res = Resolver(legs_needed)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    today = dt.date.today()
    n = 0
    for i in due.index:
        l = picks.loc[i]
        out = res.settle(l)
        if out is None:
            if (today - pd.Timestamp(l["date"]).date()).days > EXPIRE_DAYS:
                out = {"status": "void", "actual": np.nan}
            else:
                continue
        for k, v in out.items():
            picks.at[i, k] = v
        picks.at[i, "pnl"] = pnl(out["status"], l["dec"])
        picks.at[i, "graded_at"] = now
        n += 1
    try:
        backfill_prop_clv(picks, res)
    except Exception:
        pass
    _write(picks, "picks.csv")

    def settle_table(df, idx, unit_pnl):
        done = 0
        for i in idx:
            out = _settle_combo(res, json.loads(df.at[i, "legs_json"]))
            if out is None:
                if (today - pd.Timestamp(df.at[i, "date"]).date()).days > EXPIRE_DAYS:
                    df.at[i, "status"], df.at[i, "pnl"] = "void", 0.0
                continue
            st, factor = out
            df.at[i, "status"] = st
            df.at[i, "pnl"] = unit_pnl(df.loc[i], factor)
            df.at[i, "graded_at"] = now
            done += 1
        return done

    # listed parlays: 1 unit at standard parlay odds (SGP: no book price, hit rate only)
    m = settle_table(parlays, open_parlays.index,
                     lambda r, f: np.nan if r["mode"] == "sgp" else float(r["dec"]) * f - 1)
    _write(parlays, "parlays.csv")
    # bets you placed: your stake at the odds you entered
    b = settle_table(bets, open_bets.index, lambda r, f: float(r["stake"]) * (float(r["dec"]) * f - 1))
    _write(bets, "bets.csv")
    _PROP_W.clear()
    if verbose:
        print(f"Settled {n} picks, {m} parlays and {b} placed bets.")
    return {"settled": n, "parlays": m, "bets": b}


# ============================================================================= bets you actually placed
BET_COLS = ["id", "placed_at", "date", "legs", "legs_json", "n", "odds", "dec", "stake", "p_hit", "p_win_all",
            "ev", "status", "pnl", "graded_at", "note"]


def load_bets() -> pd.DataFrame:
    return _read("bets.csv", BET_COLS)


def place_bet(legs: list[dict], odds: float, stake: float, p_hit: float, p_win_all: float, ev: float,
              note: str = "") -> dict:
    import uuid

    from .oddsmath import american_to_decimal

    # what settling needs, plus what live tracking needs (the pick's name, its pre-game chance and projection)
    info = [{f: _jsonable(l.get(f)) for f in LEG_FIELDS + BET_LEG_EXTRA} for l in legs]
    row = {"id": uuid.uuid4().hex[:10], "placed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "date": max(x["date"] for x in info), "legs": " + ".join(l["selection"] for l in legs),
           "legs_json": json.dumps(info), "n": len(legs), "odds": float(odds), "dec": american_to_decimal(odds),
           "stake": float(stake), "p_hit": p_hit, "p_win_all": p_win_all, "ev": ev, "status": "open", "note": note}
    bets = pd.concat([load_bets(), pd.DataFrame([row])], ignore_index=True)
    _write(bets.reindex(columns=BET_COLS), "bets.csv")
    return row


def delete_bet(bet_id: str) -> bool:
    bets = load_bets()
    keep = bets[bets["id"].astype(str) != str(bet_id)]
    _write(keep, "bets.csv")
    return len(keep) < len(bets)


# ============================================================================= learned prop weight
_PROP_W: dict = {}


def _fit_prop_weight(df: pd.DataFrame) -> tuple[float, int]:
    d = df[(df["market"].isin(GAME_MARKETS) == False) & (df["market_key"] != "anytime_td")  # noqa: E712
           & df["status"].isin(["won", "lost"]) & df["p_fair"].notna() & df["p_model"].notna()]
    if d.empty:
        return config.PROP_MODEL_WEIGHT, 0
    y = (d["status"] == "won").to_numpy(float)
    pm = np.clip(d["p_model"] / (1 - d["p_push"].fillna(0)).clip(lower=1e-6), 1e-4, 1 - 1e-4).to_numpy()
    pf = np.clip(d["p_fair"].to_numpy(float), 1e-4, 1 - 1e-4)
    grid = np.linspace(0, 1, 21)
    ll = [-np.mean(y * np.log(w * pm + (1 - w) * pf) + (1 - y) * np.log(1 - (w * pm + (1 - w) * pf))) for w in grid]
    return float(grid[int(np.argmin(ll))]), len(d)


def prop_prior(sport: str | None = None) -> tuple[float, str]:
    """Starting weight for the prop simulator and where it comes from.

    'tested'   - learned on real closing prop lines for this sport (propcheck.py)
    'borrowed' - this sport has no free prop prices to test on, so the other sport's tested weight is used
    'default'  - nothing has been tested yet (config.PROP_MODEL_WEIGHT)"""
    from . import propcheck

    w = propcheck.learned_weight(sport) if sport else None
    if w is not None:
        return w, "tested"
    others = [x for x in (propcheck.learned_weight(s) for s in ("nba", "nfl")) if x is not None]
    if others:
        return float(min(others)), "borrowed"
    return config.PROP_MODEL_WEIGHT, "default"


def prop_weight(sport: str | None = None) -> float:
    """Simulator weight for props: the tested starting weight (see prop_prior), then, once TRACK_MIN_PROPS
    of your own props are graded, the log-loss-optimal weight on those, shrunk toward the starting weight
    by TRACK_PRIOR_PROPS pseudo-observations."""
    k = (sport or "all").upper()
    if k in _PROP_W:
        return _PROP_W[k]
    try:
        df = load_picks()
    except Exception:
        df = pd.DataFrame(columns=PICK_COLS)
    start, _ = prop_prior(sport)
    w, n = start, 0
    sub = df[df["sport"].str.upper() == k] if sport else df
    for cand in (sub, df):
        w_fit, n = _fit_prop_weight(cand)
        if n >= config.TRACK_MIN_PROPS:
            prior = config.TRACK_PRIOR_PROPS
            w = (n * w_fit + prior * start) / (n + prior)
            break
    _PROP_W[k] = float(w)
    return _PROP_W[k]


def reset_prop_weight():
    """Forget the cached weights (call after the prop test or the graded history changes)."""
    _PROP_W.clear()


# ============================================================================= reporting
def _record(d: pd.DataFrame) -> dict:
    w, l, p = (d["status"] == "won").sum(), (d["status"] == "lost").sum(), (d["status"] == "push").sum()
    bets = d[d["status"].isin(["won", "lost", "push"])]
    units = float(bets["pnl"].sum())
    roi = units / len(bets) if len(bets) else np.nan
    se = float(bets["pnl"].std(ddof=1) / np.sqrt(len(bets))) if len(bets) > 1 else np.nan
    wl = d[d["status"].isin(["won", "lost"])]
    clv = d["clv"].dropna()
    return {"n": len(bets), "w": int(w), "l": int(l), "p": int(p), "units": units, "roi": roi, "se": se,
            "win": (w / (w + l)) if (w + l) else np.nan, "model": wl["p_cond"].mean() if len(wl) else np.nan,
            "market": wl["p_fair"].mean() if len(wl) else np.nan,
            "clv": (int((clv > 1e-9).sum()), int((clv.abs() <= 1e-9).sum()), int((clv < -1e-9).sum()))}


def summary_line() -> str:
    df = load_picks()
    if df.empty:
        return ""
    s = df[df["status"].isin(["won", "lost", "push"])]
    open_n = int((df["status"] == "open").sum())
    if s.empty:
        return f"Track record: no settled picks yet ({open_n} open)."
    v = _record(s[s["in_value"] == True])  # noqa: E712
    pr = _record(s[~s["market"].isin(GAME_MARKETS)])
    parts = []
    if v["n"]:
        parts.append(f"value bets {v['w']}-{v['l']}-{v['p']}, {v['roi']:+.1%} ROI over {v['n']}")
    if pr["n"]:
        parts.append(f"props won {pr['win']:.1%} vs model {pr['model']:.1%} expected ({pr['n']})")
    body = "; ".join(parts) if parts else f"{len(s)} picks settled"
    return f"Track record: {body}; {open_n} open. Run `.\\sportsedge grade` for the full report."


def performance(days: int | None = None, sport: str | None = None):
    from .report import console

    df = load_picks()
    if sport:
        df = df[df["sport"].str.upper() == sport.upper()]
    if days:
        cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        df = df[df["date"] >= cutoff]
    s = df[df["status"].isin(["won", "lost", "push"])].copy()
    console.rule("[bold]Track record")
    console.print(f"{len(s):,} settled picks, {int((df['status'] == 'void').sum())} void "
                  f"(player out / no result), {int((df['status'] == 'open').sum())} still open. "
                  f"History: {history_dir()}")
    if s.empty:
        console.print("[dim]Nothing settled yet. Picks are logged every time you run `slate`; grade after the games.[/dim]")
        return
    s["in_value"] = s["in_value"].astype(str).str.lower() == "true"
    s["in_likely"] = s["in_likely"].astype(str).str.lower() == "true"

    t = Table(title="Results (1 unit flat stakes at the price shown when the pick was logged)", header_style="bold")
    for c in ("Group", "Picks", "W-L-P", "Win %", "Model said", "Market said", "Units", "ROI", "ROI 95% range",
              "Beat / same / worse than close"):
        t.add_column(c)
    for name, d in (("Value bets (as flagged)", s[s["in_value"]]), ("Most likely picks", s[s["in_likely"]]),
                    ("All game lines", s[s["market"].isin(GAME_MARKETS)]),
                    ("All props", s[~s["market"].isin(GAME_MARKETS)])):
        if d.empty:
            continue
        r = _record(d)
        rng = f"{r['roi'] - 1.96 * r['se']:+.1%} to {r['roi'] + 1.96 * r['se']:+.1%}" if not np.isnan(r["se"]) else "-"
        clv = "/".join(map(str, r["clv"])) if sum(r["clv"]) else "-"
        t.add_row(name, f"{r['n']:,}", f"{r['w']}-{r['l']}-{r['p']}", _p(r["win"]), _p(r["model"]), _p(r["market"]),
                  f"{r['units']:+.1f}", _p(r["roi"], signed=True), rng, clv)
    console.print(t)

    t = Table(title="By market", header_style="bold")
    for c in ("Market", "Picks", "W-L-P", "Win %", "Model said", "Units", "ROI", "Avg CLV"):
        t.add_column(c)
    for (sp, mk), d in s.groupby(["sport", "market"]):
        r = _record(d)
        clv = d["clv"].dropna()
        unit = "pp" if mk == "Moneyline" else " pts"
        if mk not in GAME_MARKETS:  # props mix line moves and price moves, so count how often the close was beaten
            avg = f"beat {int((clv > 1e-9).sum())} of {len(clv)}" if len(clv) else "-"
        else:
            avg = (f"{(100 if mk == 'Moneyline' else 1) * clv.mean():+.2f}{unit}" if len(clv) else "-")
        t.add_row(f"{sp} {mk}", f"{r['n']:,}", f"{r['w']}-{r['l']}-{r['p']}", _p(r["win"]), _p(r["model"]),
                  f"{r['units']:+.1f}", _p(r["roi"], signed=True), avg)
    console.print(t)

    wl = s[s["status"].isin(["won", "lost"])].copy()
    wl["y"] = (wl["status"] == "won").astype(float)
    wl["bin"] = pd.cut(wl["p_cond"], [0, .4, .5, .55, .6, .7, 1.0])
    c = wl.groupby("bin", observed=True).agg(n=("y", "size"), predicted=("p_cond", "mean"), actual=("y", "mean"))
    t = Table(title="Are the probabilities honest? (picks that won or lost)", header_style="bold")
    for col in ("Model probability", "Picks", "Predicted win %", "Actual win %"):
        t.add_column(col)
    for b, r in c.iterrows():
        t.add_row(str(b), f"{int(r.n):,}", f"{r.predicted:.1%}", f"{r.actual:.1%}")
    console.print(t)

    t = Table(title="Who predicted better? (log loss, lower is better)", header_style="bold")
    for col in ("Picks", "n", "Tool", "No-vig market", "Raw simulator"):
        t.add_column(col)
    for name, d in (("Game lines", wl[wl["market"].isin(GAME_MARKETS)]),
                    ("Props", wl[~wl["market"].isin(GAME_MARKETS) & (wl["market_key"] != "anytime_td")])):
        d = d.dropna(subset=["p_fair"])
        if d.empty:
            continue
        raw = d["p_model"] / (1 - d["p_push"].fillna(0)).clip(lower=1e-6)
        t.add_row(name, f"{len(d):,}", _ll(d["y"], d["p_cond"]), _ll(d["y"], d["p_fair"]),
                  _ll(d["y"], raw) if name == "Props" else "-")
    console.print(t)

    w_fit, n = _fit_prop_weight(df)
    _PROP_W.clear()
    used = prop_weight(sport)
    start, source = prop_prior(sport)
    if n >= config.TRACK_MIN_PROPS:
        console.print(f"Prop simulator weight: best fit on {n:,} graded props is {w_fit:.0%}; after shrinking toward "
                      f"the starting weight the tool now uses [bold]{used:.0%}[/bold] (starting weight {start:.0%}, {source}).")
    else:
        console.print(f"Prop simulator weight: {n}/{config.TRACK_MIN_PROPS} graded props needed before it is learned; "
                      f"using the default {config.PROP_MODEL_WEIGHT:.0%}.")

    pl = load_parlays()
    if days:
        pl = pl[pl["date"] >= (dt.date.today() - dt.timedelta(days=days)).isoformat()]
    pl = pl[pl["status"].isin(["won", "lost", "push"])]
    if not pl.empty:
        t = Table(title="Parlays", header_style="bold")
        for col in ("List", "Settled", "Hit", "Hit %", "Model said", "Units", "ROI"):
            t.add_column(col)
        names = {"value": "Best value", "safe": "Highest hit-rate", "sgp": "Same-game combos (no book price)"}
        for mode, d in pl.groupby("mode"):
            hits = (d["status"] == "won").sum()
            priced = d["pnl"].notna().any()
            t.add_row(names.get(mode, mode), f"{len(d):,}", str(int(hits)), _p(hits / len(d)), _p(d["p_hit"].mean()),
                      f"{d['pnl'].sum():+.1f}" if priced else "-",
                      _p(d["pnl"].sum() / len(d), signed=True) if priced else "-")
        console.print(t)

    recent = s.sort_values("graded_at").tail(12)
    t = Table(title="Most recently settled", header_style="bold")
    for col in ("Date", "Pick", "Game", "Odds", "Model %", "Result", "Actual", "Units"):
        t.add_column(col)
    for r in recent.itertuples():
        t.add_row(str(r.date), r.selection, r.game, f"{int(r.odds):+d}", _p(r.p_cond), r.status,
                  "-" if pd.isna(r.actual) else f"{r.actual:g}", f"{r.pnl:+.2f}")
    console.print(t)
    console.print("[dim]Small samples swing a lot: a few hundred settled picks are needed before ROI means much. "
                  "Beating the closing line consistently is the earliest reliable sign of a real edge.[/dim]")


def _p(x, signed=False):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "-"
    return f"{x:+.1%}" if signed else f"{x:.1%}"


def _ll(y, p) -> str:
    p = np.clip(np.asarray(p, float), 1e-4, 1 - 1e-4)
    y = np.asarray(y, float)
    return f"{-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)):.4f}"


# ============================================================================= data for the web UI
def performance_data(days: int | None = None, sport: str | None = None) -> dict:
    """Same numbers as `performance`, as plain data for the UI."""
    df = load_picks()
    bets, pl = load_bets(), load_parlays()
    if sport:
        df = df[df["sport"].str.upper() == sport.upper()]
    if days:
        cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        df = df[df["date"] >= cutoff]
        bets = bets[bets["date"].astype(str) >= cutoff]
        pl = pl[pl["date"].astype(str) >= cutoff]
    s = df[df["status"].isin(["won", "lost", "push"])].copy()
    out = {"settled": len(s), "void": int((df["status"] == "void").sum()), "open": int((df["status"] == "open").sum()),
           "groups": [], "by_market": [], "calibration": [], "scores": [], "series": [], "recent": [],
           "history_dir": str(history_dir())}
    game = s["market"].isin(GAME_MARKETS)
    groups = [("Value bets", s[s["in_value"]]), ("Most likely picks", s[s["in_likely"]]),
              ("All game lines", s[game]), ("All props", s[~game])]
    for name, d in groups:
        if d.empty:
            continue
        r = _record(d)
        lo = r["roi"] - 1.96 * r["se"] if not np.isnan(r["se"]) else None
        hi = r["roi"] + 1.96 * r["se"] if not np.isnan(r["se"]) else None
        out["groups"].append({"name": name, **{k: v for k, v in r.items() if k != "clv"}, "roi_lo": lo, "roi_hi": hi,
                              "clv_beat": r["clv"][0], "clv_same": r["clv"][1], "clv_worse": r["clv"][2]})
    for (sp, mk), d in s.groupby(["sport", "market"]):
        r = _record(d)
        clv = d["clv"].dropna()
        out["by_market"].append({**{k: v for k, v in r.items() if k != "clv"}, "sport": sp, "label": mk,
                                 "clv_avg": float(clv.mean()) if len(clv) else None,
                                 "clv_unit": "prob" if mk == "Moneyline" else "pts" if mk in GAME_MARKETS else "count",
                                 "clv_beat": int((clv > 1e-9).sum()), "clv_same": int((clv.abs() <= 1e-9).sum()),
                                 "clv_n": int(len(clv))})
    # props: did the line move toward the pick, or the price? (the two ways a prop beats the close)
    pc = s[~game & s["close_line"].notna() & s["clv"].notna()]
    moved = pc[(pc["close_line"] - pc["line"]).abs() > 1e-9]
    out["prop_clv"] = {"n": int(len(pc)), "beat": int((pc["clv"] > 1e-9).sum()), "same": int((pc["clv"].abs() <= 1e-9).sum()),
                       "line_moved": int(len(moved)), "line_moved_for": int((moved["clv"] > 0).sum()),
                       "no_close": int((s[~game]["close_line"].isna()).sum())}
    wl = s[s["status"].isin(["won", "lost"])].copy()
    wl["y"] = (wl["status"] == "won").astype(float)
    edges = [0, .4, .5, .55, .6, .7, 1.0]
    for lo, hi in zip(edges[:-1], edges[1:]):
        b = wl[(wl["p_cond"] > lo) & (wl["p_cond"] <= hi)]
        if len(b):
            out["calibration"].append({"lo": lo, "hi": hi, "n": len(b), "predicted": float(b["p_cond"].mean()),
                                       "actual": float(b["y"].mean())})
    for name, d in (("Game lines", wl[wl["market"].isin(GAME_MARKETS)]),
                    ("Props", wl[~wl["market"].isin(GAME_MARKETS) & (wl["market_key"] != "anytime_td")])):
        d = d.dropna(subset=["p_fair"])
        if d.empty:
            continue
        raw = d["p_model"] / (1 - d["p_push"].fillna(0)).clip(lower=1e-6)
        out["scores"].append({"name": name, "n": len(d), "tool": float(_ll(d["y"], d["p_cond"])),
                              "market": float(_ll(d["y"], d["p_fair"])),
                              "raw": float(_ll(d["y"], raw)) if name == "Props" else None})
    for name, d in (("Value bets", s[s["in_value"]]), ("Game lines", s[game]), ("Props", s[~game])):
        if d.empty:
            continue
        daily = d.groupby("date")["pnl"].sum().sort_index().cumsum()
        out["series"].append({"name": name, "points": [{"date": k, "units": float(v)} for k, v in daily.items()]})
    w_fit, n = _fit_prop_weight(df)
    _PROP_W.clear()
    start, source = prop_prior(sport)
    out["prop_weight"] = {"n": n, "needed": config.TRACK_MIN_PROPS, "w_fit": w_fit, "used": prop_weight(sport),
                          "default": start, "source": source}
    rec = s.sort_values(["graded_at", "date"]).tail(15).iloc[::-1]
    out["recent"] = [{"date": r.date, "selection": r.selection, "game": r.game, "odds": r.odds, "p": r.p_cond,
                      "status": r.status, "actual": None if pd.isna(r.actual) else float(r.actual),
                      "pnl": float(r.pnl)} for r in rec.itertuples()]
    pls = pl[pl["status"].isin(["won", "lost", "push"])]
    out["parlays"] = []
    for mode, d in pls.groupby("mode"):
        priced = d["pnl"].notna().any()
        out["parlays"].append({"mode": mode, "n": len(d), "hits": int((d["status"] == "won").sum()),
                               "predicted": float(d["p_hit"].mean()),
                               "units": float(d["pnl"].sum()) if priced else None,
                               "roi": float(d["pnl"].sum() / len(d)) if priced else None})
    bs = bets.sort_values("placed_at", ascending=False)
    settled_b = bs[bs["status"].isin(["won", "lost", "push"])]
    out["bets"] = {"rows": [{k: _jsonable(v) for k, v in r.items() if k != "legs_json"} for r in bs.to_dict("records")],
                   "settled": len(settled_b), "open": int((bs["status"] == "open").sum()),
                   "staked": float(settled_b["stake"].sum()), "pnl": float(settled_b["pnl"].sum()),
                   "roi": float(settled_b["pnl"].sum() / settled_b["stake"].sum()) if settled_b["stake"].sum() else None,
                   "series": [{"date": k, "pnl": float(v)} for k, v in
                              settled_b.groupby("date")["pnl"].sum().sort_index().cumsum().items()]}
    return out
