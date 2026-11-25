"""Live tracking of open bets and a check on a sportsbook's cash-out offer.

For every leg of a bet this works out where it stands right now (score, clock, the player's stat line),
whether its result is already decided, and a rough chance that it still wins. From those it values the
whole bet, so an offer to cash out can be compared with what the bet is actually worth.

The live chances are estimates, simpler than the pre-game models. Every one of them starts, at the opening
tip or kick-off, exactly at the pick's pre-game chance, and moves from there only because of the score, the
clock and the stat line:
  - moneylines follow ESPN's live win probability, shifted so that it starts from the pre-game chance (the
    shift fades as the game goes on);
  - spreads and totals assume the rest of the game plays out at the pre-game pace, with the uncertainty
    shrinking as the clock runs down (the standard random-walk model of a score);
  - player props assume the player keeps producing at a blend of his pre-game expectation and tonight's pace.
They ignore foul trouble, blowouts, injuries during the game and who has the ball, so treat them as a guide.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import time

import pandas as pd

from . import sources
from .oddsmath import american_to_decimal
from .tracker import settle_game, settle_prop
from .util import norm_name

SITE = "https://site.api.espn.com/apis/site/v2/sports"
TTL = 20.0  # seconds a live answer is reused
PERIODS = {"nba": (4, 12.0), "nfl": (4, 15.0)}  # regulation periods, minutes each
SD = {"nfl": {"margin": 13.3, "total": 13.3}, "nba": {"margin": 12.8, "total": 18.5}}  # final-score spread, points
# stats that only go up, so an over that is already reached (or an under already passed) is decided
COUNTS = {"pts", "reb", "ast", "fg3m", "stl", "blk", "pra", "pr", "pa", "ra", "receptions", "pass_cmp", "pass_att",
          "pass_tds", "rush_att", "anytime_td"}
PACE_WEIGHT = 0.35  # how much tonight's pace counts against the pre-game projection for the rest of the game

_cache: dict = {}


def _fresh(key, fn, ttl=TTL):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


# ============================================================================= live data
def scoreboard(sport: str, day: dt.date) -> list[dict]:
    """Every game on one day: teams, score, state ('pre' / 'in' / 'post'), period and clock."""

    def fetch():
        try:
            j = sources._get(f"{SITE}/{sources.ESPN_PATH[sport]}/scoreboard", {"dates": f"{day:%Y%m%d}", "limit": 500}, tries=2)
        except RuntimeError:
            return []
        out = []
        for ev in j.get("events", []):
            c = ev["competitions"][0]
            t = {x["homeAway"]: x for x in c["competitors"]}
            if "home" not in t or "away" not in t:
                continue
            st = c.get("status") or ev.get("status") or {}
            typ = st.get("type") or {}
            num = lambda x: float(x) if str(x).replace(".", "", 1).isdigit() else 0.0  # noqa: E731
            out.append({"event_id": str(ev["id"]), "start": ev.get("date"),
                        "home": sources.norm_team(sport, t["home"]["team"]["abbreviation"]),
                        "away": sources.norm_team(sport, t["away"]["team"]["abbreviation"]),
                        "home_score": num(t["home"].get("score")), "away_score": num(t["away"].get("score")),
                        "state": typ.get("state") or "pre", "detail": typ.get("shortDetail") or typ.get("detail") or "",
                        "period": int(st.get("period") or 0), "clock": float(st.get("clock") or 0.0),
                        "home_tid": t["home"]["team"].get("id"), "away_tid": t["away"]["team"].get("id")})
        return out

    return _fresh(("sb", sport, day), fetch)


def scores(sports=("nfl", "nba"), day: dt.date | None = None) -> list[dict]:
    """Today's games with score and clock: in play first, then still to come, then finished. A game in play
    also carries ESPN's own live win chance for the home team (theirs, not this tool's). A game that has
    started carries every player's stat line so far."""
    day = day or dt.date.today()
    out = []
    for sport in sports:
        for g in scoreboard(sport, day):
            row = {"sport": sport, "home_wp": None, "players": [],
                   **{k: g[k] for k in ("event_id", "home", "away", "home_score", "away_score", "state", "detail", "start")}}
            if g["state"] in ("in", "post"):
                # a finished game's numbers no longer change, so they are asked for far less often
                box = boxscore(sport, g["event_id"]) if g["state"] == "in" else                     _fresh(("final", sport, g["event_id"]), lambda: boxscore(sport, g["event_id"]), ttl=600)
                if g["state"] == "in":
                    row["home_wp"] = box.get("home_wp")
                who = box.get("names") or {}
                row["players"] = [{"key": k, "name": (who.get(k) or {}).get("name") or k.title(), "team": (who.get(k) or {}).get("team") or "",
                                   **{s: v for s, v in st.items() if not s.startswith("_")}} for k, st in (box.get("players") or {}).items()]
            out.append(row)
    order = {"in": 0, "pre": 1, "post": 2}
    out.sort(key=lambda r: (order.get(r["state"], 3), str(r["start"])))
    return out


