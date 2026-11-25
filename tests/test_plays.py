"""Offline tests for the suggested picks and parlays, and for recording a bet on a game that has started.
Run:  python -m unittest discover tests"""
import os
import shutil
import tempfile
import unittest

import numpy as np
import pandas as pd

from sportsedge import plays, tracker

RNG = np.random.default_rng(7)


def leg(game, name, odds=-110, p=0.52, player="", start="2026-11-29T18:00:00+00:00", sport="NFL", side="over", market="pts",
        flag="", assumed=False, lean=None, date="2026-11-29"):
    dec = 1 + (100 / -odds if odds < 0 else odds / 100)
    grp = f"{game}|{player}|{market}" if player else f"{game}|{market}"
    l = {"key": f"{sport}|{game}|{market}|{side}|{player}", "sport": sport, "game_id": game, "game": game, "home": game[-3:], "away": game[:3],
         "start": start, "date": date, "selection": name, "subject": player or game, "market": market, "market_key": market, "side": side,
         "player_key": player, "group": grp, "line": 20.5, "odds": float(odds), "dec": dec, "p_win": p, "p_push": 0.0, "p_cond": p,
         "ev": p * dec - 1, "flag": flag, "result": np.where(RNG.random(4000) < p, 1, -1).astype(np.int8)}
    if assumed:
        l.update(assumed=True, lean=lean if lean is not None else 0.2)
    return l


class PicksForAGame(unittest.TestCase):
    def test_best_return_first_one_side_per_bet_one_pick_per_player(self):
        legs = [leg("AAA@BBB", "A o20.5", p=0.50, player="a"), leg("AAA@BBB", "A u20.5", p=0.50, player="a", side="under", odds=-105),
                leg("AAA@BBB", "A o5.5 reb", p=0.56, player="a", market="reb"), leg("AAA@BBB", "B o10.5", p=0.54, player="b")]
        r = plays.ranked(legs)
        self.assertEqual([l["selection"] for l in r], ["A o5.5 reb", "B o10.5"])  # player a appears once, with his best pick

    def test_what_is_never_suggested(self):
        legs = [leg("AAA@BBB", "news", p=0.60, player="a", flag="check news"),         # the model is far from the book
                leg("AAA@BBB", "heavy favourite", odds=-900, p=0.90, player="b"),       # adds almost nothing to a payout
                leg("AAA@BBB", "long shot", odds=600, p=0.14, player="c"),
                leg("AAA@BBB", "poor price", p=0.47, player="d"),                       # gives up too much to the book
                leg("AAA@BBB", "weak lean", p=0.51, player="e", assumed=True, lean=0.04),  # unpriced, simulator barely leans
                leg("AAA@BBB", "strong lean", odds=-115, p=0.55, player="f", assumed=True, lean=0.2)]
        self.assertEqual([l["selection"] for l in plays.ranked(legs)], ["strong lean"])


class BestParlays(unittest.TestCase):
    def test_one_parlay_in_each_payout_band(self):
        pool = [leg("AAA@BBB", f"p{i}", p=0.53 - i * 0.002, player=f"p{i}") for i in range(6)]
        found = plays.best(pool, max_legs=4)
        self.assertEqual(set(found), {"safe", "mid", "long"})
        self.assertEqual([l["selection"] for l in found["safe"]], ["p0", "p1"])            # the two best picks
        self.assertEqual([l["selection"] for l in found["mid"]], ["p0", "p1", "p2"])
        for tier, legs in found.items():
            dec = np.prod([l["dec"] for l in legs])
            lo, hi = next((t[2], t[3]) for t in plays.TIERS if t[0] == tier)
            self.assertTrue(lo <= dec < hi)

    def test_across_games_no_more_than_two_from_one_game(self):
        pool = [leg("AAA@BBB", f"a{i}", p=0.56, player=f"a{i}") for i in range(4)] + [leg("CCC@DDD", "c0", p=0.50, player="c0"),
                                                                                    leg("EEE@FFF", "e0", p=0.50, player="e0")]
        found = plays.best(pool, max_legs=4, min_games=2, per_game=2)
        for legs in found.values():
            games = [l["game_id"] for l in legs]
            self.assertGreaterEqual(len(set(games)), 2)
            self.assertLessEqual(max(games.count(g) for g in set(games)), 2)
        # the best four picks are all from one game: the long shot still takes only two of them
        self.assertEqual(sorted(l["game_id"] for l in found["long"]), ["AAA@BBB", "AAA@BBB", "CCC@DDD", "EEE@FFF"])


