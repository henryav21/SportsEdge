"""Read a bet slip pasted as text from a sportsbook and match its picks to the tool's own.

A pasted slip has no fixed layout: a pick may be one line ("Jayson Tatum Over 25.5 Points") or several
("Over 25.5" / "Jayson Tatum - Points" / "BOS Celtics @ WAS Wizards"). So each line is scanned for what it
mentions (a player, a team, over/under and a number, a stat, odds, money) and picks are put together from a
line and its neighbours. Whatever cannot be matched to a pick the tool knows is handed back, never guessed.
"""
from __future__ import annotations

import re
import unicodedata

NICKNAMES = {
    "nba": {"hawks": "ATL", "celtics": "BOS", "nets": "BKN", "hornets": "CHA", "bulls": "CHI", "cavaliers": "CLE", "cavs": "CLE",
            "mavericks": "DAL", "mavs": "DAL", "nuggets": "DEN", "pistons": "DET", "warriors": "GSW", "rockets": "HOU",
            "pacers": "IND", "clippers": "LAC", "lakers": "LAL", "grizzlies": "MEM", "heat": "MIA", "bucks": "MIL",
            "timberwolves": "MIN", "wolves": "MIN", "pelicans": "NOP", "knicks": "NYK", "thunder": "OKC", "magic": "ORL",
            "76ers": "PHI", "sixers": "PHI", "suns": "PHX", "trail blazers": "POR", "blazers": "POR", "kings": "SAC",
            "spurs": "SAS", "raptors": "TOR", "jazz": "UTA", "wizards": "WAS"},
    "nfl": {"cardinals": "ARI", "falcons": "ATL", "ravens": "BAL", "bills": "BUF", "panthers": "CAR", "bears": "CHI",
            "bengals": "CIN", "browns": "CLE", "cowboys": "DAL", "broncos": "DEN", "lions": "DET", "packers": "GB",
            "texans": "HOU", "colts": "IND", "jaguars": "JAX", "chiefs": "KC", "raiders": "LV", "chargers": "LAC", "rams": "LA",
            "dolphins": "MIA", "vikings": "MIN", "patriots": "NE", "saints": "NO", "giants": "NYG", "jets": "NYJ", "eagles": "PHI",
            "steelers": "PIT", "49ers": "SF", "niners": "SF", "seahawks": "SEA", "buccaneers": "TB", "bucs": "TB", "titans": "TEN",
            "commanders": "WAS"},
}
# the longer names first, so "points + rebounds + assists" is not read as "points"
STATS = [
    (r"p(?:oin)?ts?\s*(?:\+|and|,)\s*reb(?:ound)?s?\s*(?:\+|and|,)?\s*(?:and\s*)?ass?(?:is)?ts?|\bpra\b", "pra"),
    (r"p(?:oin)?ts?\s*(?:\+|and)\s*reb(?:ound)?s?", "pr"), (r"p(?:oin)?ts?\s*(?:\+|and)\s*ass?(?:is)?ts?", "pa"),
    (r"reb(?:ound)?s?\s*(?:\+|and)\s*ass?(?:is)?ts?|ass?(?:is)?ts?\s*(?:\+|and)\s*reb(?:ound)?s?", "ra"),
    (r"rush(?:ing)?\s*(?:\+|and|plus)\s*rec(?:eiving)?\s*(?:yards|yds)?", "rush_rec_yds"),
    (r"pass(?:ing)?\s*(?:yards|yds)", "pass_yds"), (r"pass(?:ing)?\s*(?:touchdowns|tds?)", "pass_tds"),
    (r"(?:pass(?:ing)?\s*)?completions", "pass_cmp"), (r"pass(?:ing)?\s*attempts", "pass_att"),
    (r"rush(?:ing)?\s*(?:yards|yds)", "rush_yds"), (r"rush(?:ing)?\s*attempts|carries", "rush_att"),
    (r"rec(?:eiving)?\s*(?:yards|yds)", "rec_yds"), (r"receptions|catches", "receptions"),
    (r"any\s*time\s*(?:touchdown|td)|to score a touchdown", "anytime_td"),
    (r"3\s*-?\s*p(?:oin)?t(?:er)?s?(?:\s*made)?|threes|three\s*pointers?(?:\s*made)?", "fg3m"),
    (r"points|\bpts\b", "pts"), (r"rebounds|\breb\b", "reb"), (r"assists|\bast\b", "ast"), (r"steals|\bstl\b", "stl"),
    (r"blocks|\bblk\b", "blk"),
]
STATS = [(re.compile(p), k) for p, k in STATS]
OU = re.compile(r"\b(over|under|o|u)\s*(\d+(?:\.\d+)?)\b")
PLUS = re.compile(r"\b(\d+)\+(?!\d)")
SIGNED = re.compile(r"(?<![\w.])([+\-−])\s?(\d+(?:\.\d+)?)(?![\d.])")
MONEY = re.compile(r"(?<!\+)\$\s?(\d[\d,]*(?:\.\d+)?)")  # not "+$5", a quick-add button
PRICE = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d)?)\s?(?:%|¢)")     # a contract's price on a trade slip: 52% or 52¢
STAKE_WORD = re.compile(r"wager|stake|risk|bet amount|amount bet|total bet")
PAYS_WORD = re.compile(r"to win|to pay|payout|pays|returns?|winnings")
ODDS_WORD = re.compile(r"\bodds\b|parlay|\bsgp\b|\bpicks?\b|\blegs?\b")
ML_WORD = re.compile(r"money\s*line|\bml\b|to win(?! \$)|match winner|winner")
GAME_MARKETS = ("ml", "spread", "total")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+\-.@$,/ ]", " ", s.lower().replace("−", "-").replace("'", ""))).strip()


