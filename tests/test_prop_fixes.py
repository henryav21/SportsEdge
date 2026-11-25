"""Offline tests for prop lines the feed is behind on: a line corrected by hand that sticks until the feed
catches up, and automatic updates when a prop line moves on its own.
Run:  python -m unittest discover tests"""
import json
import os
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd
from fastapi import HTTPException

from sportsedge import engine, web

KRAFT = ("tucker kraft", "receptions")
FID = "nfl|tucker kraft|receptions"
_HIST = None


def setUpModule():
    # corrections are saved in the history folder: keep the tests away from the real one
    global _HIST
    _HIST = tempfile.TemporaryDirectory()
    os.environ["SPORTSEDGE_HISTORY"] = _HIST.name


def tearDownModule():
    os.environ.pop("SPORTSEDGE_HISTORY", None)
    _HIST.cleanup()


def fixes(**kw):
    row = {"sport": "nfl", "player": "Tucker Kraft", "key": "tucker kraft", "market": "receptions", "line": 3.5,
           "over_odds": np.nan, "under_odds": np.nan, "book": "corrected", "feed_line": 4.5}
    row.update(kw)
    return pd.DataFrame([row])


def result(feed_line=4.5, games=("G1",)):
    """One sport's update: the games still to come and the prop lines the feed showed for it."""
    return {"sport": "nfl", "legs": [], "lean_legs": [],
            "slate": pd.DataFrame({"game_id": list(games), "home": "LA", "away": "GB", "start": pd.Timestamp("2026-11-26T01:00Z")}),
            "_prop_feed": {} if feed_line is None else {KRAFT: feed_line}}


def leg(line=4.5, side="under", **kw):
    l = {"key": f"NFL|G1|receptions|{side}|tucker kraft", "sport": "NFL", "game_id": "G1", "subject": "Tucker Kraft",
         "player_key": "tucker kraft", "market_key": "receptions", "side": side, "line": line,
         "selection": f"Tucker Kraft {side[0]}{line:g} Receptions"}
    l.update(kw)
    return l


def a_fix(**kw):
    f = {"sport": "nfl", "player": "Tucker Kraft", "key": "tucker kraft", "market": "receptions", "line": 3.5,
         "over_odds": None, "under_odds": None, "feed_line": 4.5, "game_id": "G1"}
    f.update(kw)
    return f


class CorrectedLinesInAnUpdate(unittest.TestCase):
    """engine.current_fixes: which corrections an update still uses, ahead of the feed's own lines."""

    def test_kept_while_the_feed_still_shows_its_old_line(self):
        self.assertEqual(len(engine.current_fixes(fixes(), {KRAFT: 4.5})), 1)

    def test_kept_when_the_feed_has_nothing_for_it(self):
        self.assertEqual(len(engine.current_fixes(fixes(), {})), 1)

    def test_dropped_once_the_feed_moves(self):
        said = []
        self.assertTrue(engine.current_fixes(fixes(), {KRAFT: 3.5}, said.append, "nfl").empty)
        self.assertIn("Tucker Kraft", said[0])
        self.assertTrue(engine.current_fixes(fixes(), {KRAFT: 5.5}).empty)  # moved the other way: the feed knows best

    def test_lines_of_your_own_without_a_feed_line_are_left_alone(self):
        self.assertEqual(len(engine.current_fixes(fixes().drop(columns="feed_line"), {KRAFT: 5.5})), 1)
        self.assertEqual(len(engine.current_fixes(fixes(feed_line=np.nan), {KRAFT: 5.5})), 1)


