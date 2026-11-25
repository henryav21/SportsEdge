"""Offline tests for the line-movement log and the live scoreboard.  Run:  python -m unittest discover tests"""
import datetime as dt
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from sportsedge import betmoves, live, oddslog, tracker


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


LINE = {"spread_home": -3.0, "spread_home_odds": -110, "spread_away_odds": -110, "total": 44.5,
        "over_odds": -110, "under_odds": -110, "ml_home": -160, "ml_away": 135}


def pick(mk, side, line=None, odds=-110, p_fair=0.5, start="2026-11-26T21:30:00+00:00", key=None, **kw):
    return {"key": key or f"NFL|g1|{mk}|{side}|", "sport": "NFL", "game_id": "g1", "home": "DAL", "away": "PHI",
            "market_key": mk, "side": side, "line": line, "odds": odds, "p_fair": p_fair, "start": start, **kw}


class NumberNow(unittest.TestCase):
    def test_each_side_reads_its_own_number(self):
        self.assertEqual(betmoves.game_now(pick("spread", "home"), LINE)["line"], -3.0)
        self.assertEqual(betmoves.game_now(pick("spread", "away"), LINE)["line"], 3.0)  # the other side of the same spread
        self.assertEqual(betmoves.game_now(pick("total", "under"), LINE)["line"], 44.5)
        ml = betmoves.game_now(pick("ml", "away"), LINE)
        self.assertIsNone(ml["line"])
        self.assertEqual(ml["odds"], 135)
        self.assertLess(ml["p_fair"], 0.5)  # the underdog, with the fee removed

    def test_nothing_to_read(self):
        self.assertIsNone(betmoves.game_now(pick("spread", "home"), None))
        self.assertIsNone(betmoves.game_now(pick("total", "over"), {"total": None}))


class SinceTheBet(unittest.TestCase):
    def test_a_spread_that_moves_toward_the_pick(self):
        # took DAL -1.5; the market now makes DAL -8.5: the bet lays 7 fewer points than a new one would
        m = betmoves.since_bet(pick("spread", "home", -1.5), {"line": -8.5, "odds": -110, "p_fair": 0.5})
        self.assertEqual((m["kind"], m["verdict"]), ("line", "for"))
        self.assertAlmostEqual(m["move"], 7.0)
        # the same move seen from the other side: took PHI +1.5, it is +8.5 now
        m = betmoves.since_bet(pick("spread", "away", 1.5), {"line": 8.5, "odds": -110, "p_fair": 0.5})
        self.assertEqual(m["verdict"], "against")
        self.assertAlmostEqual(m["move"], -7.0)

    def test_totals_and_props_depend_on_the_side(self):
        now = {"line": 42.5, "odds": -110, "p_fair": 0.5}
        self.assertEqual(betmoves.since_bet(pick("total", "over", 43.5), now)["verdict"], "against")  # a new over needs fewer points
        self.assertEqual(betmoves.since_bet(pick("total", "under", 43.5), now)["verdict"], "for")
        m = betmoves.since_bet(pick("pts", "over", 24.5), {"line": 26.5, "odds": -115, "p_fair": 0.51})
        self.assertEqual(m["verdict"], "for")
        self.assertAlmostEqual(m["move"], 2.0)

    def test_same_line_is_judged_on_the_price(self):
        m = betmoves.since_bet(pick("total", "over", 43.5, -110, 0.50), {"line": 43.5, "odds": -125, "p_fair": 0.53})
        self.assertEqual((m["kind"], m["verdict"]), ("chance", "for"))  # the book now thinks the over more likely
        self.assertAlmostEqual(m["move"], 0.03)
        m = betmoves.since_bet(pick("ml", "home", None, -160, 0.60), {"line": None, "odds": -140, "p_fair": 0.57})
        self.assertEqual(m["verdict"], "against")
        m = betmoves.since_bet(pick("ml", "home", None, -160, 0.60), {"line": None, "odds": -161, "p_fair": 0.602})
        self.assertEqual(m["verdict"], "same")  # a fifth of a point is noise

    def test_an_older_bet_without_the_fee_free_chance_uses_the_prices(self):
        m = betmoves.since_bet(pick("ml", "home", None, -110, None), {"line": None, "odds": -150, "p_fair": 0.58})
        self.assertEqual((m["kind"], m["verdict"]), ("chance", "for"))

    def test_a_line_without_a_price(self):
        self.assertEqual(betmoves.since_bet(pick("rush_yds", "over", 60.5, None, None), {"line": 60.5, "odds": None, "p_fair": None})["verdict"], "same")
        self.assertEqual(betmoves.since_bet(pick("rush_yds", "over", 60.5, None, None), {"line": 55.5, "odds": None, "p_fair": None})["verdict"], "against")
        self.assertEqual(betmoves.since_bet(pick("ml", "home", None, None, None), {"line": None, "odds": None, "p_fair": None}), {})
        self.assertEqual(betmoves.since_bet(pick("total", "over", 43.5), None), {})