class Pool:
    """Every pick the tool knows, arranged for looking up by player, team and game."""

    def __init__(self, legs: list[dict]):
        self.legs = [l for l in legs if l.get("key")]
        self.players: dict[str, str] = {}   # normalised name -> player key
        self.games: dict[tuple, dict] = {}  # (sport, game_id) -> {home, away, start, total}
        for l in self.legs:
            g = self.games.setdefault((l["sport"].lower(), l["game_id"]), {"sport": l["sport"].lower(), "game_id": l["game_id"],
                                                                         "home": l["home"], "away": l["away"], "start": str(l.get("start") or ""),
                                                                         "total": None})
            if l.get("market_key") == "total" and l.get("line") is not None:
                g["total"] = float(l["line"])
            if l.get("player_key"):
                self.players[norm(l.get("subject") or l["player_key"])] = l["player_key"]
                self.players[norm(l["player_key"])] = l["player_key"]
        self.teams = {(g["sport"], t) for g in self.games.values() for t in (g["home"], g["away"])}
        last: dict[str, set] = {}
        for name, key in self.players.items():
            last.setdefault(name.split(" ")[-1], set()).add(key)
        self.surnames = {s: next(iter(k)) for s, k in last.items() if len(k) == 1 and len(s) > 3}  # a surname only one player has

    def players_in(self, text: str) -> list[str]:
        found, taken = [], text
        for name in sorted(self.players, key=len, reverse=True):
            if len(name) > 4 and re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", taken):
                if self.players[name] not in found:
                    found.append(self.players[name])
                taken = taken.replace(name, " ")
        if not found:
            for w in taken.split(" "):
                if w in self.surnames and re.search(r"over|under|\d", text):
                    found.append(self.surnames[w])
                    break
        return found

    def teams_in(self, text: str, raw: str) -> list[tuple]:
        hits = []
        for sport, names in NICKNAMES.items():
            for nick, abbr in names.items():
                m = re.search(rf"(?<![a-z0-9]){re.escape(nick)}(?![a-z0-9])", text)
                if m and (sport, abbr) in self.teams:
                    hits.append((m.start(), sport, abbr))
        if not hits:  # no nickname: the tool's own abbreviation written in capitals ("DEN ML")
            for m in re.finditer(r"\b[A-Z]{2,3}\b", raw):
                for sport in ("nfl", "nba"):
                    if (sport, m.group()) in self.teams:
                        hits.append((m.start(), sport, m.group()))
                        break
        out = []
        for _, sport, abbr in sorted(hits):
            if (sport, abbr) not in out:
                out.append((sport, abbr))
        return out

    def game_of(self, team: tuple, other: tuple | None = None) -> dict | None:
        sport, abbr = team
        cands = [g for g in self.games.values() if g["sport"] == sport and abbr in (g["home"], g["away"])
                 and (other is None or other[1] in (g["home"], g["away"]))]
        return min(cands, key=lambda g: g["start"]) if cands else None

    def leg(self, sport, game_id, market, side, player="") -> dict | None:
        for l in self.legs:
            if (l["sport"].lower() == sport and l["game_id"] == game_id and l.get("market_key") == market and l.get("side") == side
                    and (l.get("player_key") or "") == player):
                return l
        return None