def find_game(sport: str, leg: dict) -> dict | None:
    """The live game a leg belongs to. NBA legs carry ESPN's event id; NFL legs are matched on the teams."""
    try:
        day = dt.date.fromisoformat(str(leg.get("date"))[:10])
    except ValueError:
        return None
    eid = str(leg.get("game_id") or "")
    eid = eid[1:] if eid.startswith("E") and eid[1:].isdigit() else None
    for d in (day, day + dt.timedelta(days=1), day - dt.timedelta(days=1)):  # the feed's day is US Eastern
        for g in scoreboard(sport, d):
            if (eid and g["event_id"] == eid) or (not eid and g["home"] == leg.get("home") and g["away"] == leg.get("away")):
                return g
    return None


def _split(s: str, sep: str) -> tuple[float, float]:
    a, _, b = str(s).partition(sep)
    f = lambda x: float(x) if x.replace(".", "", 1).lstrip("-").isdigit() else 0.0  # noqa: E731
    return f(a), f(b)


def parse_boxscore(sport: str, j: dict) -> dict:
    """ESPN game summary -> {'players': {player_key: {our stat name: value}}, 'home_wp': live home win chance}."""
    players: dict[str, dict] = {}
    names: dict[str, dict] = {}  # player key -> how to show him: name and team
    for team in (j.get("boxscore") or {}).get("players", []):
        abbr = sources.norm_team(sport, (team.get("team") or {}).get("abbreviation") or "")
        for grp in team.get("statistics", []):
            keys = grp.get("keys") or []
            gname = grp.get("name")
            for a in grp.get("athletes", []):
                if a.get("didNotPlay"):
                    continue
                st = dict(zip(keys, a.get("stats") or []))
                if not st:
                    continue
                shown = (a.get("athlete") or {}).get("displayName", "")
                p = players.setdefault(norm_name(shown), {})
                names[norm_name(shown)] = {"name": shown, "team": abbr}
                num = lambda k: _split(st.get(k, "0"), "~")[0]  # noqa: E731
                if sport == "nba":
                    p.update(min=num("minutes"), pts=num("points"), reb=num("rebounds"), ast=num("assists"),
                             stl=num("steals"), blk=num("blocks"),
                             fg3m=_split(st.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "0-0"), "-")[0])
                    p.update(pra=p["pts"] + p["reb"] + p["ast"], pr=p["pts"] + p["reb"], pa=p["pts"] + p["ast"], ra=p["reb"] + p["ast"])
                elif gname == "passing":
                    cmp_, att = _split(st.get("completions/passingAttempts", "0/0"), "/")
                    p.update(pass_cmp=cmp_, pass_att=att, pass_yds=num("passingYards"), pass_tds=num("passingTouchdowns"))
                elif gname == "rushing":
                    p.update(rush_att=num("rushingAttempts"), rush_yds=num("rushingYards"))
                    p["_td"] = p.get("_td", 0.0) + num("rushingTouchdowns")
                elif gname == "receiving":
                    p.update(receptions=num("receptions"), rec_yds=num("receivingYards"))
                    p["_td"] = p.get("_td", 0.0) + num("receivingTouchdowns")
                elif gname in ("kickReturns", "puntReturns", "interceptions", "defensive"):
                    k = {"kickReturns": "kickReturnTouchdowns", "puntReturns": "puntReturnTouchdowns",
                         "interceptions": "interceptionTouchdowns", "defensive": "defensiveTouchdowns"}[gname]
                    p["_td"] = p.get("_td", 0.0) + num(k)
    if sport == "nfl":
        for p in players.values():
            p["rush_rec_yds"] = p.get("rush_yds", 0.0) + p.get("rec_yds", 0.0)
            p["anytime_td"] = p.pop("_td", 0.0)
    wp = j.get("winprobability") or []
    home_wp = wp[-1].get("homeWinPercentage") if wp else None
    home_wp0 = wp[0].get("homeWinPercentage") if wp else None
    return {"players": players, "names": names, "home_wp": None if home_wp is None else float(home_wp),
            "home_wp0": None if home_wp0 is None else float(home_wp0)}


def boxscore(sport: str, event_id: str) -> dict:
    def fetch():
        try:
            return parse_boxscore(sport, sources._get(f"{SITE}/{sources.ESPN_PATH[sport]}/summary", {"event": event_id}, tries=2))
        except RuntimeError:
            return {"players": {}, "home_wp": None, "home_wp0": None}

    return _fresh(("box", sport, event_id), fetch)


def remaining_fraction(sport: str, g: dict) -> float:
    """Share of regulation still to play: 1 before the game, 0 at the end. Overtime counts as almost over."""
    if g["state"] == "pre":
        return 1.0
    if g["state"] == "post":
        return 0.0
    n, mins = PERIODS[sport]
    period, clock = g.get("period") or 1, min(max(float(g.get("clock") or 0.0) / 60.0, 0.0), mins)
    if period > n:
        return max(min(clock / (n * mins), 0.1), 0.01)
    return max(min(((n - period) * mins + clock) / (n * mins), 1.0), 0.0)


