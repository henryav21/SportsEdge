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
MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
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
            "ml": bool(ML_WORD.search(t)), "total_word": "total" in t}


def read(text: str, legs: list[dict]) -> dict:
    """{'picks': [{key, selection, line, tool_line, text}], 'odds', 'stake', 'unmatched': [{text, why}]}.
    `line` is the line on the slip where it differs from the tool's (None when they agree or there is none)."""
    pool = Pool(legs)
    rows = [_scan(ln.strip(), pool) for ln in (text or "").splitlines() if ln.strip()]
    picks, unmatched, used = {}, [], set()

    def near(i, order):
        return [(j, rows[j]) for j in (i + d for d in order) if 0 <= j < len(rows)]

    def add(leg, line, src):
        if leg["key"] in picks:
            return
        tool = leg.get("line")
        differs = line is not None and tool is not None and abs(float(tool) - float(line)) > 1e-9
        picks[leg["key"]] = {"key": leg["key"], "selection": leg.get("selection"), "tool_line": tool,
                             "line": float(line) if differs else None, "text": src}

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
            add(leg, line, r["raw"])

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
                add(leg, val, r["raw"])
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
                add(leg, r["handicap"][0], r["raw"])
        elif any(x["ml"] for _, x in near(i, (0, 1, -1))):
            leg = pool.leg(game["sport"], game["game_id"], "ml", side)
            if leg:
                used.add(i)
                add(leg, None, r["raw"])

    # 3) the odds for the whole bet, and the stake
    marked = [o for r in rows if ODDS_WORD.search(r["text"]) for o in r["odds"]]
    every = [o for r in rows for o in r["odds"]]
    odds = marked[0] if marked else (max(every) if every and len(picks) > 1 else (every[0] if every else None))
    stake = next((r["money"][0] for r in rows if r["money"] and STAKE_WORD.search(r["text"]) and not PAYS_WORD.search(r["text"])), None)
    if stake is None:
        plain = [r["money"][0] for r in rows if r["money"] and not PAYS_WORD.search(r["text"])]
        stake = plain[0] if len(plain) == 1 else None
    return {"picks": list(picks.values()), "odds": odds, "stake": stake, "unmatched": unmatched, "lines_read": len(rows)}
