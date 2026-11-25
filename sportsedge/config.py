"""Central settings. Everything tunable lives here so nothing is hidden in the code."""
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".cache"
OUTPUT_DIR = ROOT / "output"

try:  # optional .env support
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # pragma: no cover
    pass

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()

# Set by the CLI (--refresh) to ignore cached downloads.
REFRESH = False

SEED = 7
N_SIMS = 20_000

# ---------------------------------------------------------------- NFL
NFL_FIRST_SEASON = 2016          # first season used for features / training
NFL_KEYNUM_FIRST_SEASON = 2006   # margin-distribution shape is learned from these seasons on
NFL_SEASON_SLOTS = 30            # time index: season * 30 + week (offseason = 12 empty "weeks")
NFL_HALFLIFE = 8.0               # weeks; team-rating decay
NFL_RIDGE = 4.0                  # rating shrinkage, in "full-weight games"
NFL_QB_HALFLIFE = 20.0
NFL_QB_PRIOR_PLAYS = 200.0
NFL_QB_PRIOR_EPA = -0.05         # what an unproven QB is assumed to be (EPA/play)

# ---------------------------------------------------------------- NBA
NBA_HISTORY_SEASONS = 5          # past seasons used for ratings/training (plus current)
NBA_ODDS_SEASONS = 3             # past seasons of closing lines pulled for the market blend/backtest
NBA_SEASON_DAYS = 215            # time index: day-of-season + season_idx * 215 (offseason ~40 "days")
NBA_HALFLIFE = 30.0              # days; team-rating decay
NBA_RIDGE = 5.0

# ---------------------------------------------------------------- Betting
DEVIG_METHOD = "power"           # power | multiplicative
KELLY_FRACTION = 0.25            # quarter Kelly
KELLY_CAP = 0.03                 # never suggest more than 3% of bankroll on one bet
MIN_EDGE_FLAG = 0.02             # 2% probability edge before something is called a "value" bet
# Backtests show long-shot moneylines are where win probabilities are least precise (~2-4 pts too generous),
# so they must clear a bigger bar.
LONGSHOT_ODDS = 250
LONGSHOT_MIN_EDGE = 0.05
# Props have no free price history to learn a blend from, so the simulator's probability is averaged
# with the no-vig market probability using this weight (1.0 = trust the simulator fully).
PROP_MODEL_WEIGHT = 0.5
# Download prop lines from ESPN's free odds feed (DraftKings). Lines you enter yourself, a CSV, or
# The Odds API take priority over it for the same player and market.
ESPN_PROPS = True
# The dashboard looks at the odds feed this often and logs any change (history/odds_log.csv).
ODDS_CHECK_MINUTES = 3
# When a check finds that a line has moved and the last full update is at least this old, update everything.
AUTO_UPDATE_MINUTES = 12
# How many recent game days of real closing prop lines the prop simulator is tested on (see propcheck.py).
PROP_CHECK_DAYS = 30
# Results tracker: once this many props are graded (history/picks.csv), the prop weight is learned from them,
# shrunk toward PROP_MODEL_WEIGHT as if TRACK_PRIOR_PROPS more props had agreed with the default.
TRACK_MIN_PROPS = 150
TRACK_PRIOR_PROPS = 300
# A model/market blend weight is used only if it lowers squared error vs the closing line in
# leave-one-season-out cross-validation by more than this many percent; otherwise the market wins (w = 0).
MIN_CV_GAIN = 0.0
# Used only until enough graded history exists to learn the market blend weight.
DEFAULT_BLEND = {"nfl": 0.35, "nba": 0.35}

# Parlays
# A prop line that arrives without a price (NFL) is given this standard price so it can go in a bet slip or a
# suggested parlay. It is an assumption and is shown as one: the real price is whatever your sportsbook shows.
ASSUMED_PROP_ODDS = -115
PARLAY_MAX_LEGS = 4
PARLAY_POOL = 28                 # best single legs considered for parlays
PARLAY_SHOW = 8
