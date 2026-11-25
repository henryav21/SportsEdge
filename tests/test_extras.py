"""Offline tests for pricing a pick at another line, grading the live log, and starting with Windows.
Run:  python -m unittest discover tests"""
import math
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd

from sportsedge import autostart, live, markets


class Bell:
    """A plain bell curve standing in for the fitted score distribution."""

    def __init__(self, mu, sd):
        self.mu, self.sd = mu, sd

    def p_gt(self, x):
        return 0.5 * (1 - math.erf((x - self.mu) / (self.sd * math.sqrt(2))))

    def p_lt(self, x):
        return 1 - self.p_gt(x)

    def p_eq(self, x):
        return 0.0


class Model:
    def dist_margin(self, mu):
        return Bell(mu, 13.0)

    def dist_total(self, mu):
        return Bell(mu, 10.0)


def setup():
    # the book has the home team -3 at even prices, total 44.5; the model agrees with the book
    g = SimpleNamespace(game_id="G", home="DAL", away="PHI", start=pd.Timestamp("2026-11-26T21:30Z"), mu_margin=3.0, mkt_margin=3.0,
                        pm=3.0, mu_total=44.5, mkt_total=44.5, pt=44.5)
    rng = np.random.default_rng(1)
    sims = {"margin": np.round(rng.normal(3, 13, 8000)), "total": np.round(rng.normal(44.5, 10, 8000))}
    odds = {"spread_home": -3.0, "spread_home_odds": -110.0, "spread_away_odds": -110.0, "total": 44.5, "over_odds": -110.0,
            "under_odds": -110.0, "book": "Book"}
    return g, sims, odds


class AnotherLine(unittest.TestCase):
    def at(self, mk, side, line):
        g, sims, odds = setup()
        return markets.game_leg_at("nfl", g, Model(), sims, odds, mk, side, line)

    def test_at_the_books_own_line_the_chance_is_the_books(self):
        self.assertAlmostEqual(self.at("spread", "home", -3.0)["p_cond"], 0.5, places=6)
        self.assertAlmostEqual(self.at("spread", "away", 3.0)["p_cond"], 0.5, places=6)
        self.assertAlmostEqual(self.at("total", "over", 44.5)["p_cond"], 0.5, places=6)

    def test_more_points_for_your_side_is_a_better_chance(self):
        a, b, c = (self.at("spread", "away", x)["p_cond"] for x in (1.5, 3.0, 7.5))
        self.assertTrue(a < b < c)
        self.assertEqual(self.at("spread", "away", 7.5)["selection"], "PHI +7.5")
        self.assertEqual(self.at("spread", "home", -7.5)["selection"], "DAL -7.5")
        # the two sides of one line are one bet seen from both ends
        self.assertAlmostEqual(self.at("spread", "away", 7.5)["p_cond"] + self.at("spread", "home", -7.5)["p_cond"], 1.0, places=6)

    def test_totals(self):
        lo, hi = self.at("total", "over", 40.5), self.at("total", "over", 48.5)
        self.assertGreater(lo["p_cond"], 0.6)
        self.assertLess(hi["p_cond"], 0.4)
        self.assertAlmostEqual(self.at("total", "under", 48.5)["p_cond"], 1 - hi["p_cond"], places=6)
        self.assertEqual((hi["line"], hi["market_key"], hi["side"], hi["key"]), (48.5, "total", "over", "NFL|G|total|over|"))

    def test_the_result_of_each_simulated_game_is_at_the_new_line(self):
        g, sims, odds = setup()
        leg = markets.game_leg_at("nfl", g, Model(), sims, odds, "spread", "away", 7.5)
        self.assertAlmostEqual(float((leg["result"] == 1).mean()), float((sims["margin"] < 7.5).mean()), places=6)

    def test_a_moneyline_has_no_other_line(self):
        self.assertIsNone(self.at("ml", "home", 1.5))


