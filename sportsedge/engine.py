"""Builds a full slate: data -> features -> model -> distributions -> simulations -> legs -> parlays."""
from __future__ import annotations

import copy
import datetime as dt
from types import SimpleNamespace

import numpy as np
import pandas as pd

from . import config, markets, parlay, plays, sources


GAME_MARKETS = ("Moneyline", "Spread", "Total")
LINE_FIELDS = ("spread_home", "spread_home_odds", "spread_away_odds", "total", "over_odds", "under_odds", "ml_home", "ml_away")


def game_info(sport: str, r, model, gl: list[dict], o: dict) -> dict:
    """One game's summary row for the reports and the dashboard. r: a slate row (attribute access)."""
    dm, _ = model.dists(r.mu_margin, r.mu_total)
    dm0, _ = model.dists(r.pm, r.pt)  # model-only view, before blending with the market
    ml_home = next((l for l in gl if l["selection"] == f"{r.home} ML"), None)
    row = r._asdict() if hasattr(r, "_asdict") else vars(r)
    info = {
        "game_id": r.game_id, "game": f"{r.away} @ {r.home}", "start": r.start, "home": r.home, "away": r.away,
        "model_margin": r.pm, "model_total": r.pt, "mkt_margin": r.mkt_margin, "mkt_total": r.mkt_total,
        "margin": r.mu_margin, "total": r.mu_total,
        "p_home": ml_home["p_cond"] if ml_home else dm.p_gt(0) + 0.5 * dm.p_eq(0),
        "p_home_model": dm0.p_gt(0) + 0.5 * dm0.p_eq(0),
        "drivers": model.explain(pd.Series(row)), "book": o.get("book"),
        # the line as the sportsbook quotes it, so it can be shown and corrected
        "line": {k: (None if o.get(k) is None or pd.isna(o.get(k)) else float(o.get(k))) for k in LINE_FIELDS},
        # the numbers the sportsbook first posted, for "moved since it opened"
        "open_line": {k: (None if o.get(f"open_{k}") is None or pd.isna(o.get(f"open_{k}")) else float(o.get(f"open_{k}")))
                      for k in ("spread_home", "total", "ml_home", "ml_away")},
        "event_id": str(getattr(r, "event_id", "") or ""),
        "edited": False,
    }
    if sport == "nfl":
        info.update({"qb_home": r.qb_name_h, "qb_away": r.qb_name_a, "indoor": bool(r.indoor),
                     "temp": r.temp_f, "wind": r.wind_mph, "precip": r.precip_raw})
    else:
        info.update({"b2b": [t for t, b in ((r.home, r.b2b_h), (r.away, r.b2b_a)) if b]})
    return info