# ============================================================================= chances
def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _phi_inv(p: float) -> float:
    """Inverse normal CDF (Acklam's approximation, accurate to about 1e-9)."""
    p = min(max(p, 1e-9), 1 - 1e-9)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > 1 - 0.02425:
        return -_phi_inv(1 - p)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def _over(mean: float, sd: float, thr: float) -> tuple[float, float]:
    """(chance a whole-number result ends above thr, chance it lands exactly on thr) for a normal spread."""
    sd = max(sd, 1e-6)
    if abs(thr - round(thr)) < 1e-9:
        hi, lo = _phi((thr + 0.5 - mean) / sd), _phi((thr - 0.5 - mean) / sd)
        return 1.0 - hi, hi - lo
    return 1.0 - _phi((thr - mean) / sd), 0.0


def _solve(fn, target: float, lo: float, hi: float) -> float:
    """The x in [lo, hi] at which the increasing function fn reaches target (bisection)."""
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if fn(mid) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _anchor_mean(p_cond: float, thr: float, sd: float, tie_is_half: bool = False) -> float:
    """The mean of a bell curve with spread sd for which 'ends above thr, given it does not land exactly on
    thr' has chance p_cond. A pick's pre-game chance is quoted that way (given no push). For a moneyline a
    level score is not a push but overtime, counted as an even chance (tie_is_half)."""
    def cond(mu):
        over, push = _over(mu, sd, thr)
        return over + 0.5 * push if tie_is_half else over / max(1.0 - push, 1e-9)

    return _solve(cond, p_cond, thr - 8 * sd, thr + 8 * sd)


def _poisson_at_least(lam: float, k: int) -> float:
    if k <= 0:
        return 1.0
    term, cdf = math.exp(-lam), 0.0
    for i in range(k):
        cdf += term
        term *= lam / (i + 1)
    return max(0.0, 1.0 - cdf)


def _logit(p: float) -> float:
    p = min(max(p, 0.002), 0.998)
    return math.log(p / (1.0 - p))


def game_leg_chance(sport: str, leg: dict, g: dict, home_wp: float | None, sd: dict | None = None,
                    home_wp0: float | None = None, at: float | None = None) -> tuple[float, float]:
    """(chance the leg wins, chance it pushes) for a moneyline, spread or total, from the score and clock.
    home_wp / home_wp0: ESPN's live win chance for the home team now and at the start of the game.
    at: judge the pick at this line instead of its own (a live line), from the same pre-game expectation."""
    mk, side = leg["market_key"], leg["side"]
    f = remaining_fraction(sport, g)
    margin, total = g["home_score"] - g["away_score"], g["home_score"] + g["away_score"]
    p_pre = min(max(float(leg.get("p_cond") or 1.0 / float(leg.get("dec") or 2.0)), 0.02), 0.98)
    sds = {**SD[sport], **(sd or {})}
    if mk == "ml" and home_wp is not None and g["state"] == "in":
        now = home_wp if side == "home" else 1.0 - home_wp
        if home_wp0 is None:
            return now, 0.0
        # ESPN's model starts the game at its own number; this tool's pre-game chance is anchored to the
        # sportsbook. Carry that difference into the game and let it fade as the clock runs down.
        start = home_wp0 if side == "home" else 1.0 - home_wp0
        return 1.0 / (1.0 + math.exp(-(_logit(now) + f * (_logit(p_pre) - _logit(start))))), 0.0
    # every game leg is "some number ends above a threshold": y is that number now, thr the threshold
    if mk == "total":
        s0, sign = sds["total"], (1.0 if side == "over" else -1.0)
        y, thr = sign * total, sign * float(leg["line"])
    else:
        s0, sign = sds["margin"], (1.0 if side == "home" else -1.0)
        y, thr = sign * margin, (0.0 if mk == "ml" else -float(leg["line"]))
    # what y was expected to end on before the game, read back from the pick's pre-game chance;
    # the part of the game still to play is expected to add its share of that
    mean = y + _anchor_mean(p_pre, thr, s0, tie_is_half=(mk == "ml")) * f
    if at is not None and mk != "ml":
        thr = sign * float(at) if mk == "total" else -float(at)
    if f <= 0:
        return (1.0 if y > thr else 0.0), (1.0 if abs(y - thr) < 1e-9 else 0.0)
    p_win, p_push = _over(mean, s0 * math.sqrt(f), thr)
    if mk == "ml":
        p_win, p_push = p_win + 0.5 * p_push, 0.0  # a tie goes to overtime
    return p_win, p_push


