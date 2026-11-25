"""Live tracking, the cash-out check, and the passcode gate. Run with: python -m unittest discover tests"""
import json
import os
import tempfile
import unittest
from unittest import mock

from sportsedge import access, live


def game(state="in", period=3, clock=360.0, home=70, away=60, sport_home="DEN", sport_away="DAL"):
    return {"event_id": "1", "start": "2026-11-25T03:00Z", "home": sport_home, "away": sport_away, "home_score": float(home),
            "away_score": float(away), "state": state, "detail": "3rd 6:00", "period": period, "clock": clock}


def leg(mk, side, line=None, p=0.5, pkey="", proj=None, dec=1.909, sport="NBA"):
    return {"key": f"{mk}|{side}|{pkey}", "sport": sport, "game_id": "E1", "date": "2026-11-24", "home": "DEN", "away": "DAL",
            "market_key": mk, "side": side, "player_key": pkey, "line": line, "dec": dec, "selection": f"{mk} {side}", "p_cond": p, "proj": proj}


class Clock(unittest.TestCase):
    def test_share_of_the_game_left(self):
        self.assertEqual(live.remaining_fraction("nba", game("pre")), 1.0)
        self.assertEqual(live.remaining_fraction("nba", game("post")), 0.0)
        self.assertAlmostEqual(live.remaining_fraction("nba", game(period=3, clock=360)), 18 / 48)   # 6:00 left in the 3rd
        self.assertAlmostEqual(live.remaining_fraction("nfl", game(period=2, clock=120)), 32 / 60)   # 2:00 left in the 2nd
        self.assertLessEqual(live.remaining_fraction("nba", game(period=5, clock=200)), 0.1)         # overtime: nearly over


class BoxScore(unittest.TestCase):
    def test_nba_stat_line(self):
        j = {"boxscore": {"players": [{"statistics": [{"keys": ["minutes", "points", "fieldGoalsMade-fieldGoalsAttempted",
                                                                 "threePointFieldGoalsMade-threePointFieldGoalsAttempted", "rebounds", "assists", "steals", "blocks"],
                                                        "athletes": [{"athlete": {"displayName": "Nikola Jokić"}, "stats": ["30", "27", "10-18", "2-5", "11", "9", "1", "0"]},
                                                                     {"athlete": {"displayName": "Sat Out"}, "didNotPlay": True, "stats": []}]}]}]},
             "winprobability": [{"homeWinPercentage": 0.5}, {"homeWinPercentage": 0.83}]}
        b = live.parse_boxscore("nba", j)
        p = b["players"]["nikola jokic"]
        self.assertEqual((p["pts"], p["reb"], p["ast"], p["fg3m"], p["pra"], p["ra"]), (27, 11, 9, 2, 47, 20))
        self.assertNotIn("sat out", b["players"])
        self.assertAlmostEqual(b["home_wp"], 0.83)

    def test_nfl_stat_line_adds_up_across_tables(self):
        j = {"boxscore": {"players": [{"statistics": [
            {"name": "passing", "keys": ["completions/passingAttempts", "passingYards", "yardsPerPassAttempt", "passingTouchdowns"],
             "athletes": [{"athlete": {"displayName": "Josh Allen"}, "stats": ["21/30", "248", "8.3", "2"]}]},
            {"name": "rushing", "keys": ["rushingAttempts", "rushingYards", "yardsPerRushAttempt", "rushingTouchdowns"],
             "athletes": [{"athlete": {"displayName": "Josh Allen"}, "stats": ["7", "41", "5.9", "1"]},
                          {"athlete": {"displayName": "James Cook"}, "stats": ["18", "-3", "0.0", "0"]}]},
            {"name": "receiving", "keys": ["receptions", "receivingYards", "yardsPerReception", "receivingTouchdowns"],
             "athletes": [{"athlete": {"displayName": "James Cook"}, "stats": ["4", "32", "8.0", "1"]}]}]}]}}
        p = live.parse_boxscore("nfl", j)["players"]
        self.assertEqual((p["josh allen"]["pass_cmp"], p["josh allen"]["pass_att"], p["josh allen"]["pass_yds"], p["josh allen"]["pass_tds"]), (21, 30, 248, 2))
        self.assertEqual((p["josh allen"]["rush_att"], p["josh allen"]["rush_yds"], p["josh allen"]["anytime_td"]), (7, 41, 1))
        self.assertEqual((p["james cook"]["rush_yds"], p["james cook"]["receptions"], p["james cook"]["rush_rec_yds"], p["james cook"]["anytime_td"]), (-3, 4, 29, 1))


