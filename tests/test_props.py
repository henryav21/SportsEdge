"""Player props: reading the ESPN feed, centring on the market, pricing another book's line, and the
weight learned from real lines. Run with: python -m unittest discover tests"""
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from sportsedge import markets, nfl_props, propcheck, sources, tracker


def _item(kind, athlete, line=None, price=None):
    it = {"type": {"name": kind}, "athlete": {"$ref": f"http://x/v2/athletes/{athlete}?lang=en"}}
    it["current"] = {} if line is None else {"target": {"value": line}}
    if price is not None:
        it["odds"] = {"american": {"value": price}}
    return it


class EspnFeed(unittest.TestCase):
    names = {"1": "Al Able", "2": "Bo Baker"}

    def test_first_of_a_pair_is_the_over(self):
        rows = sources.parse_espn_props("nba", [_item("Total Points", "1", 24.5, "-120"), _item("Total Points", "1", 24.5, "+100")], self.names)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["player"], r["market"], r["line"], r["over_odds"], r["under_odds"]), ("Al Able", "pts", 24.5, -120.0, 100.0))

    def test_ambiguous_groups_are_skipped(self):
        one = [_item("Total Points", "1", 24.5, "-120")]
        two_lines = [_item("Total Rebounds", "1", 5.5, "-110"), _item("Total Rebounds", "1", 6.5, "-110")]
        unknown_player = [_item("Total Assists", "9", 5.5, "-110"), _item("Total Assists", "9", 5.5, "-110")]
        not_a_prop = [_item("1st Quarter Total", "1", 55.5, "-110"), _item("1st Quarter Total", "1", 55.5, "-110")]
        self.assertEqual(sources.parse_espn_props("nba", one + two_lines + unknown_player + not_a_prop, self.names), [])

    def test_nfl_lines_without_prices_are_kept(self):
        kind = "Total Receiving Yards (incl. overtime)"
        rows = sources.parse_espn_props("nfl", [_item(kind, "2", 48.5), _item(kind, "2", 48.5)], self.names)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["market"], rows[0]["line"]), ("rec_yds", 48.5))
        self.assertTrue(np.isnan(rows[0]["over_odds"]) and np.isnan(rows[0]["under_odds"]))

    def test_anytime_td_needs_a_price(self):
        self.assertEqual(sources.parse_espn_props("nfl", [_item("Anytime Touchdown Scorer", "2")], self.names), [])
        rows = sources.parse_espn_props("nfl", [_item("Anytime Touchdown Scorer", "2", None, "+140")], self.names)
        self.assertEqual((rows[0]["market"], rows[0]["line"], rows[0]["over_odds"]), ("anytime_td", 0.5, 140.0))


def _slate(n_players=30, shift=0.0, seed=1):
    """Players whose simulated points sit `shift` below what an even -110 / -110 line implies."""
    rng = np.random.default_rng(seed)
    sims, rows = {"G": {}}, []
    for i in range(n_players):
        mu = 12 + i
        sims["G"][f"p{i}"] = {"pts": rng.normal(mu - shift, 5, 20000)}
        rows.append({"player": f"P {i}", "key": f"p{i}", "market": "pts", "line": mu + 0.25, "over_odds": -110.0,
                     "under_odds": -110.0, "book": "DraftKings"})
    games = pd.DataFrame([{"game_id": "G", "home": "AAA", "away": "BBB", "start": pd.Timestamp("2026-11-24T23:00:00Z")}])
    return sims, pd.DataFrame(rows), games


class Centring(unittest.TestCase):
    def test_centring_removes_an_across_the_board_lean(self):
        sims, lines, games = _slate(shift=2.0)  # the simulator is 2 points low on everyone
        raw = np.mean([np.mean(sims["G"][r.key]["pts"] > r.line) for r in lines.itertuples()])
        self.assertLess(raw, 0.40)
        with mock.patch("sportsedge.tracker.prop_weight", return_value=1.0):
            legs = markets.prop_legs("nba", lines, sims, games, {"pts": "Points"})
        overs = [l for l in legs if l["side"] == "over"]
        self.assertAlmostEqual(float(np.mean([l["p_cond"] for l in overs])), 0.5, delta=0.01)

    def test_too_few_lines_means_no_centring(self):
        sims, lines, _ = _slate(n_players=5, shift=2.0)
        self.assertEqual(markets.prop_centre(lines, sims), {"*": 0.0})

    def test_extreme_disagreement_is_flagged_and_never_value(self):
        from sportsedge import report

        sims, lines, games = _slate()
        sims["G"]["p0"]["pts"] = sims["G"]["p0"]["pts"] + 12  # one player the simulator has far too high
        with mock.patch("sportsedge.tracker.prop_weight", return_value=0.5):
            legs = markets.prop_legs("nba", lines, sims, games, {"pts": "Points"})
        p0 = [l for l in legs if l["player_key"] == "p0"]
        self.assertTrue(all(l["flag"] for l in p0))
        self.assertTrue(any(l["ev"] > 0 for l in p0))
        self.assertFalse([l for l in report.value_bets(legs) if l["player_key"] == "p0"])
        self.assertFalse(any(l["flag"] for l in legs if l["player_key"] != "p0"))