def _scan(raw: str, pool: Pool) -> dict:
    t = norm(raw)
    ou = OU.search(t)
    plus = PLUS.search(t)
    signed = [(float(n) * (-1 if s in "-−" else 1), s + n) for s, n in SIGNED.findall(raw.replace("−", "-"))]
    return {"raw": raw, "text": t, "players": pool.players_in(t), "teams": pool.teams_in(t, raw),
            "ou": (("over" if ou.group(1) in ("over", "o") else "under"), float(ou.group(2))) if ou else None,
            "plus": float(plus.group(1)) if plus else None,
            "stat": next((k for rx, k in STATS if rx.search(t)), None),
            "odds": [v for v, s in signed if abs(v) >= 100 and float(v).is_integer()],
            "handicap": [v for v, s in signed if abs(v) < 60],
            "money": [float(m.replace(",", "")) for m in MONEY.findall(raw)],
            "price": next((float(x) for x in PRICE.findall(raw) if 0 < float(x) < 100), None),
            "pays": bool(PAYS_WORD.search(t)), "stake_word": bool(STAKE_WORD.search(t)),
            "ml": bool(ML_WORD.search(t)), "total_word": "total" in t}


def read(text: str, legs: list[dict]) -> dict:
    """{'picks': [{key, selection, line, tool_line, text}], 'odds', 'stake', 'singles', 'unmatched': [{text, why}]}.
    `line` is the line on the slip where it differs from the tool's (None when they agree or there is none).

    A trade slip (a trading app's bet slip) gives each pick a price as a percent and, for a stake, a payout.
    When every pick has its own payout they are separate single bets, returned in 'singles' with the odds
    each one really pays: payout over stake, which already has the app's fee in it."""
    pool = Pool(legs)
    rows = [_scan(ln.strip(), pool) for ln in (text or "").splitlines() if ln.strip()]
    picks, unmatched, used = {}, [], set()

    def near(i, order):
        return [(j, rows[j]) for j in (i + d for d in order) if 0 <= j < len(rows)]

    where: dict = {}  # pick key -> the row it was read from

    def add(leg, line, src, at=None):
        if leg["key"] in picks:
            return
        tool = leg.get("line")
        differs = line is not None and tool is not None and abs(float(tool) - float(line)) > 1e-9
        picks[leg["key"]] = {"key": leg["key"], "selection": leg.get("selection"), "tool_line": tool,
                             "line": float(line) if differs else None, "text": src}
        where[leg["key"]] = next((j for j, x in enumerate(rows) if x["raw"] == src), 0) if at is None else at

    # 1) player props: the player's line, with the side, number and stat from it or the lines around it
    for i, r in enumerate(rows):
        for pk in r["players"]:
            side = line = stat = None
            for j, x in near(i, (0, 1, -1, 2)):
                if j != i and x["players"]:
                    continue  # that line belongs to another player
                if side is None and x["ou"]:
                    side, line = x["ou"]
                    used.add(j)
                elif side is None and x["plus"] is not None:
                    side, line = "over", x["plus"] - 0.5  # "25+" is over 24.5
                    used.add(j)
                if stat is None and x["stat"]:
                    stat = x["stat"]
            mine = [l for l in pool.legs if l.get("player_key") == pk]
            if stat is None and line is not None:  # no stat named: only one of his lines is at that number
                same = {l["market_key"] for l in mine if l.get("line") is not None and abs(float(l["line"]) - line) < 0.01}
                stat = next(iter(same)) if len(same) == 1 else None
            if stat == "anytime_td":
                side = "yes"
            if stat is None or side is None:
                unmatched.append({"text": r["raw"], "why": "could not tell which stat or which side this is"})
                continue
            leg = next((l for l in mine if l["market_key"] == stat and l["side"] == side), None)
            if leg is None:
                unmatched.append({"text": r["raw"], "why": "the tool has no line for this player and stat"})
                continue
            used.add(i)
            add(leg, line, r["raw"], i)

    # 2) game bets: a spread or moneyline on a line that names a team, a total on an over/under line
    for i, r in enumerate(rows):
        if i in used or r["players"]:
            continue
        around = near(i, (0, 1, -1, 2, -2))
        if r["ou"] and (not r["stat"] or (r["total_word"] and r["stat"] == "pts")):  # "Over 234.5 Total Points" is a game total
            side, val = r["ou"]
            two = next((x["teams"] for _, x in around if len(x["teams"]) >= 2), None)
            game = pool.game_of(two[0], two[1]) if two else None
            if game is None:  # no matchup named nearby: the only game whose total is anywhere near this number
                close = [g for g in pool.games.values() if g["total"] is not None and abs(g["total"] - val) <= 8]
                game = close[0] if len(close) == 1 else None
            if game is None or game["total"] is None or abs(game["total"] - val) > 25:
                if r["total_word"] or two:
                    unmatched.append({"text": r["raw"], "why": "could not tell which game this total is for"})
                continue
            leg = pool.leg(game["sport"], game["game_id"], "total", side)
            if leg:
                used.add(i)
                add(leg, val, r["raw"], i)
            continue
        if not r["teams"]:
            continue
        team = r["teams"][0]
        other = r["teams"][1] if len(r["teams"]) > 1 else None
        if other and not r["handicap"]:
            continue  # "BOS Celtics @ WAS Wizards": the matchup, not a pick
        game = pool.game_of(team, other)
        if game is None:
            continue
        side = "home" if game["home"] == team[1] else "away"
        if r["handicap"]:
            leg = pool.leg(game["sport"], game["game_id"], "spread", side)
            if leg:
                used.add(i)
                add(leg, r["handicap"][0], r["raw"], i)
        elif any(x["ml"] for _, x in near(i, (0, 1, -1))):
            leg = pool.leg(game["sport"], game["game_id"], "ml", side)
            if leg:
                used.add(i)
                add(leg, None, r["raw"], i)

    # 3) what sits under each pick on a trade slip: its price, its stake and what that stake pays
    order = sorted(picks, key=lambda k: where[k])
    first = where[order[0]] if order else len(rows)
    under = {}
    for n, k in enumerate(order):
        end = where[order[n + 1]] if n + 1 < len(order) else len(rows)
        mine = rows[where[k]:end]
        under[k] = {"price": next((x["price"] for x in mine if x["price"] is not None), None),
                    "payout": next((x["money"][0] for x in mine if x["pays"] and x["money"]), None),
                    "stake": next((x["money"][0] for x in mine if x["money"] and not x["pays"] and not x["stake_word"]), None)}

    def american(dec):
        return None if dec is None or dec <= 1 else ((dec - 1) * 100 if dec >= 2 else -100 / (dec - 1))

    # 4) the odds for the whole bet, and the stake. Money before the first pick is the account balance, not a stake
    marked = [o for r in rows if ODDS_WORD.search(r["text"]) for o in r["odds"]]
    every = [o for r in rows for o in r["odds"]]
    odds = marked[0] if marked else (max(every) if every and len(picks) > 1 else (every[0] if every else None))
    body = list(enumerate(rows))[first:]
    stake = next((r["money"][0] for _, r in body if r["money"] and r["stake_word"] and not r["pays"]), None)
    if stake is None:  # "Stake All" on one line, the amount on the next
        stake = next((rows[j + 1]["money"][0] for j, r in body if r["stake_word"] and not r["money"] and j + 1 < len(rows)
                      and rows[j + 1]["money"] and not rows[j + 1]["pays"]), None)
    if stake is None:
        plain = [r["money"][0] for _, r in body if r["money"] and not r["pays"]]
        stake = plain[0] if len(plain) == 1 else None
    payouts = [r["money"][0] for _, r in body if r["pays"] and r["money"]]
    price = payout = None
    singles = []
    if order and all(under[k]["payout"] is not None for k in order) and (len(order) > 1 or odds is None):
        for k in order:  # each pick has its own payout: separate single bets
            u = under[k]
            st = u["stake"] if u["stake"] is not None else stake
            dec = u["payout"] / st if st else (100.0 / u["price"] if u["price"] else None)
            singles.append({**picks[k], "stake": st, "payout": u["payout"], "price": u["price"], "odds": american(dec)})
        if len(order) == 1:  # one pick: it is simply the bet
            odds, stake = singles[0]["odds"], singles[0]["stake"]
            price, payout = singles[0]["price"], singles[0]["payout"]
            singles = []
    elif odds is None and order:
        # a combo on a trade slip: one payout for the stake, or failing that one price
        if len(payouts) == 1 and stake:
            payout = payouts[0]
            odds = american(payout / stake)
        else:
            prices = [r["price"] for _, r in body if r["price"] is not None]
            price = prices[-1] if prices and len(order) == 1 else None
            odds = american(100.0 / price) if price else None
    return {"picks": list(picks.values()), "odds": odds, "stake": stake, "price": price, "payout": payout, "singles": singles,
            "unmatched": unmatched, "lines_read": len(rows)}