class LiveChances(unittest.TestCase):
    def test_before_tip_off_the_chance_is_the_pre_game_chance(self):
        g0 = game("in", period=1, clock=720, home=0, away=0)  # the opening second
        for l in (leg("spread", "home", -4.5, p=0.52), leg("total", "over", 221.5, p=0.47), leg("total", "under", 221.5, p=0.53),
                  leg("ml", "away", p=0.35), leg("spread", "away", 3.0, p=0.51), leg("total", "under", 220.0, p=0.48)):
            win, push = live.game_leg_chance("nba", l, g0, None)
            given_no_push = win if l["market_key"] == "ml" else win / (1 - push)  # a pick's chance is quoted given no push
            self.assertAlmostEqual(given_no_push, l["p_cond"], delta=0.003, msg=l["selection"])

    def test_a_big_late_lead_is_nearly_decided(self):
        late = game(period=4, clock=60, home=110, away=92)
        self.assertGreater(live.game_leg_chance("nba", leg("ml", "home", p=0.6), late, None)[0], 0.99)
        self.assertLess(live.game_leg_chance("nba", leg("ml", "away", p=0.4), late, None)[0], 0.01)
        self.assertGreater(live.game_leg_chance("nba", leg("spread", "home", -9.5), late, None)[0], 0.95)

    def test_a_bigger_lead_and_less_time_both_help(self):
        l = leg("spread", "home", -3.5)
        up5 = live.game_leg_chance("nba", l, game(period=3, clock=360, home=70, away=65), None)[0]
        up12 = live.game_leg_chance("nba", l, game(period=3, clock=360, home=77, away=65), None)[0]
        up12_late = live.game_leg_chance("nba", l, game(period=4, clock=120, home=107, away=95), None)[0]
        self.assertLess(up5, up12)
        self.assertLess(up12, up12_late)

    def test_the_feeds_own_win_chance_is_used_for_moneylines(self):
        g = game(period=2, clock=300, home=40, away=44)
        self.assertAlmostEqual(live.game_leg_chance("nba", leg("ml", "home", p=0.6), g, 0.41)[0], 0.41)
        self.assertAlmostEqual(live.game_leg_chance("nba", leg("ml", "away", p=0.4), g, 0.41)[0], 0.59)

    def test_the_moneyline_starts_from_the_pre_game_chance_not_the_feeds(self):
        tip = game(period=1, clock=720, home=0, away=0)
        # the feed opens the home team at 47%; this tool had it at 57% before the game
        self.assertAlmostEqual(live.game_leg_chance("nba", leg("ml", "home", p=0.57), tip, 0.47, None, 0.47)[0], 0.57, places=6)
        self.assertAlmostEqual(live.game_leg_chance("nba", leg("ml", "away", p=0.43), tip, 0.47, None, 0.47)[0], 0.43, places=6)
        # late on, the feed's reading of the game takes over
        late = game(period=4, clock=30, home=100, away=90)
        self.assertAlmostEqual(live.game_leg_chance("nba", leg("ml", "home", p=0.57), late, 0.98, None, 0.47)[0], 0.98, delta=0.005)
        mid = live.game_leg_chance("nba", leg("ml", "home", p=0.57), game(period=3, clock=720, home=50, away=50), 0.47, None, 0.47)[0]
        self.assertTrue(0.47 < mid < 0.57)

    def test_a_whole_number_total_can_push(self):
        final = game("post", home=110, away=110)
        self.assertEqual(live.game_leg_chance("nba", leg("total", "over", 220.0), final, None), (0.0, 1.0))

    def test_a_prop_starts_at_its_pre_game_chance(self):
        # whatever the simulator's own projection says, the opening second matches the pick's pre-game chance
        for mk, line, p, proj in (("pts", 24.5, 0.50, 24.6), ("pts", 23.5, 0.49, 20.0), ("rec_yds", 62.5, 0.58, 80.0),
                                  ("fg3m", 2.5, 0.41, 2.9), ("receptions", 4.5, 0.55, 3.0), ("reb", 9.0, 0.47, 9.5)):
            for side in ("over", "under"):
                l = leg(mk, side, line, p=p, pkey="x", proj=proj)
                win, push = live.prop_leg_chance(l, 0.0, 1.0)
                self.assertAlmostEqual(win / (1 - push), p, delta=0.005, msg=f"{mk} {side}")  # quoted given no push
                self.assertEqual(push > 0, line == 9.0)

    def test_prop_already_over_or_running_out_of_time(self):
        over = leg("pts", "over", 24.5, pkey="x", proj=24)
        self.assertEqual(live.prop_leg_chance(over, 26.0, 0.3)[0], 1.0)
        self.assertEqual(live.prop_leg_chance(leg("pts", "under", 24.5, pkey="x", proj=24), 26.0, 0.3)[0], 0.0)
        self.assertLess(live.prop_leg_chance(over, 12.0, 0.06)[0], 0.02)            # needs 13 in the last three minutes
        self.assertGreater(live.prop_leg_chance(over, 23.0, 0.40)[0], 0.85)         # needs 2 with most of a half left
        slow, fast = live.prop_leg_chance(over, 8.0, 0.5)[0], live.prop_leg_chance(over, 16.0, 0.5)[0]
        self.assertLess(slow, fast)

    def test_small_counts_and_touchdowns(self):
        threes = leg("fg3m", "over", 2.5, p=0.45, pkey="x", proj=2.4)
        self.assertEqual(live.prop_leg_chance(threes, 3.0, 0.5)[0], 1.0)
        self.assertLess(live.prop_leg_chance(threes, 0.0, 0.1)[0], 0.05)
        td = leg("anytime_td", "yes", 0.5, p=0.40, pkey="x", sport="NFL")
        self.assertEqual(live.prop_leg_chance(td, 1.0, 0.5)[0], 1.0)
        self.assertAlmostEqual(live.prop_leg_chance(td, 0.0, 1.0)[0], 0.40, places=6)
        self.assertLess(live.prop_leg_chance(td, 0.0, 0.25)[0], 0.15)


