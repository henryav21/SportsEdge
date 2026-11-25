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


class WaitsForFinalResults(unittest.TestCase):
    """The NBA's game log lists games in progress: nothing may be settled from a score that is not final."""

    def _res(self, wl_home, wl_away, pts=(8.0, 15.0)):
        r = T.Resolver.__new__(T.Resolver)
        r.nfl_sched = r.nfl_ps = r.nba_close = None
        r._prop_close = {}
        r.nba_team = pd.DataFrame([
            {"GAME_ID": "1", "GAME_DATE": "2026-11-24", "TEAM_ABBREVIATION": "DEN", "MATCHUP": "DEN vs. DAL", "PTS": pts[0], "WL": wl_home},
            {"GAME_ID": "1", "GAME_DATE": "2026-11-24", "TEAM_ABBREVIATION": "DAL", "MATCHUP": "DAL @ DEN", "PTS": pts[1], "WL": wl_away}])
        r.nba_player = pd.DataFrame([
            {"GAME_ID": "1", "GAME_DATE": "2026-11-24", "TEAM_ABBREVIATION": "DEN", "PLAYER_NAME": "Nikola Jokic", "pkey": "nikola jokic",
             "PTS": 4.0, "REB": 3.0, "AST": 2.0, "FG3M": 0.0, "STL": 0.0, "BLK": 0.0, "WL": wl_home}])
        return r

    LEG = {"sport": "NBA", "game_id": "E1", "date": "2026-11-24", "home": "DEN", "away": "DAL", "market_key": "ml",
           "side": "home", "player_key": "", "line": None, "dec": 1.34}

    def test_a_game_in_progress_is_not_settled(self):
        self.assertIsNone(self._res(None, None).game(self.LEG))
        self.assertIsNone(self._res(np.nan, np.nan).game(self.LEG))
        self.assertIsNone(self._res(None, None).settle(pd.Series(self.LEG)))

    def test_a_finished_game_is(self):
        self.assertEqual(self._res("L", "W", (101.0, 120.0)).game(self.LEG), (-19.0, 221.0))
        self.assertEqual(self._res("L", "W", (101.0, 120.0)).settle(pd.Series(self.LEG))["status"], "lost")

    def test_player_stats_wait_for_the_final_too(self):
        prop = {**self.LEG, "market_key": "pts", "side": "over", "player_key": "nikola jokic", "line": 25.5}
        self.assertIsNone(self._res(None, None).player_value(prop))
        missing = {**prop, "player_key": "jamal murray"}  # not on the floor yet is not the same as not playing
        self.assertIsNone(self._res(None, None).player_value(missing))
        self.assertEqual(self._res("L", "W").player_value(missing), "void")
        self.assertEqual(self._res("L", "W").player_value(prop), 4.0)

    def test_a_combo_waits_three_hours_from_the_start(self):
        class Lost:
            def settle(self, leg):
                return {"status": "lost"}
        now = pd.Timestamp.now(tz="UTC")
        early = [{**self.LEG, "start": (now - pd.Timedelta(hours=1)).isoformat()}]
        later = [{**self.LEG, "start": (now - pd.Timedelta(hours=4)).isoformat()}]
        self.assertIsNone(T._settle_combo(Lost(), early))
        self.assertEqual(T._settle_combo(Lost(), later), ("lost", 0.0))
        self.assertEqual(T._settle_combo(Lost(), [dict(self.LEG)]), ("lost", 0.0))  # no start time: as before