def stat_spread(mk: str, size: float) -> float:
    """Typical game-to-game spread (standard deviation) of a stat for a player who averages `size`.
    Round figures from box scores: a 23-point scorer swings by about 8 points, a 60-yard receiver by about
    36 yards, a 240-yard passer by about 70."""
    if mk == "pass_yds":
        return max(0.27 * size + 5.0, 20.0)
    if mk not in COUNTS:                                   # rushing and receiving yards
        return max(0.5 * size + 6.0, 8.0)
    k = 3.0 if mk in ("pts", "pra", "pr", "pa") else 1.8   # points come in twos and threes, so they swing more
    return max(math.sqrt(k * max(size, 0.3)), 0.7)


def _poisson_rate_for(p_cond: float, line: float) -> float:
    """The Poisson rate at which 'ends above the line, given it does not land exactly on it' has chance p_cond."""
    need, whole = math.floor(line) + 1, abs(line - round(line)) < 1e-9

    def cond(lam):
        over = _poisson_at_least(lam, need)
        push = (_poisson_at_least(lam, need - 1) - over) if whole else 0.0
        return over / max(1.0 - push, 1e-9)

    return _solve(cond, p_cond, 1e-6, max(4.0 * need + 20.0, 20.0))


def prop_leg_chance(leg: dict, value: float, f: float, at: float | None = None) -> tuple[float, float]:
    """(chance the leg wins, chance it pushes) for a player prop, from his stat line and the time left.

    The pick's pre-game chance fixes what the player was expected to do over the whole game, so with nothing
    played and nothing recorded this returns that pre-game chance. From there the expectation for the rest
    of the game is a blend of that pre-game rate and tonight's pace."""
    mk, side, line = leg["market_key"], leg["side"], float(leg["line"])
    p_side = min(max(float(leg.get("p_cond") or 1.0 / float(leg.get("dec") or 2.0)), 0.02), 0.98)
    if mk == "anytime_td":
        if value >= 1:
            return 1.0, 0.0
        return 1.0 - math.exp(math.log(1.0 - p_side) * f), 0.0  # touchdowns arrive at the pre-game rate
    p_over_pre = p_side if side == "over" else 1.0 - p_side
    size = float(leg.get("proj") if leg.get("proj") is not None and not pd.isna(leg.get("proj")) else line)
    yards = mk not in COUNTS
    spread0 = stat_spread(mk, size)  # how spread out this stat is for a player of this size
    small = not yards and spread0 * spread0 < 9.0  # threes, steals, receptions: whole events, not a bell curve
    if f <= 0:
        over, push = (1.0 if value > line else 0.0), (1.0 if abs(value - line) < 1e-9 else 0.0)
    else:
        whole = abs(line - round(line)) < 1e-9
        # the full-game expectation that the pick's pre-game chance implies
        mu = _poisson_rate_for(p_over_pre, line) if small else _anchor_mean(p_over_pre, line, spread0)
        if at is not None:  # judged at a live line: the expectation above still comes from the pre-game one
            line = float(at)
            whole = abs(line - round(line)) < 1e-9
        pace = value / max(1.0 - f, 0.15) if f < 0.9 else mu
        rest = max(f * ((1 - PACE_WEIGHT) * mu + PACE_WEIGHT * pace), 0.0)  # expected production from here
        if small:
            need = math.floor(line - value) + 1
            over = _poisson_at_least(max(rest, 1e-6), need)
            push = (_poisson_at_least(max(rest, 1e-6), need - 1) - over) if whole else 0.0
        else:
            over, push = _over(value + rest, spread0 * math.sqrt(f), line)
            if not yards and value > line:
                over, push = 1.0, 0.0
    return (over, push) if side == "over" else (max(0.0, 1.0 - over - push), push)


# ============================================================================= live prices and hedges
# The tool does not suggest new live bets. Its live estimate knows the score, the clock and the box score, and
# nothing else: on the first night it was set against real in-game prices it "found value" in a third of them,
# with gaps of 30 points and more. A gap that size is the estimate being wrong (a big lead shrinks, starters
# sit), not the sportsbook. So live prices are shown as they are, every pairing of live price and estimate is
# logged for testing against results, and the one live bet that is pure arithmetic is offered: a hedge.
LOG_COLS = ["ts", "sport", "event_id", "key", "selection", "line", "odds", "other_odds", "p_tool", "q_book", "left", "now"]


def _log_path():
    from .tracker import history_dir

    return history_dir() / "live_log.csv"


def _log_rows(rows: list[dict]):
    """Keep the first sighting of each (pick, live line): enough to test the estimate later without filling
    the disk with every tick."""
    if not rows:
        return
    path = _log_path()
    seen = _cache.setdefault("live_logged", None)
    if seen is None:
        seen = set()
        if path.exists():
            try:
                old = pd.read_csv(path, usecols=["key", "line"])
                seen = set(zip(old["key"].astype(str), old["line"].astype(str)))
            except (ValueError, OSError):
                seen = set()
        _cache["live_logged"] = seen
    fresh = [r for r in rows if (str(r["key"]), str(r["line"])) not in seen]
    if not fresh:
        return
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    df = pd.DataFrame([{"ts": ts, "sport": r["sport"], "event_id": r["event_id"], "key": r["key"], "selection": r["selection"],
                        "line": r["line"], "odds": r["odds"], "other_odds": r["other_odds"], "p_tool": round(r["p"], 4),
                        "q_book": round(r["q"], 4), "left": round(r["left"], 3), "now": r["now"]} for r in fresh], columns=LOG_COLS)
    df.to_csv(path, mode="a", header=not path.exists(), index=False)
    seen.update((str(r["key"]), str(r["line"])) for r in fresh)


