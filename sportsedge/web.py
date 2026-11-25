"""Local web UI: a small API around the engine plus a single-page front end (sportsedge/ui/).

    python -m sportsedge ui        ->  http://127.0.0.1:8765

Runs only on this computer (127.0.0.1). The model runs in a background thread; the page polls /api/status.
Simulations stay in memory so the bet slip and newly entered prop lines are priced instantly.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import threading
import time
import traceback
from pathlib import Path

import numpy as np
import pandas as pd
from starlette.middleware.gzip import GZipMiddleware
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import access, betmoves, config, engine, live, markets, oddslog, parlay, plays, slipread, sources, tracker
from .oddsmath import american_to_decimal
from .util import norm_name

UI_DIR = Path(__file__).resolve().parent / "ui"
app = FastAPI(title="sportsedge", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=UI_DIR), name="static")

# ============================================================================= remote access gate
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>sportsedge</title><style>
:root{color-scheme:light dark;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f9f9f7;color:#0b0b0b}
main{width:min(360px,calc(100vw - 32px));background:#fcfcfb;border:1px solid rgba(11,11,11,.1);border-radius:12px;padding:24px}
h1{font-size:19px;margin:0 0 6px}p{margin:0 0 14px;color:#52514e;font-size:14px;line-height:1.45}
label{display:grid;gap:6px;font-size:13px;color:#52514e}
input{font:inherit;padding:9px 10px;border-radius:8px;border:1px solid rgba(11,11,11,.18);background:transparent;color:inherit}
button{font:inherit;font-weight:600;margin-top:12px;width:100%;padding:9px;border-radius:8px;border:0;background:#2a78d6;color:#fff;cursor:pointer}
.err{color:#a92727;font-size:13px;margin:10px 0 0}code{background:rgba(127,127,127,.18);padding:1px 5px;border-radius:4px;font-size:12.5px}
@media (prefers-color-scheme:dark){body{background:#0d0d0d;color:#f0efec}main{background:#1a1a19;border-color:rgba(255,255,255,.1)}
p,label{color:#c3c2b7}input{border-color:rgba(255,255,255,.2)}.err{color:#f0a3a3}}
</style></head><body><main>__BODY__</main></body></html>"""
LOGIN = """<h1>sportsedge</h1><p>Enter your passcode to open the dashboard.</p>
<form method="post" action="/login"><label>Passcode<input type="password" name="passcode" autocomplete="current-password" autofocus required></label>
<button type="submit">Open the dashboard</button></form>__ERR__"""
LOCKED = """<h1>Remote access is locked</h1><p>No passcode has been set yet, so the dashboard cannot be opened from outside the computer it runs on.</p>
<p>On that computer, open a terminal in the project folder, run <code>.\\sportsedge passcode</code> and choose one. Then reload this page.</p>"""


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(PAGE.replace("__BODY__", body), status_code=status, headers={"Cache-Control": "no-store", "X-Sportsedge": "1"})


def _who(request: Request) -> str:
    h = request.headers
    return (h.get("cf-connecting-ip") or h.get("x-real-ip") or (h.get("x-forwarded-for") or "").split(",")[0].strip()
            or (request.client.host if request.client else "?"))


@app.middleware("http")
async def gate(request: Request, call_next):
    """Requests from this computer pass straight through. Anything arriving from outside needs the passcode."""
    if not access.is_remote(request.headers, request.headers.get("host")):
        return await call_next(request)
    path = request.url.path
    if path in ("/login", "/logout"):
        resp = await call_next(request)
    elif not access.has_passcode():
        resp = JSONResponse({"detail": "Remote access is locked: no passcode is set."}, status_code=403) if path.startswith("/api/") else _page(LOCKED, 403)
    elif access.valid_token(request.cookies.get(access.COOKIE)):
        resp = await call_next(request)
    elif path.startswith("/api/"):
        resp = JSONResponse({"detail": "Sign in first"}, status_code=401)
    else:
        resp = RedirectResponse("/login", status_code=303)
    resp.headers["X-Sportsedge"] = "1"
    resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    return resp


@app.get("/login")
def login_page(request: Request):
    if not access.is_remote(request.headers, request.headers.get("host")):
        return RedirectResponse("/", status_code=303)
    if not access.has_passcode():
        return _page(LOCKED, 403)
    if access.valid_token(request.cookies.get(access.COOKIE)):
        return RedirectResponse("/", status_code=303)
    return _page(LOGIN.replace("__ERR__", ""))


@app.post("/login")
async def login_submit(request: Request):
    if not access.has_passcode():
        return _page(LOCKED, 403)
    who = _who(request)
    if access.blocked(who):
        return _page(LOGIN.replace("__ERR__", '<p class="err">Too many wrong tries. Wait 15 minutes and try again.</p>'), 429)
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    if not access.verify((form.get("passcode") or [""])[0]):
        access.failed(who)
        return _page(LOGIN.replace("__ERR__", '<p class="err">That passcode is not right.</p>'), 401)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(access.COOKIE, access.new_token(), max_age=access.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=access.is_remote(request.headers, request.headers.get("host")))
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(access.COOKIE)
    return resp


# the slate is half a megabyte of JSON: compressed it is about a tenth of that, which matters on a phone
app.add_middleware(GZipMiddleware, minimum_size=2000)

LOCK = threading.Lock()
STATE: dict = {"status": "idle", "log": [], "error": None, "run_at": None, "started_at": None,
               "results": [], "legs": {}, "parlays": {}, "plays": {}, "params": {},
               # lines the user corrected by hand: (sport, game_id) -> {"change": {...}, "feed": {...}}
               "overrides": {},
               # prop lines corrected by hand: "sport|player key|market" -> the line, and the feed's line at the time
               "prop_fixes": {},
               # a change arrived while an update was running: run again as soon as it ends
               "again": False,
               # player props the user's own app does not offer, kept out of every suggestion:
               # "SPORT|market|side ('' = both)|player key ('' = every player)" -> what it is, for the page
               "not_offered": {},
               # the automatic odds check: when it last ran, and what the feed showed then
               "checked_at": None, "feed": {}, "changes": 0}


# ============================================================================= helpers
def clean(o):
    """numpy / pandas -> JSON-safe Python."""
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items() if k != "result"}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return clean(o.tolist())
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if not math.isfinite(float(o)) else float(o)
    if o is pd.NaT or o is None:
        return None
    if isinstance(o, (pd.Timestamp, dt.datetime, dt.date)):
        return o.isoformat()
    return o