def price_at(res: dict, leg: dict, line: float, change: dict | None = None) -> dict | None:
    """The same pick at a different line, priced there: for a bet made at an alternate line or on an app whose
    line is not the feed's. A spread or total is read from the score distributions; a prop from the player's
    simulated stat, with the market side moved from the reference line as for any other sportsbook's line.
    change: the user's own correction of this game's line, if there is one. None when it cannot be priced
    (a moneyline has no line, or the simulation for it is no longer loaded)."""
    mk, gid, sport = leg.get("market_key"), leg["game_id"], res["sport"]
    line = float(line)
    own_odds = None  # the feed's own price for this line, where it lists one as an "N or more" bet
    if mk in ("spread", "total"):
        if gid not in res.get("_feed_rows", {}) or gid not in res.get("_sims", {}):
            return None
        model = res["_model"]
        row, odds = dict(res["_feed_rows"][gid]), dict(res["_feed_odds"][gid])
        change = {k: float(v) for k, v in (change or {}).items() if k in LINE_FIELDS and v is not None}
        odds.update(change)
        if "spread_home" in change:
            row["mkt_margin"] = -change["spread_home"]
        if "total" in change:
            row["mkt_total"] = change["total"]
        row["mu_margin"] = float(model.blend(row["pm"], row["mkt_margin"], model.w_margin))
        row["mu_total"] = float(model.blend(row["pt"], row["mkt_total"], model.w_total))
        new = markets.game_leg_at(sport, SimpleNamespace(**row), model, res["_sims"][gid], odds, mk, leg["side"], line)
    elif mk == "anytime_td" and leg.get("sim_only"):  # 2+ or 3+ touchdowns from the anytime pick
        sims, who = res.get("prop_sims") or {}, leg.get("player_key")
        if gid not in sims or who not in sims[gid] or mk not in sims[gid][who] or line < 0.5:
            return None
        new = markets.td_leg(sport, gid, res["slate"].set_index("game_id").loc[gid], leg.get("subject"), who,
                             sims[gid][who][mk], line, res["labels"], leg.get("book") or "")
    elif mk == "ml" or leg.get("side") not in ("over", "under"):
        return None
    else:
        sims, who = res.get("prop_sims") or {}, leg.get("player_key")
        ref = (res.get("prop_ref") or {}).get((who, mk))
        if gid not in sims or who not in sims[gid] or mk not in sims[gid][who] or ref is None:
            return None
        df = pd.DataFrame([{"sport": sport, "player": leg.get("subject"), "key": who, "market": mk, "line": line,
                            "over_odds": leg["odds"], "under_odds": leg["odds"], "book": leg.get("book") or ""}])
        out = markets.prop_legs(sport, df, sims, res["slate"], res["labels"], res.get("prop_centre") or {"*": 0.0},
                                {(who, mk): ref})
        new = next((l for l in out if l["side"] == leg["side"]), None)
        if new is not None:
            new["p_fair"] = None  # the prices handed in were placeholders: there is no market price at this line
            new["edge"] = np.nan
            # a line the feed lists as an "N or more" bet: the chance shown on the page for it is the one used
            rung = next((t for t in (res.get("ladders") or {}).get(leg.get("group"), []) if abs(t[0] - 0.5 - line) < 1e-9), None)
            if rung is not None:
                p = rung[1] if leg["side"] == "over" else 1.0 - rung[1]
                new.update(p_cond=p, p_win=p * (1 - new["p_push"]), rung=rung[0])
                own_odds = rung[2] if leg["side"] == "over" else None  # "N or more" is the over: its price is not the under's
    if new is None:
        return None
    # the price shown with it still belongs to the tool's line; the caller supplies the real one
    new.update(odds=leg["odds"], dec=leg["dec"], alt_of=leg.get("line"), book=leg.get("book") or "")
    if leg.get("assumed"):
        new["assumed"] = True
    if own_odds is not None:  # the sportsbook does price this line: that price, not the main line's
        new.update(odds=float(own_odds), dec=markets.american_to_decimal(own_odds), own_price=True)
        new.pop("assumed", None)
        new["ev"] = markets.expected_value(new["p_win"], new["dec"], new["p_push"])
    return new