class OtherBooksLine(unittest.TestCase):
    def test_a_better_line_shows_as_an_edge_against_that_book(self):
        sims, lines, games = _slate()
        centre, ref = markets.prop_centre(lines, sims), markets.prop_reference(lines)
        dk = lines.iloc[10]
        mine = pd.DataFrame([{**dk.to_dict(), "line": dk["line"] - 1.5, "book": "manual"}])  # 1.5 points easier to go over
        with mock.patch("sportsedge.tracker.prop_weight", return_value=0.1):
            same = markets.prop_legs("nba", pd.DataFrame([{**dk.to_dict(), "book": "manual"}]), sims, games, {"pts": "Points"}, centre, ref)
            better = markets.prop_legs("nba", mine, sims, games, {"pts": "Points"}, centre, ref)
        o_same = next(l for l in same if l["side"] == "over")
        o_better = next(l for l in better if l["side"] == "over")
        self.assertAlmostEqual(o_same["p_fair"], 0.5, places=6)
        self.assertLess(abs(o_same["edge"]), 0.02)             # same line, same price: nothing to find
        self.assertGreater(o_better["edge"], 0.08)             # easier line at the same price: a real edge
        self.assertGreater(o_better["ev"], 0)
        self.assertEqual(o_better["ref_line"], dk["line"])

    def test_unpriced_reference_line_counts_as_even(self):
        ln = pd.DataFrame([{"player": "A", "key": "a", "market": "rec_yds", "line": 40.5, "over_odds": np.nan, "under_odds": np.nan},
                           {"player": "A", "key": "a", "market": "anytime_td", "line": 0.5, "over_odds": 150.0, "under_odds": np.nan}])
        self.assertEqual(markets.prop_reference(ln), {("a", "rec_yds"): (40.5, 0.5)})


class LearnedWeight(unittest.TestCase):
    @staticmethod
    def _history(kind, n=6000, seed=3):
        rng = np.random.default_rng(seed)
        truth = np.clip(rng.normal(0.5, 0.08, n), 0.1, 0.9)       # real chance of the over
        market = np.clip(truth + rng.normal(0, 0.02, n), 0.05, 0.95)
        sim = truth if kind == "sharp" else np.clip(rng.normal(0.5, 0.15, n), 0.05, 0.95)
        over = rng.random(n) < truth
        fair_to_odds = lambda p: -100 * p / (1 - p) if p >= 0.5 else 100 * (1 - p) / p  # noqa: E731
        return pd.DataFrame({"date": rng.integers(0, 20, n), "key": [f"p{i % 200}" for i in range(n)], "player": "x", "market": "pts",
                             "line": 20.5, "over_odds": [fair_to_odds(p) for p in market], "under_odds": [fair_to_odds(1 - p) for p in market],
                             "pc_raw": sim, "pp": 0.0, "actual": np.where(over, 25.0, 15.0), "mean": 20.0, "med": 20.0})

    def test_a_simulator_that_is_noise_gets_no_weight(self):
        s = propcheck.evaluate("nba", self._history("noise"))
        self.assertLessEqual(s["w_used"], 0.05)  # at most the smallest step, never a real say

    def test_a_simulator_that_knows_more_than_the_market_gets_weight(self):
        s = propcheck.evaluate("nba", self._history("sharp"))
        self.assertTrue(s["beats_market"])
        self.assertGreaterEqual(s["w_used"], 0.5)