class BetAsItStands(unittest.TestCase):
    def _view(self, legs, games, box, stake=10.0, dec=None):
        d = dec or 1.0
        if dec is None:
            for l in legs:
                d *= l["dec"]
        with mock.patch.object(live, "find_game", side_effect=lambda sport, l: games[l["key"]]), \
                mock.patch.object(live, "boxscore", return_value=box):
            return live.bet_view({"id": "b", "legs": "x", "legs_json": json.dumps(legs), "stake": stake, "dec": d, "odds": 0})

    def test_five_of_six_won_one_in_play(self):
        won = [leg("total", "over", 200.5 + i, p=0.5) for i in range(5)]
        last = leg("pts", "over", 24.5, p=0.5, pkey="star", proj=25.0)
        games = {l["key"]: game("post", home=120, away=110) for l in won}
        games[last["key"]] = game("in", period=4, clock=300, home=95, away=90)
        v = self._view(won + [last], games, {"players": {"star": {"pts": 21.0}}, "home_wp": 0.7})
        self.assertEqual((v["won"], v["open"], v["lost"], v["status"]), (5, 1, 0, "live"))
        self.assertAlmostEqual(v["payout"], 10 * 1.909 ** 6, places=4)
        self.assertAlmostEqual(v["value"], v["payout"] * v["p_all"], places=6)  # a half-point line cannot push
        self.assertEqual(v["p_all"], v["legs"][-1]["p_now"])
        self.assertTrue(0.05 < v["p_all"] < 0.95)

    def test_a_lost_leg_ends_it_and_a_push_drops_out(self):
        a, b, c = leg("total", "over", 250.5), leg("total", "over", 230.0), leg("total", "under", 260.5)
        fin = game("post", home=120, away=110)  # total 230
        v = self._view([a, c], {a["key"]: fin, c["key"]: fin}, {"players": {}, "home_wp": None})
        self.assertEqual((v["status"], v["value"]), ("lost", 0.0))
        v = self._view([b, c], {b["key"]: fin, c["key"]: fin}, {"players": {}, "home_wp": None})
        self.assertEqual(v["legs"][0]["status"], "push")
        self.assertEqual(v["status"], "won")
        self.assertAlmostEqual(v["payout"], 10 * 1.909, places=4)  # paid as a single

    def test_a_leg_that_might_push_adds_its_reduced_payout(self):
        l = leg("total", "over", 220.0, p=0.5)  # whole-number line, level game late on: a push is a real possibility
        v = self._view([l], {l["key"]: game("in", period=4, clock=20, home=110, away=108)}, {"players": {}, "home_wp": None})
        x = v["legs"][0]
        self.assertGreater(x["p_push"], 0.05)
        self.assertAlmostEqual(v["value"], 10 * 1.909 * x["p_now"] + 10 * x["p_push"], places=6)  # win pays in full, a push returns the stake

    def test_an_nba_player_missing_from_the_box_score_is_not_written_off(self):
        l = leg("pts", "over", 22.5, p=0.5, pkey="absent", proj=24.0)
        v = self._view([l], {l["key"]: game("in", period=3, clock=400, home=60, away=50)}, {"players": {"other": {"pts": 15.0}}, "home_wp": 0.6})
        x = v["legs"][0]
        self.assertEqual((x["status"], x["decided"]), ("live", False))
        self.assertIn("not in the box score", x["need"])
        self.assertAlmostEqual(x["p_now"], 0.5)  # his chance stays where it was, rather than falling as if he had scored nothing

    def test_an_over_already_reached_is_decided_before_the_final_whistle(self):
        l = leg("reb", "over", 9.5, pkey="big", proj=10)
        v = self._view([l], {l["key"]: game("in", period=2, clock=100)}, {"players": {"big": {"reb": 11.0}}, "home_wp": None})
        self.assertEqual((v["legs"][0]["status"], v["legs"][0]["decided"], v["status"]), ("won", True, "won"))


