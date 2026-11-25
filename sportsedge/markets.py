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
            mo = float(np.clip(ref_fair + (pc - ref_pc), 0.01, 0.99))
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
