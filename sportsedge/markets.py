"""Turn model distributions + sportsbook prices into bet 'legs' with probability, fair price, edge and EV."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import config
from .oddsmath import american_to_decimal, devig, expected_value, implied, kelly

RESULT_WIN, RESULT_PUSH, RESULT_LOSS = 1, 0, -1


def _result(win: np.ndarray, push: np.ndarray | None = None) -> np.ndarray:
    r = np.where(win, RESULT_WIN, RESULT_LOSS).astype(np.int8)
    if push is not None:
        r[push] = RESULT_PUSH
    return r


def _leg(base: dict, selection: str, market: str, group: str, odds, p_win, p_push, p_model, p_fair, result,
         line=None, proj=None, flag="", market_key="", side="", player_key="") -> dict | None:
    if odds is None or np.isnan(odds):
        return None
    dec = american_to_decimal(odds)
    ev = expected_value(p_win, dec, p_push)
    p_cond = p_win / max(1 - p_push, 1e-9)  # probability of winning given no push
    return {**base, "selection": selection, "market": market, "group": group, "line": line, "odds": float(odds),
            "dec": dec, "p_win": p_win, "p_push": p_push, "p_cond": p_cond, "p_model": p_model, "p_fair": p_fair,
            "edge": (p_cond - p_fair) if p_fair is not None and not np.isnan(p_fair) else np.nan, "ev": ev,
            "kelly": kelly(p_win, dec, p_push), "proj": proj, "flag": flag, "result": result,
            "market_key": market_key, "side": side, "player_key": player_key,
            # identity of the bet across runs (the line may move; the tracker keeps the latest pre-game price)
            "key": "|".join([base["sport"], str(base["game_id"]), market_key, side, player_key])}


def _base(sport: str, game_id, home: str, away: str, start, subject: str, book="") -> dict:
    date = pd.Timestamp(start).tz_convert("America/New_York").strftime("%Y-%m-%d") if start is not None and not pd.isna(start) else ""
    return {"sport": sport.upper(), "game_id": game_id, "game": f"{away} @ {home}", "home": home, "away": away,
            "start": start, "date": date, "subject": subject, "book": book or ""}


def _fair_pair(odds_a, odds_b):
    if odds_a is None or odds_b is None or np.isnan(odds_a) or np.isnan(odds_b):
        return np.nan, np.nan
    a, b = devig([implied(odds_a), implied(odds_b)])
    return float(a), float(b)


GAP_LIMIT = {"nfl": (7.0, 7.0), "nba": (8.0, 12.0)}  # (margin, total) points


def _cond(d, kind: str, x: float) -> float:
    """P(win | no push) for an event on a ScoreDist: 'gt' = outcome above x, 'lt' = below x."""
    pe = d.p_eq(x)
    p = d.p_gt(x) if kind == "gt" else d.p_lt(x)
    return p / max(1 - pe, 1e-9)


def game_legs(sport: str, g, model, sims: dict, odds: dict) -> list[dict]:
    """Probabilities are anchored to the market: start from the no-vig price, then move it by what the
    model's point difference from the line is worth at that line (key-number aware). Without a two-sided
    market price the model distribution is used directly."""
    M, T = sims["margin"], sims["total"]
    game = f"{g.away} @ {g.home}"
    base = _base(sport, g.game_id, g.home, g.away, g.start, game, odds.get("book"))
    lim_m, lim_t = GAP_LIMIT[sport]
    gap_m = abs(g.pm - g.mkt_margin) > lim_m if not np.isnan(g.mkt_margin) else False
    gap_t = abs(g.pt - g.mkt_total) > lim_t if not np.isnan(g.mkt_total) else False
    flag_side = "model far from market: check news" if gap_m else ""
    flag_total = "model far from market: check news" if gap_t else ""
    dB_m, dB_t = model.dist_margin(g.mu_margin), model.dist_total(g.mu_total)       # blended view
    dK_m = model.dist_margin(g.mkt_margin) if not np.isnan(g.mkt_margin) else dB_m  # market's own line
    dK_t = model.dist_total(g.mkt_total) if not np.isnan(g.mkt_total) else dB_t
    d0_m, d0_t = model.dist_margin(g.pm), model.dist_total(g.pt)                   # model only (display)
    legs = []

    def add(sel, market, grp, o, fair, dB, dK, d0, kind, x, result, mkey, side, line=None, flag=""):
        pb = _cond(dB, kind, x)
        pc = pb if fair is None or np.isnan(fair) else float(np.clip(fair + pb - _cond(dK, kind, x), 0.005, 0.995))
        pp = dB.p_eq(x)
        legs.append(_leg(base, sel, market, grp, o, pc * (1 - pp), pp, _cond(d0, kind, x), fair, result,
                         line=line, flag=flag, market_key=mkey, side=side))

    fh, fa = _fair_pair(odds.get("ml_home"), odds.get("ml_away"))
    add(f"{g.home} ML", "Moneyline", f"{g.game_id}|side", odds.get("ml_home"), fh, dB_m, dK_m, d0_m, "gt", 0,
        _result(M > 0, M == 0), "ml", "home", flag=flag_side)
    add(f"{g.away} ML", "Moneyline", f"{g.game_id}|side", odds.get("ml_away"), fa, dB_m, dK_m, d0_m, "lt", 0,
        _result(M < 0, M == 0), "ml", "away", flag=flag_side)
    L = odds.get("spread_home")
    if L is not None and not np.isnan(L):
        fh, fa = _fair_pair(odds.get("spread_home_odds"), odds.get("spread_away_odds"))
        add(f"{g.home} {L:+g}", "Spread", f"{g.game_id}|side", odds.get("spread_home_odds"), fh, dB_m, dK_m, d0_m,
            "gt", -L, _result(M + L > 0, M + L == 0), "spread", "home", line=L, flag=flag_side)
        add(f"{g.away} {-L:+g}", "Spread", f"{g.game_id}|side", odds.get("spread_away_odds"), fa, dB_m, dK_m, d0_m,
            "lt", -L, _result(M + L < 0, M + L == 0), "spread", "away", line=-L, flag=flag_side)
    X = odds.get("total")
    if X is not None and not np.isnan(X):
        fo, fu = _fair_pair(odds.get("over_odds"), odds.get("under_odds"))
        add(f"Over {X:g}", "Total", f"{g.game_id}|total", odds.get("over_odds"), fo, dB_t, dK_t, d0_t, "gt", X,
            _result(T > X, T == X), "total", "over", line=X, flag=flag_total)
        add(f"Under {X:g}", "Total", f"{g.game_id}|total", odds.get("under_odds"), fu, dB_t, dK_t, d0_t, "lt", X,
            _result(T < X, T == X), "total", "under", line=X, flag=flag_total)
    return [l for l in legs if l is not None]


def game_leg_at(sport: str, g, model, sims: dict, odds: dict, market_key: str, side: str, line: float) -> dict | None:
    """One side of a spread or total at a line other than the sportsbook's own: an alternate line, or the
    line a different app is offering.

    There is no price for that line, so the market's chance there is its no-vig price at its own line, moved
    by what the gap between the two lines is worth in the market-centred score distribution (key numbers
    included). The model's shift is then applied exactly as it is at the main line. `line` is from the
    pick's own side, as everywhere else: the away team at +7.5 is line 7.5."""
    M, T = sims["margin"], sims["total"]
    base = _base(sport, g.game_id, g.home, g.away, g.start, f"{g.away} @ {g.home}", odds.get("book"))
    line = float(line)
    if market_key == "spread":
        L0 = odds.get("spread_home")
        if L0 is None or np.isnan(L0):
            return None
        home = side == "home"
        L = line if home else -line                      # the same line seen from the home side
        fh, fa = _fair_pair(odds.get("spread_home_odds"), odds.get("spread_away_odds"))
        fair, o = (fh, odds.get("spread_home_odds")) if home else (fa, odds.get("spread_away_odds"))
        dB, d0 = model.dist_margin(g.mu_margin), model.dist_margin(g.pm)
        dK = model.dist_margin(g.mkt_margin) if not np.isnan(g.mkt_margin) else dB
        kind, x0, x = ("gt" if home else "lt"), -float(L0), -L
        result = _result(M + L > 0, M + L == 0) if home else _result(M + L < 0, M + L == 0)
        sel, market, grp = (f"{g.home} {L:+g}" if home else f"{g.away} {-L:+g}"), "Spread", f"{g.game_id}|side"
    elif market_key == "total":
        X0 = odds.get("total")
        if X0 is None or np.isnan(X0):
            return None
        over = side == "over"
        fo, fu = _fair_pair(odds.get("over_odds"), odds.get("under_odds"))
        fair, o = (fo, odds.get("over_odds")) if over else (fu, odds.get("under_odds"))
        dB, d0 = model.dist_total(g.mu_total), model.dist_total(g.pt)
        dK = model.dist_total(g.mkt_total) if not np.isnan(g.mkt_total) else dB
        kind, x0, x = ("gt" if over else "lt"), float(X0), line
        result = _result(T > line, T == line) if over else _result(T < line, T == line)
        sel, market, grp = f"{'Over' if over else 'Under'} {line:g}", "Total", f"{g.game_id}|total"
    else:
        return None
    pb = _cond(dB, kind, x)
    if fair is None or np.isnan(fair):
        pc, fair_at = pb, np.nan
    else:
        fair_at = float(np.clip(fair + _cond(dK, kind, x) - _cond(dK, kind, x0), 0.005, 0.995))  # the market, moved to this line
        pc = float(np.clip(fair + pb - _cond(dK, kind, x0), 0.005, 0.995))
    pp = dB.p_eq(x)
    o = -110.0 if o is None or np.isnan(o) else o  # the price belongs to the other line: it is only a placeholder here
    return _leg(base, sel, market, grp, o, pc * (1 - pp), pp, _cond(d0, kind, x), fair_at, result, line=line,
                market_key=market_key, side=side)