class AtALiveLine(unittest.TestCase):
    def test_a_pick_judged_at_its_own_line_is_unchanged(self):
        g = game("in", period=2, clock=300, home=60, away=50)
        l = leg("total", "over", 220.5, p=0.5)
        self.assertAlmostEqual(live.game_leg_chance("nba", l, g, None)[0], live.game_leg_chance("nba", l, g, None, at=220.5)[0])
        p = leg("pts", "over", 24.5, p=0.5, pkey="a", proj=25)
        self.assertAlmostEqual(live.prop_leg_chance(p, 12.0, 0.5)[0], live.prop_leg_chance(p, 12.0, 0.5, at=24.5)[0])

    def test_a_higher_line_is_harder_to_go_over(self):
        g = game("in", period=2, clock=300, home=60, away=50)
        l = leg("total", "over", 220.5, p=0.5)
        self.assertGreater(live.game_leg_chance("nba", l, g, None, at=215.5)[0], live.game_leg_chance("nba", l, g, None, at=228.5)[0])
        p = leg("pts", "over", 24.5, p=0.5, pkey="a", proj=25)
        self.assertGreater(live.prop_leg_chance(p, 12.0, 0.5, at=20.5)[0], live.prop_leg_chance(p, 12.0, 0.5, at=30.5)[0])
        home = leg("spread", "home", -3.5, p=0.5)
        self.assertGreater(live.game_leg_chance("nba", home, g, None, at=-3.5)[0], live.game_leg_chance("nba", home, g, None, at=-14.5)[0])