class LastNumberBeforeTheGame(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old = os.environ.get("SPORTSEDGE_HISTORY")
        os.environ["SPORTSEDGE_HISTORY"] = self.dir
        legs = [pick("total", "over", 43.5, key="t"), pick("pts", "over", 24.5, key="p", book="manual")]
        self.bets = pd.DataFrame([{"id": "b1", "legs_json": json.dumps(legs), "status": "open"}])

    def tearDown(self):
        if self.old is None:
            os.environ.pop("SPORTSEDGE_HISTORY", None)
        else:
            os.environ["SPORTSEDGE_HISTORY"] = self.old
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_move_follows_the_line_then_freezes_at_the_start(self):
        before = dt.datetime(2026, 11, 26, 18, 0, tzinfo=dt.timezone.utc)
        now = {"t": {"line": 44.5, "odds": -110, "p_fair": 0.5}}
        out = betmoves.update(self.bets, lambda leg: now.get(leg["key"]), before)["b1"]
        self.assertEqual(out["t"]["verdict"], "for")
        self.assertFalse(out["t"]["final"])
        self.assertNotIn("p", out)  # nothing has been read for the prop yet
        now["t"] = {"line": 45.5, "odds": -110, "p_fair": 0.5}
        self.assertAlmostEqual(betmoves.update(self.bets, lambda leg: now.get(leg["key"]), before)["b1"]["t"]["move"], 2.0)
        # after kick-off a later number (an in-play line, say) is ignored: the last pre-game number stands
        after = dt.datetime(2026, 11, 26, 22, 0, tzinfo=dt.timezone.utc)
        out = betmoves.update(self.bets, lambda leg: {"line": 60.5, "odds": -110, "p_fair": 0.5}, after)["b1"]
        self.assertAlmostEqual(out["t"]["move"], 2.0)
        self.assertTrue(out["t"]["final"])

    def test_off_the_board_keeps_the_last_number_and_says_so(self):
        before = dt.datetime(2026, 11, 26, 18, 0, tzinfo=dt.timezone.utc)
        betmoves.update(self.bets, lambda leg: {"line": leg["line"] + 1, "odds": -110, "p_fair": 0.5}, before)
        out = betmoves.update(self.bets, lambda leg: None, before)["b1"]
        self.assertTrue(out["t"]["stale"])
        self.assertFalse(out["t"]["final"])
        self.assertTrue(out["p"]["other_book"])  # a number typed in from another sportsbook is flagged
        self.assertEqual(betmoves.tally(out), {"for": 2, "against": 0, "same": 0})

    def test_a_bet_is_saved_with_what_the_comparison_needs(self):
        # NFL kick-off times arrive as timestamps, not text: recording such a pick used to fail outright
        leg = {**pick("total", "over", 43.5, -121, 0.5255), "date": "2026-11-26", "selection": "Over 43.5", "dec": 1.826,
               "player_key": "", "book": "DraftKings", "start": pd.Timestamp("2026-11-26T21:30:00Z")}
        tracker.place_bet([leg], -121, 10.0, 0.55, 0.55, 0.01)
        saved = json.loads(tracker.load_bets().iloc[0]["legs_json"])[0]
        self.assertEqual(saved["start"][:16], "2026-11-26T21:30")
        self.assertEqual((saved["p_fair"], saved["book"], saved["line"]), (0.5255, "DraftKings", 43.5))
        self.assertFalse(betmoves.started(saved, dt.datetime(2026, 11, 26, 21, 0, tzinfo=dt.timezone.utc)))
        self.assertTrue(betmoves.started(saved, dt.datetime(2026, 11, 26, 21, 30, tzinfo=dt.timezone.utc)))

    def test_a_deleted_bet_takes_its_numbers_with_it(self):
        before = dt.datetime(2026, 11, 26, 18, 0, tzinfo=dt.timezone.utc)
        betmoves.update(self.bets, lambda leg: {"line": 1.0, "odds": -110, "p_fair": 0.5}, before)
        self.assertIn("b1", betmoves.load_seen())
        self.assertEqual(betmoves.update(self.bets.iloc[0:0], lambda leg: None, before), {})
        self.assertEqual(betmoves.load_seen(), {})


if __name__ == "__main__":
    unittest.main()