# Simulator vs no-vig market, after centring. On real lines (propcheck.py) disagreements below this were
# not harmful in either sport; above it the NFL simulator's side lost more than it won, which is what
# stale information (a role change, a late scratch) looks like. Such props are labelled and never listed as value.
PROP_GAP_FLAG = 0.30
PROP_LEAN = 0.15       # from here up to the flag is a "strong lean": the range reported in the prop test
CENTRE_PRIOR = 10      # a market's own shift counts fully only once it has many more lines than this
CENTRE_MIN_LINES = 20  # fewer two-sided lines than this on the slate: no centring at all


def _logit(p):
    p = np.clip(p, 0.002, 0.998)
    return np.log(p / (1 - p))


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _moved(pc: float, ref_pc: float, ref_fair: float) -> float:
    """The market's chance at a line it has not priced: the simulator's chance there, shifted in log-odds by as
    much as the simulator sits from the market at the line it has priced. Next to the priced line this is the
    plain gap in chance. Far from it the chance still falls away towards nothing, as it must, where adding the
    gap would leave "45 or more points" stuck at the size of the gap."""
    return float(_sigmoid(_logit(pc) + _logit(ref_fair) - _logit(ref_pc)))


def _locate(prop_sims: dict) -> dict:
    where = {}
    for gid, players in prop_sims.items():
        for key in players:
            where.setdefault(key, gid)
    return where