def board(pre_legs: list[dict], sd_by_sport: dict | None = None, day: dt.date | None = None, log: bool = True) -> dict:
    """The sportsbook's in-game prices for every game in play.

    pre_legs: each game's picks at their last pre-game number, which is where a live price is matched to a
    pick and where the logged estimate starts from. Returns {'games': [...], 'rows': {pick key: live row}}:
    a row is the pick at its live line with the live price on it and on the other side."""
    from concurrent.futures import ThreadPoolExecutor

    from .oddsmath import devig, implied
    from .tracker import with_line

    by_game: dict = {}
    for l in pre_legs:
        by_game.setdefault((str(l["sport"]).lower(), l["home"], l["away"]), []).append(l)
    playing = [(sport, g) for sport in ("nfl", "nba") for g in scoreboard(sport, day or dt.date.today()) if g["state"] == "in"]

    def prices(x):
        sport, g = x
        return _fresh(("liveodds", sport, g["event_id"]), lambda: sources.espn_live_odds(sport, g["event_id"], g.get("home_tid"), g.get("away_tid")))

    with ThreadPoolExecutor(max_workers=6) as ex:
        live_odds = list(ex.map(prices, playing)) if playing else []
    games, rows, logged = [], {}, []
    for (sport, g), lo in zip(playing, live_odds):
        pre = by_game.get((sport, g["home"], g["away"])) or []
        f = remaining_fraction(sport, g)
        info = {"sport": sport, "event_id": g["event_id"], "game": f"{g['away']} @ {g['home']}", "home": g["home"], "away": g["away"],
                "book": (lo or {}).get("book"), "line": (lo or {}).get("line"), "lines": [], "n_props": 0}
        games.append(info)
        if not lo or not pre:
            continue
        box = boxscore(sport, g["event_id"])
        sd = (sd_by_sport or {}).get(sport)

        def row(leg, line, odds, other, p, push, now):
            if odds is None or other is None or pd.isna(odds) or pd.isna(other):
                return None
            at = leg if line is None else with_line(leg, line)
            return {"key": leg["key"], "sport": sport, "event_id": g["event_id"], "game": info["game"], "selection": at["selection"],
                    "market_key": leg["market_key"], "side": leg["side"], "player": leg.get("subject") if leg.get("player_key") else None,
                    "line": line, "pre_line": leg.get("line"), "odds": float(odds), "other_odds": float(other),
                    "dec": american_to_decimal(odds), "q": float(devig([implied(odds), implied(other)])[0]),
                    "p": p / max(1.0 - push, 1e-9), "left": f, "now": now, "book": lo["book"]}

        find = lambda mk, side, who="": next((l for l in pre if l["market_key"] == mk and l["side"] == side  # noqa: E731
                                              and (l.get("player_key") or "") == who), None)
        L = lo["line"]
        sp = L.get("spread_home")
        for mk, side, line, odds, other in (
                ("ml", "home", None, L.get("ml_home"), L.get("ml_away")), ("ml", "away", None, L.get("ml_away"), L.get("ml_home")),
                ("spread", "home", sp, L.get("spread_home_odds"), L.get("spread_away_odds")),
                ("spread", "away", None if sp is None else -sp, L.get("spread_away_odds"), L.get("spread_home_odds")),
                ("total", "over", L.get("total"), L.get("over_odds"), L.get("under_odds")),
                ("total", "under", L.get("total"), L.get("under_odds"), L.get("over_odds"))):
            leg = find(mk, side)
            if leg is None or (mk != "ml" and line is None):
                continue
            p, push = game_leg_chance(sport, leg, g, box.get("home_wp"), sd, box.get("home_wp0"), at=line)
            r = row(leg, line, odds, other, p, push, f"{g['away']} {g['away_score']:g}, {g['home']} {g['home_score']:g}")
            if r:
                info["lines"].append({k: r[k] for k in ("selection", "market_key", "side", "line", "pre_line", "odds", "q")})
                rows[r["key"]] = r
                logged.append(r)
        for pl in lo["props"]:
            over, under = find(pl["market"], "over", pl["key"]), find(pl["market"], "under", pl["key"])
            stat = box["players"].get(pl["key"])
            if over is None or under is None or stat is None or pl.get("line") is None:
                continue  # no pre-game number to match it to, or he has not played
            value = float(stat.get(pl["market"], 0.0))
            p_over, push = prop_leg_chance(over, value, f, at=float(pl["line"]))
            info["n_props"] += 1
            for leg, p, odds, other in ((over, p_over, pl.get("over_odds"), pl.get("under_odds")),
                                        (under, max(0.0, 1.0 - p_over - push), pl.get("under_odds"), pl.get("over_odds"))):
                r = row(leg, float(pl["line"]), odds, other, p, push, f"{value:g}")
                if r:
                    rows[r["key"]] = r
                    logged.append(r)
    if log:
        try:
            _log_rows(logged)
        except OSError:
            pass
    return {"games": games, "rows": rows}


