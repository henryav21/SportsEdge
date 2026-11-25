"""Offline tests for the "N or more" bets (4+ receptions, 2+ threes) and the touchdown scorers: reading them
from the feed, and the chance the tool gives each.
Run:  python -m unittest discover tests"""
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from sportsedge import engine, markets, sources

RNG = np.random.default_rng(3)
GAMES = pd.DataFrame([{"game_id": "G", "home": "LA", "away": "GB", "start": pd.Timestamp("2026-11-26T01:00Z")}])
LABELS = {"receptions": "Receptions", "pts": "Points", "anytime_td": "Anytime TD"}


def item(kind, athlete, value=None, price=None):
    it = {"type": {"name": kind}, "athlete": {"$ref": f"http://x/athletes/{athlete}?lang=en"}}
    if value is not None:
        it["current"] = {"target": {"value": value, "displayValue": f"{value:g}+"}}
    if price is not None:
        it["odds"] = {"american": {"value": price}}
    return it


class ReadingTheFeed(unittest.TestCase):
    NAMES = {"1": "Tucker Kraft", "2": "Bam Adebayo"}

    def test_thresholds_come_with_their_stat_and_touchdown_scorers_as_counts(self):
        rows = sources.parse_espn_rungs("nfl", [
            item("Receptions Milestones", "1", 4.0), item("Receptions Milestones", "1", 5.0), item("Receptions Milestones", "1", 4.0),
            item("Anytime Touchdown Scorer", "1"), item("Player to score 2 or more touchdowns", "1"),
            item("Receiving Yards Milestones - 1st Half", "1", 20.0),   # not a full-game stat: left out
            item("Tackles Milestones", "1", 3.0),                       # nothing simulates it: left out
            item("Receptions Milestones", "9", 4.0)], self.NAMES)      # a player nobody can name: left out
        got = sorted((r["player"], r["market"], r["n"]) for r in rows)
        self.assertEqual(got, [("Tucker Kraft", "anytime_td", 1.0), ("Tucker Kraft", "anytime_td", 2.0),
                               ("Tucker Kraft", "receptions", 4.0), ("Tucker Kraft", "receptions", 5.0)])
        self.assertTrue(all(np.isnan(r["odds"]) for r in rows))  # the NFL feed carries no prices

    def test_an_nba_threshold_keeps_its_price(self):
        rows = sources.parse_espn_rungs("nba", [item("3-Point Field Goals Milestones", "2", 2.0, "+172")], self.NAMES)
        self.assertEqual([(r["key"], r["market"], r["n"], r["odds"]) for r in rows], [("bam adebayo", "fg3m", 2.0, 172.0)])