def prop_centre(lines: pd.DataFrame, prop_sims: dict) -> dict:
    """How far, in log-odds, the simulator sits from the no-vig market on average, per market.

    Sportsbooks only post a line for players with a role that night and they see news the simulator does
    not, so across a whole slate the average line is better information than the simulator's average.
    Subtracting this shift leaves the simulator's opinion about each player *relative to the others*, which
    is the part that can carry an edge. Game lines are anchored to the market the same way.
    '*' is the shift pooled over every market (used for a market with too few lines of its own)."""
    where = _locate(prop_sims)
    diffs: dict[str, list] = {}
    for r in lines.itertuples():
        gid = where.get(r.key)
        if gid is None or r.market not in prop_sims[gid][r.key]:
            continue
        fo, _ = _fair_pair(r.over_odds, r.under_odds)
        if np.isnan(fo):
            continue
        x = prop_sims[gid][r.key][r.market]
        pp = float(np.mean(x == float(r.line)))
        pc = float(np.mean(x > float(r.line))) / max(1 - pp, 1e-9)
        diffs.setdefault(r.market, []).append(float(np.clip(_logit(fo) - _logit(pc), -1.5, 1.5)))
    n = sum(len(v) for v in diffs.values())
    if n < CENTRE_MIN_LINES:
        return {"*": 0.0}
    pooled = float(np.mean([d for v in diffs.values() for d in v]))
    out = {m: float((np.sum(v) + pooled * CENTRE_PRIOR) / (len(v) + CENTRE_PRIOR)) for m, v in diffs.items()}
    out["*"] = pooled
    return out


