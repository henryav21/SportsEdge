"""Suggested picks and parlays: for each game, each group of games that start together, and each day.

What "best" means here. Every pick has an expected return at its price (its chance x what it pays, minus 1).
A parlay priced from its picks' own odds returns the product of those, so for a payout of a given size the
best parlay is the one whose picks give up the least (or gain the most). The search ranks by exactly that,
inside three payout bands, so each scope gets a safer option, a middle one and a long shot, and each is the
most the picks allow for its payout. Picking by payout alone would just pick the longest odds on the board.

Two things the numbers cannot know, and the output says so (est = True):
  * picks from the same game: a sportsbook sets its own price for a same-game parlay, usually below the
    separate-pick price when the picks tend to win together. The chance to hit is measured from the shared
    simulation, so the fair odds are right; the payout is what the picks would pay if priced separately.
  * props that arrive without a price (NFL): they carry an assumed standard price (config.ASSUMED_PROP_ODDS).
"""
from __future__ import annotations

import math
from itertools import combinations

import numpy as np
import pandas as pd

from . import config, parlay

# (key, label, lowest payout, highest payout) as decimal odds: +100 to +300, +300 to +800, +800 to +4000
TIERS = [("safe", "Safer", 2.0, 4.0), ("mid", "Balanced", 4.0, 9.0), ("long", "Long shot", 9.0, 41.0)]
MIN_DEC, MAX_DEC = 1.25, 4.0   # one pick: shorter than -400 adds almost nothing to a payout, longer than +300 rarely lands
MIN_EV = -0.06                 # a pick giving up more than this to the sportsbook is not suggested
LEAN_MIN = 0.10                # an unpriced prop needs the simulator this far from an even bet to be suggested
PICKS_PER_GAME = 5
POOL_GAME, LEGS_GAME = 9, 4    # one game: best picks considered, most picks in a parlay
POOL_MULTI, LEGS_MULTI = 12, 6 # several games
PER_GAME = 2                   # picks from any one game in a parlay across games: "a few from each, not all on one"
GROUP_MINUTES = 90             # games starting within this long of each other form a group


def _ok(l: dict) -> bool:
    if l.get("flag") or l.get("result") is None:
        return False
    if not (MIN_DEC <= l["dec"] <= MAX_DEC) or l["ev"] is None or pd.isna(l["ev"]) or l["ev"] < MIN_EV:
        return False
    if l.get("assumed") and abs(l.get("lean") or 0.0) < LEAN_MIN:
        return False
    return True


def ranked(legs: list[dict]) -> list[dict]:
    """One game's picks, best expected return first: one side of each bet, one pick per player."""
    out, groups, players = [], set(), set()
    for l in sorted((l for l in legs if _ok(l)), key=lambda l: (-l["ev"], -l["p_win"])):
        who = l.get("player_key") or ""
        if l["group"] in groups or (who and who in players):
            continue
        groups.add(l["group"])
        if who:
            players.add(who)
        out.append(l)
    return out


def _value(l: dict) -> float:
    """log of what a pick returns per unit staked, on average."""
    return math.log(max(l["p_win"] * l["dec"] + l["p_push"], 1e-9))


def best(pool: list[dict], max_legs: int, min_games: int = 1, per_game: int | None = None) -> dict:
    """The best combination in each payout band: {tier key: [picks]}."""
    n = len(pool)
    val, ldec = [_value(l) for l in pool], [math.log(l["dec"]) for l in pool]
    gid = [(l["sport"], l["game_id"]) for l in pool]
    top: dict = {}
    for size in range(2, min(max_legs, n) + 1):
        for idx in combinations(range(n), size):
            games = [gid[i] for i in idx]
            distinct = len(set(games))
            if distinct < min(min_games, size):
                continue
            if per_game is not None and max(games.count(g) for g in set(games)) > per_game:
                continue
            dec = math.exp(sum(ldec[i] for i in idx))
            tier = next((t[0] for t in TIERS if t[2] <= dec < t[3]), None)
            if tier is None:
                continue
            # the picks that give up least for this payout; then the ones spread over more games, then fewer picks
            score = (sum(val[i] for i in idx), distinct, -size)
            if tier not in top or score > top[tier][0]:
                top[tier] = (score, idx)
    return {t: [pool[i] for i in idx] for t, (_, idx) in top.items()}


