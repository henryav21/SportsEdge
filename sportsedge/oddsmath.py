"""Odds conversions, vig removal, expected value, Kelly, and score distributions."""
from __future__ import annotations

import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm

from . import config


def american_to_decimal(a) -> float:
    a = float(a)
    return 1 + a / 100 if a > 0 else 1 + 100 / -a


def decimal_to_american(d) -> int:
    d = float(d)
    if d >= 2:
        return int(round((d - 1) * 100))
    return int(round(-100 / (d - 1)))


def implied(a) -> float:
    return 1 / american_to_decimal(a)


def prob_to_american(p) -> int:
    p = min(max(float(p), 1e-6), 1 - 1e-6)
    return decimal_to_american(1 / p)


def fmt_american(a) -> str:
    if a is None or (isinstance(a, float) and np.isnan(a)):
        return "-"
    a = int(round(a))
    return f"+{a}" if a > 0 else str(a)


def devig(implied_probs, method: str | None = None) -> np.ndarray:
    """Remove the bookmaker margin from a set of mutually exclusive outcomes."""
    method = method or config.DEVIG_METHOD
    ps = np.asarray(implied_probs, float)
    if ps.sum() <= 1 or method == "multiplicative":
        return ps / ps.sum()
    k = brentq(lambda k: (ps**k).sum() - 1, 1.0, 20.0)  # power method (handles favourite-longshot bias)
    return ps**k


def expected_value(p_win, dec, p_push=0.0) -> float:
    """Profit per 1 unit staked."""
    return p_win * (dec - 1) - (1 - p_win - p_push)


def kelly(p_win, dec, p_push=0.0, fraction=None, cap=None) -> float:
    fraction = config.KELLY_FRACTION if fraction is None else fraction
    cap = config.KELLY_CAP if cap is None else cap
    b = dec - 1
    p_lose = 1 - p_win - p_push
    if b <= 0:
        return 0.0
    f = (b * p_win - p_lose) / b
    return float(np.clip(f * fraction, 0, cap))


# ----------------------------------------------------------------------------- score distributions
def discrete_normal_pmf(mu: float, sd: float, support: np.ndarray) -> np.ndarray:
    p = norm.cdf(support + 0.5, mu, sd) - norm.cdf(support - 0.5, mu, sd)
    return p / p.sum()


def _tilt(p: np.ndarray, support: np.ndarray, mu: float) -> np.ndarray:
    """Exponentially re-weight p so its mean is exactly mu (the smallest change that does it)."""
    x = support - mu
    lp = np.log(np.maximum(p, 1e-300))

    def mean_gap(th):
        q = np.exp(lp + th * x - (lp + th * x).max())
        return float((q * x).sum() / q.sum())

    try:
        th = brentq(mean_gap, -3.0, 3.0)
    except ValueError:
        return p
    q = np.exp(lp + th * x - (lp + th * x).max())
    return q / q.sum()


class ScoreDist:
    """Integer outcome distribution (margin or total).

    Base: discretised normal reshaped by learned key-number weights. When `emp` is given (historical
    outcomes of games whose closing line was close to `mu`), the empirical outcomes are mixed in with
    pseudo-count `prior`, so tails and key numbers follow what actually happened in similar games.
    """

    def __init__(self, mu: float, sd: float, support: np.ndarray, shape: np.ndarray | None = None,
                 emp: tuple | None = None, bandwidth: float = 1.0, prior: float = 100.0):
        p = discrete_normal_pmf(mu, sd, support)
        if shape is not None:
            p = p * shape
        p = p / p.sum()
        if emp is not None:
            lines, outcomes, w0 = emp
            w = w0 * np.exp(-0.5 * ((lines - mu) / bandwidth) ** 2)
            # outcomes stay on their real integers (so key numbers survive); the mean is matched below
            idx = np.clip(np.round(outcomes).astype(int) - support[0], 0, len(support) - 1)
            counts = np.bincount(idx, weights=w, minlength=len(support))
            p = (counts + prior * p) / (w.sum() + prior)
            p = _tilt(p, support, mu)
        self.support = support
        self.pmf = p / p.sum()
        self.mu = float((self.support * self.pmf).sum())

    @classmethod
    def from_pmf(cls, support, pmf):
        d = cls.__new__(cls)
        d.support, d.pmf = support, pmf / pmf.sum()
        d.mu = float((support * d.pmf).sum())
        return d

    def p_gt(self, x) -> float:
        return float(self.pmf[self.support > x].sum())

    def p_lt(self, x) -> float:
        return float(self.pmf[self.support < x].sum())

    def p_eq(self, x) -> float:
        return float(self.pmf[self.support == x].sum())

    def sample(self, n, rng) -> np.ndarray:
        return rng.choice(self.support, size=n, p=self.pmf).astype(np.float32)


def learn_shape(actual: np.ndarray, mus: np.ndarray, sd: float, support: np.ndarray, smooth: float = 30.0) -> np.ndarray:
    """Ratio of observed frequency of each integer outcome to what a plain discretised normal predicts.

    This is how NFL key numbers (3, 7, 10, 6, 14...) enter the model: learned from data, not hard-coded.
    """
    expected = np.zeros(len(support))
    for m in mus:
        expected += discrete_normal_pmf(m, sd, support)
    observed = np.zeros(len(support))
    idx = np.searchsorted(support, np.clip(actual, support[0], support[-1]))
    np.add.at(observed, idx, 1)
    # empirical-Bayes shrink toward 1 where data is thin
    return (observed + smooth) / (expected + smooth)