class UseThisLineEverywhere(unittest.TestCase):
    """The bet slip's button: /api/props/correct."""

    def setUp(self):
        self.state = mock.patch.dict(web.STATE, {"results": [result()], "legs": {leg()["key"]: leg()},
                                                 "prop_fixes": {}, "again": False})
        self.state.start()
        self.run = mock.patch.object(web, "start_run", return_value=True)
        self.started = self.run.start()

    def tearDown(self):
        self.run.stop()
        self.state.stop()
        if web._prop_fixes_path().exists():
            web._prop_fixes_path().unlink()

    def fix(self, **kw):
        return web.api_prop_correct(web.PropFixReq(key=leg()["key"], **kw))

    def test_the_line_is_saved_with_the_feeds_line_beside_it_and_everything_updated(self):
        self.fix(line=3.5)
        f = web.STATE["prop_fixes"][FID]
        self.assertEqual((f["line"], f["feed_line"], f["game_id"], f["player"]), (3.5, 4.5, "G1", "Tucker Kraft"))
        self.assertIsNone(f["over_odds"])  # the feed's price belonged to its own line
        self.started.assert_called_once_with(queue=True)
        self.assertIn(FID, json.loads(web._prop_fixes_path().read_text(encoding="utf-8")))  # survives a restart

    def test_typing_the_feeds_own_line_or_resetting_goes_back_to_the_feed(self):
        self.fix(line=3.5)
        self.fix(line=4.5)
        self.assertEqual(web.STATE["prop_fixes"], {})
        self.fix(line=3.5)
        self.fix(reset=True)
        self.assertEqual(web.STATE["prop_fixes"], {})
        self.assertEqual(json.loads(web._prop_fixes_path().read_text(encoding="utf-8")), {})

    def test_correcting_again_keeps_the_feeds_original_line(self):
        self.fix(line=3.5)
        web.STATE["results"][0]["_prop_feed"] = {}  # a later read of the feed that missed the player
        self.fix(line=2.5)
        self.assertEqual(web.STATE["prop_fixes"][FID]["feed_line"], 4.5)

    def test_a_number_that_is_not_a_line_is_refused(self):
        for bad in (3.7, -1.0, None):
            with self.assertRaises(HTTPException):
                self.fix(line=bad)
        with self.assertRaises(HTTPException) as e:
            web.api_prop_correct(web.PropFixReq(key="NFL|G1|ml|home|", line=3.5))  # not on the slate (or not a prop)
        self.assertEqual(e.exception.status_code, 404)
        self.assertEqual(web.STATE["prop_fixes"], {})
        self.started.assert_not_called()


class CorrectionsEnd(unittest.TestCase):
    """What each update does with the saved corrections."""

    def setUp(self):
        self.state = mock.patch.dict(web.STATE, {"prop_fixes": {FID: a_fix()}})
        self.state.start()

    def tearDown(self):
        self.state.stop()
        if web._prop_fixes_path().exists():
            web._prop_fixes_path().unlink()

    def test_kept_while_the_feed_is_behind(self):
        web._prune_prop_fixes([result(4.5)])
        web._prune_prop_fixes([result(None)])  # the feed showing nothing for it is not the feed catching up
        self.assertIn(FID, web.STATE["prop_fixes"])

    def test_ends_when_the_feed_catches_up(self):
        web._prune_prop_fixes([result(3.5)])
        self.assertEqual(web.STATE["prop_fixes"], {})

    def test_ends_when_the_game_is_no_longer_to_come(self):
        web._prune_prop_fixes([result(4.5, games=("G2",))])
        self.assertEqual(web.STATE["prop_fixes"], {})
        web.STATE["prop_fixes"][FID] = a_fix()
        web._prune_prop_fixes([{"sport": "nfl", "games": pd.DataFrame(), "legs": [], "model": None}])  # no games left
        self.assertEqual(web.STATE["prop_fixes"], {})

    def test_an_update_of_another_sport_leaves_it_alone(self):
        web._prune_prop_fixes([{**result(3.5), "sport": "nba"}])
        self.assertIn(FID, web.STATE["prop_fixes"])

    def test_the_engine_gets_it_as_a_corrected_line_without_a_price(self):
        row = web._prop_fix_lines().iloc[0]
        self.assertEqual((row["book"], row["line"], row["feed_line"]), ("corrected", 3.5, 4.5))
        self.assertTrue(np.isnan(row["over_odds"]) and np.isnan(row["under_odds"]))

    def test_picks_at_the_corrected_line_are_marked_for_the_page(self):
        res = result()
        res["lean_legs"] = [leg(3.5), leg(3.5, side="over")]
        res["legs"] = [leg(4.5, key="other")]  # not at the corrected line: left as it is
        web._mark_prop_fixes([res])
        self.assertTrue(all(l["corrected"] and l["feed_line"] == 4.5 for l in res["lean_legs"]))
        self.assertNotIn("corrected", res["legs"][0])