class TheChanceOfReachingANumber(unittest.TestCase):
    def setUp(self):
        self.x = RNG.poisson(4.0, 20000).astype(float)  # receptions, about four a game
        self.sims = {"G": {"k": {"receptions": self.x, "anytime_td": RNG.poisson(0.35, 20000).astype(float)}}}
        self.ref = {("k", "receptions"): (3.5, 0.5)}  # the main line, an even bet
        w = mock.patch("sportsedge.tracker.prop_weight", return_value=0.2)
        w.start()
        self.addCleanup(w.stop)

    def rungs(self, rows):
        return pd.DataFrame([{"home": "LA", "away": "GB", "player": "K", "key": "k", "book": "DraftKings", "odds": np.nan, **r} for r in rows],
                            columns=sources.RUNG_COLS)

    def test_the_main_line_itself_is_the_main_lines_chance(self):
        p = markets.rung_chance(self.x, 3.5, 0.0, (3.5, 0.5), 0.0)
        self.assertAlmostEqual(p, 0.5)

    def test_a_far_threshold_falls_away_to_nothing(self):
        # the simulator has more receptions than the line implies; that gap must not prop up "12 or more"
        far = markets.rung_chance(self.x, 11.5, 0.0, (3.5, 0.5), 0.2)
        self.assertLess(far, 0.01)
        steps = [markets.rung_chance(self.x, n - 0.5, 0.0, (3.5, 0.5), 0.2) for n in range(1, 12)]
        self.assertEqual(steps, sorted(steps, reverse=True))  # each higher number is harder to reach

    def test_a_threshold_with_its_own_price_is_anchored_to_that_price(self):
        own = markets.rung_chance(self.x, 6.5, 0.0, (3.5, 0.5), 0.0, own=0.08)  # all weight on the market
        self.assertAlmostEqual(own, 0.08)

    def test_the_ladder_lists_each_threshold_lowest_first_and_drops_the_hopeless(self):
        lad = markets.ladders("nfl", self.rungs([{"market": "receptions", "n": n} for n in (5, 3, 4, 25)]), self.sims, {"*": 0.0}, self.ref)
        steps = lad["G|k|receptions"]
        self.assertEqual([t[0] for t in steps], [3, 4, 5])          # 25+ is under 1%: not a bet worth listing
        # 4+ is the over on the 3.5 line: the simulator's chance (weight 0.2) blended with the line as an even bet
        self.assertAlmostEqual(steps[1][1], 0.2 * float(np.mean(self.x > 3.5)) + 0.8 * 0.5, places=3)
        self.assertTrue(all(t[2] is None for t in steps))

    def test_a_stat_without_a_main_line_is_left_out_but_touchdowns_are_not(self):
        lad = markets.ladders("nfl", self.rungs([{"market": "receptions", "n": 4}, {"market": "anytime_td", "n": 1},
                                                  {"market": "anytime_td", "n": 2}]), self.sims, {"*": 0.0}, {})
        self.assertEqual(list(lad), ["G|k|anytime_td"])
        td = self.sims["G"]["k"]["anytime_td"]
        self.assertAlmostEqual(lad["G|k|anytime_td"][0][1], float(np.mean(td >= 1)), places=3)  # the simulator's own chance
        self.assertAlmostEqual(lad["G|k|anytime_td"][1][1], float(np.mean(td >= 2)), places=3)

    def test_the_price_in_the_feed_has_the_fee_taken_out(self):
        r = self.rungs([{"market": "receptions", "n": 7, "odds": 400.0}])  # +400 says 20% with the fee in
        lad = markets.ladders("nfl", r, self.sims, {"*": 0.0}, self.ref, {("k", "receptions"): 1.05})
        with mock.patch("sportsedge.tracker.prop_weight", return_value=0.0):
            only_market = markets.ladders("nfl", r, self.sims, {"*": 0.0}, self.ref, {("k", "receptions"): 1.05})
        self.assertAlmostEqual(only_market["G|k|receptions"][0][1], 0.2 / 1.05, places=3)
        self.assertEqual(lad["G|k|receptions"][0][2], 400.0)


class TouchdownPicks(unittest.TestCase):
    def setUp(self):
        self.td = RNG.poisson(0.4, 20000).astype(float)
        self.sims = {"G": {"k": {"anytime_td": self.td}}}

    def test_an_unpriced_scorer_becomes_a_pick_with_the_simulators_chance(self):
        rungs = pd.DataFrame([{"home": "LA", "away": "GB", "player": "Kyren Williams", "key": "k", "market": "anytime_td", "n": 1.0,
                               "odds": np.nan, "book": "DraftKings"}], columns=sources.RUNG_COLS)
        (leg,) = markets.td_legs("nfl", rungs, self.sims, GAMES, LABELS)
        self.assertEqual((leg["selection"], leg["side"], leg["line"], leg["key"]), ("Kyren Williams anytime TD", "yes", 0.5, "NFL|G|anytime_td|yes|k"))
        self.assertAlmostEqual(leg["p_cond"], float(np.mean(self.td >= 1)))
        self.assertTrue(leg["sim_only"] and leg["assumed"])
        self.assertIsNone(leg["ev"])  # nothing to rate it against, so it can never be suggested

    def test_never_suggested(self):
        from sportsedge import plays

        rungs = pd.DataFrame([{"home": "LA", "away": "GB", "player": "K", "key": "k", "market": "anytime_td", "n": 1.0, "odds": np.nan,
                               "book": "DraftKings"}], columns=sources.RUNG_COLS)
        (leg,) = markets.td_legs("nfl", rungs, self.sims, GAMES, LABELS)
        self.assertFalse(plays._ok(leg))

    def test_two_or_more_is_the_same_pick_at_a_higher_count(self):
        leg = markets.td_leg("nfl", "G", GAMES.set_index("game_id").loc["G"], "K", "k", self.td, 0.5, LABELS)
        res = {"sport": "nfl", "prop_sims": self.sims, "slate": GAMES, "labels": LABELS}
        two = engine.price_at(res, leg, 1.5)
        self.assertEqual((two["selection"], two["line"], two["alt_of"]), ("K 2+ TDs", 1.5, 0.5))
        self.assertAlmostEqual(two["p_cond"], float(np.mean(self.td >= 2)))
        self.assertTrue(two["assumed"] and two["sim_only"])


