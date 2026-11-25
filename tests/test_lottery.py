"""Offline tests for the lottery tickets: big-payout parlays for a small stake, each the likeliest to hit for
its size of payout.
Run:  python -m unittest discover tests"""
import itertools
import math
import unittest

import numpy as np

from sportsedge import plays

RNG = np.random.default_rng(11)


def leg(game, name, dec, p, market="ml", fair=None, sport="NBA", date="2026-11-25", **kw):
    """A priced pick with its own simulated results (independent of every other pick's)."""
    res = np.where(RNG.random(4000) < p, 1, 0).astype(np.int8)
    l = {"key": f"{sport}|{game}|{market}|{name}|", "sport": sport, "game_id": game, "game": f"A @ {game}", "group": f"{game}|{market}",
         "selection": name, "market_key": market, "side": name, "dec": dec, "odds": 100 * (dec - 1) if dec >= 2 else -100 / (dec - 1),
         "p_win": p, "p_push": 0.0, "p_cond": p, "p_fair": p if fair is None else fair, "ev": p * dec - 1, "flag": "",
         "result": res, "start": f"{date}T23:00:00+00:00", "date": date, "player_key": ""}
    l.update(kw)
    return l


class BestShot(unittest.TestCase):
    def games(self):
        # three games, each with a favourite, an underdog and a spread
        return {g: [leg(g, "fav", 1.5, 0.64), leg(g, "dog", 2.8, 0.34), leg(g, "spread", 1.91, 0.5, market="spread")] for g in ("G1", "G2", "G3")}

    def brute(self, by_game, target):
        """Every way to take at most one pick a game: the likeliest that pays the target."""
        best, how = -1.0, None
        for combo in itertools.product(*[[None] + ls for ls in by_game.values()]):
            ls = [l for l in combo if l is not None]
            if ls and math.prod(l["dec"] for l in ls) >= target:
                p = math.prod(l["p_win"] for l in ls)
                if p > best:
                    best, how = p, ls
        return best, how

    def test_it_finds_the_likeliest_ticket_that_pays_the_target(self):
        by_game = self.games()
        for target in (2.5, 4.0, 6.0, 10.0, 20.0):
            got = plays.best_shot(by_game, target)
            want_p, _ = self.brute(by_game, target)
            self.assertGreaterEqual(math.prod(l["dec"] for l in got), target)             # never pays less than promised
            self.assertGreaterEqual(math.prod(l["p_win"] for l in got), want_p * 0.985)   # within the small steps it counts in
            self.assertEqual(len({l["game_id"] for l in got}), len(got))                  # one pick a game

    def test_a_payout_the_games_cannot_reach_is_not_invented(self):
        self.assertIsNone(plays.best_shot(self.games(), 500.0))

    def test_a_cheaper_pick_for_the_same_payout_is_preferred(self):
        # two picks pay the same; one costs less of its chance to the sportsbook's fee
        by_game = {"G1": [leg("G1", "dear", 2.0, 0.45), leg("G1", "fair", 2.0, 0.49)], "G2": [leg("G2", "x", 2.0, 0.48)]}
        self.assertEqual([l["selection"] for l in plays.best_shot(by_game, 4.0)], ["fair", "x"])


class NoEdgeClaimed(unittest.TestCase):
    def test_where_the_tool_is_more_hopeful_than_the_price_the_price_is_believed(self):
        self.assertEqual(plays._sober(leg("G", "a", 2.0, 0.55, fair=0.48)), 0.48)
        self.assertEqual(plays._sober(leg("G", "a", 2.0, 0.44, fair=0.48)), 0.44)  # and where it is gloomier, the tool is
        self.assertEqual(plays._sober(leg("G", "a", 2.0, 0.5, p_fair=None)), 0.5)

    def test_a_tickets_chance_and_return_use_those_chances(self):
        a, b = leg("G1", "a", 2.0, 0.55, fair=0.48), leg("G2", "b", 3.0, 0.30, fair=0.31)
        t = plays._ticket("shot", "x", [a, b], 6.0)
        self.assertAlmostEqual(t["p_hit"], 0.48 * 0.30)
        self.assertAlmostEqual(t["ev"], 0.48 * 0.30 * 6.0 - 1)
        self.assertEqual((t["n"], t["dec"], t["leg_keys"]), (2, 6.0, [a["key"], b["key"]]))


class Tickets(unittest.TestCase):
    def slate(self):
        legs = []
        for g in ("G1", "G2", "G3", "G4"):  # four games today
            legs += [leg(g, "home", 1.5, 0.64), leg(g, "away", 2.8, 0.34), leg(g, "over", 1.91, 0.5, market="total")]
        for g in ("N1", "N2"):               # two NFL games on Sunday
            legs += [leg(g, "home", 1.4, 0.69, sport="NFL", date="2026-11-29"), leg(g, "away", 3.2, 0.3, sport="NFL", date="2026-11-29")]
        return legs

    def test_every_day_each_sport_and_everything_get_their_own_tickets(self):
        L = plays.lottery(self.slate())
        self.assertEqual([(s["key"], s["kind"], s["n_games"]) for s in L["scopes"]],
                         [("day:2026-11-25", "day", 4), ("day:2026-11-29", "day", 2), ("all", "all", 6)])  # a sport that is one day is that day

    def test_calling_every_game_takes_the_likelier_winner_of_each(self):
        day = plays.lottery(self.slate())["scopes"][0]
        call = next(t for t in day["tickets"] if t["kind"] == "all")
        self.assertEqual((call["legs"], call["n"], call["missing"]), (["home"] * 4, 4, 0))
        self.assertAlmostEqual(call["dec"], 1.5 ** 4)

    def test_best_shots_pay_what_they_say_and_longer_ones_are_less_likely(self):
        day = plays.lottery(self.slate())["scopes"][0]
        shots = [t for t in day["tickets"] if t["kind"] == "shot"]
        self.assertEqual([t["target"] for t in shots], [25.0])  # four games at these prices reach 25x, not 100x
        self.assertGreaterEqual(shots[0]["dec"], 25.0)
        every = plays.lottery(self.slate())["scopes"][-1]
        more = [t for t in every["tickets"] if t["kind"] == "shot"]
        self.assertTrue(all(t["dec"] >= t["target"] for t in more))
        self.assertEqual([t["p_hit"] for t in more], sorted((t["p_hit"] for t in more), reverse=True))

    def test_a_pick_without_a_real_price_or_with_a_warning_is_never_used(self):
        legs = self.slate()
        for l in legs:
            if l["selection"] == "away":
                l["assumed"] = True       # the only long prices on the board have no real price
        L = plays.lottery(legs)
        used = {k for s in L["scopes"] for t in s["tickets"] for k in t["leg_keys"]}
        self.assertFalse([k for k in used if "|away|" in k])
        self.assertFalse(plays._lotto_ok({**leg("G", "a", 2.0, 0.5), "flag": "check news"}))
        self.assertFalse(plays._lotto_ok(leg("G", "a", 9.0, 0.1)))  # too long a price for the chances to be trusted

    def test_one_game_is_not_a_ticket(self):
        self.assertEqual(plays.lottery([leg("G1", "home", 1.5, 0.64), leg("G1", "away", 2.8, 0.34)])["scopes"], [])


if __name__ == "__main__":
    unittest.main()
