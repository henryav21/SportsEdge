"""External data: ESPN (slate, odds, injuries, historical lines), Open-Meteo (weather), The Odds API (props)."""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests

from . import config
from .util import ESPN_TO_NBA, ESPN_TO_NFLVERSE, cached, norm_name

UA = {"User-Agent": "Mozilla/5.0 (sports-model research tool)"}
ESPN_PATH = {"nfl": "football/nfl", "nba": "basketball/nba"}


def _get(url, params=None, timeout=30, tries=3):
    last = None
    for _ in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
        except Exception as e:  # network hiccup
            last = str(e)
    raise RuntimeError(f"GET {url} failed: {last}")


def norm_team(sport: str, abbr: str) -> str:
    m = ESPN_TO_NFLVERSE if sport == "nfl" else ESPN_TO_NBA
    return m.get(abbr, abbr)


def _american(s):
    if s is None:
        return np.nan
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().upper()
    if s in ("EVEN", "EV", "PK"):
        return 100.0
    try:
        return float(s.replace("+", ""))
    except ValueError:
        return np.nan


def _line(s):
    if s is None:
        return np.nan
    s = str(s).strip().lower().lstrip("ou")
    if s in ("pk", "even"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return np.nan


# ----------------------------------------------------------------------------- ESPN slate + odds
def _parse_odds(o: dict) -> dict:
    out = {k: np.nan for k in ["ml_home", "ml_away", "spread_home", "spread_home_odds", "spread_away_odds",
                                "total", "over_odds", "under_odds"]}
    if not o:
        return out
    out["book"] = (o.get("provider") or {}).get("name")
    ml = o.get("moneyline") or {}
    ps = o.get("pointSpread") or {}
    tot = o.get("total") or {}
    pick = lambda d: (d.get("close") or d.get("current") or d.get("open") or {})  # noqa: E731
    if ml:
        out["ml_home"] = _american(pick(ml.get("home", {})).get("odds"))
        out["ml_away"] = _american(pick(ml.get("away", {})).get("odds"))
    if ps:
        h = pick(ps.get("home", {}))
        a = pick(ps.get("away", {}))
        out["spread_home"] = _line(h.get("line"))
        out["spread_home_odds"] = _american(h.get("odds"))
        out["spread_away_odds"] = _american(a.get("odds"))
    if tot:
        ov = pick(tot.get("over", {}))
        un = pick(tot.get("under", {}))
        out["total"] = _line(ov.get("line"))
        out["over_odds"] = _american(ov.get("odds"))
        out["under_odds"] = _american(un.get("odds"))
    # fallbacks (older / core API format)
    if np.isnan(out["spread_home"]) and o.get("spread") is not None:
        out["spread_home"] = float(o["spread"])
    if np.isnan(out["total"]) and o.get("overUnder") is not None:
        out["total"] = float(o["overUnder"])
    ho, ao = o.get("homeTeamOdds") or {}, o.get("awayTeamOdds") or {}
    if np.isnan(out["ml_home"]) and ho.get("moneyLine") is not None:
        out["ml_home"] = float(ho["moneyLine"])
    if np.isnan(out["ml_away"]) and ao.get("moneyLine") is not None:
        out["ml_away"] = float(ao["moneyLine"])
    if np.isnan(out["spread_home_odds"]) and ho.get("spreadOdds") is not None:
        out["spread_home_odds"] = float(ho["spreadOdds"])
    if np.isnan(out["spread_away_odds"]) and ao.get("spreadOdds") is not None:
        out["spread_away_odds"] = float(ao["spreadOdds"])
    for k in ("over", "under"):
        v = o.get(f"{k}Odds")
        if np.isnan(out[f"{k}_odds"]) and v is not None:
            out[f"{k}_odds"] = float(v)
    for k in ("spread_home_odds", "spread_away_odds", "over_odds", "under_odds"):
        if np.isnan(out[k]) and not np.isnan(out["spread_home" if "spread" in k else "total"]):
            out[k] = -110.0
    return out


def espn_scoreboard(sport: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    """One request per day (ESPN rejects date ranges)."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/{ESPN_PATH[sport]}/scoreboard"
    events, seen = [], set()
    d = start
    while d <= end:
        for ev in _get(url, {"dates": f"{d:%Y%m%d}", "limit": 500}).get("events", []):
            if ev["id"] not in seen:
                seen.add(ev["id"])
                events.append(ev)
        d += dt.timedelta(days=1)
    rows = []
    for ev in events:
        c = ev["competitions"][0]
        teams = {x["homeAway"]: x for x in c["competitors"]}
        if "home" not in teams or "away" not in teams:
            continue
        venue = c.get("venue") or {}
        row = {
            "event_id": ev["id"],
            "start": pd.Timestamp(ev["date"]),
            "state": ev["status"]["type"]["state"],
            "home": norm_team(sport, teams["home"]["team"]["abbreviation"]),
            "away": norm_team(sport, teams["away"]["team"]["abbreviation"]),
            "home_name": teams["home"]["team"].get("displayName"),
            "away_name": teams["away"]["team"].get("displayName"),
            "home_tid": teams["home"]["team"].get("id"),
            "away_tid": teams["away"]["team"].get("id"),
            "neutral": bool(c.get("neutralSite", False)),
            "indoor": venue.get("indoor"),
            "venue": venue.get("fullName"),
            "city": (venue.get("address") or {}).get("city"),
            "week": (ev.get("week") or {}).get("number"),
        }
        row.update(_parse_odds((c.get("odds") or [{}])[0]))
        rows.append(row)
    return pd.DataFrame(rows)


def espn_injuries(sport: str) -> pd.DataFrame:
    def fetch():
        j = _get(f"https://site.api.espn.com/apis/site/v2/sports/{ESPN_PATH[sport]}/injuries")
        teams = _get(f"https://site.api.espn.com/apis/site/v2/sports/{ESPN_PATH[sport]}/teams")
        name2abbr = {t["team"]["displayName"]: t["team"]["abbreviation"]
                     for t in teams["sports"][0]["leagues"][0]["teams"]}
        rows = []
        for t in j.get("injuries", []):
            abbr = norm_team(sport, name2abbr.get(t.get("displayName"), ""))
            for i in t.get("injuries", []):
                a = i.get("athlete", {})
                rows.append({
                    "team": abbr, "name": a.get("displayName"), "key": norm_name(a.get("displayName", "")),
                    "position": (a.get("position") or {}).get("abbreviation"), "status": i.get("status"),
                    "detail": i.get("shortComment"), "date": i.get("date"),
                })
        return pd.DataFrame(rows, columns=["team", "name", "key", "position", "status", "detail", "date"])

    return cached(f"espn_injuries_{sport}", 1.0, fetch)


OUT_STATUSES = {"Out", "Doubtful", "Injured Reserve", "Suspension", "Physically Unable to Perform",
                "Non-Football Injury", "Injured Reserve - Designated for Return"}


def is_out(status: str) -> bool:
    return str(status) in OUT_STATUSES


# ----------------------------------------------------------------------------- historical NBA closing lines
def nba_closing_lines(dates: list[dt.date]) -> pd.DataFrame:
    """Closing lines for completed NBA games (ESPN core API). Cached per day forever."""

    def day(d: dt.date):
        def fetch():
            sb = espn_scoreboard("nba", d, d)
            rows = []
            for _, g in sb.iterrows():
                try:
                    j = _get(f"https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/"
                             f"{g.event_id}/competitions/{g.event_id}/odds")
                except RuntimeError:
                    continue
                items = j.get("items", [])
                items.sort(key=lambda o: ("Live" in (o.get("provider") or {}).get("name", ""),
                                          (o.get("provider") or {}).get("name") not in ("DraftKings", "ESPN BET")))
                if not items:
                    continue
                o = _parse_odds(items[0])
                rows.append({"date": d, "home": g.home, "away": g.away, **o})
            return rows

        try:
            return cached(f"nba_lines/{d:%Y%m%d}", None if d < dt.date.today() - dt.timedelta(days=2) else 6, fetch)
        except RuntimeError:
            return []

    (config.CACHE_DIR / "nba_lines").mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=8) as ex:
        out = [r for rows in ex.map(day, dates) for r in rows]
    return pd.DataFrame(out)


# ----------------------------------------------------------------------------- weather (Open-Meteo, free, no key)
def weather_history(site, start: dt.date, end: dt.date) -> pd.DataFrame:
    lat, lon = site[0], site[1]

    def fetch():
        j = _get("https://archive-api.open-meteo.com/v1/archive", {
            "latitude": lat, "longitude": lon, "start_date": f"{start}", "end_date": f"{end}",
            "daily": "precipitation_sum,snowfall_sum", "timezone": "auto"}, timeout=90)
        d = j["daily"]
        return pd.DataFrame({"date": pd.to_datetime(d["time"]).date, "precip_mm": d["precipitation_sum"],
                             "snow_cm": d["snowfall_sum"]})

    return cached(f"wx_hist_{lat:.2f}_{lon:.2f}_{start}", 72, fetch)


def weather_forecast(site, kickoff_utc: pd.Timestamp) -> dict:
    """Temperature (F) and wind (mph) at kickoff, plus precipitation over the local game day."""
    lat, lon = site[0], site[1]

    def fetch():
        return _get("https://api.open-meteo.com/v1/forecast", {
            "latitude": lat, "longitude": lon, "hourly": "temperature_2m,wind_speed_10m,precipitation",
            "daily": "precipitation_sum,snowfall_sum", "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph", "timezone": "auto", "forecast_days": 16})

    try:
        j = cached(f"wx_fc_{lat:.2f}_{lon:.2f}", 3, fetch)
    except RuntimeError:
        return {}
    off = pd.Timedelta(seconds=j.get("utc_offset_seconds", 0))
    local = (kickoff_utc.tz_convert(None) if kickoff_utc.tzinfo else kickoff_utc) + off
    hourly = pd.DataFrame(j["hourly"])
    hourly["time"] = pd.to_datetime(hourly["time"])
    h = hourly[hourly["time"] <= local.floor("h")].tail(1)
    daily = pd.DataFrame(j["daily"])
    d = daily[daily["time"] == f"{local.date()}"]
    if h.empty or d.empty:
        return {}
    return {"temp": float(h["temperature_2m"].iloc[0]), "wind": float(h["wind_speed_10m"].iloc[0]),
            "precip_mm": float(d["precipitation_sum"].iloc[0] or 0), "snow_cm": float(d["snowfall_sum"].iloc[0] or 0)}


# ----------------------------------------------------------------------------- player-prop lines
ODDS_API_SPORT = {"nfl": "americanfootball_nfl", "nba": "basketball_nba"}
ODDS_API_MARKETS = {
    "nfl": {"player_pass_yds": "pass_yds", "player_pass_tds": "pass_tds", "player_pass_completions": "pass_cmp",
            "player_pass_attempts": "pass_att", "player_rush_yds": "rush_yds", "player_rush_attempts": "rush_att",
            "player_reception_yds": "rec_yds", "player_receptions": "receptions",
            "player_rush_reception_yds": "rush_rec_yds", "player_anytime_td": "anytime_td"},
    "nba": {"player_points": "pts", "player_rebounds": "reb", "player_assists": "ast", "player_threes": "fg3m",
            "player_points_rebounds_assists": "pra", "player_points_rebounds": "pr", "player_points_assists": "pa",
            "player_rebounds_assists": "ra", "player_steals": "stl", "player_blocks": "blk"},
}


def oddsapi_props(sport: str, games: pd.DataFrame, book: str = "draftkings") -> pd.DataFrame:
    """Player-prop lines from The Odds API (needs ODDS_API_KEY). One request per game."""
    key = config.ODDS_API_KEY
    cols = ["home", "away", "player", "key", "market", "line", "over_odds", "under_odds", "book"]
    if not key or games.empty:
        return pd.DataFrame(columns=cols)
    base = f"https://api.the-odds-api.com/v4/sports/{ODDS_API_SPORT[sport]}"
    events = cached(f"oddsapi_events_{sport}", 1, lambda: _get(f"{base}/events", {"apiKey": key}))
    last = lambda s: str(s).split()[-1].lower()  # noqa: E731  team nickname
    rows = []
    for _, g in games.iterrows():
        ev = next((e for e in events if last(e["home_team"]) == last(g.home_name)
                   and last(e["away_team"]) == last(g.away_name)), None)
        if ev is None:
            continue
        mk = ",".join(ODDS_API_MARKETS[sport])

        def fetch(eid=ev["id"]):
            return _get(f"{base}/events/{eid}/odds", {"apiKey": key, "regions": "us", "markets": mk,
                                                      "oddsFormat": "american"})

        try:
            j = cached(f"oddsapi_{sport}_{ev['id']}", 1, fetch)
        except RuntimeError:
            continue
        books = j.get("bookmakers", [])
        books.sort(key=lambda b: b["key"] != book)
        seen = set()
        for b in books:
            for m in b.get("markets", []):
                mkey = ODDS_API_MARKETS[sport].get(m["key"])
                if not mkey:
                    continue
                by_player = {}
                for o in m.get("outcomes", []):
                    p = o.get("description") or o.get("name")
                    side = o.get("name", "").lower()
                    by_player.setdefault(p, {})[side] = (o.get("point", 0.5), o.get("price"))
                for p, sides in by_player.items():
                    if (p, mkey) in seen:
                        continue
                    seen.add((p, mkey))
                    over = sides.get("over") or sides.get("yes")
                    under = sides.get("under") or sides.get("no")
                    if not over:
                        continue
                    rows.append({"home": g.home, "away": g.away, "player": p, "key": norm_name(p), "market": mkey,
                                 "line": float(over[0] if over[0] is not None else 0.5), "over_odds": over[1],
                                 "under_odds": under[1] if under else np.nan, "book": b["key"]})
    return pd.DataFrame(rows, columns=cols)


# ----------------------------------------------------------------------------- free prop lines (ESPN)
ESPN_CORE = {"nfl": "football/leagues/nfl", "nba": "basketball/leagues/nba"}
# ESPN's name for each over/under market -> ours. Only full-game markets the simulators cover.
ESPN_PROP_TYPES = {
    "nfl": {"Total Passing Yards (incl. overtime)": "pass_yds", "Total Passing Touchdowns (incl. overtime)": "pass_tds",
            "Total Pass Completions (incl. overtime)": "pass_cmp", "Total Passing Attempts (incl. overtime)": "pass_att",
            "Total Rushing Yards (incl. overtime)": "rush_yds", "Total Carries (incl. overtime)": "rush_att",
            "Total Receiving Yards (incl. overtime)": "rec_yds", "Total Receptions (incl. overtime)": "receptions",
            "Total Rushing Plus Receiving Yards (incl. overtime)": "rush_rec_yds"},
    "nba": {"Total Points": "pts", "Total Rebounds": "reb", "Total Assists": "ast",
            "Total 3-Point Field Goals": "fg3m", "Total Steals": "stl", "Total Blocks": "blk",
            "Total Points, Rebounds, and Assists": "pra", "Total Points and Rebounds": "pr",
            "Total Points and Assists": "pa", "Total Assists and Rebounds": "ra"},
}
ESPN_YES_ONLY = {"nfl": {"Anytime Touchdown Scorer": "anytime_td"}, "nba": {}}  # one price, no "No" side
PROP_COLS = ["home", "away", "player", "key", "market", "line", "over_odds", "under_odds", "book"]


def _athlete_id(item: dict) -> str | None:
    ref = (item.get("athlete") or {}).get("$ref", "")
    return ref.split("/athletes/")[1].split("?")[0] if "/athletes/" in ref else None


def _espn_roster(sport: str, team_id) -> dict:
    """ESPN athlete id -> display name for one team."""

    def fetch():
        j = _get(f"https://site.api.espn.com/apis/site/v2/sports/{ESPN_PATH[sport]}/teams/{team_id}/roster")
        out = {}
        for a in j.get("athletes", []):
            for p in a.get("items", [a]):  # NFL groups players by unit; NBA lists them directly
                if p.get("id") and p.get("displayName"):
                    out[str(p["id"])] = p["displayName"]
        return out

    try:
        return cached(f"espn_props/roster_{sport}_{team_id}", 12, fetch)
    except RuntimeError:
        return {}


def _espn_athlete(sport: str, athlete_id: str) -> str | None:
    """Name of a player who is not on either roster list (recent signing or trade)."""

    def fetch():
        return _get(f"https://sports.core.api.espn.com/v2/sports/{ESPN_CORE[sport]}/athletes/{athlete_id}").get("displayName")

    try:
        return cached(f"espn_props/athlete_{sport}_{athlete_id}", 24 * 30, fetch)
    except RuntimeError:
        return None


def parse_espn_props(sport: str, items: list[dict], names: dict) -> list[dict]:
    """ESPN lists an over/under market as two entries per player with no side label: the over first, then the
    under. (Checked against the one-sided 'N or more' prices for the same players: the first entry matched the
    over in every pair where the two prices differed enough to tell.)"""
    ou, yes = ESPN_PROP_TYPES[sport], ESPN_YES_ONLY[sport]
    groups: dict[tuple, list] = {}
    for it in items:
        t = (it.get("type") or {}).get("name")
        aid = _athlete_id(it)
        if aid is None or (t not in ou and t not in yes):
            continue
        # the NFL feed carries the line but no price; those lines are kept and shown as "line only"
        price = _american(((it.get("odds") or {}).get("american") or {}).get("value"))
        line = ((it.get("current") or {}).get("target") or {}).get("value")
        groups.setdefault((t, aid), []).append((None if line is None else float(line), price))
    rows = []
    for (t, aid), v in groups.items():
        name = names.get(aid)
        if not name:
            continue
        if t in yes:
            if not np.isnan(v[0][1]):
                rows.append({"player": name, "key": norm_name(name), "market": yes[t], "line": 0.5,
                             "over_odds": v[0][1], "under_odds": np.nan})
            continue
        # anything other than one clean over/under pair on one line is ambiguous, so it is skipped
        if len(v) != 2 or v[0][0] is None or v[0][0] != v[1][0] or np.isnan(v[0][1]) != np.isnan(v[1][1]):
            continue
        rows.append({"player": name, "key": norm_name(name), "market": ou[t], "line": v[0][0],
                     "over_odds": v[0][1], "under_odds": v[1][1]})
    return rows


def espn_props(sport: str, games: pd.DataFrame, ttl: float | None = 0.25) -> pd.DataFrame:
    """Player-prop lines published through ESPN's odds feed (DraftKings). Free, no key. Cached 15 minutes
    (ttl=None keeps a finished game's closing lines for good). NBA lines come with prices; NFL lines come
    without, so over_odds / under_odds are NaN there."""
    if games is None or games.empty:
        return pd.DataFrame(columns=PROP_COLS)
    (config.CACHE_DIR / "espn_props").mkdir(parents=True, exist_ok=True)
    core = f"https://sports.core.api.espn.com/v2/sports/{ESPN_CORE[sport]}"

    def one(g) -> list[dict]:
        def fetch():
            odds = _get(f"{core}/events/{g.event_id}/competitions/{g.event_id}/odds").get("items", [])
            odds = [o for o in odds if (o.get("propBets") or {}).get("$ref")]
            odds.sort(key=lambda o: (o.get("provider") or {}).get("name") != "DraftKings")
            if not odds:
                return {"book": None, "items": []}
            prov = odds[0].get("provider") or {}
            url = f"{core}/events/{g.event_id}/competitions/{g.event_id}/odds/{prov.get('id')}/propBets"
            items, page = [], 1
            while True:
                j = _get(url, {"limit": 1000, "page": page})
                items += j.get("items", [])
                if page >= int(j.get("pageCount") or 1) or page >= 10:
                    break
                page += 1
            wanted = set(ESPN_PROP_TYPES[sport]) | set(ESPN_YES_ONLY[sport])
            return {"book": prov.get("name"), "items": [i for i in items if (i.get("type") or {}).get("name") in wanted]}

        try:
            got = cached(f"espn_props/{sport}_{g.event_id}" + ("_final" if ttl is None else ""), ttl, fetch)
        except RuntimeError:
            return []
        if not got["items"]:
            return []
        names = {}
        for tid in (getattr(g, "home_tid", None), getattr(g, "away_tid", None)):
            if tid is not None and not pd.isna(tid):
                names.update(_espn_roster(sport, tid))
        missing = {a for a in map(_athlete_id, got["items"]) if a and a not in names}
        for aid in sorted(missing)[:25]:
            nm = _espn_athlete(sport, aid)
            if nm:
                names[aid] = nm
        return [{"home": g.home, "away": g.away, "book": got["book"] or "ESPN", **r}
                for r in parse_espn_props(sport, got["items"], names)]

    with ThreadPoolExecutor(max_workers=6) as ex:
        rows = [r for part in ex.map(one, list(games.itertuples())) for r in part]
    return pd.DataFrame(rows, columns=PROP_COLS)


def espn_props_history(sport: str, days: list[dt.date]) -> pd.DataFrame:
    """Closing prop lines for finished games on the given days (kept in the cache for good)."""
    (config.CACHE_DIR / "espn_props").mkdir(parents=True, exist_ok=True)
    out = []
    for d in days:
        try:
            sb = cached(f"espn_props/sb_{sport}_{d:%Y%m%d}", None, lambda d=d: espn_scoreboard(sport, d, d))
        except RuntimeError:
            continue
        sb = sb[sb["state"] == "post"] if not sb.empty else sb
        if sb.empty:
            continue
        df = espn_props(sport, sb, ttl=None)
        if not df.empty:
            out.append(df.assign(date=d))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=PROP_COLS + ["date"])


def csv_props(path, sport: str) -> pd.DataFrame:
    """User-supplied lines. Columns: sport, player, market, line, over_odds, under_odds."""
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    df = df[df["sport"].str.lower() == sport].copy()
    df["key"] = df["player"].map(norm_name)
    df["book"] = "csv"
    for c in ("over_odds", "under_odds"):
        if c not in df:
            df[c] = np.nan
    return df