def lean_legs(sport: str, lines: pd.DataFrame, prop_sims: dict, games: pd.DataFrame, labels: dict,
              centre: dict) -> list[dict]:
    """Both sides of every line that came without a price, as picks that can go in a bet slip.

    The chance is the tested blend: the simulator against an even bet at that line, with the weight the prop
    test gave the simulator. The price is an assumed standard one (config.ASSUMED_PROP_ODDS) and each pick is
    marked `assumed`. They are kept apart from the priced picks: never rated as value and never logged as
    priced picks, because nothing here knows what the sportsbook is really charging."""
    where = _locate(prop_sims)
    ginfo = games.set_index("game_id")
    from .tracker import prop_weight as _learned  # lazy: tracker reads the graded history

    wp, odds = _learned(sport), float(config.ASSUMED_PROP_ODDS)
    legs = []
    for r in lines.itertuples():
        gid = where.get(r.key)
        if gid is None or r.market == "anytime_td" or r.market not in prop_sims[gid][r.key] or pd.isna(r.line):
            continue
        x = prop_sims[gid][r.key][r.market]
        g = ginfo.loc[gid]
        line = float(r.line)
        pp = float(np.mean(x == line))
        pc = float(np.mean(x > line)) / max(1 - pp, 1e-9)
        pc = float(_sigmoid(_logit(pc) + centre.get(r.market, centre.get("*", 0.0))))
        po = (wp * pc + (1 - wp) * 0.5) * (1 - pp)
        pu = (wp * (1 - pc) + (1 - wp) * 0.5) * (1 - pp)
        label = labels.get(r.market, r.market)
        flag = "simulator far from market: check news" if abs(pc - 0.5) >= PROP_GAP_FLAG else ""
        base = {**_base(sport, gid, g.home, g.away, g.start, r.player, getattr(r, "book", "")),
                "proj_med": float(np.median(x)), "sim_over": pc, "open_line": _opt(r, "open_line"), "assumed": True}
        common = dict(line=line, proj=float(np.mean(x)), flag=flag, market_key=r.market, player_key=r.key)
        grp = f"{gid}|{r.key}|{r.market}"
        over = _leg(base, f"{r.player} o{line:g} {label}", label, grp, odds, po, pp, pc * (1 - pp), 0.5,
                    _result(x > line, x == line), side="over", **common)
        under = _leg(base, f"{r.player} u{line:g} {label}", label, grp, odds, pu, pp, (1 - pc) * (1 - pp), 0.5,
                     _result(x < line, x == line), side="under", **common)
        over["lean"], under["lean"] = pc - 0.5, 0.5 - pc  # how far the simulator is from an even bet, toward this side
        legs += [over, under]
    return legs