class PropsMoveOnTheirOwn(unittest.TestCase):
    """The automatic update looks at the prop feed too, not only at the game lines."""

    NOW = pd.Timestamp("2026-11-25T18:00Z")
    LOVE = ("jordan love", "pass_yds")

    def res(self, start="2026-11-26T01:00Z"):
        up = pd.DataFrame({"event_id": ["1"], "home": ["LA"], "away": ["GB"], "start": [pd.Timestamp(start)]})
        return {"sport": "nfl", "_prop_feed": {KRAFT: 4.5, self.LOVE: 240.5}, "_prop_games": up}

    def moved(self, res, feed):
        rows = pd.DataFrame([{"key": k, "market": m, "line": v} for (k, m), v in feed.items()])
        with mock.patch.dict(web.STATE, {"results": [res]}), \
                mock.patch.object(web.engine, "feed_prop_lines", return_value=rows) as read:
            return web._moved_props(self.NOW), read

    def test_a_line_that_moved_or_is_new_is_found(self):
        self.assertEqual(self.moved(self.res(), {KRAFT: 3.5, self.LOVE: 240.5})[0], 1)
        self.assertEqual(self.moved(self.res(), {KRAFT: 4.5, self.LOVE: 240.5, ("christian watson", "rec_yds"): 30.5})[0], 1)

    def test_nothing_new_finds_nothing(self):
        self.assertEqual(self.moved(self.res(), {KRAFT: 4.5, self.LOVE: 240.5})[0], 0)
        self.assertEqual(self.moved(self.res(), {KRAFT: 4.5})[0], 0)  # a line taken down is not a reason to update

    def test_games_that_have_started_are_left_out(self):
        n, read = self.moved(self.res(start="2026-11-25T17:00Z"), {KRAFT: 3.5})
        self.assertEqual(n, 0)
        read.assert_not_called()


class OneMoreUpdate(unittest.TestCase):
    """A correction made while an update is running: that update began before it, so another one follows."""

    def test_a_change_during_an_update_asks_for_one_more(self):
        with mock.patch.dict(web.STATE, {"status": "running", "again": False}):
            self.assertFalse(web.start_run(queue=True))
            self.assertTrue(web.STATE["again"])
        with mock.patch.dict(web.STATE, {"status": "running", "again": False}):
            self.assertFalse(web.start_run())  # the automatic check does not queue: it looks again in 3 minutes
            self.assertFalse(web.STATE["again"])

    def test_the_running_update_starts_it_when_it_ends(self):
        with mock.patch.dict(web.STATE, {"status": "running", "again": True, "results": [], "legs": {}, "plays": {},
                                         "parlays": {}, "overrides": {}, "prop_fixes": {}, "log": []}), \
                mock.patch.object(web, "check_odds"), mock.patch.object(web, "_suggestions", return_value={}), \
                mock.patch.object(web, "start_run") as again:
            web._run([], {}, True, False)
            again.assert_called_once_with()
            self.assertFalse(web.STATE["again"])