def _flip(key: str) -> str | None:
    """The key of the other side of the same bet."""
    parts = key.split("|")
    other = {"home": "away", "away": "home", "over": "under", "under": "over"}.get(parts[3] if len(parts) > 3 else "")
    return None if other is None else "|".join(parts[:3] + [other] + parts[4:])


def hedge(bet: dict, rows: dict) -> dict | None:
    """For a bet with exactly one pick left and that game in play: the stake on the other side, at the
    sportsbook's live price, that returns the same whichever way it goes. Pure arithmetic on real prices.

    When the live line is not the line the bet was made at, the two bets can both win (a middle: the result
    lands between the two lines) or both lose (a gap). Both are reported, with what each pays."""
    if bet.get("status") in ("won", "lost", "push") or bet.get("open") != 1:
        return None
    leg = next((v for v in bet["legs"] if not v["decided"]), None)
    if leg is None or leg.get("state") != "in":
        return None
    other = rows.get(_flip(str(leg.get("key"))) or "")
    if other is None:
        return None
    payout, stake = float(bet["payout"]), float(bet["stake"])
    h = payout / other["dec"]              # the hedge returns exactly what the bet would have
    out = {"selection": other["selection"], "odds": other["odds"], "book": other["book"], "stake": h,
           "locked": payout - stake - h,   # one of the two wins: the same either way
           "line": other["line"], "bet_line": leg.get("line"), "middle": None, "gap": None}
    mk, side = leg["market_key"], leg["side"]
    if mk != "ml" and leg.get("line") is not None and other["line"] is not None:
        # put both on one number X: the home margin for a spread, otherwise the total or the stat itself
        if mk == "spread":
            mine = -float(leg["line"]) if side == "home" else float(leg["line"])      # home wins above, away below
            theirs = float(other["line"]) if side == "home" else -float(other["line"])
            above = side == "home"
        else:
            mine, theirs, above = float(leg["line"]), float(other["line"]), side == "over"
        lo, hi = min(mine, theirs), max(mine, theirs)
        if hi - lo > 1e-9:
            both_win = (theirs > mine) if above else (theirs < mine)
            span = {"lo": lo, "hi": hi, "unit": "margin" if mk == "spread" else "result"}
            if both_win:
                out["middle"] = {**span, "profit": 2 * payout - stake - h}
            else:
                out["gap"] = {**span, "loss": stake + h}
    return out


# ============================================================================= one leg, one bet
def _need_text(leg: dict, value: float, g: dict) -> str:
    mk, side = leg["market_key"], leg["side"]
    n = lambda x: f"{x:g}" if abs(x - round(x)) < 1e-9 else f"{x:.1f}"  # noqa: E731
    margin = g["home_score"] - g["away_score"]
    done = g["state"] == "post"
    if mk == "ml":
        m = margin if side == "home" else -margin
        if done:
            return f"{'won' if m > 0 else 'lost'} by {n(abs(m))}"
        return "tied" if m == 0 else f"{'ahead' if m > 0 else 'behind'} by {n(abs(m))}"
    if mk == "spread":
        m = (margin if side == "home" else -margin) + float(leg["line"])
        if m == 0:
            return "landed exactly on the spread" if done else "level with the spread"
        if done:
            return f"{'covered' if m > 0 else 'missed'} by {n(abs(m))}"
        return f"{'covering' if m > 0 else 'not covering'} by {n(abs(m))}"
    line = float(leg["line"])
    if mk == "anytime_td":
        return ("scored" if done else "has scored") if value >= 1 else ("did not score" if done else "no touchdown yet")
    if done:
        return f"finished on {n(value)}"
    gap = line - value
    whole = mk in COUNTS or mk == "total"
    if gap == 0:
        return "exactly on the line"
    if side == "over":
        return f"over by {n(-gap)}" if gap < 0 else f"needs {n(math.floor(gap) + 1 if whole else gap)} more"
    return f"already over by {n(-gap)}" if gap < 0 else f"can allow {n(math.ceil(gap) - 1 if whole else gap)} more"