def rung_chance(x: np.ndarray, line: float, shift: float, ref: tuple, weight: float, own: float | None = None) -> float:
    """The chance a stat finishes over a line that is not the sportsbook's main one (given no push): the
    simulator's chance at that line, blended with the market's. The market's is, in order: the main line's fair
    chance when this is the main line; the sportsbook's own price for this threshold with the fee taken out
    (`own`), when the feed has one; otherwise the main line's fair chance moved to this line (see _moved)."""
    pp = float(np.mean(x == line))
    pc = float(_sigmoid(_logit(float(np.mean(x > line)) / max(1 - pp, 1e-9)) + shift))
    ref_line, ref_fair = ref
    if abs(line - ref_line) < 1e-9:
        mo = ref_fair
    elif own is not None:
        mo = own
    else:
        rp = float(np.mean(x == ref_line))
        ref_pc = float(_sigmoid(_logit(float(np.mean(x > ref_line)) / max(1 - rp, 1e-9)) + shift))
        mo = _moved(pc, ref_pc, ref_fair)
    return weight * pc + (1 - weight) * mo


RUNG_HOLD = 1.07  # a one-sided price has the fee in it: about what DraftKings' two-sided prop prices add up to


def prop_hold(lines: pd.DataFrame) -> dict:
    """(player key, market) -> what the two sides of its main line add up to (1.00 would be no fee)."""
    out = {}
    for r in lines.itertuples():
        if not pd.isna(r.over_odds) and not pd.isna(r.under_odds):
            out[(r.key, r.market)] = float(implied(r.over_odds) + implied(r.under_odds))
    return out


RUNG_MIN, RUNG_MAX = 0.01, 0.985  # a threshold almost sure to be missed or met is not a bet worth listing


def ladders(sport: str, rungs: pd.DataFrame, prop_sims: dict, centre: dict, ref: dict, hold: dict | None = None) -> dict:
    """The "N or more" bets in the feed, each with the tool's chance:
    {"game id|player key|stat": [[N, chance, the feed's odds or None], ...]}, lowest N first.

    "4+ receptions" is the over on 3.5. Its chance blends the simulator with the market: the sportsbook's own
    price for that threshold where the feed has one (NBA), its fee taken out with the hold of the same player's
    main line; otherwise the main over/under line moved to the threshold (NFL). A stat with no main line is left
    out, since there is nothing to anchor it to. Touchdowns are the exception: no line exists for them at all,
    so 1+ (anytime), 2+ and 3+ carry the simulator's own chance and the page says so."""
    if rungs is None or rungs.empty or not prop_sims:
        return {}
    from .tracker import prop_weight as _learned  # lazy: tracker reads the graded history

    where, wp, out = _locate(prop_sims), _learned(sport), {}
    for r in rungs.itertuples():
        gid = where.get(r.key)
        if gid is None or r.market not in prop_sims[gid][r.key]:
            continue
        x, line = prop_sims[gid][r.key][r.market], float(r.n) - 0.5
        if r.market == "anytime_td":
            p = float(np.mean(x > line))
        else:
            rf = (ref or {}).get((r.key, r.market))
            if rf is None:
                continue
            own = None
            if not pd.isna(r.odds):
                own = float(np.clip(implied(r.odds) / (hold or {}).get((r.key, r.market), RUNG_HOLD), 0.002, 0.998))
            p = rung_chance(x, line, centre.get(r.market, centre.get("*", 0.0)), rf, wp, own)
        if not (RUNG_MIN <= p <= RUNG_MAX):
            continue
        odds = None if pd.isna(r.odds) else float(r.odds)
        out.setdefault(f"{gid}|{r.key}|{r.market}", []).append([int(r.n) if float(r.n).is_integer() else float(r.n), round(p, 4), odds])
    for v in out.values():
        v.sort(key=lambda t: t[0])
    return out


RUNG_DEC = (1.2, 4.5)  # a priced threshold far outside the prices a suggestion uses is never built