def records(df: pd.DataFrame) -> list:
    return [] if df is None or df.empty else clean(df.to_dict(orient="records"))


def _log(msg: str):
    STATE["log"].append(f"{dt.datetime.now():%H:%M:%S}  {msg}")
    STATE["log"] = STATE["log"][-200:]


def _run(sports: list[str], days: dict, props: bool, track: bool):
    try:
        results = []
        for sp in sports:
            _log(f"{sp.upper()}: starting")
            res = engine.run_sport(sp, days=days.get(sp, 1), props=props, verbose=_log, prop_lines=_prop_fix_lines())
            results.append(res)
        _prune_prop_fixes(results)
        _mark_prop_fixes(results)
        for (sp, gid), ov in list(STATE["overrides"].items()):
            # a hand-corrected line is kept only while the feed still shows the number it showed then
            res = next((r for r in results if r["sport"] == sp and gid in r.get("_feed_odds", {})), None)
            if res is not None and _feed_line(res, gid) == ov["feed"]:
                engine.reprice_game(res, gid, ov["change"])
                _log(f"{sp.upper()}: kept your corrected line for {gid}")
            else:
                STATE["overrides"].pop((sp, gid), None)
        _save_overrides()
        legs = [l for r in results for l in r["legs"]]
        _log("Searching parlays ...")
        parlays = engine.build_parlays(_on_offer(legs)) if legs else {}
        if track:
            try:
                tracker.grade(verbose=False)
                n = tracker.log_slate(results, parlays)
                _log(f"Logged {n} priced picks to history")
            except Exception as e:  # tracking must never block the slate
                _log(f"Tracking skipped: {e!r}")
        with LOCK:
            STATE.update(results=results, legs={l["key"]: l for l in _every_leg(results)}, parlays=parlays,
                         plays=_suggestions(results), status="ready",
                         run_at=dt.datetime.now().isoformat(timespec="seconds"), error=None)
        _PROPS_CHECKED["at"] = time.time()  # this update has just read the prop feed
        try:
            check_odds()
        except Exception as e:
            _log(f"Odds check skipped: {e!r}")
        _log("Done")
    except Exception as e:
        _log(f"Failed: {e!r}")
        with LOCK:
            STATE.update(status="error" if not STATE["results"] else "ready", error=f"{e!r}\n{traceback.format_exc(limit=6)}")
    with LOCK:
        again, STATE["again"] = STATE["again"], False
    if again:  # a line was corrected while this update ran, and this update began before the change
        start_run()


def _overrides_path() -> Path:
    return tracker.history_dir() / "line_corrections.json"


# ----------------------------------------------------------------------------- prop lines corrected by hand
def _prop_fixes_path() -> Path:
    return tracker.history_dir() / "prop_corrections.json"


def _save_prop_fixes():
    try:
        _prop_fixes_path().write_text(json.dumps(STATE["prop_fixes"], indent=1), encoding="utf-8")
    except Exception as e:
        _log(f"Could not save prop corrections: {e!r}")


def _load_prop_fixes():
    try:
        p = _prop_fixes_path()
        if p.exists():
            STATE["prop_fixes"].update(json.loads(p.read_text(encoding="utf-8")))
    except Exception as e:
        _log(f"Could not read saved prop corrections: {e!r}")


def _fix_id(sport: str, player_key: str, market: str) -> str:
    return f"{str(sport).lower()}|{player_key}|{market}"