class NflRoles(unittest.TestCase):
    @staticmethod
    def _team():
        rows = []
        for key, pos, tgt, car, last in (("qb", "QB", 0.0, 0.10, 11), ("rb1", "RB", 0.08, 0.50, 11), ("fillin", "RB", 0.07, 0.65, 7),
                                         ("wr1", "WR", 0.28, 0.0, 11), ("wr2", "WR", 0.22, 0.0, 11), ("te1", "TE", 0.18, 0.0, 11),
                                         ("wr4", "WR", 0.20, 0.0, 8)):
            rows.append({"player_id": key, "name": key, "position": pos, "team": "AAA", "key": key, "tgt_share": tgt, "car_share": car,
                         "w_targets": 40, "w_rec": 26, "w_rec_yds": 300, "w_carries": 60, "w_rush_yds": 250, "w_att": 200, "w_cmp": 130,
                         "w_pass_yds": 1400, "w_rec_td": 2, "w_rush_td": 2, "w_team_pass_td": 10, "w_team_rush_td": 6,
                         "last_t": float(last), "team_last_t": 11.0})
        return pd.DataFrame(rows)

    def test_shares_add_up_to_one_team(self):
        u, _ = nfl_props.prepare_team(self._team(), set(), "qb")
        self.assertLessEqual(u["tgt_share"].sum(), 1.0 + 1e-9)
        self.assertLessEqual(u["car_share"].sum(), 1.0 + 1e-9)

    def test_a_player_without_a_line_gives_way_to_those_with_one(self):
        blind, _ = nfl_props.prepare_team(self._team(), set(), "qb")
        lined, _ = nfl_props.prepare_team(self._team(), set(), "qb", {"qb", "rb1", "wr1", "wr2", "te1"})
        share = lambda u, k, c: float(u.set_index("key").loc[k, c])  # noqa: E731
        self.assertGreater(share(lined, "rb1", "car_share"), share(blind, "rb1", "car_share") + 0.15)
        self.assertLess(share(lined, "fillin", "car_share"), 0.10)
        self.assertLess(share(lined, "wr4", "tgt_share"), 0.06)

    def test_out_players_are_removed(self):
        u, removed = nfl_props.prepare_team(self._team(), {"wr1"}, "qb")
        self.assertNotIn("wr1", set(u["key"]))
        self.assertEqual(removed, ["wr1"])


class PropClosingLine(unittest.TestCase):
    """Closing-line value for props: + always means the pick beat the sportsbook's last pre-game number."""
    close = {"line": 24.5, "over_odds": -120.0, "under_odds": 100.0}  # no-vig: over about 52%, under about 48%

    @staticmethod
    def _pick(side, line, p_fair=0.5):
        return {"side": side, "line": line, "p_fair": p_fair}

    def test_a_lower_line_helps_an_over_and_hurts_an_under(self):
        self.assertAlmostEqual(tracker._prop_clv(self._pick("over", 23.5), self.close)["clv"], 1.0)
        self.assertAlmostEqual(tracker._prop_clv(self._pick("under", 23.5), self.close)["clv"], -1.0)
        self.assertAlmostEqual(tracker._prop_clv(self._pick("under", 26.0), self.close)["clv"], 1.5)
        self.assertEqual(tracker._prop_clv(self._pick("over", 23.5), self.close)["close_line"], 24.5)

    def test_same_line_is_judged_on_price(self):
        over = tracker._prop_clv(self._pick("over", 24.5, 0.50), self.close)
        under = tracker._prop_clv(self._pick("under", 24.5, 0.50), self.close)
        self.assertGreater(over["clv"], 0.01)    # the over became more likely by the close: the pick beat it
        self.assertLess(under["clv"], -0.01)
        self.assertAlmostEqual(over["close_p"] + under["close_p"], 1.0, places=6)

    def test_line_without_prices_still_counts_line_moves(self):
        nfl_close = {"line": 48.5, "over_odds": float("nan"), "under_odds": float("nan")}
        out = tracker._prop_clv(self._pick("under", 51.5), nfl_close)
        self.assertAlmostEqual(out["clv"], 3.0)
        self.assertNotIn("close_p", out)
        self.assertNotIn("clv", tracker._prop_clv(self._pick("under", 48.5), nfl_close))  # same line, no price: unknown

    def test_anytime_td_uses_the_price(self):
        out = tracker._prop_clv({"side": "yes", "line": 0.5, "p_fair": 0.40}, {"line": 0.5, "over_odds": 120.0, "under_odds": float("nan")})
        self.assertAlmostEqual(out["close_p"], 100 / 220, places=6)
        self.assertGreater(out["clv"], 0)

    def test_no_closing_line_means_no_value(self):
        self.assertEqual(tracker._prop_clv(self._pick("over", 24.5), None), {})


if __name__ == "__main__":
    unittest.main()
