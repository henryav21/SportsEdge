"""Offline tests for bet settlement and history bookkeeping.  Run:  python -m unittest discover tests"""
import unittest

import numpy as np
import pandas as pd

from sportsedge import tracker as T


class SettleGame(unittest.TestCase):
    def test_moneyline(self):
        self.assertEqual(T.settle_game("ml", "home", None, 3, 40)[0], "won")
        self.assertEqual(T.settle_game("ml", "away", None, 3, 40)[0], "lost")
        self.assertEqual(T.settle_game("ml", "home", None, 0, 40)[0], "push")  # NFL tie

    def test_spread_both_sides(self):
        # home -3: home wins by 3 -> push; by 4 -> won; by 2 -> lost
        self.assertEqual(T.settle_game("spread", "home", -3, 3, 40)[0], "push")
        self.assertEqual(T.settle_game("spread", "home", -3, 4, 40)[0], "won")
        self.assertEqual(T.settle_game("spread", "home", -3, 2, 40)[0], "lost")
        # away +3.5 when home wins by 3 -> won; by 4 -> lost
        self.assertEqual(T.settle_game("spread", "away", 3.5, 3, 40)[0], "won")
        self.assertEqual(T.settle_game("spread", "away", 3.5, 4, 40)[0], "lost")
        # away -2.5 (away favourite) when away wins by 3 (margin -3) -> won
        self.assertEqual(T.settle_game("spread", "away", -2.5, -3, 40)[0], "won")

    def test_total(self):
        self.assertEqual(T.settle_game("total", "over", 44.5, 0, 45)[0], "won")
        self.assertEqual(T.settle_game("total", "under", 44.5, 0, 45)[0], "lost")
        self.assertEqual(T.settle_game("total", "under", 45, 0, 45)[0], "push")


class SettleProp(unittest.TestCase):
    def test_sides(self):
        self.assertEqual(T.settle_prop("over", 24.5, 25), "won")
        self.assertEqual(T.settle_prop("under", 24.5, 25), "lost")
        self.assertEqual(T.settle_prop("over", 25, 25), "push")
        self.assertEqual(T.settle_prop("yes", 0.5, 1), "won")
        self.assertEqual(T.settle_prop("yes", 0.5, 0), "lost")

    def test_pnl(self):
        self.assertAlmostEqual(T.pnl("won", 1.909), 0.909)
        self.assertEqual(T.pnl("lost", 1.909), -1.0)
        self.assertEqual(T.pnl("push", 1.909), 0.0)
        self.assertEqual(T.pnl("void", 1.909), 0.0)


class ClosingLineValue(unittest.TestCase):
    def test_spread_and_total(self):
        close = {"spread_home": -4.0, "total": 46.0, "ml_home": -200, "ml_away": 170}
        # bet home -3 and it closed -4: got 1 point better than the close
        self.assertAlmostEqual(T._clv(pd.Series({"market_key": "spread", "side": "home", "line": -3.0}), close)["clv"], 1.0)
        # bet away +3 (the other side of -3) and it closed +4: 1 point worse
        self.assertAlmostEqual(T._clv(pd.Series({"market_key": "spread", "side": "away", "line": 3.0}), close)["clv"], -1.0)
        # bet over 44.5, closed 46: +1.5;  under 44.5: -1.5
        self.assertAlmostEqual(T._clv(pd.Series({"market_key": "total", "side": "over", "line": 44.5}), close)["clv"], 1.5)
        self.assertAlmostEqual(T._clv(pd.Series({"market_key": "total", "side": "under", "line": 44.5}), close)["clv"], -1.5)

    def test_moneyline_uses_no_vig_probability(self):
        close = {"spread_home": np.nan, "total": np.nan, "ml_home": -200, "ml_away": 170}
        out = T._clv(pd.Series({"market_key": "ml", "side": "home", "line": np.nan, "p_fair": 0.60}), close)
        self.assertGreater(out["close_p"], 0.60)  # the price moved toward the home team after the bet
        self.assertAlmostEqual(out["clv"], out["close_p"] - 0.60)


class History(unittest.TestCase):
    def test_merge_keeps_latest_open_and_never_touches_settled(self):
        old = pd.DataFrame([{"key": "a", "status": "open", "odds": -110, "logged_at": "1"},
                            {"key": "b", "status": "won", "odds": +120, "logged_at": "1"}])
        new = pd.DataFrame([{"key": "a", "status": "open", "odds": -105, "logged_at": "2"},
                            {"key": "b", "status": "open", "odds": +100, "logged_at": "2"},
                            {"key": "c", "status": "open", "odds": +150, "logged_at": "2"}])
        m = T._merge_latest(old, new).set_index("key")
        self.assertEqual(m.loc["a", "odds"], -105)        # latest pre-game price kept
        self.assertEqual(m.loc["b", "status"], "won")     # settled row untouched
        self.assertEqual(m.loc["b", "odds"], 120)
        self.assertIn("c", m.index)
        self.assertEqual(len(m), 3)


if __name__ == "__main__":
    unittest.main()