class TheWholeSlate(unittest.TestCase):
    def setUp(self):
        early = [leg(g, f"{g} {i}", p=0.53, player=f"{g}{i}") for g in ("AAA@BBB", "CCC@DDD") for i in range(3)]
        late = [leg("EEE@FFF", f"late {i}", p=0.53, player=f"l{i}", start="2026-11-29T21:25:00+00:00") for i in range(3)]
        monday = [leg("GGG@HHH", f"mon {i}", odds=-115, p=0.55, player=f"m{i}", start="2026-12-01T01:15:00+00:00", date="2026-11-30",
                      assumed=True) for i in range(3)]
        self.out = plays.build(early + late + monday)

    def test_each_game_gets_picks_and_parlays(self):
        self.assertEqual([g["game"] for g in self.out["games"]], ["AAA@BBB", "CCC@DDD", "EEE@FFF", "GGG@HHH"])
        g = self.out["games"][0]
        self.assertEqual(len(g["picks"]), 3)
        self.assertTrue(all(p["same_game"] and p["est"] for p in g["parlays"]))  # a same-game price is the sportsbook's own

    def test_days_and_groups(self):
        self.assertEqual([(d["date"], d["n_games"]) for d in self.out["days"]], [("2026-11-29", 3)])  # Monday has one game: no day parlay
        self.assertEqual([(x["n_games"], x["games"]) for x in self.out["groups"]], [(2, ["AAA@BBB", "CCC@DDD"])])  # the early window
        safe = next(p for p in self.out["days"][0]["parlays"] if p["tier"] == "safe")
        self.assertEqual((safe["n"], len(safe["games"]), safe["est"]), (2, 2, False))  # two priced picks, two games: an exact price

    def test_an_unpriced_pick_makes_the_payout_an_estimate(self):
        monday = self.out["games"][-1]
        self.assertTrue(all(p["assumed"] == p["n"] and p["est"] for p in monday["parlays"]))

    def test_a_priced_pick_replaces_the_assumed_one(self):
        a = leg("AAA@BBB", "assumed", odds=-115, p=0.56, player="x", assumed=True)
        b = {**leg("AAA@BBB", "priced", odds=-105, p=0.56, player="x"), "key": a["key"]}
        out = plays.build([a, b, leg("AAA@BBB", "other", p=0.53, player="y")])
        self.assertIn(a["key"], out["games"][0]["picks"])
        self.assertFalse(any(p["assumed"] for p in out["games"][0]["parlays"]))


class StartedGames(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old = os.environ.get("SPORTSEDGE_HISTORY")
        os.environ["SPORTSEDGE_HISTORY"] = self.dir
        self.now = pd.Timestamp("2026-11-25T02:00:00Z")

        def row(key, start, status="open", **kw):
            base = {"key": key, "sport": "NBA", "game_id": "E1", "date": "2026-11-24", "start": start, "game": "DET @ ORL", "home": "ORL",
                    "away": "DET", "market": "Points", "market_key": "pts", "side": "over", "player_key": "a b", "subject": "A B",
                    "selection": "A B o20.5 Points", "line": 20.5, "odds": -110.0, "dec": 1.909, "p_win": 0.5, "p_push": 0.0,
                    "p_cond": 0.5, "p_fair": 0.5, "book": "DraftKings", "status": status}
            return {**base, **kw}

        rows = [row("in-play", "2026-11-25 00:00:00+00:00"),
                row("total", "2026-11-25 00:00:00+00:00", market="Total", market_key="total", player_key=None, subject="DET @ ORL",
                    selection="Over 210.5", line=210.5),
                row("to-come", "2026-11-25 03:00:00+00:00"), row("yesterday", "2026-11-24 00:00:00+00:00"),
                row("settled", "2026-11-25 00:30:00+00:00", status="won")]
        tracker._write(pd.DataFrame(rows).reindex(columns=tracker.PICK_COLS), "picks.csv")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("SPORTSEDGE_HISTORY", None)
        else:
            os.environ["SPORTSEDGE_HISTORY"] = self.old
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_only_games_under_way_and_unsettled(self):
        legs = {l["key"]: l for l in tracker.started_legs(now=self.now)}
        self.assertEqual(set(legs), {"in-play", "total"})
        prop, total = legs["in-play"], legs["total"]
        self.assertTrue(prop["started"])
        self.assertEqual((prop["line"], prop["odds"], prop["group"]), (20.5, -110.0, "E1|a b|pts"))
        self.assertEqual((total["player_key"], total["group"]), ("", "E1|total"))  # the two sides of the total share a group
        self.assertEqual(prop["start"][:16], "2026-11-25T00:00")

    def test_a_bet_can_be_recorded_at_the_line_it_was_really_made_at(self):
        legs = {l["key"]: l for l in tracker.started_legs(now=self.now)}
        self.assertEqual(tracker.with_line(legs["in-play"], 22.5)["selection"], "A B o22.5 Points")
        self.assertEqual(tracker.with_line(legs["total"], 208)["selection"], "Over 208")
        spread = {"market_key": "spread", "side": "home", "home": "ORL", "away": "DET", "line": -2.5, "selection": "ORL -2.5"}
        self.assertEqual(tracker.with_line(spread, 3.5)["selection"], "ORL +3.5")
        ml = {"market_key": "ml", "side": "home", "line": None, "selection": "ORL ML"}
        self.assertIs(tracker.with_line(ml, 1.5), ml)  # a moneyline has no line to change


if __name__ == "__main__":
    unittest.main()