def reprice_game(res: dict, game_id, change: dict | None) -> list[dict]:
    """The sportsbook's line has moved and the feed has not caught up: re-price one game at the line the
    user reads off their sportsbook. The corrected number is treated as the market's line, exactly as if the
    feed had delivered it, so a stale feed can never look like a bargain. change=None restores the feed's line.
    Returns the game's new legs (also swapped into res)."""
    sport, model = res["sport"], res["_model"]
    row = dict(res["_feed_rows"][game_id])
    odds = dict(res["_feed_odds"][game_id])
    change = {k: float(v) for k, v in (change or {}).items() if k in LINE_FIELDS and v is not None}
    odds.update(change)
    if "spread_home" in change:
        row["mkt_margin"] = -change["spread_home"]
    if "total" in change:
        row["mkt_total"] = change["total"]
    row["mu_margin"] = float(model.blend(row["pm"], row["mkt_margin"], model.w_margin))
    row["mu_total"] = float(model.blend(row["pt"], row["mkt_total"], model.w_total))
    r = SimpleNamespace(**row)
    gl = markets.game_legs(sport, r, model, res["_sims"][game_id], odds)
    feed = res["_feed_odds"][game_id]
    spread_moved = "spread_home" in change and change["spread_home"] != feed.get("spread_home")
    if spread_moved and not {"ml_home", "ml_away"} <= set(change):
        for l in gl:  # the moneyline moves with the spread; its old price would look like value
            if l["market"] == "Moneyline":
                l["flag"] = "spread corrected: enter the current moneyline odds too"
    info = game_info(sport, r, model, gl, odds)
    info["edited"] = bool(change)
    info["feed_line"] = {k: (None if feed.get(k) is None or pd.isna(feed.get(k)) else float(feed.get(k))) for k in LINE_FIELDS}
    res["legs"] = [l for l in res["legs"] if not (l["game_id"] == game_id and l["market"] in GAME_MARKETS)] + gl
    recs = res["games"].to_dict(orient="records")
    res["games"] = pd.DataFrame([info if g["game_id"] == game_id else g for g in recs])
    return gl