def rung_legs(sport: str, rungs: pd.DataFrame, prop_sims: dict, games: pd.DataFrame, labels: dict, centre: dict,
              ref: dict, hold: dict, ladder: dict, mains: dict, min_ev: float = -0.06, min_lean: float = 0.10) -> list[dict]:
    """The "N or more" bets that can be suggested, as picks of their own. They are kept apart from the slate's
    picks: each is another way to bet a stat that already has an over, and a bet slip holds one as that over at
    the threshold's line (`base_key`, `rung`).

    Priced in the feed (NBA): any threshold whose price is near the band a suggestion uses, at that price.
    Not priced (NFL): only the threshold that is the main line itself ("37+" beside an over 36.5). It is the same
    bet as the over and carries the over's assumed price, so the over can still be suggested where an app
    lists the stat only as "N or more".
    ladder: ladders() for the same lines, so a pick here has exactly the chance the page shows for it.
    mains: (player key, stat) -> the main over pick. A threshold is built only where that pick exists.
    min_ev, min_lean: what a suggestion asks of a pick (plays.MIN_EV, plays.LEAN_MIN). A threshold that cannot
    be suggested is not built at all: each pick carries a simulated result per run, and there are thousands."""
    if rungs is None or rungs.empty or not prop_sims:
        return []
    where, ginfo, legs = _locate(prop_sims), games.set_index("game_id"), []
    for r in rungs.itertuples():
        gid, main, rf = where.get(r.key), mains.get((r.key, r.market)), (ref or {}).get((r.key, r.market))
        if r.market == "anytime_td" or gid is None or main is None or rf is None or r.market not in prop_sims[gid][r.key]:
            continue
        grp = f"{gid}|{r.key}|{r.market}"
        n = int(r.n) if float(r.n).is_integer() else float(r.n)
        step = next((t for t in (ladder or {}).get(grp, []) if t[0] == n), None)
        if step is None:
            continue
        line, priced = float(r.n) - 0.5, not pd.isna(r.odds)
        twin = abs(line - rf[0]) < 1e-9
        if priced:
            if not (RUNG_DEC[0] <= american_to_decimal(r.odds) <= RUNG_DEC[1]):
                continue
            odds = float(r.odds)
        elif twin and main.get("assumed"):
            odds = float(main["odds"])
        else:
            continue
        x, g = prop_sims[gid][r.key][r.market], ginfo.loc[gid]
        pc = float(_sigmoid(_logit(float(np.mean(x > line))) + centre.get(r.market, centre.get("*", 0.0))))
        own = float(np.clip(implied(odds) / (hold or {}).get((r.key, r.market), RUNG_HOLD), 0.002, 0.998)) if priced else None
        market = rf[1] if twin else own
        label = labels.get(r.market, r.market)
        base = {**_base(sport, gid, g.home, g.away, g.start, r.player, getattr(r, "book", "")),
                "proj_med": float(np.median(x)), "sim_over": pc}
        if abs(pc - market) >= PROP_GAP_FLAG:  # the simulator is far from the market: news it has not seen
            continue
        if (step[1] * american_to_decimal(odds) - 1.0 < min_ev) if priced else (abs(pc - 0.5) < min_lean):
            continue
        flag = ""
        leg = _leg(base, f"{r.player} {n}+ {label}", label, grp, odds, step[1], 0.0, pc, own, _result(x > line),
                   line=line, proj=float(np.mean(x)), flag=flag, market_key=r.market, side="over", player_key=r.key)
        leg.update(key=f"{main['key']}|{n}+", rung=n, base_key=main["key"])
        if not priced:
            leg.update(assumed=True, lean=pc - 0.5)  # as for any line without a price: suggested on the simulator's lean
        legs.append(leg)
    return legs


def td_leg(sport: str, gid, g, player: str, key: str, x: np.ndarray, line: float, labels: dict, book: str = "") -> dict:
    """A touchdown-scorer pick: anytime (line 0.5), or 2+ / 3+ touchdowns (1.5, 2.5). The chance is the
    simulator's own: no sportsbook line or price exists to check it against, so it is marked `sim_only`, carries
    an assumed price only so that it can sit in a bet slip, and is never rated or suggested."""
    n = int(round(line + 0.5))
    p = float(np.mean(x > line))
    base = {**_base(sport, gid, g.home, g.away, g.start, player, book), "proj_med": float(np.median(x)),
            "assumed": True, "sim_only": True}
    leg = _leg(base, f"{player} anytime TD" if n <= 1 else f"{player} {n}+ TDs", labels.get("anytime_td", "Anytime TD"),
               f"{gid}|{key}|anytime_td", float(config.ASSUMED_PROP_ODDS), p, 0.0, p, None, _result(x > line),
               line=float(line), proj=float(np.mean(x)), market_key="anytime_td", side="yes", player_key=key)
    leg.update(ev=None, kelly=0.0, lean=None)  # nothing to rate it against until a real price is typed
    return leg