def describe(tier: str, legs: list[dict]) -> dict:
    """A suggested parlay with its chance (same-game picks keep their measured correlation) and its payout."""
    e = parlay.evaluate(legs)
    assumed = sum(1 for l in legs if l.get("assumed"))
    return {"tier": tier, "label": next(t[1] for t in TIERS if t[0] == tier),
            "leg_keys": [l["key"] for l in legs], "legs": [l["selection"] for l in legs],
            "games": sorted({l["game"] for l in legs}), "n": len(legs), "dec": e["dec"], "odds": e["odds"],
            "p_hit": e["p_win_all"], "fair_odds": e["fair_odds"], "ev": e["ev"], "lift": e["lift"],
            "same_game": bool(e["same_game"]), "assumed": assumed,
            # the payout is exact only when every pick has a real price and no two come from the same game
            "est": bool(e["same_game"] or assumed)}


def _suggest(pool: list[dict], max_legs: int, min_games: int = 1, per_game: int | None = None) -> list[dict]:
    found = best(pool, max_legs, min_games, per_game)
    return [describe(t[0], found[t[0]]) for t in TIERS if t[0] in found]


# ----------------------------------------------------------------------------- lottery tickets
# A lottery ticket is a parlay built for the size of the payout: a small stake, a small chance, a large return.
# There is still a best one. Of all the tickets that pay at least a given multiple of the stake, the best is the
# one most likely to hit, and that is what is searched for here.
LOTTO_TARGETS = [25, 100, 500, 2500]  # payout as a multiple of the stake: $10 becomes $250, $1,000, $5,000, $25,000
LOTTO_MIN_DEC = 1.12   # a pick shorter than about -830 adds almost nothing to a payout
LOTTO_MAX_DEC = 4.0    # longer than +300 and the tool's chances are at their least reliable
LOTTO_PER_GAME = 40    # picks considered from one game
LOTTO_STEP = 0.005     # payouts are counted in steps of half a percent (in log)


def _lotto_ok(l: dict) -> bool:
    """A pick a ticket can use: a real price (a payout cannot be promised on an assumed one), no warning on it,
    and a chance the tool can stand behind."""
    if l.get("assumed") or l.get("sim_only") or l.get("flag") or l.get("result") is None:
        return False
    return LOTTO_MIN_DEC <= l["dec"] <= LOTTO_MAX_DEC and 0 < l["p_win"] < 1


def _reach(by_game: dict, need: int) -> list[dict] | None:
    """One pass of the search: the likeliest picks, one a game at most, whose payouts add up to `need` steps."""
    lowest = -np.inf
    best = np.full(need + 1, lowest)
    best[0] = 0.0
    trail = []
    for cands in by_game.values():
        new = best.copy()
        frm, who = np.full(need + 1, -1), np.full(need + 1, -1)
        for i, l in enumerate(cands):
            w = int(round(math.log(l["dec"]) / LOTTO_STEP))
            if w <= 0:
                continue
            val = best + math.log(_sober(l))
            if w < need:  # payouts still short of the target: each lands w steps on
                seg = val[:need - w]
                idx = np.nonzero(seg > new[w:need])[0]
                new[w + idx], frm[w + idx], who[w + idx] = seg[idx], idx, i
            lo = max(need - w, 0)  # payouts that reach the target with this pick: the likeliest of them
            j = int(np.argmax(val[lo:]))
            if val[lo + j] > new[need]:
                new[need], frm[need], who[need] = val[lo + j], lo + j, i
        trail.append((frm, who, cands))
        best = new
    if not np.isfinite(best[need]):
        return None
    legs, at = [], need
    for frm, who, cands in reversed(trail):
        if who[at] >= 0:
            legs.append(cands[int(who[at])])
            at = int(frm[at])
    return legs[::-1]