class Hedge(unittest.TestCase):
    def _bet(self, key, mk, side, line, payout=19.09, stake=10.0, **kw):
        return {"status": "live", "open": 1, "payout": payout, "stake": stake,
                "legs": [{"key": key, "decided": False, "state": "in", "market_key": mk, "side": side, "line": line}], **kw}

    def _row(self, selection, line, odds=-115.0):
        return {"selection": selection, "line": line, "odds": odds, "dec": 1 + 100 / abs(odds) if odds < 0 else 1 + odds / 100, "book": "live"}

    def test_the_other_side_returns_the_same_either_way(self):
        h = live.hedge(self._bet("NBA|g|total|over|", "total", "over", 218.5), {"NBA|g|total|under|": self._row("Under 218.5", 218.5)})
        self.assertAlmostEqual(h["stake"] * (1 + 100 / 115), 19.09, places=6)  # the hedge pays what the bet would have
        self.assertAlmostEqual(h["locked"], 19.09 - 10.0 - h["stake"], places=6)
        self.assertIsNone(h["middle"])
        self.assertIsNone(h["gap"])

    def test_a_line_that_moved_past_yours_leaves_a_middle(self):
        # holds over 218.5; the live total is 222.5: a finish of 219 to 222 wins both
        h = live.hedge(self._bet("NBA|g|total|over|", "total", "over", 218.5), {"NBA|g|total|under|": self._row("Under 222.5", 222.5)})
        self.assertEqual((h["middle"]["lo"], h["middle"]["hi"]), (218.5, 222.5))
        self.assertAlmostEqual(h["middle"]["profit"], 2 * 19.09 - 10.0 - h["stake"], places=6)
        self.assertIsNone(h["gap"])

    def test_a_line_that_moved_the_other_way_leaves_a_gap(self):
        h = live.hedge(self._bet("NBA|g|total|over|", "total", "over", 225.5), {"NBA|g|total|under|": self._row("Under 222.5", 222.5)})
        self.assertIsNone(h["middle"])
        self.assertAlmostEqual(h["gap"]["loss"], 10.0 + h["stake"], places=6)  # 223 to 225 loses both
        under = live.hedge(self._bet("NBA|g|pts|under|a", "pts", "under", 24.5), {"NBA|g|pts|over|a": self._row("A o20.5", 20.5)})
        self.assertEqual((under["middle"]["lo"], under["middle"]["hi"]), (20.5, 24.5))  # 21 to 24 wins both

    def test_spreads_are_compared_on_the_home_margin(self):
        # holds the home team -3.5 (needs to win by 4+); live, the away team is +8.5 (wins if home wins by 8 or less)
        h = live.hedge(self._bet("NBA|g|spread|home|", "spread", "home", -3.5), {"NBA|g|spread|away|": self._row("AWAY +8.5", 8.5)})
        self.assertEqual((h["middle"]["lo"], h["middle"]["hi"], h["middle"]["unit"]), (3.5, 8.5, "margin"))
        h = live.hedge(self._bet("NBA|g|spread|home|", "spread", "home", -3.5), {"NBA|g|spread|away|": self._row("AWAY +1.5", 1.5)})
        self.assertEqual((h["gap"]["lo"], h["gap"]["hi"]), (1.5, 3.5))

    def test_only_a_bet_with_one_pick_left_and_that_game_on(self):
        rows = {"NBA|g|ml|away|": self._row("AWAY ML", None, 150.0)}
        ok = self._bet("NBA|g|ml|home|", "ml", "home", None)
        self.assertIsNotNone(live.hedge(ok, rows))
        self.assertIsNone(live.hedge(ok, rows)["middle"])  # a moneyline has no line to land between
        self.assertIsNone(live.hedge({**ok, "open": 2}, rows))
        self.assertIsNone(live.hedge({**ok, "status": "lost"}, rows))
        waiting = self._bet("NBA|g|ml|home|", "ml", "home", None)
        waiting["legs"][0]["state"] = "pre"
        self.assertIsNone(live.hedge(waiting, rows))
        self.assertIsNone(live.hedge(ok, {}))  # no live price for the other side