class TheSlipUsesTheLaddersChance(unittest.TestCase):
    """A pick taken at a threshold from the ladder is priced, in the slip, at the chance the ladder shows."""

    def test_over_and_under_at_a_listed_threshold(self):
        x = RNG.poisson(22.0, 20000).astype(float)
        sims = {"G": {"k": {"pts": x}}}
        lines = pd.DataFrame([{"player": "K", "key": "k", "market": "pts", "line": 21.5, "over_odds": -110.0, "under_odds": -110.0, "book": "DraftKings"}])
        with mock.patch("sportsedge.tracker.prop_weight", return_value=0.3):
            ref = markets.prop_reference(lines)
            legs = markets.prop_legs("nba", lines, sims, GAMES, LABELS, {"*": 0.0})
            rungs = pd.DataFrame([{"home": "LA", "away": "GB", "player": "K", "key": "k", "market": "pts", "n": 30.0, "odds": 550.0, "book": "DraftKings"}],
                                 columns=sources.RUNG_COLS)
            lad = markets.ladders("nba", rungs, sims, {"*": 0.0}, ref, markets.prop_hold(lines))
            res = {"sport": "nba", "prop_sims": sims, "slate": GAMES, "labels": LABELS, "prop_centre": {"*": 0.0}, "prop_ref": ref, "ladders": lad}
            over, under = (next(l for l in legs if l["side"] == s) for s in ("over", "under"))
            p = lad["G|k|pts"][0][1]
            at = engine.price_at(res, over, 29.5)
            self.assertAlmostEqual(at["p_cond"], p)
            self.assertEqual((at["odds"], at["own_price"]), (550.0, True))  # the sportsbook prices "30 or more": that price
            self.assertAlmostEqual(at["ev"], p * 6.5 - 1)
            un = engine.price_at(res, under, 29.5)
            self.assertAlmostEqual(un["p_cond"], 1 - p)
            self.assertEqual((un["odds"], un.get("own_price")), (under["odds"], None))  # no price for the under of it
            # a line the ladder does not list is still priced, from the main line moved to it
            self.assertTrue(0 < engine.price_at(res, over, 26.5)["p_cond"] < 0.5)