def leg_view(leg: dict, sd: dict | None = None, fresh: dict | None = None) -> dict:
    """Where one leg stands. fresh: the same leg from the current slate, if it is still listed (pre-game)."""
    sport = str(leg["sport"]).lower()
    mk, side = leg["market_key"], leg["side"]
    is_prop = mk not in ("ml", "spread", "total")
    line = None if leg.get("line") is None or pd.isna(leg.get("line")) else float(leg["line"])
    p_ref = float((fresh or {}).get("p_cond") or leg.get("p_cond") or 1.0 / float(leg.get("dec") or 2.0))
    # The line that chance belongs to: the tool's line now if the pick is still listed, otherwise the tool's
    # line when the bet was recorded. If the bet was made at another line, the pre-game expectation is read
    # from the tool's line and the chance is then worked out at the bet's own.
    ref = (fresh or {}).get("line") if fresh else leg.get("ref_line")
    ref = None if ref is None or pd.isna(ref) or line is None or abs(float(ref) - line) < 1e-9 else float(ref)
    base = {**leg, "p_cond": p_ref} if ref is None else {**leg, "p_cond": p_ref, "line": ref}
    at = None if ref is None else line
    if ref is None:
        p_pre = p_ref
    elif is_prop:
        p_pre = prop_leg_chance(base, 0.0, 1.0, at=at)[0]
    else:
        before = {"state": "pre", "home_score": 0.0, "away_score": 0.0, "period": 0, "clock": 0.0}
        p_pre = game_leg_chance(sport, base, before, None, sd, at=at)[0]
    out = {"key": leg.get("key"), "selection": leg.get("selection") or leg.get("key"), "sport": sport.upper(),
           "game": f"{leg.get('away')} @ {leg.get('home')}", "market_key": mk, "side": side, "line": line,
           "dec": float(leg.get("dec") or 0) or None, "is_prop": is_prop, "p_pre": p_pre, "p_now": p_pre, "p_push": 0.0,
           "state": "unknown", "detail": "", "score": "", "current": None, "need": "", "status": "pre", "decided": False,
           "progress": None, "start": leg.get("start")}
    g = find_game(sport, leg)
    if g is None:
        out["detail"] = "game not found in the live feed"
        return out
    out.update(state=g["state"], detail=g["detail"], start=g["start"] or out["start"],
               score=f"{g['away']} {g['away_score']:g}, {g['home']} {g['home_score']:g}" if g["state"] != "pre" else "")
    if g["state"] == "pre":
        return out
    f = remaining_fraction(sport, g)
    box = boxscore(sport, g["event_id"])
    if is_prop:
        row = box["players"].get(str(leg.get("player_key")))
        if row is None and g["state"] == "post":
            out.update(status="void", decided=True, need="not in the box score: the leg is void", p_now=0.0)
            return out
        if row is None and not box["players"]:
            out.update(status="live", need="waiting for the box score")
            return out
        if row is None and sport == "nba":
            # an NBA box score lists everyone who has played: a missing player has not come on yet or is not
            # playing tonight. Treating that as zero so far would write the pick off; if he sits out it is void.
            out.update(status="live", need="not in the box score so far: if he does not play, this pick is void and drops out")
            return out
        value = float((row or {}).get(mk, 0.0))  # in the game but nothing recorded in this stat yet
        out["current"] = value
        p_win, p_push = prop_leg_chance(base, value, f, at=at)
        if g["state"] == "post":
            out.update(status=settle_prop(side, line, value), decided=True)
        elif mk in COUNTS and line is not None and value > line:
            out.update(status="won" if side != "under" else "lost", decided=True)
        else:
            out["status"] = "live"
        if line:
            out["progress"] = max(0.0, min(value / line, 1.0)) if mk != "anytime_td" else float(value >= 1)
    else:
        margin, total = g["home_score"] - g["away_score"], g["home_score"] + g["away_score"]
        out["current"] = total if mk == "total" else (margin if side == "home" else -margin)
        p_win, p_push = game_leg_chance(sport, base, g, box.get("home_wp"), sd, box.get("home_wp0"), at=at)
        if g["state"] == "post":
            out.update(status=settle_game(mk, side, line, margin, total)[0], decided=True)
        elif mk == "total" and line is not None and total > line:
            out.update(status="won" if side == "over" else "lost", decided=True)
        else:
            out["status"] = "live"
        if mk == "total" and line:
            out["progress"] = max(0.0, min(total / line, 1.0))
    if out["decided"]:
        p_win, p_push = float(out["status"] == "won"), float(out["status"] == "push")
    out.update(p_now=float(min(max(p_win, 0.0), 1.0)), p_push=float(min(max(p_push, 0.0), 1.0)), need=_need_text(leg, out["current"] or 0.0, g))
    return out