def best_shot(by_game: dict, target: float) -> list[dict] | None:
    """The picks most likely to all win among those that together pay at least `target` times the stake: one pick
    a game at most, so the picks are independent and the payout is the picks' own prices multiplied.

    For each game in turn it keeps, for every payout reached so far, the likeliest way to reach it (a knapsack
    over the log of the payout, counted in small steps). "Likely" is the sober chance (see _sober), so a pick is
    taken for being cheap for what it pays, never for an edge the tool thinks it sees. The steps round each
    price a little, so the answer is checked against the real prices and the search nudged up until the ticket
    truly pays the target. None when the games cannot pay that much."""
    need = max(int(math.floor(math.log(target) / LOTTO_STEP)), 1)
    for _ in range(24):
        legs = _reach(by_game, need)
        if legs is None:
            return None
        if math.prod(l["dec"] for l in legs) >= target * (1 - 1e-9):
            return legs
        need += 1
    return None


def _sober(l: dict) -> float:
    """A pick's chance with no edge claimed: the tool's, or the sportsbook's own (its price with the fee taken out)
    where the tool is the more hopeful of the two. A search that takes the best-looking picks out of thousands
    finds, above all, the picks the tool has overrated, so the edge it seems to find is not counted."""
    fair = l.get("p_fair")
    if fair is None or pd.isna(fair):
        return float(l["p_win"])
    return float(min(l["p_win"], fair * (1.0 - float(l.get("p_push") or 0.0))))


def _ticket(kind: str, label: str, legs: list[dict], target: float | None = None) -> dict:
    e = parlay.evaluate(legs)
    sober = float(math.prod(_sober(l) for l in legs))
    return {"kind": kind, "label": label, "target": target, "leg_keys": [l["key"] for l in legs],
            "legs": [l["selection"] for l in legs], "games": sorted({l["game"] for l in legs}), "n": len(legs),
            "dec": e["dec"], "odds": e["odds"],
            # the chance and the average return shown: no edge claimed (see _sober). p_tool is the tool's own.
            "p_hit": sober, "ev": sober * e["dec"] - 1.0, "p_tool": e["p_win_all"]}


def lottery(legs: list[dict]) -> dict:
    """Lottery tickets for each day with two games or more, for each sport's whole slate, and for everything:
    {"scopes": [{"key", "kind", "date", "sport", "n_games", "start", "tickets": [...]}], "targets": [...]}.

    Each scope has a "call every game" ticket (the likelier winner of every game, by the tool's chances) and, for
    each payout in LOTTO_TARGETS the games can reach, the ticket most likely to hit that pays at least that."""
    by_key: dict = {}
    for l in legs:
        if l["key"] not in by_key or by_key[l["key"]].get("assumed"):
            by_key[l["key"]] = l
    games: dict = {}
    for l in by_key.values():
        games.setdefault((l["sport"], l["game_id"]), []).append(l)
    info = {g: (ls[0].get("date") or "", _start(ls[0])) for g, ls in games.items()}
    dates = sorted({d for d, _ in info.values() if d})
    sports = sorted({g[0] for g in games})
    wanted = [(f"day:{d}", "day", d, None, [g for g in games if info[g][0] == d]) for d in dates]
    wanted += [(f"sport:{sp}", "sport", None, sp, [g for g in games if g[0] == sp]) for sp in sports]
    wanted.append(("all", "all", None, None, list(games)))
    scopes, done = [], set()
    for key, kind, date, sport, mine in wanted:
        same = frozenset(mine)
        if len(mine) < 2 or same in done:  # one game is not a parlay; the same games twice is one scope
            continue
        done.add(same)
        mine.sort(key=lambda g: info[g][1])
        tickets, seen = [], set()
        # call every game: the likelier side of each game's moneyline
        winners = []
        for g in mine:
            ml = [l for l in games[g] if l.get("market_key") == "ml" and not l.get("assumed") and l.get("result") is not None and l["dec"] > 1]
            if ml:
                winners.append(max(ml, key=lambda l: l["p_win"]))
        if len(winners) >= 2:
            t = _ticket("all", "Call every game", winners)
            t["missing"] = len(mine) - len(winners)
            tickets.append(t)
            seen.add(tuple(sorted(t["leg_keys"])))
        pool = {g: sorted((l for l in games[g] if _lotto_ok(l)), key=lambda l: -_sober(l) * l["dec"])[:LOTTO_PER_GAME] for g in mine}
        pool = {g: ls for g, ls in pool.items() if ls}
        for target in LOTTO_TARGETS:
            found = best_shot(pool, float(target))
            if not found or len(found) < 2:
                continue
            t = _ticket("shot", f"Best shot at {target}x", found, float(target))
            sig = tuple(sorted(t["leg_keys"]))
            if sig in seen:
                continue
            seen.add(sig)
            tickets.append(t)
        if tickets:
            scopes.append({"key": key, "kind": kind, "date": date, "sport": None if sport is None else str(sport).lower(),
                           "n_games": len(mine), "start": info[mine[0]][1].isoformat(), "tickets": tickets})
    return {"scopes": scopes, "targets": LOTTO_TARGETS}


