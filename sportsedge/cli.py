"""Command line interface.

  python -m sportsedge slate                 # today's NFL + NBA games, picks, props, parlays
  python -m sportsedge slate --sport nfl --days 7
  python -m sportsedge backtest --sport nba  # walk-forward test against real closing lines
  python -m sportsedge factors --sport nfl   # what the model learned (points per 1 SD of each factor)
  python -m sportsedge share                 # reach the dashboard from anywhere (Vercel address + tunnel)
  python -m sportsedge passcode              # set the passcode that guards remote access
"""
from __future__ import annotations

import argparse
import sys
import warnings

from . import config


def main(argv=None):
    warnings.filterwarnings("ignore")
    for stream in (sys.stdout, sys.stderr):  # player names like Jokic need UTF-8 on Windows consoles
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="sportsedge", description="NFL / NBA prediction and bet-value tool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("slate", help="price upcoming games, props and parlays")
    s.add_argument("--sport", choices=["nfl", "nba", "all"], default="all")
    s.add_argument("--days", type=int, default=None, help="days ahead to include (default: NFL 7, NBA 1)")
    s.add_argument("--no-props", action="store_true")
    s.add_argument("--props-csv", help="CSV of prop lines: sport,player,market,line,over_odds,under_odds")
    s.add_argument("--bankroll", type=float, help="show quarter-Kelly stake sizes for this bankroll")
    s.add_argument("--sims", type=int, default=config.N_SIMS)
    s.add_argument("--max-legs", type=int, default=config.PARLAY_MAX_LEGS)
    s.add_argument("--refresh", action="store_true", help="ignore cached downloads")
    s.add_argument("--no-track", action="store_true",
                   help="do not log these picks to history/ (use with made-up test lines)")

    b = sub.add_parser("backtest", help="walk-forward backtest vs closing lines")
    b.add_argument("--sport", choices=["nfl", "nba", "all"], default="all")
    b.add_argument("--min-edge", type=float, default=0.02)
    b.add_argument("--refresh", action="store_true")

    gr = sub.add_parser("grade", help="settle logged picks after the games and show the track record")
    gr.add_argument("--sport", choices=["nfl", "nba", "all"], default="all")
    gr.add_argument("--days", type=int, help="only report picks from the last N days")

    u = sub.add_parser("ui", help="open the web dashboard in your browser")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--no-browser", action="store_true", help="start the server without opening a browser")

    sh = sub.add_parser("share", help="reach the dashboard from anywhere through your Vercel address")
    sh.add_argument("--port", type=int, default=8765)

    pc = sub.add_parser("passcode", help="set the passcode that guards remote access")
    pc.add_argument("--clear", action="store_true", help="remove the passcode (locks remote access)")

    au = sub.add_parser("autostart", help="start the dashboard and its remote address when you sign in to Windows")
    au.add_argument("state", nargs="?", choices=["on", "off", "status"], default="status")

    f = sub.add_parser("factors", help="show learned factor weights")
    f.add_argument("--sport", choices=["nfl", "nba"], default="nfl")

    a = ap.parse_args(argv)
    config.REFRESH = getattr(a, "refresh", False)
    sports = ["nfl", "nba"] if getattr(a, "sport", "all") == "all" else [a.sport]

    if a.cmd == "slate":
        from . import engine, report

        config.PARLAY_MAX_LEGS = a.max_legs
        results = []
        for sp in sports:
            days = a.days or (7 if sp == "nfl" else 1)
            res = engine.run_sport(sp, days=days, props_csv=a.props_csv, props=not a.no_props, n_sims=a.sims)
            report.print_sport(res, a.bankroll)
            results.append(res)
        legs = [l for r in results for l in r["legs"]]
        parlays = engine.build_parlays(legs) if legs else {}
        if legs:
            report.console.rule("[bold]Parlays")
            report.print_parlays(parlays)
        track_line = ""
        if not a.no_track:
            from . import tracker

            try:  # settle anything that has finished since the last run
                tracker.grade(verbose=False)
                track_line = tracker.summary_line()
            except Exception as e:
                report.console.print(f"[dim]Could not grade past picks right now: {e!r}[/dim]")
        paths = report.write_outputs(results, parlays, a.bankroll, track_line)
        report.console.print(f"\nReport: {paths['html']}" + (f"\nAll bets (CSV): {paths['csv']}" if paths["csv"] else ""))
        if not a.no_track and legs:
            n = tracker.log_slate(results, parlays)
            report.console.print(f"Logged {n} priced picks to {tracker.history_dir()} (graded automatically later).")
        if track_line:
            report.console.print(track_line)
    elif a.cmd == "ui":
        from . import web

        web.serve(port=a.port, open_browser=not a.no_browser)
    elif a.cmd == "share":
        from . import share

        share.run(port=a.port)
    elif a.cmd == "autostart":
        from . import autostart

        try:
            if a.state == "on":
                where = autostart.enable()
                print("On. The dashboard and its remote address will start when you sign in to Windows.")
                print("It is this one file, which you can delete to undo it:")
                print(f"  {where}")
            elif a.state == "off":
                print("Off: removed." if autostart.disable() else "It was not on.")
            elif autostart.enabled():
                print(f"On ({autostart.path()})")
            else:
                print("Off. Turn it on with:  .\\sportsedge autostart on")
        except RuntimeError as e:
            print(e)
    elif a.cmd == "passcode":
        import getpass

        from . import access

        if a.clear:
            access.clear_passcode()
            print("Passcode removed. Remote access is locked.")
            return
        print("Choose the passcode for opening the dashboard away from this computer.")
        print("It is not shown as you type, and only a scrambled form of it is saved.")
        one = getpass.getpass("New passcode (8 characters or more): ")
        if len(one) < 8:
            raise SystemExit("Too short: use 8 characters or more. Nothing was changed.")
        if one != getpass.getpass("Type it again: "):
            raise SystemExit("Those did not match. Nothing was changed.")
        access.set_passcode(one)
        print("Passcode saved. Anyone signed in before has to sign in again.")
    elif a.cmd == "grade":
        from . import tracker

        tracker.grade(verbose=True)
        tracker.performance(a.days, None if a.sport == "all" else a.sport)
    elif a.cmd == "backtest":
        from . import backtest

        for sp in sports:
            backtest.run(sp, a.min_edge)
    elif a.cmd == "factors":
        from rich.table import Table

        from .report import console

        mod = __import__(f"sportsedge.{a.sport}", fromlist=["build"])
        g, _ = mod.build(None, verbose=False)
        m = mod.fit_model(g)
        t = Table(title=f"{a.sport.upper()} learned factor weights (points per 1 SD; + = more home margin / more points)",
                  header_style="bold")
        for c in ("Model", "Factor", "Weight"):
            t.add_column(c)
        for r in m.coef_table().itertuples():
            t.add_row(r.model, r.feature, f"{r.points_per_sd:+.2f}")
        console.print(t)


if __name__ == "__main__":
    main()
