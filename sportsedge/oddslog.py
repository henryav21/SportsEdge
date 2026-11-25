"""Line movement: a running log of every change to a game's spread, total and moneyline.

The feed gives each line's opening number, so "moved since it opened" is known straight away. On top of that
the dashboard checks the feed every few minutes and writes a row here whenever a game's numbers change
(history/odds_log.csv), which is what the charts on the Line moves page are drawn from.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from . import tracker
from .oddsmath import devig, implied

FIELDS = ["spread_home", "spread_home_odds", "spread_away_odds", "total", "over_odds", "under_odds", "ml_home", "ml_away"]
COLS = ["ts", "sport", "event_id", "home", "away", "start", "state"] + FIELDS


def _path():
    return tracker.history_dir() / "odds_log.csv"


def load() -> pd.DataFrame:
    p = _path()
    if not p.exists():
        return pd.DataFrame(columns=COLS)
    df = pd.read_csv(p, dtype={"event_id": str, "sport": str, "home": str, "away": str, "state": str, "ts": str, "start": str})
    for c in FIELDS:
        df[c] = pd.to_numeric(df.get(c), errors="coerce")
    return df.reindex(columns=COLS)


def _same(a, b) -> bool:
    return all((pd.isna(x) and pd.isna(y)) or (not pd.isna(x) and not pd.isna(y) and abs(float(x) - float(y)) < 1e-9) for x, y in zip(a, b))


def record(sport: str, games: pd.DataFrame, now: dt.datetime | None = None) -> int:
    """Append a row for every game whose numbers differ from the last row logged for it. Returns how many changed."""
    if games is None or games.empty:
        return 0
    log = load()
    last = log[log["sport"] == sport].drop_duplicates("event_id", keep="last").set_index("event_id") if not log.empty else pd.DataFrame()
    ts = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
    rows = []
    for g in games.itertuples():
        vals = [getattr(g, f, np.nan) for f in FIELDS]
        if all(pd.isna(v) for v in vals):
            continue
        eid = str(g.event_id)
        if len(last) and eid in last.index and _same(vals, [last.at[eid, f] for f in FIELDS]):
            continue
        rows.append({"ts": ts, "sport": sport, "event_id": eid, "home": g.home, "away": g.away, "start": str(g.start),
                     "state": g.state, **dict(zip(FIELDS, vals))})
    if rows:
        out = pd.concat([log, pd.DataFrame(rows, columns=COLS)], ignore_index=True) if not log.empty else pd.DataFrame(rows, columns=COLS)
        out.to_csv(_path(), index=False)
    return len(rows)


def home_chance(ml_home, ml_away) -> float | None:
    """The home team's win chance the moneyline implies, with the sportsbook's fee removed."""
    if ml_home is None or ml_away is None or pd.isna(ml_home) or pd.isna(ml_away):
        return None
    ph, _ = devig([implied(ml_home), implied(ml_away)])
    return float(ph)


def _num(x):
    return None if x is None or pd.isna(x) else float(x)


def series(log: pd.DataFrame, sport: str, home: str, away: str, start) -> list[dict]:
    """The logged changes for one game, oldest first."""
    if log.empty:
        return []
    d = log[(log["sport"] == sport) & (log["home"] == home) & (log["away"] == away)]
    if d.empty:
        return []
    try:  # the same two teams can meet twice in a season: keep rows for this game's date only
        day = pd.Timestamp(start).tz_convert("UTC").date()
        d = d[(pd.to_datetime(d["start"], utc=True, errors="coerce").dt.date - day).abs() <= dt.timedelta(days=1)]
    except Exception:
        pass
    return [{"t": r.ts, "spread_home": _num(r.spread_home), "total": _num(r.total), "ml_home": _num(r.ml_home),
             "ml_away": _num(r.ml_away), "p_home": home_chance(r.ml_home, r.ml_away)} for r in d.itertuples()]


def movement(open_line: dict, now_line: dict) -> dict:
    """How far each number has moved since the line opened. Positive spread_home = the home team is getting
    more points than at open (it has become less favoured)."""
    o, n = open_line or {}, now_line or {}

    def diff(k):
        a, b = _num(o.get(k)), _num(n.get(k))
        return None if a is None or b is None else b - a

    po, pn = home_chance(o.get("ml_home"), o.get("ml_away")), home_chance(n.get("ml_home"), n.get("ml_away"))
    return {"spread_home": diff("spread_home"), "total": diff("total"),
            "p_home": None if po is None or pn is None else pn - po, "p_home_open": po, "p_home_now": pn}