def td_legs(sport: str, rungs: pd.DataFrame, prop_sims: dict, games: pd.DataFrame, labels: dict) -> list[dict]:
    """An anytime-touchdown pick for every player the feed lists as a scorer without a price."""
    if rungs is None or rungs.empty or not prop_sims:
        return []
    where, ginfo, legs = _locate(prop_sims), games.set_index("game_id"), []
    for r in rungs[(rungs["market"] == "anytime_td") & (rungs["n"] == 1) & rungs["odds"].isna()].itertuples():
        gid = where.get(r.key)
        if gid is None or "anytime_td" not in prop_sims[gid][r.key]:
            continue
        legs.append(td_leg(sport, gid, ginfo.loc[gid], r.player, r.key, prop_sims[gid][r.key]["anytime_td"], 0.5,
                           labels, getattr(r, "book", "")))
    return legs


def _opt(r, name):
    """An optional column of a line row as a float, or None when it is absent or empty."""
    v = getattr(r, name, None)
    return None if v is None or pd.isna(v) else float(v)


def prop_reference(lines: pd.DataFrame) -> dict:
    """(player key, market) -> (line, no-vig chance of the over) for the reference sportsbook's lines.
    A line that came without a price (NFL) is taken as an even bet at that line."""
    ref = {}
    for r in lines.itertuples():
        if r.market == "anytime_td" or pd.isna(r.line):
            continue
        fo, _ = _fair_pair(r.over_odds, r.under_odds)
        if np.isnan(fo):
            if not (pd.isna(r.over_odds) and pd.isna(r.under_odds)):
                continue
            fo = 0.5
        ref[(r.key, r.market)] = (float(r.line), float(fo))
    return ref


def prop_leans(sport: str, lines: pd.DataFrame, prop_sims: dict, games: pd.DataFrame, labels: dict,
               centre: dict) -> list[dict]:
    """Lines that came without a price: projection and the simulator's lean, but no EV (nothing to bet
    against until a price is entered)."""
    where = _locate(prop_sims)
    ginfo = games.set_index("game_id")
    out = []
    for r in lines.itertuples():
        gid = where.get(r.key)
        if gid is None or r.market not in prop_sims[gid][r.key] or pd.isna(r.line):
            continue
        x = prop_sims[gid][r.key][r.market]
        line = float(r.line)
        pp = float(np.mean(x == line))
        pc = float(np.mean(x > line)) / max(1 - pp, 1e-9)
        pc = float(_sigmoid(_logit(pc) + centre.get(r.market, centre.get("*", 0.0))))
        g = ginfo.loc[gid]
        out.append({"sport": sport.upper(), "game_id": gid, "game": f"{g.away} @ {g.home}", "start": g.start,
                    "player": r.player, "player_key": r.key, "market_key": r.market,
                    "market": labels.get(r.market, r.market), "line": line, "proj": float(np.mean(x)),
                    "proj_med": float(np.median(x)), "p_over": pc, "book": getattr(r, "book", "") or "",
                    "open_line": _opt(r, "open_line")})
    return out


