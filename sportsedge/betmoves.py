"""How the sportsbook's number for each recorded pick has moved since the bet was recorded.

Before a game the pick is compared with the number the dashboard has for it now. The last number seen before
the game starts is kept (history/bet_lines.json), so after kick-off the comparison stays put: that is the
bet's closing-line value, as near to the close as this tool looked.

A move is always signed from the bettor's side: + means the bet holds a better number than the market offers
now (the market has moved toward the pick), - means a worse one.
"""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd

from . import tracker
from .oddsmath import devig, implied

GAME_MARKETS = ("ml", "spread", "total")
MIN_CHANCE = 0.005  # a change in the no-vig chance smaller than half a point is noise, not a move


def _num(x):
    try:
        return None if x is None or pd.isna(x) else float(x)
    except (TypeError, ValueError):
        return None


def _pair(a, b) -> tuple[float | None, float | None]:
    """The chance of each side of a two-way market with the sportsbook's fee removed."""
    a, b = _num(a), _num(b)
    if a is None or b is None:
        return None, None
    pa, pb = devig([implied(a), implied(b)])
    return float(pa), float(pb)


def game_now(leg: dict, line: dict | None) -> dict | None:
    """The current number for a moneyline, spread or total pick, read from the game's current line
    (spread_home, total, the two moneylines and the prices on each side)."""
    if not line:
        return None
    mk, home = leg.get("market_key"), leg.get("side") in ("home", "over")  # "first side": home, or the over
    if mk == "ml":
        odds = (line.get("ml_home"), line.get("ml_away"))
        ln = None
    elif mk == "spread":
        sh = _num(line.get("spread_home"))
        if sh is None:
            return None
        odds = (line.get("spread_home_odds"), line.get("spread_away_odds"))
        ln = sh if home else -sh
    elif mk == "total":
        ln = _num(line.get("total"))
        if ln is None:
            return None
        odds = (line.get("over_odds"), line.get("under_odds"))
    else:
        return None
    p = _pair(*odds)
    mine = _num(odds[0 if home else 1])
    if ln is None and mine is None:
        return None
    return {"line": ln, "odds": mine, "p_fair": p[0 if home else 1]}


def since_bet(leg: dict, now: dict | None) -> dict:
    """One recorded pick against its number now. {} when there is nothing to compare, otherwise:

    kind     "line"   the line itself moved; move is in points (or the stat's own units)
             "chance" same line; move is the change in the pick's no-vig chance (0.02 = 2 points)
    move     signed so that + is in the bettor's favour
    verdict  "for", "against" or "same"
    """
    if not now:
        return {}
    mk, side = leg.get("market_key"), leg.get("side")
    lt, ln = _num(leg.get("line")), _num(now.get("line"))
    out = {"line_then": lt, "line_now": ln, "odds_then": _num(leg.get("odds")), "odds_now": _num(now.get("odds"))}
    if lt is not None and ln is not None and abs(ln - lt) > 1e-9:
        # a spread is the pick's own handicap: holding more points than the market gives now is the better number.
        # A total or a prop: a higher number now helps an over, a lower one helps an under.
        move = (lt - ln) if mk == "spread" else (ln - lt) * (-1.0 if side == "under" else 1.0)
        return {**out, "kind": "line", "move": move, "verdict": "for" if move > 0 else "against"}
    pt, pn = _num(leg.get("p_fair")), _num(now.get("p_fair"))
    if pt is None or pn is None:  # no fee-free chance on one side: compare the two prices as they stand
        if out["odds_then"] is None or out["odds_now"] is None:
            # a line with no price (NFL props) that has not moved
            return {**out, "kind": "line", "move": 0.0, "verdict": "same"} if lt is not None and ln is not None else {}
        pt, pn = float(implied(out["odds_then"])), float(implied(out["odds_now"]))
    move = pn - pt
    return {**out, "kind": "chance", "move": move, "verdict": "same" if abs(move) < MIN_CHANCE else "for" if move > 0 else "against"}


def started(leg: dict, now: dt.datetime | None = None) -> bool:
    """Has the pick's game started? Unknown start times count as not started."""
    try:
        t = pd.Timestamp(leg.get("start"))
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    except (TypeError, ValueError):
        return False
    if pd.isna(t):
        return False
    return t.to_pydatetime() <= (now or dt.datetime.now(dt.timezone.utc))


# ============================================================================= the last number seen
def _path():
    return tracker.history_dir() / "bet_lines.json"


def load_seen() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_seen(seen: dict):
    _path().write_text(json.dumps(seen, indent=1), encoding="utf-8")


def _differs(a, b) -> bool:
    a, b = _num(a), _num(b)
    return (a is None) != (b is None) or (a is not None and abs(a - b) > 1e-9)


def update(bets: pd.DataFrame, current, now: dt.datetime | None = None) -> dict:
    """Movement since the bet for every pick of every recorded bet: {bet id: {pick key: move}}.

    current(leg) gives the pick's number right now ({"line", "odds", "p_fair"}) or None when there is none.
    While a game is still to come the latest number is remembered. Once it has started nothing newer is
    taken, so the move stays as it was at the last look before the game ("final": True). A pick that is
    simply off the board for the moment keeps its last number and is marked "stale"."""
    now = now or dt.datetime.now(dt.timezone.utc)
    seen, out, changed, ids = load_seen(), {}, False, set()
    if bets is None or bets.empty:
        bets = pd.DataFrame(columns=["id", "legs_json", "status"])
    for b in bets.itertuples():
        bid = str(b.id)
        ids.add(bid)
        try:
            legs = json.loads(b.legs_json)
        except (TypeError, ValueError):
            continue
        for leg in legs:
            key = str(leg.get("key"))
            over = started(leg, now)
            cur = None if over or str(b.status) != "open" else current(leg)
            rec = (seen.get(bid) or {}).get(key)
            if cur is not None and (rec is None or any(_differs(rec.get(f), cur.get(f)) for f in ("line", "odds", "p_fair"))):
                rec = {**{f: _num(cur.get(f)) for f in ("line", "odds", "p_fair")}, "seen": now.isoformat(timespec="seconds")}
                seen.setdefault(bid, {})[key] = rec
                changed = True
            move = since_bet(leg, rec)
            if move:
                out.setdefault(bid, {})[key] = {**move, "final": over, "stale": cur is None and not over, "seen": rec.get("seen"),
                                                # a number typed in from another sportsbook is being set against the feed's book
                                                "other_book": leg.get("book") == "manual"}
    for bid in [k for k in seen if k not in ids]:  # a deleted bet takes its numbers with it
        del seen[bid]
        changed = True
    if changed:
        _save_seen(seen)
    return out


def tally(moves: dict | None) -> dict:
    """How many of a bet's picks have moved each way."""
    t = {"for": 0, "against": 0, "same": 0}
    for m in (moves or {}).values():
        t[m["verdict"]] = t.get(m["verdict"], 0) + 1
    return t
