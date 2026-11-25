"""Offline tests for the Live page's two money cards: what every bet together can still pay, and what has been
earned today, this week, this month and this year.
Run:  python -m unittest discover tests"""
import datetime as dt
import json
import unittest

import pandas as pd

from sportsedge import live

CT = dt.timezone(dt.timedelta(hours=-6))
NOW = dt.datetime(2026, 11, 25, 13, 0, tzinfo=CT)  # a Wednesday, 1 PM Central


def view(bid, status, stake, payout, value=0.0, legs=()):
    return {"id": bid, "status": status, "stake": stake, "payout": payout, "value": value,
            "legs": [{"status": s, "start": t} for s, t in legs]}


def bet(bid, stake, starts, status="open", pnl=None, graded_at=None):
    return {"id": bid, "placed_at": "2026-11-20T00:00:00+00:00", "stake": stake, "status": status, "pnl": pnl, "graded_at": graded_at,
            "legs_json": json.dumps([{"start": s} for s in starts])}


class TotalPayout(unittest.TestCase):
    def test_won_is_banked_lost_is_gone_and_the_rest_is_still_to_play_for(self):
        b = live.payout_board([view("a", "won", 5, 20), view("b", "lost", 10, 40), view("c", "pre", 5, 30, 6.0),
                               view("d", "live", 5, 50, 20.0), view("e", "unknown", 5, 0)])
        self.assertEqual((b["won"], b["alive"], b["lost"], b["possible"]), (20.0, 80.0, 40.0, 100.0))
        self.assertEqual((b["n_won"], b["n_alive"], b["n_live"], b["n_lost"]), (1, 2, 1, 1))
        self.assertEqual((b["staked"], b["staked_alive"], b["lost_stake"]), (25.0, 10.0, 10.0))  # a bet that could not be read is left out
        self.assertEqual(b["expected"], 46.0)  # the banked 20, plus what the two still alive are worth on average

    def test_it_falls_when_a_bet_loses_and_holds_when_one_wins(self):
        before = live.payout_board([view("a", "live", 5, 20, 10.0), view("b", "live", 5, 30, 10.0)])
        a_wins = live.payout_board([view("a", "won", 5, 20), view("b", "live", 5, 30, 10.0)])
        b_loses = live.payout_board([view("a", "won", 5, 20), view("b", "lost", 5, 30)])
        self.assertEqual((before["possible"], a_wins["possible"], b_loses["possible"]), (50.0, 50.0, 20.0))
        self.assertEqual((a_wins["won"], b_loses["won"]), (20.0, 20.0))

    def test_nothing_recorded(self):
        self.assertEqual(live.payout_board([])["possible"], 0.0)


class Earnings(unittest.TestCase):
    def spans(self, bets, views=None, now=NOW):
        return {e["key"]: e for e in live.earnings(pd.DataFrame(bets), views, now)}

    def test_each_span_counts_the_bets_decided_inside_it(self):
        e = self.spans([
            bet("today", 5, ["2026-11-25T18:00:00+00:00"], "won", 15.0, "2026-11-25T18:59:00+00:00"),   # noon today
            bet("monday", 10, ["2026-11-24T01:00:00+00:00"], "lost", -10.0, "2026-11-24T05:00:00+00:00"),  # Monday night
            bet("sunday", 10, ["2026-11-22T19:00:00+00:00"], "won", 8.0, "2026-11-22T23:00:00+00:00"),   # last week
            bet("october", 10, ["2026-10-10T19:00:00+00:00"], "lost", -10.0, "2026-10-11T01:00:00+00:00"),
            bet("lastyear", 10, ["2025-12-10T19:00:00+00:00"], "won", 30.0, "2025-12-11T01:00:00+00:00"),
            bet("open", 10, ["2026-11-26T19:00:00+00:00"])])
        self.assertEqual([e[k]["pnl"] for k in ("day", "week", "month", "year")], [15.0, 5.0, 13.0, 3.0])
        self.assertEqual((e["week"]["n"], e["week"]["won"], e["week"]["lost"], e["week"]["staked"]), (2, 1, 1, 15.0))
        self.assertEqual(e["day"]["label"], "Today")

    def test_a_late_game_belongs_to_the_night_it_started(self):
        # tipped off at 9 PM Central on the 24th, graded after midnight: it is the 24th's result, not the 25th's
        late = bet("late", 10, ["2026-11-25T03:00:00+00:00"], "won", 12.0, "2026-11-25T06:30:00+00:00")
        e = self.spans([late])
        self.assertEqual((e["day"]["n"], e["week"]["n"]), (0, 1))
        self.assertEqual(self.spans([late], now=dt.datetime(2026, 11, 24, 23, 59, tzinfo=CT))["day"]["pnl"], 12.0)

    def test_a_parlay_that_lost_counts_when_it_lost_not_when_its_last_game_is_played(self):
        # sunk by Monday night's game and graded then; its last game is not until Sunday
        lost = bet("p", 10, ["2026-11-24T01:00:00+00:00", "2026-11-29T18:00:00+00:00"], "lost", -10.0, "2026-11-24T04:10:00+00:00")
        self.assertEqual(self.spans([lost])["week"]["pnl"], -10.0)
        self.assertEqual(live.settled_bets(pd.DataFrame([lost]))[0]["at"], pd.Timestamp("2026-11-24T01:10:00+00:00"))
        # graded a week late (the dashboard was off): still inside the span of the bet's own games
        stale = bet("s", 10, ["2026-11-24T01:00:00+00:00"], "lost", -10.0, "2026-12-05T04:00:00+00:00")
        self.assertEqual(live.settled_bets(pd.DataFrame([stale]))[0]["at"], pd.Timestamp("2026-11-24T01:00:00+00:00"))

    def test_the_live_scores_count_before_the_grader_has_written_the_result(self):
        open_bet = bet("x", 5, ["2026-11-25T18:00:00+00:00", "2026-11-25T19:00:00+00:00"])
        won = view("x", "won", 5, 20, legs=[("won", "2026-11-25T18:00:00+00:00"), ("won", "2026-11-25T19:00:00+00:00")])
        lost = view("x", "lost", 5, 20, legs=[("lost", "2026-11-25T18:00:00+00:00"), ("pre", "2026-11-25T19:00:00+00:00")])
        self.assertEqual(self.spans([open_bet], [won])["day"]["pnl"], 15.0)
        self.assertEqual(self.spans([open_bet], [lost])["day"]["pnl"], -5.0)
        self.assertEqual(self.spans([open_bet], [view("x", "live", 5, 20)])["day"]["n"], 0)  # still in play: nothing yet

    def test_a_bet_is_never_counted_twice(self):
        graded = bet("x", 5, ["2026-11-25T18:00:00+00:00"], "won", 15.0, "2026-11-25T21:10:00+00:00")
        won = view("x", "won", 5, 20, legs=[("won", "2026-11-25T18:00:00+00:00")])
        e = self.spans([graded], [won])
        self.assertEqual((e["day"]["pnl"], e["day"]["n"]), (15.0, 1))

    def test_no_bets_at_all(self):
        self.assertEqual([x["pnl"] for x in live.earnings(pd.DataFrame(), None, NOW)], [0.0, 0.0, 0.0, 0.0])


if __name__ == "__main__":
    unittest.main()
