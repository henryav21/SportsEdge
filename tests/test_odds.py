"""Offline tests for the line-movement log and the live scoreboard.  Run:  python -m unittest discover tests"""
import datetime as dt
import os
import shutil
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from sportsedge import live, oddslog


def game(event_id="1", total=44.5, spread=-3.0, ml_home=-160, ml_away=135, state="pre", home="DAL", away="PHI"):
    return {"event_id": event_id, "home": home, "away": away, "start": "2026-11-26T21:30Z", "state": state,
            "spread_home": spread, "spread_home_odds": -110, "spread_away_odds": -110, "total": total,
            "over_odds": -110, "under_odds": -110, "ml_home": ml_home, "ml_away": ml_away}


class MovementLog(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old = os.environ.get("SPORTSEDGE_HISTORY")
        os.environ["SPORTSEDGE_HISTORY"] = self.dir

    def tearDown(self):
        if self.old is None:
            os.environ.pop("SPORTSEDGE_HISTORY", None)
        else:
            os.environ["SPORTSEDGE_HISTORY"] = self.old
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_only_changes_are_logged(self):
        t0 = dt.datetime(2026, 11, 24, 18, 0, tzinfo=dt.timezone.utc)
        games = pd.DataFrame([game("1"), game("2", home="BUF", away="KC")])
        self.assertEqual(oddslog.record("nfl", games, t0), 2)                           # first sight of both games
        self.assertEqual(oddslog.record("nfl", games, t0 + dt.timedelta(minutes=3)), 0)  # nothing moved
        moved = pd.DataFrame([game("1", total=43.5), game("2", home="BUF", away="KC")])
        self.assertEqual(oddslog.record("nfl", moved, t0 + dt.timedelta(minutes=6)), 1)  # one total moved
        self.assertEqual(len(oddslog.load()), 3)

    def test_series_for_one_game_oldest_first(self):
        t0 = dt.datetime(2026, 11, 24, 18, 0, tzinfo=dt.timezone.utc)
        oddslog.record("nfl", pd.DataFrame([game("1")]), t0)
        oddslog.record("nfl", pd.DataFrame([game("1", total=43.5, ml_home=-180, ml_away=150)]), t0 + dt.timedelta(minutes=9))
        s = oddslog.series(oddslog.load(), "nfl", "DAL", "PHI", "2026-11-26T21:30Z")
        self.assertEqual([p["total"] for p in s], [44.5, 43.5])
        self.assertLess(s[0]["t"], s[1]["t"])
        self.assertGreater(s[1]["p_home"], s[0]["p_home"])  # a shorter home price is a higher home chance
        self.assertEqual(oddslog.series(oddslog.load(), "nfl", "BUF", "KC", "2026-11-26T21:30Z"), [])

    def test_a_game_without_any_numbers_is_skipped(self):
        blank = {**game("9"), **{k: np.nan for k in oddslog.FIELDS}}
        self.assertEqual(oddslog.record("nfl", pd.DataFrame([blank])), 0)
        self.assertTrue(oddslog.load().empty)

    def test_the_same_teams_meeting_again_are_kept_apart(self):
        t0 = dt.datetime(2026, 11, 24, 18, 0, tzinfo=dt.timezone.utc)
        oddslog.record("nba", pd.DataFrame([game("1"), {**game("2", total=40.5), "start": "2026-12-20T18:00Z"}]), t0)
        s = oddslog.series(oddslog.load(), "nba", "DAL", "PHI", "2026-12-20T18:00Z")
        self.assertEqual([p["total"] for p in s], [40.5])


class MovementSinceOpen(unittest.TestCase):
    def test_moves_are_now_minus_open(self):
        m = oddslog.movement({"spread_home": -1.5, "total": 49.5, "ml_home": -120, "ml_away": 100},
                             {"spread_home": -8.5, "total": 47.5, "ml_home": -471, "ml_away": 364})
        self.assertAlmostEqual(m["spread_home"], -7.0)  # the home team gives 7 more points: the line moved toward it
        self.assertAlmostEqual(m["total"], -2.0)
        self.assertGreater(m["p_home"], 0.2)
        self.assertAlmostEqual(m["p_home_now"] - m["p_home_open"], m["p_home"])

    def test_missing_numbers_give_no_move(self):
        m = oddslog.movement({"total": None}, {"total": 44.5, "ml_home": -110, "ml_away": -110})
        self.assertIsNone(m["total"])
        self.assertIsNone(m["spread_home"])
        self.assertIsNone(m["p_home"])
        self.assertAlmostEqual(m["p_home_now"], 0.5)

    def test_the_fee_is_removed_from_the_win_chance(self):
        self.assertAlmostEqual(oddslog.home_chance(-110, -110), 0.5)
        self.assertIsNone(oddslog.home_chance(None, -110))


class Scoreboard(unittest.TestCase):
    def test_games_in_play_come_first_with_the_live_win_chance(self):
        def board(sport, day):
            if sport != "nba":
                return []
            base = {"home_score": 0.0, "away_score": 0.0, "detail": "", "period": 0, "clock": 0.0}
            return [{**base, "event_id": "3", "home": "DEN", "away": "DAL", "state": "post", "start": "2026-11-24T23:00Z"},
                    {**base, "event_id": "2", "home": "WAS", "away": "BOS", "state": "pre", "start": "2026-11-25T01:00Z"},
                    {**base, "event_id": "1", "home": "ORL", "away": "DET", "state": "in", "start": "2026-11-25T00:00Z",
                     "home_score": 27.0, "away_score": 26.0, "detail": "8:16 - 2nd"}]

        with mock.patch.object(live, "scoreboard", board), \
                mock.patch.object(live, "boxscore", lambda sport, eid: {"home_wp": 0.52}) as _:
            rows = live.scores(day=dt.date(2026, 11, 24))
        self.assertEqual([r["state"] for r in rows], ["in", "pre", "post"])
        self.assertEqual(rows[0]["home_wp"], 0.52)
        self.assertIsNone(rows[1]["home_wp"])  # only a game in play is asked for a live win chance
        self.assertEqual(rows[0]["sport"], "nba")


if __name__ == "__main__":
    unittest.main()