class SuggestedAsPicks(unittest.TestCase):
    """The "N or more" bets that can be suggested, built as picks of their own beside the over they belong to."""

    def setUp(self):
        self.x = RNG.poisson(22.0, 20000).astype(float)
        self.sims = {"G": {"k": {"pts": self.x, "pass_att": RNG.poisson(34.0, 20000).astype(float)}}}
        w = mock.patch("sportsedge.tracker.prop_weight", return_value=0.3)
        w.start()
        self.addCleanup(w.stop)

    def rungs(self, rows):
        return pd.DataFrame([{"home": "LA", "away": "GB", "player": "K", "key": "k", "book": "DraftKings", **r} for r in rows], columns=sources.RUNG_COLS)

    def build(self, rungs, lines, labels=None):
        labels = labels or {"pts": "Points", "pass_att": "Pass Att"}
        ref, hold = markets.prop_reference(lines), markets.prop_hold(lines)
        priced = markets.prop_legs("nba", lines, self.sims, GAMES, labels, {"*": 0.0})
        lean = markets.lean_legs("nba", lines[lines["over_odds"].isna()], self.sims, GAMES, labels, {"*": 0.0})
        mains = {(l["player_key"], l["market_key"]): l for l in lean + priced if l["side"] == "over"}
        lad = markets.ladders("nba", rungs, self.sims, {"*": 0.0}, ref, hold)
        return markets.rung_legs("nba", rungs, self.sims, GAMES, labels, {"*": 0.0}, ref, hold, lad, mains), lad, mains

    def test_a_priced_threshold_is_a_pick_at_its_own_price_with_the_ladders_chance(self):
        lines = pd.DataFrame([{"player": "K", "key": "k", "market": "pts", "line": 21.5, "over_odds": -110.0, "under_odds": -110.0, "book": "DraftKings"}])
        legs, lad, mains = self.build(self.rungs([{"market": "pts", "n": 25.0, "odds": 260.0}]), lines)
        (leg,) = legs
        main = mains[("k", "pts")]
        self.assertEqual((leg["selection"], leg["line"], leg["side"], leg["odds"], leg["rung"]), ("K 25+ Points", 24.5, "over", 260.0, 25))
        self.assertEqual((leg["key"], leg["base_key"], leg["group"]), (main["key"] + "|25+", main["key"], main["group"]))
        self.assertAlmostEqual(leg["p_cond"], lad["G|k|pts"][0][1])
        self.assertAlmostEqual(leg["ev"], leg["p_cond"] * 3.6 - 1)
        self.assertNotIn("assumed", leg)

    def test_one_that_cannot_be_suggested_is_not_built(self):
        lines = pd.DataFrame([{"player": "K", "key": "k", "market": "pts", "line": 21.5, "over_odds": -110.0, "under_odds": -110.0, "book": "DraftKings"}])
        cases = [{"market": "pts", "n": 25.0, "odds": 150.0},    # pays far too little for its chance
                 {"market": "pts", "n": 12.0, "odds": -2500.0},  # shorter than any suggestion uses
                 {"market": "pts", "n": 40.0, "odds": 5000.0},   # longer than any suggestion uses
                 {"market": "reb", "n": 5.0, "odds": 100.0}]     # no main line for that stat
        legs, _, _ = self.build(self.rungs(cases), lines)
        self.assertEqual(legs, [])

    def test_without_prices_only_the_twin_of_the_main_over_and_only_on_a_real_lean(self):
        # the simulator has about 34 attempts: a clear lean over 30.5, none to speak of at 33.5
        for line, expect in ((30.5, ["K 31+ Pass Att"]), (33.5, [])):
            lines = pd.DataFrame([{"player": "K", "key": "k", "market": "pass_att", "line": line, "over_odds": np.nan, "under_odds": np.nan, "book": "DraftKings"}])
            rungs = self.rungs([{"market": "pass_att", "n": line + 0.5, "odds": np.nan}, {"market": "pass_att", "n": line + 2.5, "odds": np.nan}])
            legs, _, mains = self.build(rungs, lines)
            self.assertEqual([l["selection"] for l in legs], expect)
            if legs:
                main = mains[("k", "pass_att")]
                self.assertTrue(legs[0]["assumed"])
                self.assertEqual(legs[0]["odds"], main["odds"])                # the same bet as the over: its assumed price
                self.assertAlmostEqual(legs[0]["p_cond"], main["p_cond"], places=3)  # and its chance
                self.assertAlmostEqual(legs[0]["lean"], main["lean"], places=6)


if __name__ == "__main__":
    unittest.main()