class LiveLogOnTrial(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old = os.environ.get("SPORTSEDGE_HISTORY")
        os.environ["SPORTSEDGE_HISTORY"] = self.dir
        live._cache.clear()

    def tearDown(self):
        if self.old is None:
            os.environ.pop("SPORTSEDGE_HISTORY", None)
        else:
            os.environ["SPORTSEDGE_HISTORY"] = self.old
        shutil.rmtree(self.dir, ignore_errors=True)
        live._cache.clear()

    def test_each_kind_of_bet_is_settled_from_the_final_score(self):
        fin = {"margin": 12.0, "total": 205.0, "players": {"a b": {"pts": 24.0}}}
        self.assertEqual(live.settle_logged("NBA|E1|spread|home|", -10.5, fin), "won")
        self.assertEqual(live.settle_logged("NBA|E1|spread|away|", 10.5, fin), "lost")
        self.assertEqual(live.settle_logged("NBA|E1|total|over|", 205, fin), "push")
        self.assertEqual(live.settle_logged("NBA|E1|ml|away|", None, fin), "lost")
        self.assertEqual(live.settle_logged("NBA|E1|pts|over|a b", 22.5, fin), "won")
        self.assertEqual(live.settle_logged("NBA|E1|pts|under|a b", 22.5, fin), "lost")
        self.assertEqual(live.settle_logged("NBA|E1|pts|over|did not play", 9.5, fin), "void")

    def _log(self, rows):
        ts = pd.Timestamp.now(tz="UTC").isoformat()
        pd.DataFrame([{"ts": ts, "sport": "nba", "event_id": e, "key": k, "selection": k, "line": line, "odds": -110.0,
                       "other_odds": -110.0, "p_tool": p, "q_book": q, "left": left, "now": ""} for e, k, line, p, q, left in rows],
                     columns=live.LOG_COLS).to_csv(live._log_path(), index=False)

    def test_only_finished_games_are_settled_and_only_once(self):
        self._log([("1", "NBA|E1|total|over|", 200.5, 0.6, 0.5, 0.5), ("2", "NBA|E2|total|over|", 210.5, 0.6, 0.5, 0.5)])
        done = {"1": {"margin": 5.0, "total": 205.0, "players": {}}, "2": None}  # game 2 is still on
        with mock.patch.object(live, "final", side_effect=lambda sport, eid: done[eid]):
            self.assertEqual(live.grade_log(), 1)
            self.assertEqual(live.grade_log(), 0)
        g = pd.read_csv(live._graded_path())
        self.assertEqual(list(g["result"]), ["won"])

    def test_no_verdict_until_there_is_enough(self):
        self._log([("1", "NBA|E1|total|over|", 200.5 + i, 0.6, 0.5, 0.5) for i in range(20)])
        with mock.patch.object(live, "final", return_value={"margin": 5.0, "total": 230.0, "players": {}}):
            live.grade_log()
        s = live.log_summary()
        self.assertEqual((s["n"], s["games"], s["ready"]), (20, 1, False))

    def test_stale_prices_are_left_out(self):
        self._log([("1", "NBA|E1|total|over|", 200.5, 0.6, 0.5, 0.5),      # counted
                   ("1", "NBA|E1|total|under|", 200.5, 0.4, 0.5, 0.5),     # the same market from the other side
                   ("1", "NBA|E1|total|over|", 190.5, 1.0, 0.6, 0.5),      # already settled by the score: a stale price
                   ("1", "NBA|E1|total|over|", 215.5, 0.5, 0.5, 0.02)])    # the last minute of the game
        with mock.patch.object(live, "final", return_value={"margin": 5.0, "total": 230.0, "players": {}}):
            self.assertEqual(live.grade_log(), 4)
        self.assertEqual(live.log_summary()["n"], 1)

    def test_the_weight_goes_to_whichever_predicted_better(self):
        rows = [("1", "NBA|E1|total|over|", 150.5 + i, 0.8, 0.5, 0.5) for i in range(30)]   # the estimate says 80%, the price 50%
        self._log(rows)
        with mock.patch.object(live, "final", return_value={"margin": 5.0, "total": 174.0, "players": {}}):  # 24 of 30 go over
            live.grade_log()
        s = live.log_summary()
        self.assertGreater(s["w"], 0.8)
        self.assertLess(s["ll_tool"], s["ll_book"])


class StartWithWindows(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_one_file_in_the_startup_folder_and_it_can_be_removed(self):
        with mock.patch.dict(os.environ, {"APPDATA": self.dir}), mock.patch.object(os, "name", "nt"):
            self.assertFalse(autostart.enabled())
            p = autostart.enable()
            self.assertTrue(autostart.enabled())
            self.assertEqual(p.name, "sportsedge-start.cmd")
            self.assertEqual(p.parent.name, "Startup")
            text = p.read_text(encoding="ascii")
            self.assertIn("sportsedge.cmd", text)
            self.assertIn(" share", text)
            self.assertIn("/min", text)  # a minimised window, not one in the way
            self.assertTrue(autostart.disable())
            self.assertFalse(autostart.enabled())
            self.assertFalse(autostart.disable())

    def test_other_systems_are_told_it_is_windows_only(self):
        with mock.patch.object(os, "name", "posix"):
            self.assertFalse(autostart.enabled())
            with self.assertRaises(RuntimeError):
                autostart.path()


if __name__ == "__main__":
    unittest.main()