class NotOnYourApp(unittest.TestCase):
    """The feed is a sportsbook's menu and an app can offer less of it: bets marked as not offered are left out
    of every suggestion, for every player or for one."""

    def leg(self, player, side, market="fg3m", key=None, **kw):
        l = {"key": key or f"NBA|G9|{market}|{side}|{player}", "sport": "NBA", "game_id": "G9", "subject": player.title(),
             "player_key": player, "market_key": market, "market": "Threes" if market == "fg3m" else "Points", "side": side}
        l.update(kw)
        return l

    def setUp(self):
        self.bam, self.maxey = self.leg("bam adebayo", "under"), self.leg("tyrese maxey", "under")
        self.over, self.pts = self.leg("bam adebayo", "over"), self.leg("tyrese maxey", "under", market="pts")
        self.ml = {"key": "NBA|G9|ml|home|", "sport": "NBA", "game_id": "G9", "market_key": "ml", "side": "home", "player_key": ""}
        legs = [self.bam, self.maxey, self.over, self.pts, self.ml]
        self.state = mock.patch.dict(web.STATE, {"results": [], "legs": {l["key"]: l for l in legs}, "not_offered": {},
                                                 "plays": {}, "parlays": {}})
        self.state.start()
        self.sug = mock.patch.object(web, "_suggestions", return_value={"rebuilt": True})
        self.rebuilt = self.sug.start()

    def tearDown(self):
        self.sug.stop()
        self.state.stop()
        if web._not_offered_path().exists():
            web._not_offered_path().unlink()

    def test_a_whole_stat_can_be_hidden(self):
        out = web.api_offered(web.OfferedReq(key=self.bam["key"]))  # no over/under on threes at all
        self.assertEqual([(r["id"], r["side"], r["player"]) for r in out["not_offered"]], [("NBA|fg3m||", "", "")])
        self.assertEqual([web._offered(l) for l in (self.bam, self.maxey, self.over, self.pts, self.ml)],
                         [False, False, False, True, True])  # overs too; other stats and game bets stay

    def test_or_one_side_of_it_for_every_player(self):
        out = web.api_offered(web.OfferedReq(key=self.bam["key"], scope="side"))
        self.assertEqual([r["id"] for r in out["not_offered"]], ["NBA|fg3m|under|"])
        self.assertEqual([web._offered(l) for l in (self.bam, self.maxey, self.over, self.pts, self.ml)],
                         [False, False, True, True, True])  # threes unders only: overs, other stats and game bets stay
        self.assertEqual(web._on_offer([self.bam, self.over, self.ml]), [self.over, self.ml])
        self.rebuilt.assert_called_once()  # the suggestions are rebuilt at once, without a new simulation
        self.assertEqual(web.STATE["plays"], {"rebuilt": True})
        self.assertIn("NBA|fg3m|under|", json.loads(web._not_offered_path().read_text(encoding="utf-8")))

    def test_or_for_one_player_only(self):
        out = web.api_offered(web.OfferedReq(key=self.bam["key"], scope="player"))
        self.assertEqual((out["not_offered"][0]["player"], out["not_offered"][0]["market"]), ("Bam Adebayo", "Threes"))
        self.assertEqual([web._offered(l) for l in (self.bam, self.maxey)], [False, True])

    def test_and_brought_back(self):
        web.api_offered(web.OfferedReq(key=self.bam["key"]))
        out = web.api_offered(web.OfferedReq(restore="NBA|fg3m||"))
        self.assertEqual(out["not_offered"], [])
        self.assertTrue(web._offered(self.bam))
        self.assertEqual(json.loads(web._not_offered_path().read_text(encoding="utf-8")), {})

    def test_only_a_player_prop_on_the_slate_can_be_marked(self):
        for key in (self.ml["key"], "NBA|G9|fg3m|under|nobody"):
            with self.assertRaises(HTTPException) as e:
                web.api_offered(web.OfferedReq(key=key))
            self.assertEqual(e.exception.status_code, 404)
        self.assertEqual(web.STATE["not_offered"], {})

    def test_the_suggestions_never_see_a_hidden_bet(self):
        self.sug.stop()  # the real builder, with the search itself stubbed
        try:
            web.STATE["not_offered"]["NBA|fg3m|under|"] = {"id": "NBA|fg3m|under|"}
            res = [{"sport": "nba", "legs": [self.bam, self.over, self.ml], "lean_legs": [self.maxey]}]
            with mock.patch.object(web.plays, "build", side_effect=lambda legs: {"keys": [l["key"] for l in legs]}):
                self.assertEqual(web._suggestions(res)["keys"], [self.over["key"], self.ml["key"]])
        finally:
            self.sug.start()