def run_sport(sport: str, days: int = 1, props_csv: str | None = None, props: bool = True,
              n_sims: int | None = None, verbose=True, prop_lines: pd.DataFrame | None = None) -> dict:
    """verbose may be a callable that receives progress messages (used by the web UI)."""
    n_sims = n_sims or config.N_SIMS
    rng = np.random.default_rng(config.SEED)
    log = verbose if callable(verbose) else (print if verbose else (lambda *a, **k: None))
    today = dt.date.today()
    end = today + dt.timedelta(days=max(days - 1, 0))
    up = sources.espn_scoreboard(sport, today, end)
    up = up[up["state"] == "pre"].reset_index(drop=True) if not up.empty else up
    if up.empty:
        log(f"{sport.upper()}: no upcoming games between {today} and {end}.")
        return {"sport": sport, "games": pd.DataFrame(), "legs": [], "proj": pd.DataFrame(), "model": None}

    if sport == "nfl":
        from . import nfl as mod
        from . import nfl_props as pmod
    else:
        from . import nba as mod
        from . import nba_props as pmod
    g, ctx = mod.build(up, verbose=verbose)
    log(f"{sport.upper()}: training game model (walk-forward) ...")
    model = mod.fit_model(g)
    slate = g[(~g["completed"]) & g["event_id"].notna()].copy()
    slate = model.predict(slate).reset_index(drop=True)

    odds_by_event = up.set_index("event_id")
    sims, legs, rows = {}, [], []
    feed_rows, feed_odds = {}, {}
    for r in slate.itertuples(index=False):
        dm, dt_ = model.dists(r.mu_margin, r.mu_total)
        sims[r.game_id] = {"margin": dm.sample(n_sims, rng), "total": dt_.sample(n_sims, rng)}
        o = odds_by_event.loc[r.event_id].to_dict()
        gl = markets.game_legs(sport, r, model, sims[r.game_id], o)
        legs += gl
        rows.append(game_info(sport, r, model, gl, o))
        feed_rows[r.game_id], feed_odds[r.game_id] = r._asdict(), copy.deepcopy(o)
    games = pd.DataFrame(rows)

    proj = pd.DataFrame()
    prop_sims = {}
    prop_book = ""
    prop_centre = {"*": 0.0}
    prop_ref, prop_only, prop_check, lean = {}, [], None, []
    prop_feed, ladders, rung_legs = {}, {}, []
    if props:
        # your own lines first, then The Odds API, then the free ESPN feed; one line per player and market
        lines = []
        if props_csv:
            lines.append(sources.csv_props(props_csv, sport))
        if config.ESPN_PROPS:
            log(f"{sport.upper()}: downloading prop lines ...")
        feed = feed_prop_lines(sport, up, log)
        prop_feed = line_map(feed)
        if prop_lines is not None and not prop_lines.empty:
            lines.append(current_fixes(prop_lines[prop_lines["sport"].str.lower() == sport], prop_feed, log, sport))
        lines.append(feed)
        lines = [x for x in lines if x is not None and not x.empty]
        lined = set(pd.concat(lines, ignore_index=True)["key"]) if lines else None
        log(f"{sport.upper()}: simulating player props ...")
        try:
            if sport == "nfl":  # a posted line tells the simulator who has a role this week
                prop_sims, proj = pmod.run(slate, sims, ctx, g, rng, lined)
            else:
                prop_sims, proj = pmod.run(slate, sims, ctx, rng)
        except Exception as e:  # props are a bonus; never lose the game picks because of them
            import traceback

            log(f"{sport.upper()}: prop simulation failed: {e!r}")
            log(traceback.format_exc(limit=4))
            prop_sims = {}
        if prop_sims:
            # how much the simulator is trusted next to the market: tested on real closing prop lines
            from . import propcheck, tracker

            try:
                prop_check = propcheck.update(sport, g, ctx, model, log) or propcheck.summary(sport)
            except Exception as e:
                log(f"{sport.upper()}: prop test skipped: {e!r}")
                prop_check = propcheck.summary(sport)
            tracker.reset_prop_weight()
        if lines and prop_sims:
            ln = pd.concat(lines, ignore_index=True).drop_duplicates(subset=["key", "market"], keep="first")
            prop_centre = markets.prop_centre(ln, prop_sims)
            prop_ref = markets.prop_reference(ln)
            unpriced = ln[ln["over_odds"].isna() & ln["under_odds"].isna()]
            prop_only = markets.prop_leans(sport, unpriced, prop_sims, slate.assign(start=slate["start"]),
                                           pmod.MARKET_LABEL, prop_centre)
            lean = markets.lean_legs(sport, unpriced, prop_sims, slate.assign(start=slate["start"]),
                                     pmod.MARKET_LABEL, prop_centre)
            pl = markets.prop_legs(sport, ln, prop_sims, slate.assign(start=slate["start"]), pmod.MARKET_LABEL,
                                   prop_centre)
            legs += pl
            # the "N or more" bets and the touchdown scorers: what a trading app lists beside, or instead of,
            # an over/under
            rungs = feed_rungs(sport, up, log)
            ladders = markets.ladders(sport, rungs, prop_sims, prop_centre, prop_ref, markets.prop_hold(ln))
            # the ones that can be suggested, as picks: beside the overs they are another form of
            mains = {(l["player_key"], l["market_key"]): l for l in lean + pl if l.get("side") == "over"}
            rung_legs = markets.rung_legs(sport, rungs, prop_sims, slate.assign(start=slate["start"]), pmod.MARKET_LABEL,
                                          prop_centre, prop_ref, markets.prop_hold(ln), ladders, mains,
                                          plays.MIN_EV, plays.LEAN_MIN)
            lean += markets.td_legs(sport, rungs, prop_sims, slate.assign(start=slate["start"]), pmod.MARKET_LABEL)
            prop_book = ln["book"].dropna().astype(str).value_counts().index[0] if ln["book"].notna().any() else ""
            log(f"{sport.upper()}: {len(ln)} prop lines, {len(pl)} priced bets"
                + (f", {len(prop_only)} lines without a price" if prop_only else ""))
    summary = {
        "w_margin": model.w_margin, "w_total": model.w_total, "sd_margin": model.sd_margin,
        "sd_total": model.sd_total, "blend_n": model.blend_n,
    }
    if model.oos is not None and not model.oos.empty:
        o = model.oos.dropna(subset=["mkt_margin"])
        if len(o):
            summary["mae_model"] = float(np.abs(o["margin"] - o["pm"]).mean())
            summary["mae_market"] = float(np.abs(o["margin"] - o["mkt_margin"]).mean())
            summary["n_oos"] = len(o)
    summary["factors"] = model.coef_table().to_dict(orient="records")
    return {"sport": sport, "games": games, "legs": legs, "proj": proj, "model": summary,
            # kept so the UI can price parlays / new prop lines without re-simulating
            "prop_book": prop_book, "prop_centre": prop_centre, "prop_ref": prop_ref, "prop_lines": prop_only,
            # unpriced props as picks at an assumed price: for the bet slip and the suggested parlays only
            "lean_legs": lean,
            # "game|player|stat" -> the "N or more" bets on it, each with the tool's chance
            "ladders": ladders,
            # the "N or more" bets that may be suggested, as picks (never part of `legs`: see markets.rung_legs)
            "rung_legs": rung_legs,
            "prop_check": prop_check,
            # kept so one game can be re-priced when the user corrects a line the feed has not caught up with
            "_model": model, "_sims": sims, "_feed_rows": feed_rows, "_feed_odds": feed_odds,
            # (player key, market) -> the prop line the feed showed for this run, and the games it was read for,
            # to tell when it moves
            "_prop_feed": prop_feed, "_prop_games": up if props else None,
            "prop_sims": prop_sims, "slate": slate[["game_id", "home", "away", "start"]].copy(),
            "labels": pmod.MARKET_LABEL}


