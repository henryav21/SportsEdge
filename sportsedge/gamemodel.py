"""Sport-agnostic game model: predicts home margin and total, then turns them into full score distributions.

Pipeline
  1. Ridge regressions (standardised features, CV-chosen penalty) predict margin and total.
  2. Walk-forward by season gives honest out-of-sample predictions.
  3. Those OOS predictions learn (a) how much to trust the model vs the closing market line,
     (b) the spread of outcomes, (c) the shape of the score distribution (key numbers).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.linear_model import RidgeCV
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from . import config
from .oddsmath import ScoreDist, learn_shape

ALPHAS = np.logspace(-1, 3.5, 25)


def _pipe(intercept: bool) -> Pipeline:
    return Pipeline([("sc", StandardScaler(with_mean=intercept)),
                     ("r", RidgeCV(alphas=ALPHAS, fit_intercept=intercept))])


@dataclass
class GameModel:
    sport: str
    margin_feats: list
    total_feats: list
    labels: dict
    m_model: Pipeline = None
    t_model: Pipeline = None
    sd_margin: float = 13.0
    sd_total: float = 13.0
    w_margin: float = 0.35
    w_total: float = 0.35
    blend_n: int = 0
    margin_support: np.ndarray = None
    total_support: np.ndarray = None
    margin_shape: np.ndarray = None
    total_shape: np.ndarray = None
    emp_margin: tuple = None   # (closing lines, outcomes, weights, seasons) of past games, mirrored
    emp_total: tuple = None
    oos: pd.DataFrame = field(default=None, repr=False)

    # ------------------------------------------------------------------ fitting
    def _fit(self, df):
        m = _pipe(False).fit(df[self.margin_feats].to_numpy(float), df["margin"].to_numpy(float))
        t = _pipe(True).fit(df[self.total_feats].to_numpy(float), df["total"].to_numpy(float))
        return m, t

    def walk_forward(self, df: pd.DataFrame, min_train_seasons: int = 2) -> pd.DataFrame:
        done = df[df["completed"]].copy()
        seasons = sorted(done["season"].unique())
        out = []
        for s in seasons[min_train_seasons:]:
            tr, te = done[done["season"] < s], done[done["season"] == s]
            if len(tr) < 200 or te.empty:
                continue
            m, t = self._fit(tr)
            te = te.copy()
            te["pm"] = m.predict(te[self.margin_feats].to_numpy(float))
            te["pt"] = t.predict(te[self.total_feats].to_numpy(float))
            out.append(te)
        return pd.concat(out, ignore_index=True) if out else pd.DataFrame()

    def fit(self, df: pd.DataFrame, shape_history: pd.DataFrame | None = None):
        oos = self.walk_forward(df)
        self.oos = oos
        # ---- market blend weight: argmin sum (y - (w p + (1-w) m))^2, kept only if it survives
        #      leave-one-season-out cross-validation (otherwise the market alone is better: w = 0)
        self.cv = {}
        for key, y, p, m, attr in (("margin", "margin", "pm", "mkt_margin", "w_margin"),
                                   ("total", "total", "pt", "mkt_total", "w_total")):
            has = oos.dropna(subset=[m]) if not oos.empty else oos
            if len(has) < 150:
                setattr(self, attr, config.DEFAULT_BLEND[self.sport])
                continue
            w_full, gain = _cv_blend(has, y, p, m)
            setattr(self, attr, w_full if gain > config.MIN_CV_GAIN else 0.0)
            self.cv[key] = {"w_fit": w_full, "cv_mse_gain_pct": gain, "n": len(has)}
            if key == "margin":
                self.blend_n = len(has)
        # ---- outcome spread (blended where the market exists, model-only otherwise)
        if not oos.empty:
            bm = self.blend(oos["pm"], oos["mkt_margin"], self.w_margin)
            bt = self.blend(oos["pt"], oos["mkt_total"], self.w_total)
            self.sd_margin = float(np.std(oos["margin"] - bm))
            self.sd_total = float(np.std(oos["total"] - bt))
        # ---- final fit on everything
        self.m_model, self.t_model = self._fit(df[df["completed"]])
        # ---- score-distribution shape (key numbers) from long market history
        hist = shape_history if shape_history is not None else oos.rename(
            columns={"mkt_margin": "mkt_margin", "mkt_total": "mkt_total"})
        hist = hist.dropna(subset=["mkt_margin", "mkt_total"])
        lo_m = -70 if self.sport == "nba" else -60
        self.margin_support = np.arange(lo_m, -lo_m + 1)
        self.total_support = np.arange(150, 321) if self.sport == "nba" else np.arange(0, 121)
        if len(hist) >= 300:
            sdm = float(np.std(hist["margin"] - hist["mkt_margin"]))
            sdt = float(np.std(hist["total"] - hist["mkt_total"]))
            self.margin_shape = learn_shape(hist["margin"].to_numpy(), hist["mkt_margin"].to_numpy(), sdm,
                                            self.margin_support)
            self.total_shape = learn_shape(hist["total"].to_numpy(), hist["mkt_total"].to_numpy(), sdt,
                                           self.total_support)
        else:
            self.margin_shape = np.ones(len(self.margin_support))
            self.total_shape = np.ones(len(self.total_support))
        if self.sport == "nba":  # basketball games cannot end tied
            self.margin_shape[self.margin_support == 0] = 0.0
        # ---- empirical neighbours: what actually happened in games with a similar closing line
        if len(hist) >= 300:
            seasons = hist["season"].to_numpy(float) if "season" in hist else np.zeros(len(hist))
            w = 0.5 ** ((seasons.max() - seasons) / 8.0)  # recent seasons count more
            mm, ym = hist["mkt_margin"].to_numpy(float), hist["margin"].to_numpy(float)
            self.emp_margin = (np.r_[mm, -mm], np.r_[ym, -ym], np.r_[w, w], np.r_[seasons, seasons])
            self.emp_total = (hist["mkt_total"].to_numpy(float), hist["total"].to_numpy(float), w, seasons)
        return self

    # ------------------------------------------------------------------ prediction
    @staticmethod
    def blend(p, m, w):
        p = np.asarray(p, float)
        m = np.asarray(m, float)
        return np.where(np.isnan(m), p, w * p + (1 - w) * m)

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["pm"] = self.m_model.predict(df[self.margin_feats].to_numpy(float))
        out["pt"] = self.t_model.predict(df[self.total_feats].to_numpy(float))
        out["mu_margin"] = self.blend(out["pm"], out.get("mkt_margin", np.nan), self.w_margin)
        out["mu_total"] = self.blend(out["pt"], out.get("mkt_total", np.nan), self.w_total)
        return out

    def _emp(self, emp, exclude_season):
        if emp is None:
            return None
        lines, out, w, seasons = emp
        if exclude_season is not None:
            w = np.where(seasons == exclude_season, 0.0, w)
        return lines, out, w

    def dist_margin(self, mu, sd=None, exclude_season=None) -> ScoreDist:
        bw = 1.0 if self.sport == "nfl" else 1.5
        d = ScoreDist(mu, sd or self.sd_margin, self.margin_support, self.margin_shape,
                      emp=self._emp(self.emp_margin, exclude_season), bandwidth=bw)
        if self.sport == "nba":
            p = d.pmf.copy()
            p[self.margin_support == 0] = 0.0
            d = ScoreDist.from_pmf(self.margin_support, p)
        return d

    def dist_total(self, mu, sd=None, exclude_season=None) -> ScoreDist:
        bw = 1.5 if self.sport == "nfl" else 2.0
        return ScoreDist(mu, sd or self.sd_total, self.total_support, self.total_shape,
                         emp=self._emp(self.emp_total, exclude_season), bandwidth=bw)

    def dists(self, mu_margin, mu_total):
        return self.dist_margin(mu_margin), self.dist_total(mu_total)

    def explain(self, row: pd.Series, top: int = 4) -> list[tuple[str, float]]:
        """Largest drivers of the model's margin (in points, + favours home)."""
        sc, r = self.m_model.named_steps["sc"], self.m_model.named_steps["r"]
        x = row[self.margin_feats].to_numpy(float) / sc.scale_
        contrib = x * r.coef_
        order = np.argsort(-np.abs(contrib))[:top]
        return [(self.labels.get(self.margin_feats[i], self.margin_feats[i]), float(contrib[i])) for i in order]

    def coef_table(self) -> pd.DataFrame:
        rows = []
        for name, mdl, feats in (("margin", self.m_model, self.margin_feats), ("total", self.t_model, self.total_feats)):
            sc, r = mdl.named_steps["sc"], mdl.named_steps["r"]
            for f, c in zip(feats, r.coef_):
                rows.append({"model": name, "feature": self.labels.get(f, f), "points_per_sd": c})
        return pd.DataFrame(rows)


def _cv_blend(df: pd.DataFrame, y: str, p: str, m: str) -> tuple[float, float]:
    """Full-sample blend weight and its out-of-sample gain: % reduction in squared error vs the market
    when each season's weight is fitted on the other seasons only."""
    w_full = _blend(df[y], df[p], df[m])
    err_b, err_m = 0.0, 0.0
    for s, te in df.groupby("season"):
        tr = df[df["season"] != s]
        if len(tr) < 100:
            continue
        w = _blend(tr[y], tr[p], tr[m])
        b = w * te[p] + (1 - w) * te[m]
        err_b += float(((te[y] - b) ** 2).sum())
        err_m += float(((te[y] - te[m]) ** 2).sum())
    gain = 100 * (1 - err_b / err_m) if err_m > 0 else 0.0
    return w_full, gain


def _blend(y, p, m) -> float:
    y, p, m = (np.asarray(v, float) for v in (y, p, m))
    d = p - m
    if (d**2).sum() == 0:
        return 0.0
    return float(np.clip(((y - m) * d).sum() / (d**2).sum(), 0.0, 1.0))
