"""Offline tests for reading a pasted bet slip.  Run:  python -m unittest discover tests"""
import unittest

from sportsedge import slipread


def prop(game, home, away, player, market, label, line, sport="NBA"):
    out = []
    for side in ("over", "under"):
        out.append({"key": f"{sport}|{game}|{market}|{side}|{player.lower()}", "sport": sport, "game_id": game, "home": home, "away": away,
                    "start": "2026-11-25T01:00:00+00:00", "subject": player, "player_key": player.lower(), "market": label,
                    "market_key": market, "side": side, "line": line,
                    "selection": f"{player} {'o' if side == 'over' else 'u'}{line:g} {label}"})
    return out


def game(gid, home, away, total, spread_home, sport="NBA", start="2026-11-25T01:00:00+00:00"):
    base = {"sport": sport, "game_id": gid, "home": home, "away": away, "start": start, "subject": f"{away} @ {home}", "player_key": ""}
    k = lambda mk, side: f"{sport}|{gid}|{mk}|{side}|"  # noqa: E731
    return [{**base, "key": k("ml", "home"), "market_key": "ml", "side": "home", "line": None, "selection": f"{home} ML"},
            {**base, "key": k("ml", "away"), "market_key": "ml", "side": "away", "line": None, "selection": f"{away} ML"},
            {**base, "key": k("spread", "home"), "market_key": "spread", "side": "home", "line": spread_home, "selection": f"{home} {spread_home:+g}"},
            {**base, "key": k("spread", "away"), "market_key": "spread", "side": "away", "line": -spread_home, "selection": f"{away} {-spread_home:+g}"},
            {**base, "key": k("total", "over"), "market_key": "total", "side": "over", "line": total, "selection": f"Over {total:g}"},
            {**base, "key": k("total", "under"), "market_key": "total", "side": "under", "line": total, "selection": f"Under {total:g}"}]


LEGS = (game("G1", "WAS", "BOS", 218.5, 10.5) + game("G2", "DEN", "DAL", 234.5, -8.5)
        + game("N1", "DAL", "PHI", 47.5, -8.5, sport="NFL", start="2026-11-26T21:30:00+00:00")
        + prop("G1", "WAS", "BOS", "Jayson Tatum", "pts", "Points", 25.5) + prop("G1", "WAS", "BOS", "Jayson Tatum", "reb", "Rebounds", 8.5)
        + prop("G1", "WAS", "BOS", "Jayson Tatum", "pra", "Pts+Reb+Ast", 39.5) + prop("G2", "DEN", "DAL", "Nikola Jokić", "ast", "Assists", 9.5)
        + prop("G2", "DEN", "DAL", "Jamal Murray", "fg3m", "Threes", 2.5)
        + prop("N1", "DAL", "PHI", "Saquon Barkley", "rush_yds", "Rush Yds", 78.5, sport="NFL"))


def keys(out):
    return [p["key"] for p in out["picks"]]


class OnePickPerLine(unittest.TestCase):
    def test_a_parlay_with_props_a_total_a_spread_and_a_moneyline(self):
        out = slipread.read("""3 Pick Parlay  +584
            Jayson Tatum Over 25.5 Points
            BOS Celtics @ WAS Wizards
            Nikola Jokic Under 9.5 Assists
            Over 234.5 Total Points
            DAL Mavericks @ DEN Nuggets
            PHI Eagles +8.5
            DEN Nuggets Moneyline
            Wager: $10.00
            To Pay: $68.40""", LEGS)
        self.assertEqual(keys(out), ["NBA|G1|pts|over|jayson tatum", "NBA|G2|ast|under|nikola jokić", "NBA|G2|total|over|",
                                     "NFL|N1|spread|away|", "NBA|G2|ml|home|"])
        self.assertEqual((out["odds"], out["stake"]), (584.0, 10.0))
        self.assertEqual(out["unmatched"], [])
        self.assertTrue(all(p["line"] is None for p in out["picks"]))  # every line agrees with the tool's

    def test_a_different_line_is_kept_as_yours(self):
        out = slipread.read("Jayson Tatum Over 23.5 Points\nBOS Celtics -9.5\nUnder 220.5\nBOS Celtics @ WAS Wizards\n-110", LEGS)
        by = {p["key"]: p for p in out["picks"]}
        self.assertEqual(by["NBA|G1|pts|over|jayson tatum"]["line"], 23.5)
        self.assertEqual(by["NBA|G1|spread|away|"]["line"], -9.5)
        self.assertEqual(by["NBA|G1|total|under|"]["line"], 220.5)
        self.assertEqual(out["odds"], -110.0)  # a handicap is not mistaken for the odds


class SpreadOverSeveralLines(unittest.TestCase):
    def test_the_number_the_stat_and_the_player_on_separate_lines(self):
        out = slipread.read("""Over 8.5
            Jayson Tatum - Rebounds
            Boston Celtics @ Washington Wizards
            2+
            Jamal Murray Threes
            Total wager $25""", LEGS)
        by = {p["key"]: p for p in out["picks"]}
        self.assertIn("NBA|G1|reb|over|jayson tatum", by)
        self.assertEqual(by["NBA|G2|fg3m|over|jamal murray"]["line"], 1.5)  # "2+" means over 1.5
        self.assertEqual(out["stake"], 25.0)

    def test_combined_stats_are_not_read_as_points(self):
        out = slipread.read("Jayson Tatum Over 39.5 Points + Rebounds + Assists", LEGS)
        self.assertEqual(keys(out), ["NBA|G1|pra|over|jayson tatum"])

    def test_football(self):
        out = slipread.read("Saquon Barkley Under 78.5 Rushing Yards\nDallas Cowboys -8.5\nOver 47.5\nPHI Eagles @ DAL Cowboys", LEGS)
        self.assertEqual(keys(out), ["NFL|N1|rush_yds|under|saquon barkley", "NFL|N1|spread|home|", "NFL|N1|total|over|"])


class WhatCannotBeRead(unittest.TestCase):
    def test_a_stat_the_tool_has_no_line_for_is_handed_back(self):
        out = slipread.read("Jayson Tatum Over 1.5 Steals\nJayson Tatum Over 25.5 Points", LEGS)
        self.assertEqual(keys(out), ["NBA|G1|pts|over|jayson tatum"])
        self.assertEqual(len(out["unmatched"]), 1)
        self.assertIn("no line", out["unmatched"][0]["why"])

    def test_nothing_is_guessed_from_plain_text(self):
        out = slipread.read("Good luck tonight!\nBet placed 7:02 PM\nID 1234567890", LEGS)
        self.assertEqual((out["picks"], out["odds"], out["stake"]), ([], None, None))

    def test_the_matchup_line_is_not_a_pick(self):
        self.assertEqual(slipread.read("BOS Celtics @ WAS Wizards", LEGS)["picks"], [])

    def test_accents_and_the_same_pick_twice(self):
        out = slipread.read("Nikola Jokić Over 9.5 Assists\nNikola Jokic Over 9.5 Assists", LEGS)
        self.assertEqual(keys(out), ["NBA|G2|ast|over|nikola jokić"])


if __name__ == "__main__":
    unittest.main()