def prop_legs(sport: str, lines: pd.DataFrame, prop_sims: dict, games: pd.DataFrame, labels: dict,
              centre: dict | None = None, ref: dict | None = None) -> list[dict]:
    """lines: player, key, market, line, over_odds, under_odds. prop_sims: game_id -> key -> market -> array.
    centre: shifts from prop_centre() (computed from these lines when not given).
    ref: prop_reference() of the slate's own lines, given when pricing a line from another sportsbook. The
         market side of the blend is then the reference book's price, moved to this line by what the
         difference between the two lines is worth in the simulation, so a line or price that is out of step
         with the reference book shows up as an edge against this book's own no-vig price."""
    where = _locate(prop_sims)
    ginfo = games.set_index("game_id")
    from .tracker import prop_weight as _learned  # lazy: tracker reads the graded history

    prop_weight = _learned(sport)
    if centre is None:
        centre = prop_centre(lines, prop_sims)
    legs = []
    for r in lines.itertuples():
        gid = where.get(r.key)
        if gid is None or r.market not in prop_sims[gid][r.key]:
            continue
        x = prop_sims[gid][r.key][r.market]
        g = ginfo.loc[gid]
        base = _base(sport, gid, g.home, g.away, g.start, r.player, getattr(r, "book", ""))
        label = labels.get(r.market, r.market)
        line = float(r.line)
        pp = float(np.mean(x == line))
        fo, fu = _fair_pair(r.over_odds, r.under_odds)
        two_sided = not np.isnan(fo)
        if not two_sided and not np.isnan(r.over_odds):
            fo = implied(r.over_odds)  # one-sided market: compare to the vigged price
        grp = f"{gid}|{r.key}|{r.market}"
        proj = float(np.mean(x))
        # 1) the simulator's chance of going over (given no push), centred on the market for this stat
        pc = float(np.mean(x > line)) / max(1 - pp, 1e-9)
        if two_sided:
            pc = float(_sigmoid(_logit(pc) + centre.get(r.market, centre.get("*", 0.0))))
        sim_o, sim_u = pc * (1 - pp), (1 - pc) * (1 - pp)
        flag = "simulator far from market: check news" if two_sided and abs(pc - fo) >= PROP_GAP_FLAG else ""
        # 2) the market side of the blend: this book's own no-vig price, or the reference book's price moved
        #    to this line when the line comes from another sportsbook
        mo, mu = fo, fu
        rl = (ref or {}).get((r.key, r.market))
        if rl is not None and r.market != "anytime_td":
            ref_line, ref_fair = rl
            rp = float(np.mean(x == ref_line))
            ref_pc = float(np.mean(x > ref_line)) / max(1 - rp, 1e-9)
            ref_pc = float(_sigmoid(_logit(ref_pc) + centre.get(r.market, centre.get("*", 0.0))))
            mo = _moved(pc, ref_pc, ref_fair)
            mu = 1 - mo
            base = {**base, "ref_line": ref_line, "ref_fair": ref_fair}
            flag = "simulator far from market: check news" if abs(ref_pc - ref_fair) >= PROP_GAP_FLAG else ""
        # 3) blend (weight learned from real closing prop lines, then from your own graded props)
        wp = prop_weight
        po, pu = sim_o, sim_u
        if not np.isnan(mo):
            po = wp * sim_o + (1 - wp) * mo * (1 - pp)
        if not np.isnan(mu):
            pu = wp * sim_u + (1 - wp) * mu * (1 - pp)
        base = {**base, "proj_med": float(np.median(x)), "sim_over": pc, "open_line": _opt(r, "open_line"),
                "open_over_odds": _opt(r, "open_over_odds"), "open_under_odds": _opt(r, "open_under_odds")}
        if r.market == "anytime_td":
            legs.append(_leg(base, f"{r.player} anytime TD", label, grp, r.over_odds, po, 0.0, sim_o, fo,
                             _result(x > line), line=line, proj=proj, market_key=r.market, side="yes",
                             player_key=r.key))
            continue
        legs.append(_leg(base, f"{r.player} o{line:g} {label}", label, grp, r.over_odds, po, pp, sim_o, fo,
                         _result(x > line, x == line), line=line, proj=proj, flag=flag, market_key=r.market,
                         side="over", player_key=r.key))
        legs.append(_leg(base, f"{r.player} u{line:g} {label}", label, grp, r.under_odds, pu, pp, sim_u, fu,
                         _result(x < line, x == line), line=line, proj=proj, flag=flag, market_key=r.market,
                         side="under", player_key=r.key))
    # the median sits next to the line in the UI: a line is a median, and yardage means run well above it
    return [l for l in legs if l is not None]