class OrMoreBetsAreMarkedOnTheirOwn(unittest.TestCase):
    """An app can list "2+ threes" and no over/under on threes: a mark on one never hides the other."""

    def setUp(self):
        self.over = {"key": "NBA|G9|fg3m|over|bam adebayo", "sport": "NBA", "game_id": "G9", "subject": "Bam Adebayo", "player_key": "bam adebayo",
                     "market_key": "fg3m", "market": "Threes", "side": "over"}
        self.plus = {**self.over, "key": self.over["key"] + "|2+", "rung": 2, "base_key": self.over["key"]}
        self.state = mock.patch.dict(web.STATE, {"results": [], "legs": {self.over["key"]: self.over}, "not_offered": {}, "plays": {}, "parlays": {}})
        self.state.start()
        self.sug = mock.patch.object(web, "_suggestions", return_value={})
        self.sug.start()

    def tearDown(self):
        self.sug.stop()
        self.state.stop()
        if web._not_offered_path().exists():
            web._not_offered_path().unlink()

    def test_no_over_under_on_a_stat_leaves_its_or_more_bets_suggested(self):
        web.api_offered(web.OfferedReq(key=self.over["key"]))  # "Threes: overs and unders"
        self.assertFalse(web._offered(self.over))
        self.assertTrue(web._offered(self.plus))

    def test_a_mark_made_on_an_or_more_bet_hides_those_and_not_the_over_under(self):
        out = web.api_offered(web.OfferedReq(key=self.over["key"], scope="stat", rung=True))  # held in the slip as 2+
        self.assertEqual([(r["id"], r["side"]) for r in out["not_offered"]], [("NBA|fg3m|plus|", "plus")])
        self.assertFalse(web._offered(self.plus))
        self.assertTrue(web._offered(self.over))
        web.api_offered(web.OfferedReq(restore="NBA|fg3m|plus|"))
        web.api_offered(web.OfferedReq(key=self.over["key"], scope="player", rung=True))
        self.assertFalse(web._offered(self.plus))
        self.assertTrue(web._offered({**self.plus, "player_key": "tyrese maxey"}))

    def test_an_unpriced_or_more_bet_stands_in_only_for_an_over_that_is_not_offered(self):
        # "2+ threes" with no price of its own is the over on 1.5 again: suggested under one name, never both
        twin = {**self.plus, "assumed": True}
        res = [{"sport": "nba", "legs": [self.over], "lean_legs": [], "rung_legs": [twin]}]
        self.assertEqual([l["key"] for l in web._candidates(res)], [self.over["key"]])
        web.STATE["not_offered"]["NBA|fg3m||"] = {"id": "NBA|fg3m||"}  # no over/under on threes on the app
        self.assertEqual([l["key"] for l in web._candidates(res)], [twin["key"]])

    def test_the_suggestions_are_offered_the_or_more_picks_too(self):
        self.sug.stop()
        try:
            res = [{"sport": "nba", "legs": [self.over], "lean_legs": [], "rung_legs": [self.plus]}]
            with mock.patch.object(web.plays, "build", side_effect=lambda legs: {"keys": [l["key"] for l in legs]}):
                self.assertEqual(web._suggestions(res)["keys"], [self.over["key"], self.plus["key"]])
                web.STATE["not_offered"]["NBA|fg3m||"] = {"id": "NBA|fg3m||"}
                self.assertEqual(web._suggestions(res)["keys"], [self.plus["key"]])
        finally:
            self.sug.start()


class SlipCoach(unittest.TestCase):
    """What the bet slip's coach is told: what the open bets already hold, and the slip without each pick."""

    def bets(self):
        def row(bid, stake, status, legs):
            return {"id": bid, "stake": stake, "status": status, "legs_json": json.dumps(legs)}
        la_ml = {"key": "NFL|G1|ml|home|", "sport": "NFL", "game_id": "G1"}
        over = {"key": "NFL|G1|total|over|", "sport": "NFL", "game_id": "G1"}
        hou = {"key": "NBA|E2|ml|home|", "sport": "NBA", "game_id": "E2"}
        return pd.DataFrame([row("a", 5.0, "open", [la_ml, over]), row("b", 10.0, "open", [la_ml, hou]),
                             row("c", 20.0, "lost", [la_ml]), row("d", 5.0, "open", "not json")])

    def test_open_bets_are_counted_per_pick_and_per_game(self):
        h = web.held(self.bets())
        self.assertEqual((h["open"], h["staked"]), (2, 15.0))  # the settled bet and the unreadable one are left out
        self.assertEqual(h["picks"]["NFL|G1|ml|home|"], {"bets": 2, "stake": 15.0})
        self.assertEqual(h["picks"]["NFL|G1|total|over|"], {"bets": 1, "stake": 5.0})
        # a parlay counts once for a game however many of its picks are from it
        self.assertEqual(h["games"], {"nfl|G1": {"bets": 2, "stake": 15.0}, "nba|E2": {"bets": 1, "stake": 10.0}})

    def test_no_bets(self):
        self.assertEqual(web.held(pd.DataFrame()), {"picks": {}, "games": {}, "open": 0, "staked": 0.0})

    def test_the_slip_is_priced_again_without_each_pick(self):
        legs = [{"key": k} for k in ("a", "b", "c")]
        seen = []

        def fake(ls):
            seen.append([l["key"] for l in ls])
            return {"p_win_all": 0.1 * len(ls), "odds": 100.0 * len(ls)}

        with mock.patch.object(web.parlay, "evaluate", side_effect=fake):
            out = web._without_each(legs)
            self.assertEqual(seen, [["b", "c"], ["a", "c"], ["a", "b"]])
            self.assertEqual(out["a"], {"p": 0.2, "odds": 200.0})
            self.assertEqual(web._without_each(legs[:2]), {})  # two picks: leaving one out is just the other pick


if __name__ == "__main__":
    unittest.main()