def bet_view(bet: dict, sd_by_sport: dict | None = None, fresh_legs: dict | None = None) -> dict:
    """A recorded bet as it stands now: every leg, the chance the rest come in, and what the bet is worth.
    The worth counts every way the remaining legs can go: a leg that might still push would drop out of the
    parlay rather than lose it, so it adds its reduced payout times the chance of that."""
    legs = json.loads(bet["legs_json"]) if isinstance(bet.get("legs_json"), str) else list(bet.get("legs") or [])
    views = [leg_view(l, (sd_by_sport or {}).get(str(l["sport"]).lower()), (fresh_legs or {}).get(l.get("key"))) for l in legs]
    stake, dec = float(bet["stake"]), float(bet["dec"])
    payout_dec, p_all, worth, lost = dec, 1.0, 1.0, False
    for v in views:
        if v["status"] == "lost":
            lost = True
        elif v["status"] in ("push", "void"):
            payout_dec /= (v["dec"] or 1.0)  # a pushed or void leg drops out of the parlay, as at a sportsbook
        elif not v["decided"]:
            p_all *= v["p_now"]
            worth *= v["p_now"] + v["p_push"] / (v["dec"] or 1.0)
    n_won = sum(v["status"] == "won" for v in views)
    n_open = sum(not v["decided"] for v in views)
    payout = stake * payout_dec
    if lost:
        status, p_all, worth = "lost", 0.0, 0.0
    elif n_open == 0:
        status = "won" if n_won else "push"
        p_all = worth = 1.0
    else:
        status = "live" if any(v["state"] == "in" for v in views) or n_won else "pre"
    return {"id": bet.get("id"), "placed_at": bet.get("placed_at"), "label": bet.get("legs"), "n": len(views), "legs": views,
            "stake": stake, "odds": bet.get("odds"), "payout": payout, "profit_if_win": payout - stake,
            "p_all": p_all, "value": payout * worth, "worth_share": worth, "status": status, "won": n_won, "open": n_open,
            "lost": sum(v["status"] == "lost" for v in views), "in_play": any(v["state"] == "in" for v in views),
            "same_game": len({(v["sport"], v["game"]) for v in views if not v["decided"]}) < n_open}


# ============================================================================= cash-out
def cash_out(offer: float, payout: float, p: float, bankroll: float | None = None, kelly_fraction: float = 0.25) -> dict:
    """Compare a sportsbook's cash-out offer with what a live bet is worth.

    payout: what the bet returns if everything left wins.  p: the chance of that.
    fair value = payout x p. Sportsbooks usually offer less than that and keep the difference.
    A sure amount is worth more than a risky one of the same average, more so when the swing is large next
    to your bankroll. That is priced with the same risk setting that sizes the stakes (fractional Kelly):
    `risk_value` is the sure amount that is exactly as good as holding the bet. Below it, hold; above it, cash out."""
    p = min(max(float(p), 0.0), 1.0)
    fair = payout * p
    out = {"offer": float(offer), "payout": float(payout), "p": p, "fair": fair,
           "offer_share": (offer / fair) if fair > 0 else None,      # 0.90 = the book keeps 10%
           "breakeven_p": (offer / payout) if payout > 0 else None,  # the offer is the better deal only below this chance
           "risk_value": None, "bankroll_share": None}
    if bankroll and bankroll > 0 and payout > 0:
        g = 1.0 / max(kelly_fraction, 1e-6)  # relative risk aversion: quarter Kelly = 4
        if abs(g - 1.0) < 1e-9:
            ce = math.exp(p * math.log(bankroll + payout) + (1 - p) * math.log(bankroll)) - bankroll
        else:
            u = p * (bankroll + payout) ** (1 - g) + (1 - p) * bankroll ** (1 - g)
            ce = u ** (1 / (1 - g)) - bankroll
        out.update(risk_value=float(ce), bankroll_share=payout / bankroll)
    if fair <= 0:
        out.update(verdict="gone", text="This bet can no longer win, so there is nothing to cash out.")
    elif offer >= fair:
        out.update(verdict="take", text="Cash out: the offer is more than the bet is worth on these numbers.")
    elif out["risk_value"] is not None and offer >= out["risk_value"]:
        out.update(verdict="reasonable", text="Cashing out is reasonable: the offer is below the bet's average value, but the swing is "
                                               "large for your bankroll and the sure money is worth more to you than the gamble.")
    else:
        out.update(verdict="hold", text="Hold: the sportsbook is offering less than the bet is worth"
                                        + (", even allowing for the risk." if out["risk_value"] is not None else "."))
    return out


def open_bets(bets: pd.DataFrame, sd_by_sport: dict | None = None, fresh_legs: dict | None = None, recent_hours: float = 18.0) -> list[dict]:
    """Views of every open bet, plus bets settled in the last few hours so a finished night stays visible."""
    if bets is None or bets.empty:
        return []
    cut = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=recent_hours)
    graded = pd.to_datetime(bets["graded_at"], utc=True, errors="coerce") if "graded_at" in bets else pd.Series(pd.NaT, index=bets.index)
    keep = bets[(bets["status"] == "open") | (graded > cut)]
    out = []
    for b in keep.sort_values("placed_at", ascending=False).to_dict("records"):
        try:
            out.append(bet_view(b, sd_by_sport, fresh_legs))
        except Exception as e:  # one unreadable bet must not hide the others
            out.append({"id": b.get("id"), "label": b.get("legs"), "error": repr(e), "legs": [], "status": "unknown",
                        "stake": float(b.get("stake") or 0), "payout": 0.0, "value": 0.0, "p_all": 0.0, "n": 0})
    return out


def leg_decimal(leg: dict) -> float:
    return float(leg.get("dec") or american_to_decimal(leg.get("odds") or 100))