def _prop_fix_lines() -> pd.DataFrame | None:
    """The corrections as prop lines for the engine. It puts them ahead of the feed's own lines and drops any
    whose line the feed has moved since (see engine.current_fixes)."""
    fx = list(STATE["prop_fixes"].values())
    if not fx:
        return None
    df = pd.DataFrame([{"sport": f["sport"], "player": f["player"], "key": f["key"], "market": f["market"],
                        "line": f["line"], "over_odds": f.get("over_odds"), "under_odds": f.get("under_odds"),
                        "book": "corrected", "feed_line": f.get("feed_line")} for f in fx])
    for c in ("line", "over_odds", "under_odds", "feed_line"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _prune_prop_fixes(results: list[dict]):
    """A correction ends when its game is no longer to come, or when the feed shows a different line from the one
    it showed when the correction was made: by then the feed has caught up with the sportsbook."""
    before = len(STATE["prop_fixes"])
    for fid, f in list(STATE["prop_fixes"].items()):
        res = next((r for r in results if r["sport"] == f["sport"]), None)
        if res is None:  # that sport was not part of this update
            continue
        slate = res.get("slate")
        gone = slate is None or slate.empty or str(f.get("game_id")) not in set(slate["game_id"].astype(str))
        now = (res.get("_prop_feed") or {}).get((f["key"], f["market"]))
        moved = now is not None and f.get("feed_line") is not None and abs(now - float(f["feed_line"])) > 1e-9
        if gone or moved:
            STATE["prop_fixes"].pop(fid)
        else:
            _log(f"{f['sport'].upper()}: kept your corrected line for {f['player']} ({float(f['line']):g})")
    if len(STATE["prop_fixes"]) != before:
        _save_prop_fixes()


def _store_fix(res: dict, player: str, key: str, market: str, game_id, line: float, over=None, under=None):
    """Keep a corrected line, with the line the feed shows for it now (or showed when it was first corrected)."""
    sport = res["sport"]
    fid = _fix_id(sport, key, market)
    old = STATE["prop_fixes"].get(fid) or {}
    feed = (res.get("_prop_feed") or {}).get((key, market), old.get("feed_line"))
    num = lambda v: None if v is None or pd.isna(v) else float(v)  # noqa: E731
    STATE["prop_fixes"][fid] = {"sport": sport, "player": player, "key": key, "market": market, "line": float(line),
                                "over_odds": num(over), "under_odds": num(under), "feed_line": num(feed),
                                "game_id": str(game_id), "at": dt.datetime.now().isoformat(timespec="seconds")}


def _mark_prop_fixes(results: list[dict]):
    """Mark the picks priced at a corrected line, with the feed's line beside it, for the page."""
    for r in results:
        for l in (r.get("legs") or []) + (r.get("lean_legs") or []):
            f = STATE["prop_fixes"].get(_fix_id(l.get("sport", ""), l.get("player_key", ""), l.get("market_key", "")))
            if f is not None and l.get("line") is not None and abs(float(l["line"]) - float(f["line"])) < 1e-9:
                l["corrected"], l["feed_line"] = True, f.get("feed_line")


def _save_overrides():
    """Corrections are kept on disk so restarting the dashboard does not lose them."""
    try:
        data = {f"{sp}|{gid}": ov for (sp, gid), ov in STATE["overrides"].items()}
        _overrides_path().write_text(json.dumps(data, indent=1), encoding="utf-8")
    except Exception as e:
        _log(f"Could not save line corrections: {e!r}")


def _load_overrides():
    try:
        p = _overrides_path()
        if p.exists():
            for k, ov in json.loads(p.read_text(encoding="utf-8")).items():
                sp, gid = k.split("|", 1)
                STATE["overrides"][(sp, gid)] = ov
    except Exception as e:
        _log(f"Could not read saved line corrections: {e!r}")


def _feed_line(res: dict, game_id) -> dict:
    o = res["_feed_odds"][game_id]
    return {k: (None if o.get(k) is None or pd.isna(o.get(k)) else float(o.get(k))) for k in ("spread_home", "total")}


# ============================================================================= every pick, and the suggestions
def _every_leg(results: list[dict]) -> list[dict]:
    """Priced picks plus the unpriced props at their assumed price. Where both exist for one bet (a prop the
    user priced from their own sportsbook), the priced one is kept."""
    priced = [l for r in results for l in r["legs"]]
    have = {l["key"] for l in priced}
    return priced + [l for r in results for l in r.get("lean_legs") or [] if l["key"] not in have]


def _not_offered_path() -> Path:
    return tracker.history_dir() / "not_offered.json"


def _save_not_offered():
    try:
        _not_offered_path().write_text(json.dumps(STATE["not_offered"], indent=1), encoding="utf-8")
    except Exception as e:
        _log(f"Could not save the bets marked as not offered: {e!r}")


def _load_not_offered():
    try:
        p = _not_offered_path()
        if p.exists():
            STATE["not_offered"].update(json.loads(p.read_text(encoding="utf-8")))
    except Exception as e:
        _log(f"Could not read the bets marked as not offered: {e!r}")


SCOPES = ("stat", "side", "player")  # a whole stat (overs and unders) / one side of it / one player's side of it


def _rule_id(leg: dict, scope: str = "stat") -> str:
    return "|".join([str(leg.get("sport", "")).upper(), str(leg.get("market_key", "")),
                     "" if scope == "stat" else str(leg.get("side", "")),
                     str(leg.get("player_key", "")) if scope == "player" else ""])


def _offered(leg: dict) -> bool:
    """False for a player prop the user marked as not on their app: a whole stat (no over/under on pass attempts),
    one side of it (no unders on threes), or one player's. The feed is a sportsbook's menu, and an app can offer
    less of it."""
    rules = STATE["not_offered"]
    if not rules or not leg.get("player_key"):
        return True
    if leg.get("rung") is not None:
        # an "N or more" bet is its own listing on an app: only a mark made on one hides it, never a mark on
        # the stat's over/under (an app can have "2+ threes" and no over/under on threes)
        plus = {**leg, "side": "plus"}
        return not any(_rule_id(plus, sc) in rules for sc in ("side", "player"))
    return not any(_rule_id(leg, sc) in rules for sc in SCOPES)


def _on_offer(legs: list[dict]) -> list[dict]:
    return [l for l in legs if _offered(l)]


def _rung_legs(results: list[dict]) -> list[dict]:
    return [l for r in results for l in r.get("rung_legs") or []]


def _suggestions(results: list[dict]) -> dict:
    try:
        return plays.build(_on_offer(_every_leg(results) + _rung_legs(results)))
    except Exception as e:  # suggestions are an extra: the slate must still load without them
        _log(f"Suggestions skipped: {e!r}")
        return {}


_STARTED: dict = {"at": 0.0, "legs": {}}


def _started() -> dict:
    """key -> pick, for games that have started and are not settled, at their last number before the start.
    Read from the pick history at most once a minute. A pick the slate still holds is left to the slate."""
    if time.time() - _STARTED["at"] > 60:
        try:
            _STARTED.update(at=time.time(), legs={l["key"]: l for l in tracker.started_legs()})
        except Exception as e:
            _log(f"Started games skipped: {e!r}")
            _STARTED.update(at=time.time(), legs={})
    return {k: l for k, l in _STARTED["legs"].items() if k not in STATE["legs"]}


def _at_line(leg: dict, line: float) -> dict:
    """The pick at another line. A game still to come is priced at that line. One that has started cannot be
    (its simulation is gone), so it keeps its last chance with the tool's line noted beside it."""
    if not leg.get("started"):
        res = next((r for r in STATE["results"] if r["sport"] == str(leg["sport"]).lower()), None)
        if res is not None:
            try:
                ov = STATE["overrides"].get((res["sport"], leg["game_id"])) or {}
                new = engine.price_at(res, leg, line, ov.get("change"))
                if new is not None:
                    return new
            except Exception as e:
                _log(f"Could not price {leg.get('selection')} at {line}: {e!r}")
    return tracker.with_line(leg, line)


def _picks(keys: list[str], lines: dict | None = None) -> list[dict]:
    """The picks for these keys: from the slate, or for a game that has started, from its last number.
    lines: the line to take a pick at, where the bettor's is not the tool's."""
    late, out = _started(), []
    for k in keys:
        leg = STATE["legs"].get(k) or late.get(k)
        if leg is None:
            continue
        mine = (lines or {}).get(k)
        if mine is not None and leg.get("line") is not None and abs(float(mine) - float(leg["line"])) > 1e-9:
            leg = _at_line(leg, float(mine))
        out.append(leg)
    return out


def _with_live(legs: list[dict]) -> tuple[list[dict], dict]:
    """The picks with a game already in play (or over) at the tool's live chance instead of the pre-game one,
    so a slip that mixes started and coming games is priced as it stands. Returns the picks to price and, per
    started pick, what the live feed showed. The pre-game chance (p_cond) is kept: the Live page starts from it."""
    if not any(betmoves.started(l) for l in legs):
        return legs, {}
    sd, _ = _live_context()
    out, info = [], {}
    for l in legs:
        if not betmoves.started(l):
            out.append(l)
            continue
        try:
            v = live.leg_view(l, sd.get(str(l.get("sport", "")).lower()), None)
        except Exception as e:  # no live chance: keep the last pre-game one rather than refuse the slip
            _log(f"Live chance for {l.get('selection')} skipped: {e!r}")
            v = None
        if v is None or v["state"] not in ("in", "post"):
            out.append(l)
            continue
        info[l["key"]] = {"p_now": v["p_now"], "p_pre": v["p_pre"], "p_push": v["p_push"], "score": v["score"],
                          "detail": v["detail"], "status": v["status"], "need": v["need"]}
        # result=None: the pre-game simulation no longer describes this game, so it is priced on its own
        out.append({**l, "p_win": v["p_now"], "p_push": v["p_push"], "result": None,
                    "p_at_bet": v["p_now"], "score_at_bet": v["score"] or v["detail"]})
    return out, info


# ============================================================================= automatic odds check
def check_odds() -> int:
    """Look at the feed's game lines, log any change, and return how many games changed. Cheap: one request
    per day of schedule, no model run."""
    today = dt.date.today()
    changed, feed = 0, {}
    for sport, days in (("nfl", 7), ("nba", 1)):
        try:
            up = sources.espn_scoreboard(sport, today, today + dt.timedelta(days=days - 1))
        except Exception:
            continue
        if up.empty:
            continue
        changed += oddslog.record(sport, up[up["state"] == "pre"])  # pre-game numbers only: never an in-play line
        for g in up[up["state"] == "pre"].itertuples():
            feed[(sport, g.home, g.away)] = {k: (None if pd.isna(getattr(g, k)) else float(getattr(g, k))) for k in oddslog.FIELDS}
    STATE.update(checked_at=dt.datetime.now().isoformat(timespec="seconds"), feed=feed, changes=STATE["changes"] + changed)
    _bet_moves()  # keep the last number seen for each recorded pick up to date, whether or not anyone is looking
    return changed


# ============================================================================= recorded bets against the line now
def _game_line(sport: str, leg: dict) -> dict | None:
    """The line to compare a game pick with: the user's own correction if there is one, otherwise the newest
    numbers from the feed, otherwise the ones the picks were priced at."""
    for r in STATE["results"]:
        games = r.get("games")
        if r["sport"] != sport or games is None or games.empty:
            continue
        hit = games[games["game_id"].astype(str) == str(leg.get("game_id"))]
        if hit.empty:
            continue
        g = hit.iloc[0]
        if bool(g.get("edited")):
            return g["line"]
        return STATE["feed"].get((sport, g["home"], g["away"])) or g["line"]
    return None


def _leg_now(leg: dict) -> dict | None:
    """A recorded pick's number right now, or None when there is nothing newer to compare it with."""
    sport = str(leg.get("sport", "")).lower()
    if leg.get("market_key") in betmoves.GAME_MARKETS:
        return betmoves.game_now(leg, _game_line(sport, leg))
    fresh = STATE["legs"].get(leg.get("key"))
    if fresh is not None:
        if fresh.get("book") in ("manual", "corrected"):  # a number the user typed in is not a newer reading of the market
            return None
        return {"line": fresh.get("line"), "odds": fresh.get("odds"), "p_fair": fresh.get("p_fair")}
    for r in STATE["results"]:  # a prop that arrives as a line without prices
        if r["sport"] != sport:
            continue
        for pl in r.get("prop_lines") or []:
            if (str(pl.get("game_id")) == str(leg.get("game_id")) and pl.get("player_key") == leg.get("player_key")
                    and pl.get("market_key") == leg.get("market_key")):
                return {"line": pl.get("line"), "odds": None, "p_fair": None}
    return None


def _bet_moves(bets: pd.DataFrame | None = None) -> dict:
    """{bet id: {pick key: movement since the bet}} for every recorded bet."""
    try:
        return betmoves.update(tracker.load_bets() if bets is None else bets, _leg_now)
    except Exception as e:  # an extra: it must never break the page or the odds check
        _log(f"Movement since your bets skipped: {e!r}")
        return {}


def _stale_games() -> int:
    """Games whose spread or total in the feed now differs from the one the current picks were priced at."""
    n = 0
    for r in STATE["results"]:
        for gid, o in (r.get("_feed_odds") or {}).items():
            row = r["_feed_rows"][gid]
            f = STATE["feed"].get((r["sport"], row["home"], row["away"]))
            if f and any(f.get(k) is not None and o.get(k) is not None and not pd.isna(o.get(k)) and abs(f[k] - float(o[k])) > 1e-9
                         for k in ("spread_home", "total")):
                n += 1
    return n


_PROPS_CHECKED = {"at": time.time()}  # the start-up update has just read the prop feed


def _moved_props(now: pd.Timestamp | None = None) -> int:
    """Prop lines in the feed, for games still to come, that differ from the ones the current picks were priced
    at, or that are new since. The feed is read through its own 15-minute cache, so this costs little."""
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    n = 0
    for r in STATE["results"]:
        seen, up = r.get("_prop_feed"), r.get("_prop_games")
        if seen is None or up is None or up.empty:
            continue
        up = up[pd.to_datetime(up["start"], utc=True) > now]
        if up.empty:
            continue
        cur = engine.line_map(engine.feed_prop_lines(r["sport"], up))
        n += sum(1 for k, v in cur.items() if k not in seen or abs(seen[k] - v) > 1e-9)
    return n


def _odds_loop():
    while True:
        time.sleep(max(config.ODDS_CHECK_MINUTES, 1) * 60)
        try:
            if STATE["status"] == "running":
                continue
            check_odds()
            try:
                _live_board()  # logs each new live price beside the tool's estimate, for testing against results
                live.grade_log()  # and settles the logged prices of games that have finished
            except Exception as e:
                _log(f"Live price log skipped: {e!r}")
            last = dt.datetime.fromisoformat(STATE["run_at"]) if STATE["run_at"] else None
            old_enough = last is None or (dt.datetime.now() - last).total_seconds() >= config.AUTO_UPDATE_MINUTES * 60
            if old_enough and _stale_games():
                _log("A line moved in the feed: updating automatically")
                start_run()
            elif old_enough and time.time() - _PROPS_CHECKED["at"] >= config.PROP_CHECK_MINUTES * 60:
                # player props move on their own, without the game's spread or total moving
                _PROPS_CHECKED["at"] = time.time()
                n = _moved_props()
                if n:
                    _log(f"{n} prop line{'s' if n > 1 else ''} moved in the feed: updating automatically")
                    start_run()
        except Exception as e:  # the check must never take the dashboard down
            _log(f"Odds check failed: {e!r}")


def start_run(sports=("nfl", "nba"), days=None, props=True, track=True, queue=False) -> bool:
    """queue: when an update is already running, run once more as soon as it ends (it began before a change)."""
    with LOCK:
        if STATE["status"] == "running":
            STATE["again"] = STATE["again"] or queue
            return False
        STATE.update(status="running", log=[], started_at=time.time(),
                     params={"sports": list(sports), "days": days or {"nfl": 7, "nba": 1}, "props": props})
    threading.Thread(target=_run, args=(list(sports), days or {"nfl": 7, "nba": 1}, props, track), daemon=True).start()
    return True


# ============================================================================= API
class RunReq(BaseModel):
    sports: list[str] = ["nfl", "nba"]
    nfl_days: int = 7
    nba_days: int = 1
    props: bool = True


class SlipReq(BaseModel):
    keys: list[str]
    lines: dict[str, float] = {}  # pick key -> the line to price it at, where it is not the tool's


class ImportReq(BaseModel):
    text: str


class PropLine(BaseModel):
    sport: str
    player: str
    market: str
    line: float
    over_odds: float | None = None
    under_odds: float | None = None
    # True: this is the reference sportsbook's own current line (the feed is behind), so it replaces the
    # reference. False: a line from another sportsbook, priced against the reference.
    same_book: bool = False


class CashOutReq(BaseModel):
    bet_id: str
    offer: float
    bankroll: float | None = None


class GameLineReq(BaseModel):
    sport: str
    game_id: str
    spread_home: float | None = None
    spread_home_odds: float | None = None
    spread_away_odds: float | None = None
    total: float | None = None
    over_odds: float | None = None
    under_odds: float | None = None
    ml_home: float | None = None
    ml_away: float | None = None
    reset: bool = False


class PriceReq(BaseModel):
    lines: list[PropLine]
    track: bool = True


class OfferedReq(BaseModel):
    key: str | None = None      # a prop pick the user's app does not offer
    scope: str = "stat"         # "stat": its whole stat, overs and unders. "side": that side of it. "player": this player's
    restore: str | None = None  # the id of a rule to take away again
    rung: bool = False          # the pick is held as an "N or more" bet: mark those, not the stat's over/under


class PropFixReq(BaseModel):
    key: str                    # a prop pick whose line the sportsbook has moved
    line: float | None = None   # the line the sportsbook shows now
    reset: bool = False         # go back to the feed's line


class BetReq(BaseModel):
    keys: list[str]
    odds: float
    stake: float
    note: str = ""
    lines: dict[str, float] = {}  # pick key -> the line the bet was really made at, where it differs


@app.get("/")
def index():
    # version the asset URLs by file time so a code update is never hidden behind a cached copy
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("styles.css", "app.js"):
        v = int((UI_DIR / name).stat().st_mtime)
        html = html.replace(f"/static/{name}", f"/static/{name}?v={v}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.post("/api/run")
def api_run(req: RunReq):
    sports = [s for s in req.sports if s in ("nfl", "nba")] or ["nfl", "nba"]
    started = start_run(sports, {"nfl": req.nfl_days, "nba": req.nba_days}, req.props)
    return {"started": started, **api_status(None)}


def _share_info() -> dict | None:
    try:
        p = tracker.history_dir() / "share.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if time.time() - float(d.get("beat", 0)) < 90 else None  # the share command refreshes it while it runs
    except Exception:
        return None


@app.get("/api/status")
def api_status(request: Request = None):
    elapsed = time.time() - STATE["started_at"] if STATE["started_at"] and STATE["status"] == "running" else None
    remote = bool(request is not None and access.is_remote(request.headers, request.headers.get("host")))
    share = _share_info()
    return {"status": STATE["status"], "log": STATE["log"][-12:], "run_at": STATE["run_at"], "error": STATE["error"],
            "elapsed": elapsed, "params": STATE["params"], "odds_api": bool(config.ODDS_API_KEY),
            "remote": remote, "share_url": (share or {}).get("url") if not remote else None,
            "has_passcode": access.has_passcode() if not remote else True}


# what the page needs of a prop that has no price: enough to list it, add it to the slip and record it
LEAN_FIELDS = ["key", "sport", "game_id", "game", "home", "away", "start", "date", "subject", "book", "selection", "market",
               "market_key", "side", "player_key", "group", "line", "odds", "dec", "p_win", "p_push", "p_cond", "ev", "proj",
               "proj_med", "sim_over", "lean", "flag", "assumed", "corrected", "feed_line", "sim_only"]


RUNG_FIELDS = LEAN_FIELDS + ["rung", "base_key", "p_fair", "edge"]


STARTED_FIELDS = ["key", "sport", "game_id", "game", "home", "away", "start", "date", "subject", "book", "selection",
                  "market", "market_key", "side", "player_key", "group", "line", "odds", "dec", "p_win", "p_push", "p_cond",
                  "proj", "started"]


@app.get("/api/slate")
def api_slate():
    out = {"run_at": STATE["run_at"], "sports": [], "parlays": {}, "settings": {
        "kelly_fraction": config.KELLY_FRACTION, "kelly_cap": config.KELLY_CAP, "min_edge": config.MIN_EDGE_FLAG,
        "longshot_odds": config.LONGSHOT_ODDS, "longshot_min_edge": config.LONGSHOT_MIN_EDGE,
        "prop_gap_flag": markets.PROP_GAP_FLAG, "prop_lean": markets.PROP_LEAN,
        "prop_weight": {sp: tracker.prop_weight(sp) for sp in ("nfl", "nba")},
        "prop_weight_source": {sp: tracker.prop_prior(sp)[1] for sp in ("nfl", "nba")}}}
    for r in STATE["results"]:
        if r.get("model") is None:
            out["sports"].append({"sport": r["sport"], "games": [], "legs": [], "proj": [], "model": None})
            continue
        out["sports"].append({"sport": r["sport"], "games": records(r["games"]), "legs": clean(r["legs"]),
                              "proj": records(r["proj"]), "model": clean(r["model"]),
                              "markets": r.get("labels", {}), "prop_book": r.get("prop_book") or "",
                              "prop_lines": clean(r.get("prop_lines") or []),
                              "ladders": r.get("ladders") or {},
                              "prop_check": clean(r.get("prop_check"))})
    for mode, df in (STATE["parlays"] or {}).items():
        out["parlays"][mode] = records(df) if df is not None else []
    # suggested picks and parlays, with the assumed-price picks they use (the priced ones are listed above)
    # games that have started: their picks at the last number before the start, so a bet can still be recorded
    late: dict = {}
    for l in _started().values():
        g = late.setdefault((l["sport"], l["game_id"]), {"sport": l["sport"].lower(), "game_id": l["game_id"], "game": l["game"],
                                                         "home": l["home"], "away": l["away"], "start": l["start"], "legs": []})
        g["legs"].append({f: l.get(f) for f in STARTED_FIELDS})
    out["started"] = clean(sorted(late.values(), key=lambda g: g["start"]))
    try:
        out["live_test"] = clean(live.log_summary())
    except Exception as e:
        _log(f"Live test summary skipped: {e!r}")
        out["live_test"] = None
    sug = STATE.get("plays") or {}
    lean = [{f: l.get(f) for f in LEAN_FIELDS} for l in STATE["legs"].values() if l.get("assumed")]
    used = set(sug.get("keys") or [])  # the "N or more" picks the suggestions use: the page needs only those
    rung = [{f: l.get(f) for f in RUNG_FIELDS} for l in _rung_legs(STATE["results"]) if l["key"] in used]
    out["plays"] = clean({"games": sug.get("games") or [], "groups": sug.get("groups") or [], "days": sug.get("days") or [],
                          "lean_legs": lean, "rung_legs": rung, "assumed_odds": config.ASSUMED_PROP_ODDS})
    out["not_offered"] = list(STATE["not_offered"].values())
    return out


@app.post("/api/slip/import")
def api_slip_import(req: ImportReq):
    """Read a bet slip pasted as text: which of the tool's picks it holds, its odds and its stake. Nothing is
    recorded here: the page fills the bet slip with what was found, for checking before it is recorded."""
    pool = {**_started(), **STATE["legs"]}  # games still to come, and games that have started
    return clean(slipread.read(req.text[:20000], list(pool.values())))


@app.post("/api/slip")
def api_slip(req: SlipReq):
    legs = _picks(req.keys, req.lines)
    if not legs:
        return {"n": 0}
    # a pick taken at another line: what it is called there, its chance there, and whether it could be priced
    alts = {l["key"]: {"line": l.get("line"), "selection": l.get("selection"), "p_cond": l.get("p_cond"),
                       "priced": l.get("alt_of") is not None,
                       # an "N or more" bet the sportsbook prices: that price belongs to this line
                       "own_price": bool(l.get("own_price")), "odds": l.get("odds") if l.get("own_price") else None}
            for l in legs if l["key"] in req.lines and
            (l.get("alt_of") is not None or l.get("chance_line") is not None)}
    legs, now = _with_live(legs)
    return clean({**parlay.evaluate(legs), "alts": alts, "live": now})


@app.post("/api/props/price")
def api_price(req: PriceReq):
    out, errors = [], []
    by_sport: dict[str, list] = {}
    for ln in req.lines:
        by_sport.setdefault(ln.sport.lower(), []).append(ln)
    for sport, lns in by_sport.items():
        res = next((r for r in STATE["results"] if r["sport"] == sport and r.get("prop_sims")), None)
        if res is None:
            errors.append(f"No {sport.upper()} simulation loaded - refresh the slate first.")
            continue
        df = pd.DataFrame([{"sport": sport, "player": l.player, "key": norm_name(l.player), "market": l.market,
                            "line": l.line, "over_odds": np.nan if l.over_odds is None else l.over_odds,
                            "under_odds": np.nan if l.under_odds is None else l.under_odds,
                            "book": "corrected" if l.same_book else "manual", "same": l.same_book}
                           for l in lns])
        known = {k for players in res["prop_sims"].values() for k in players}
        for r in df.itertuples():
            if r.key not in known:
                errors.append(f"{r.player}: not found in today's {sport.upper()} projections")
        centre = res.get("prop_centre") or {"*": 0.0}
        same, other = df[df["same"]], df[~df["same"]]
        legs = []
        if not same.empty:  # the reference book's own line has moved: it becomes the reference
            fixed_legs = markets.prop_legs(sport, same, res["prop_sims"], res["slate"], res["labels"], centre)
            legs += fixed_legs
            # and it is kept through later updates, until the feed's line moves or the game starts
            for r in same.itertuples():
                gid = next((l["game_id"] for l in fixed_legs if l.get("player_key") == r.key and l.get("market_key") == r.market), None)
                if gid is not None:
                    _store_fix(res, r.player, r.key, r.market, gid, r.line, r.over_odds, r.under_odds)
            _save_prop_fixes()
            _mark_prop_fixes([{"legs": fixed_legs}])
            res.setdefault("prop_ref", {}).update(markets.prop_reference(same))
            fixed = set(zip(same["key"], same["market"]))
            res["prop_lines"] = [p for p in res.get("prop_lines") or [] if (p["player_key"], p["market_key"]) not in fixed]
        if not other.empty:
            legs += markets.prop_legs(sport, other, res["prop_sims"], res["slate"], res["labels"], centre,
                                      res.get("prop_ref") or {})
        for l in legs:
            STATE["legs"][l["key"]] = l
            res["legs"] = [x for x in res["legs"] if x["key"] != l["key"]] + [l]
        if req.track and legs:
            tracker.log_slate([{"legs": legs}], {})
        out += legs
    if out:  # a line priced by hand can change which picks and parlays are best
        STATE["plays"] = _suggestions(STATE["results"])
    return {"legs": clean(out), "errors": errors}


@app.post("/api/props/correct")
def api_prop_correct(req: PropFixReq):
    """The sportsbook has a prop at a line the feed has not caught up with (or, with reset, go back to the feed's
    line). The correction is saved and the slate updated, so every pick, suggestion and parlay is priced at that
    line, and later updates keep it until the feed's own line moves or the game starts. The feed's price belonged
    to its line, so a corrected line is priced like a line without a price until odds are entered for it."""
    leg = STATE["legs"].get(req.key)
    if leg is None or not leg.get("player_key") or not leg.get("market_key"):
        raise HTTPException(404, "That prop is not on the slate any more: its game may have started. Refresh the page.")
    res = next((r for r in STATE["results"] if r["sport"] == str(leg["sport"]).lower()), None)
    if res is None:
        raise HTTPException(404, "That sport is not loaded - refresh first.")
    fid = _fix_id(res["sport"], leg["player_key"], leg["market_key"])
    feed = (res.get("_prop_feed") or {}).get((leg["player_key"], leg["market_key"]))
    if req.reset or (req.line is not None and feed is not None and abs(req.line - feed) < 1e-9):
        STATE["prop_fixes"].pop(fid, None)  # back to the feed (typing the feed's own line is the same thing)
    else:
        if req.line is None or not math.isfinite(req.line) or req.line < 0 or abs(req.line * 2 - round(req.line * 2)) > 1e-9:
            raise HTTPException(400, "Type the line your sportsbook shows, such as 3.5.")
        _store_fix(res, leg.get("subject") or leg["player_key"], leg["player_key"], leg["market_key"], leg["game_id"], req.line)
    _save_prop_fixes()
    start_run(queue=True)
    return api_status(None)


@app.post("/api/offered")
def api_offered(req: OfferedReq):
    """Mark a player prop as not on the user's app (or take a mark away). The feed is a sportsbook's menu and an
    app can offer less of it, so marked bets are left out of the suggested picks and parlays from then on.
    Nothing is re-simulated: the suggestions are rebuilt from the picks already priced."""
    if req.restore is not None:
        STATE["not_offered"].pop(req.restore, None)
    else:
        leg = STATE["legs"].get(req.key or "") or _started().get(req.key or "")
        if leg is None or not leg.get("player_key"):
            raise HTTPException(404, "That player prop is not on the slate any more. Refresh the page.")
        scope = req.scope if req.scope in SCOPES else "stat"
        if req.rung:
            leg, scope = {**leg, "side": "plus"}, "player" if scope == "player" else "side"
        rid = _rule_id(leg, scope)
        one = scope == "player"
        STATE["not_offered"][rid] = {"id": rid, "sport": str(leg["sport"]).upper(), "market_key": leg["market_key"],
                                     "market": leg.get("market") or leg["market_key"],
                                     "side": "" if scope == "stat" else (leg.get("side") or ""),
                                     "player_key": leg["player_key"] if one else "",
                                     "player": (leg.get("subject") or leg["player_key"]) if one else ""}
    _save_not_offered()
    with LOCK:
        legs = [l for r in STATE["results"] for l in r.get("legs") or []]
        if legs:
            STATE["parlays"] = engine.build_parlays(_on_offer(legs))
        STATE["plays"] = _suggestions(STATE["results"])
    return {"not_offered": list(STATE["not_offered"].values())}


@app.post("/api/games/line")
def api_game_line(req: GameLineReq):
    """Correct a game's spread / total / odds when the feed is behind the sportsbook, or reset it."""
    sport = req.sport.lower()
    res = next((r for r in STATE["results"] if r["sport"] == sport and req.game_id in r.get("_feed_odds", {})), None)
    if res is None:
        raise HTTPException(404, "That game is no longer loaded - refresh first.")
    change = None if req.reset else {k: getattr(req, k) for k in engine.LINE_FIELDS if getattr(req, k) is not None}
    for k in ("spread_home_odds", "spread_away_odds", "over_odds", "under_odds", "ml_home", "ml_away"):
        v = (change or {}).get(k)
        if v is not None and -100 < v < 100:
            raise HTTPException(400, "Odds must be American odds, such as -110 or +120.")
    with LOCK:
        gl = engine.reprice_game(res, req.game_id, change)
        if change:
            STATE["overrides"][(sport, req.game_id)] = {"change": change, "feed": _feed_line(res, req.game_id)}
        else:
            STATE["overrides"].pop((sport, req.game_id), None)
        _save_overrides()
        legs = [l for r in STATE["results"] for l in r["legs"]]
        STATE["legs"] = {l["key"]: l for l in _every_leg(STATE["results"])}
        STATE["parlays"] = engine.build_parlays(_on_offer(legs))
        STATE["plays"] = _suggestions(STATE["results"])
    try:  # the logged pick should carry the line that was really on offer
        tracker.log_slate([{"legs": gl}], {})
    except Exception as e:
        _log(f"Tracking skipped: {e!r}")
    return {"legs": clean(gl)}


def _live_context() -> tuple[dict, dict]:
    """Score spreads from the fitted models and the current pre-game legs, for the live estimates."""
    sd = {}
    for r in STATE["results"]:
        m = r.get("model") or {}
        if m.get("sd_margin") and m.get("sd_total"):
            sd[r["sport"]] = {"margin": float(m["sd_margin"]), "total": float(m["sd_total"])}
    return sd, STATE["legs"]


@app.get("/api/live")
def api_live():
    """Every open bet as it stands right now."""
    sd, legs = _live_context()
    recorded = tracker.load_bets()
    bets = live.open_bets(recorded, sd, legs)
    moves = _bet_moves(recorded)
    for b in bets:  # how the sportsbook's number for each pick has moved since the bet was recorded
        mine = moves.get(str(b.get("id"))) or {}
        for v in b.get("legs") or []:
            v["move"] = mine.get(str(v.get("key")))
        b["moves"] = betmoves.tally(mine)
    try:
        scores = live.scores()
    except Exception:  # the scoreboard is an extra: never let it take the bets down with it
        scores = []
    try:  # the two money cards: what everything can still pay, and what has been earned
        board, earned = live.payout_board(bets), live.earnings(recorded, bets)
    except Exception as e:  # an extra: never let it take the bets down with it
        _log(f"Payout and earnings cards skipped: {e!r}")
        board, earned = None, []
    return clean({"bets": bets, "scores": scores, "board": board, "earnings": earned,
                  "updated": dt.datetime.now().isoformat(timespec="seconds"),
                  "in_play": any(b.get("in_play") for b in bets) or any(g["state"] == "in" for g in scores),
                  "kelly_fraction": config.KELLY_FRACTION})


def _live_board(log: bool = True) -> dict:
    """Live prices for games in play, matched to the picks of those games at their last pre-game number."""
    sd, _ = _live_context()
    _started()  # refresh the picks of games that have started
    pre = {**_STARTED["legs"], **{k: l for k, l in STATE["legs"].items() if not l.get("assumed")}}
    return live.board(list(pre.values()), sd, log=log)


@app.get("/api/live/board")
def api_live_board():
    """The sportsbook's live prices for games in play, and for each open bet with one pick left, the hedge
    that locks in the same return either way."""
    try:
        b = _live_board()
        sd, legs = _live_context()
        hedges = {}
        for bet in live.open_bets(tracker.load_bets(), sd, legs):
            h = live.hedge(bet, b["rows"]) if not bet.get("error") else None
            if h:
                hedges[str(bet["id"])] = h
        return clean({"games": b["games"], "hedges": hedges, "checked_at": dt.datetime.now().isoformat(timespec="seconds")})
    except Exception as e:  # an extra: the Live page must still load without it
        _log(f"Live prices skipped: {e!r}")
        return {"games": [], "hedges": {}, "error": repr(e)}


@app.post("/api/live/cashout")
def api_cashout(req: CashOutReq):
    """Compare the sportsbook's cash-out offer with what the bet is worth right now."""
    bets = tracker.load_bets()
    row = bets[bets["id"].astype(str) == req.bet_id]
    if row.empty:
        raise HTTPException(404, "Bet not found")
    if req.offer < 0:
        raise HTTPException(400, "The offer must be zero or more.")
    sd, legs = _live_context()
    v = live.bet_view(row.iloc[0].to_dict(), sd, legs)
    return clean({**live.cash_out(req.offer, v["payout"], v["worth_share"], req.bankroll, config.KELLY_FRACTION,
                                  stake=v["stake"], p_nothing=v["p_nothing"], cap=config.KELLY_CAP),
                  "bet_id": req.bet_id, "stake": v["stake"]})


@app.get("/api/odds")
def api_odds():
    """Line movement for every upcoming game (opening number, every logged change, the number now) and the
    props whose line has moved since it opened."""
    log = oddslog.load()
    games, props = [], []
    for r in STATE["results"]:
        sport = r["sport"]
        if r.get("games") is None or r["games"].empty:
            continue
        for g in r["games"].to_dict(orient="records"):
            now, opened = g.get("line") or {}, g.get("open_line") or {}
            feed = STATE["feed"].get((sport, g["home"], g["away"]))
            games.append({"sport": sport, "game_id": g["game_id"], "game": g["game"], "home": g["home"], "away": g["away"],
                          "start": g["start"], "edited": bool(g.get("edited")), "open": opened, "now": now, "feed": feed,
                          "move": oddslog.movement(opened, now), "model_margin": g.get("model_margin"), "model_total": g.get("model_total"),
                          "series": oddslog.series(log, sport, g["home"], g["away"], g["start"])})
        seen = set()
        for l in r["legs"]:
            ol = l.get("open_line")
            if ol is None or l["market"] in ("Moneyline", "Spread", "Total") or (l["player_key"], l["market_key"]) in seen or l.get("line") is None:
                continue
            seen.add((l["player_key"], l["market_key"]))
            if abs(float(l["line"]) - float(ol)) > 1e-9:
                props.append({"sport": sport, "player": l["subject"], "market": l["market"], "game": l["game"], "game_id": l["game_id"],
                              "open_line": float(ol), "line": float(l["line"]), "move": float(l["line"]) - float(ol),
                              "sim_over": l.get("sim_over"), "start": l.get("start")})
        for pl in r.get("prop_lines") or []:
            ol = pl.get("open_line")
            if ol is not None and abs(float(pl["line"]) - float(ol)) > 1e-9:
                props.append({"sport": sport, "player": pl["player"], "market": pl["market"], "game": pl["game"], "game_id": pl["game_id"],
                              "open_line": float(ol), "line": float(pl["line"]), "move": float(pl["line"]) - float(ol),
                              "sim_over": pl.get("p_over"), "start": pl.get("start")})
    size = lambda p: -abs(p["move"]) / max(abs(p["open_line"]), 2.0)  # noqa: E731  biggest move for its size first
    counts, top = {}, []
    for sport in sorted({p["sport"] for p in props}):  # the biggest movers of each sport, so a sport filter still has a full list
        mine = sorted((p for p in props if p["sport"] == sport), key=size)
        counts[sport] = len(mine)
        top += mine[:45]
    top.sort(key=size)
    recorded, mine = tracker.load_bets(), []
    moves = _bet_moves(recorded)
    for b in (recorded[recorded["status"] == "open"].to_dict(orient="records") if not recorded.empty else []):
        try:
            blegs = json.loads(b["legs_json"])
        except (TypeError, ValueError):
            continue
        mv = moves.get(str(b["id"])) or {}
        mine.append({"id": b["id"], "n": len(blegs), "stake": b["stake"], "odds": b["odds"], "placed_at": b["placed_at"],
                     "tally": betmoves.tally(mv),
                     "legs": [{"key": l.get("key"), "selection": l.get("selection"), "sport": l.get("sport"),
                               "game": f"{l.get('away')} @ {l.get('home')}", "game_id": l.get("game_id"), "market_key": l.get("market_key"),
                               "start": l.get("start"),
                               "started": betmoves.started(l), "move": mv.get(str(l.get("key")))} for l in blegs]})
    return clean({"games": games, "props": top, "prop_moves": len(props), "prop_moves_by_sport": counts, "checked_at": STATE["checked_at"],
                  "bets": mine,
                  "check_minutes": config.ODDS_CHECK_MINUTES, "auto_update_minutes": config.AUTO_UPDATE_MINUTES,
                  "stale_games": _stale_games(), "log_rows": int(len(log)),
                  "log_since": None if log.empty else str(log["ts"].min())})


@app.get("/api/track")
def api_track(days: int | None = None, sport: str | None = None):
    return clean(tracker.performance_data(days, sport))


@app.post("/api/grade")
def api_grade():
    return clean(tracker.grade(verbose=False))


@app.post("/api/bets")
def api_place(req: BetReq):
    legs = _picks(req.keys, req.lines)
    if not legs:
        raise HTTPException(400, "Those picks are no longer loaded - refresh the slate.")
    if req.stake <= 0:
        raise HTTPException(400, "Stake must be positive.")
    legs, _ = _with_live(legs)  # a game already in play counts at its chance now, not the one before it began
    ev = parlay.evaluate(legs)
    # the value at the price actually paid. evaluate() prices the slip at the picks' own odds multiplied together;
    # scaling by paid / those odds keeps its handling of pushes, which settle the same way (the pushed pick drops out)
    paid = american_to_decimal(req.odds)
    ev_paid = (1.0 + ev["ev"]) * paid / ev["dec"] - 1.0 if ev["dec"] > 1 else ev["ev"]
    row = tracker.place_bet(legs, req.odds, req.stake, ev["p_hit"], ev["p_win_all"], ev_paid, req.note)
    return clean(row)


@app.delete("/api/bets/{bet_id}")
def api_delete(bet_id: str):
    if not tracker.delete_bet(bet_id):
        raise HTTPException(404, "Bet not found")
    return {"deleted": bet_id}


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True, autorun: bool = True):
    import uvicorn

    _load_overrides()
    _load_prop_fixes()
    _load_not_offered()
    if autorun:
        start_run()
    threading.Thread(target=_odds_loop, daemon=True).start()
    if open_browser:
        import webbrowser

        threading.Timer(1.5, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    print(f"sportsedge UI running at http://{host}:{port}/  (Ctrl+C to stop)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