def _start(l: dict) -> pd.Timestamp:
    t = pd.Timestamp(l["start"])
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def build(legs: list[dict]) -> dict:
    """legs: every pick on the slate, priced ones and assumed-price ones (a priced pick wins a shared key).
    Returns the suggestions for each game, each group of games and each day."""
    by_key: dict = {}
    for l in legs:
        if l["key"] not in by_key or by_key[l["key"]].get("assumed"):
            by_key[l["key"]] = l
    by_game: dict = {}
    for l in by_key.values():
        by_game.setdefault((l["sport"], l["game_id"]), []).append(l)
    games, info = {}, {}
    for g, ls in by_game.items():
        r = ranked(ls)
        if not r:
            continue
        games[g] = r
        info[g] = {"sport": g[0].lower(), "game_id": g[1], "game": ls[0]["game"], "start": _start(ls[0]), "date": ls[0].get("date") or ""}

    out = {"games": [], "groups": [], "days": [], "keys": set()}

    def note(parlays):
        for p in parlays:
            out["keys"].update(p["leg_keys"])
        return parlays

    for g, r in sorted(games.items(), key=lambda kv: info[kv[0]]["start"]):
        picks = r[:PICKS_PER_GAME]
        out["keys"].update(l["key"] for l in picks)
        out["games"].append({**info[g], "start": info[g]["start"].isoformat(), "picks": [l["key"] for l in picks],
                             "parlays": note(_suggest(r[:POOL_GAME], LEGS_GAME))})

    def across(gs):
        pool = sorted((l for g in gs for l in games[g][:PER_GAME]), key=lambda l: (-l["ev"], -l["p_win"]))[:POOL_MULTI]
        return note(_suggest(pool, LEGS_MULTI, min_games=2, per_game=PER_GAME))

    days: dict = {}
    for g in games:
        days.setdefault(info[g]["date"], []).append(g)
    for date, gs in sorted(days.items()):
        gs.sort(key=lambda g: info[g]["start"])
        if len(gs) >= 2:
            p = across(gs)
            if p:
                out["days"].append({"date": date, "start": info[gs[0]]["start"].isoformat(), "n_games": len(gs),
                                    "sports": sorted({info[g]["sport"] for g in gs}), "parlays": p})
        # groups: one sport's games that start close together (the Sunday early window, a night's tip-offs)
        for sport in sorted({info[g]["sport"] for g in gs}):
            cluster: list = []
            mine = [g for g in gs if info[g]["sport"] == sport]
            for g in mine + [None]:
                if g is not None and (not cluster or (info[g]["start"] - info[cluster[0]]["start"]).total_seconds() <= GROUP_MINUTES * 60):
                    cluster.append(g)
                    continue
                if 2 <= len(cluster) < len(gs):  # a group that is the whole day would only repeat the day
                    p = across(cluster)
                    if p:
                        out["groups"].append({"sport": sport, "date": date, "start": info[cluster[0]]["start"].isoformat(),
                                              "n_games": len(cluster), "games": [info[x]["game"] for x in cluster], "parlays": p})
                cluster = [g] if g is not None else []
    out["groups"].sort(key=lambda x: x["start"])
    out["keys"] = sorted(out["keys"])
    return out