def feed_prop_lines(sport: str, games: pd.DataFrame, log=None) -> pd.DataFrame:
    """The prop lines the feeds carry for these games: The Odds API first when there is a key, then ESPN's copy
    of DraftKings. One line per player and market."""
    parts = []
    if config.ODDS_API_KEY:
        parts.append(sources.oddsapi_props(sport, games))
    if config.ESPN_PROPS:
        try:
            parts.append(sources.espn_props(sport, games))
        except Exception as e:  # a missing feed must not cost the game picks
            if log:
                log(f"{sport.upper()}: prop lines unavailable: {e!r}")
    parts = [x for x in parts if x is not None and not x.empty]
    if not parts:
        return pd.DataFrame(columns=sources.PROP_COLS)
    return pd.concat(parts, ignore_index=True).drop_duplicates(subset=["key", "market"], keep="first")


def feed_rungs(sport: str, games: pd.DataFrame, log=None) -> pd.DataFrame:
    """The "N or more" bets and touchdown scorers in ESPN's copy of DraftKings (the same download as the lines)."""
    if config.ESPN_PROPS:
        try:
            return sources.espn_rungs(sport, games)
        except Exception as e:  # an extra: never lose the picks because of it
            if log:
                log(f"{sport.upper()}: 'N or more' bets unavailable: {e!r}")
    return pd.DataFrame(columns=sources.RUNG_COLS)


def line_map(lines: pd.DataFrame) -> dict:
    """(player key, market) -> line."""
    if lines is None or lines.empty:
        return {}
    return {(r.key, r.market): float(r.line) for r in lines.itertuples() if not pd.isna(r.line)}


def current_fixes(mine: pd.DataFrame, feed: dict, log=None, sport: str = "") -> pd.DataFrame:
    """The lines the user corrected by hand that still apply. A correction carries the line the feed showed when
    it was made (feed_line) and is used while the feed still shows that line, or shows nothing for it. Once the
    feed moves, it has caught up with the sportsbook and its own line is used again."""
    if mine is None or mine.empty or "feed_line" not in mine:
        return mine
    keep = []
    for r in mine.itertuples():
        now = feed.get((r.key, r.market))
        ok = pd.isna(r.feed_line) or now is None or abs(now - float(r.feed_line)) < 1e-9
        if not ok and log:
            log(f"{sport.upper()}: the feed now has {r.player} at {now:g}, so your corrected line ({float(r.line):g}) is no longer needed")
        keep.append(ok)
    return mine[keep]


def build_parlays(legs: list[dict]) -> dict:
    return {
        "value": parlay.search(legs, "value"),
        "safe": parlay.search(legs, "safe"),
        "sgp": parlay.search(legs, "sgp", max_legs=3),
    }