class CashOut(unittest.TestCase):
    def test_fair_value_and_the_books_cut(self):
        c = live.cash_out(150, 205, 0.80)
        self.assertAlmostEqual(c["fair"], 164.0)
        self.assertAlmostEqual(c["offer_share"], 150 / 164)
        self.assertAlmostEqual(c["breakeven_p"], 150 / 205)
        self.assertEqual(c["verdict"], "hold")
        self.assertIsNone(c["risk_value"])

    def test_an_offer_above_fair_value_is_taken(self):
        self.assertEqual(live.cash_out(170, 205, 0.80, 5000)["verdict"], "take")

    def test_a_small_bankroll_makes_sure_money_worth_more(self):
        big, small = live.cash_out(150, 205, 0.80, 50_000), live.cash_out(150, 205, 0.80, 300)
        self.assertLess(small["risk_value"], big["risk_value"])
        self.assertLess(big["risk_value"], big["fair"])           # never above the average value
        self.assertAlmostEqual(big["risk_value"], big["fair"], delta=1.0)
        self.assertEqual((big["verdict"], small["verdict"]), ("hold", "reasonable"))

    def test_a_dead_bet_has_nothing_to_cash(self):
        self.assertEqual(live.cash_out(5, 205, 0.0, 1000)["verdict"], "gone")


class PasscodeGate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"SPORTSEDGE_HISTORY": self.dir.name})
        self.env.start()
        access._fails.clear()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def test_locked_until_a_passcode_is_set(self):
        self.assertFalse(access.has_passcode())
        self.assertFalse(access.verify("anything"))
        self.assertFalse(access.valid_token("9999999999.abc"))

    def test_passcode_and_session(self):
        access.set_passcode("correct horse battery")
        self.assertTrue(access.has_passcode())
        self.assertTrue(access.verify("correct horse battery"))
        self.assertFalse(access.verify("correct horse batterx"))
        tok = access.new_token()
        self.assertTrue(access.valid_token(tok))
        self.assertFalse(access.valid_token(tok[:-1] + ("0" if tok[-1] != "0" else "1")))
        self.assertFalse(access.valid_token("1.deadbeef"))                      # expired and forged
        with open(os.path.join(self.dir.name, "access.json"), encoding="utf-8") as f:
            stored = f.read()
        self.assertNotIn("correct horse battery", stored)                       # only a scrambled form is kept
        access.set_passcode("a different one")                                  # changing it signs everyone out
        self.assertFalse(access.valid_token(tok))

    def test_what_counts_as_coming_from_outside(self):
        self.assertFalse(access.is_remote({}, "127.0.0.1:8765"))
        self.assertFalse(access.is_remote({}, "localhost:8765"))
        self.assertTrue(access.is_remote({}, "something.trycloudflare.com"))
        self.assertTrue(access.is_remote({}, "192.168.1.20:8765"))
        self.assertTrue(access.is_remote({"x-forwarded-for": "203.0.113.5"}, "127.0.0.1:8765"))
        self.assertTrue(access.is_remote({"cf-connecting-ip": "203.0.113.5"}, "localhost:8765"))

    def test_wrong_tries_are_limited(self):
        for _ in range(access.MAX_FAILS - 1):
            access.failed("203.0.113.5")
        self.assertFalse(access.blocked("203.0.113.5"))
        access.failed("203.0.113.5")
        self.assertTrue(access.blocked("203.0.113.5"))
        self.assertFalse(access.blocked("198.51.100.1"))


if __name__ == "__main__":
    unittest.main()
