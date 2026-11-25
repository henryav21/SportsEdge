"""Caching, name matching, and geography helpers."""
from __future__ import annotations

import math
import pickle
import re
import time
import unicodedata

import numpy as np

from . import config


# ----------------------------------------------------------------------------- cache
def cached(key: str, ttl_hours: float | None, fn, refresh: bool | None = None):
    """Pickle-cache fn() under .cache/<key>.pkl. ttl_hours=None means keep forever."""
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = config.CACHE_DIR / f"{key}.pkl"
    refresh = config.REFRESH if refresh is None else refresh
    if path.exists() and not refresh:
        age_h = (time.time() - path.stat().st_mtime) / 3600
        if ttl_hours is None or age_h < ttl_hours:
            try:
                with open(path, "rb") as f:
                    return pickle.load(f)
            except Exception:
                pass
    val = fn()
    with open(path, "wb") as f:
        pickle.dump(val, f)
    return val


# ----------------------------------------------------------------------------- names
_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def norm_name(s) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'`’]", "", s).replace("-", " ")
    s = _SUFFIX.sub("", s)
    return " ".join(s.split())


# ----------------------------------------------------------------------------- geography
# (lat, lon, utc offset in standard time, elevation m)
NFL_SITES = {
    "ARI": (33.528, -112.263, -7, 331), "ATL": (33.755, -84.401, -5, 320), "BAL": (39.278, -76.623, -5, 10),
    "BUF": (42.774, -78.787, -5, 190), "CAR": (35.226, -80.853, -5, 230), "CHI": (41.862, -87.617, -6, 180),
    "CIN": (39.096, -84.516, -5, 150), "CLE": (41.506, -81.700, -5, 180), "DAL": (32.747, -97.094, -6, 170),
    "DEN": (39.744, -105.020, -7, 1609), "DET": (42.340, -83.046, -5, 190), "GB": (44.501, -88.062, -6, 200),
    "HOU": (29.685, -95.411, -6, 15), "IND": (39.760, -86.164, -5, 220), "JAX": (30.324, -81.637, -5, 5),
    "KC": (39.049, -94.484, -6, 270), "LA": (33.954, -118.339, -8, 30), "LAC": (33.954, -118.339, -8, 30),
    "LV": (36.091, -115.183, -8, 620), "MIA": (25.958, -80.239, -5, 3), "MIN": (44.974, -93.258, -6, 260),
    "NE": (42.091, -71.264, -5, 90), "NO": (29.951, -90.081, -6, 1), "NYG": (40.814, -74.074, -5, 5),
    "NYJ": (40.814, -74.074, -5, 5), "PHI": (39.901, -75.168, -5, 5), "PIT": (40.447, -80.016, -5, 230),
    "SEA": (47.595, -122.332, -8, 5), "SF": (37.403, -121.970, -8, 5), "TB": (27.976, -82.503, -5, 10),
    "TEN": (36.167, -86.771, -6, 130), "WAS": (38.908, -76.865, -5, 50),
    # historical homes
    "OAK": (37.752, -122.201, -8, 5), "SD": (32.783, -117.120, -8, 20), "STL": (38.633, -90.189, -6, 140),
}
NFL_CANON = {"OAK": "LV", "SD": "LAC", "STL": "LA"}

NBA_SITES = {
    "ATL": (33.757, -84.396, -5, 320), "BOS": (42.366, -71.062, -5, 5), "BKN": (40.683, -73.975, -5, 10),
    "CHA": (35.225, -80.839, -5, 230), "CHI": (41.881, -87.674, -6, 180), "CLE": (41.497, -81.688, -5, 180),
    "DAL": (32.791, -96.810, -6, 140), "DEN": (39.749, -105.008, -7, 1609), "DET": (42.341, -83.055, -5, 190),
    "GSW": (37.768, -122.388, -8, 5), "HOU": (29.751, -95.362, -6, 15), "IND": (39.764, -86.156, -5, 220),
    "LAC": (33.945, -118.341, -8, 30), "LAL": (34.043, -118.267, -8, 90), "MEM": (35.138, -90.051, -6, 80),
    "MIA": (25.781, -80.187, -5, 3), "MIL": (43.045, -87.917, -6, 180), "MIN": (44.980, -93.276, -6, 260),
    "NOP": (29.949, -90.082, -6, 1), "NYK": (40.751, -73.993, -5, 10), "OKC": (35.463, -97.515, -6, 370),
    "ORL": (28.539, -81.384, -5, 30), "PHI": (39.901, -75.172, -5, 5), "PHX": (33.446, -112.071, -7, 330),
    "POR": (45.532, -122.667, -8, 15), "SAC": (38.580, -121.500, -8, 10), "SAS": (29.427, -98.438, -6, 200),
    "TOR": (43.644, -79.379, -5, 80), "UTA": (40.768, -111.901, -7, 1288), "WAS": (38.898, -77.021, -5, 10),
}

ESPN_TO_NFLVERSE = {"WSH": "WAS", "LAR": "LA"}
ESPN_TO_NBA = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS"}


def nfl_site(team: str, season: int | None = None):
    if season is not None:
        if team == "LV" and season < 2020:
            return NFL_SITES["OAK"]
        if team == "LAC" and season < 2017:
            return NFL_SITES["SD"]
    return NFL_SITES.get(team)


def haversine_km(a, b) -> float:
    if a is None or b is None:
        return float("nan")
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def safe_div(a, b):
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(b > 0, a / np.where(b > 0, b, 1), np.nan)
    return out
