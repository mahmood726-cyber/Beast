"""Random-effects meta-analysis engine.

Pools a set of study-level effect sizes ``yi`` with variances ``vi`` (already on
the analysis scale -- e.g. log-OR for odds ratios) and returns the pooled
estimate, confidence interval, prediction interval and heterogeneity statistics.

Design choices follow standard methodological guidance:

* Heterogeneity variance ``tau^2`` can be estimated by DerSimonian-Laird (``DL``),
  Paule-Mandel (``PM``) or restricted maximum likelihood (``REML``). REML is the
  default; DL is biased downward for small ``k`` and should not be relied on for
  ``k < 10`` (we still expose it for comparison / validation).
* The prediction interval uses the t distribution with ``k - 1`` degrees of
  freedom (Cochrane Handbook v6.5). It is undefined for ``k < 2``.
* ``I^2`` is ``max(0, (Q - df) / Q)``; it measures the *proportion* of variance
  due to heterogeneity, not its magnitude, so ``tau^2`` is always reported too.

All pooling for ratio measures must be done on the log scale and back-transformed
by the caller (see :mod:`beast.effects`); this module is scale-agnostic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Sequence

import numpy as np
from scipy import stats

# Methods that estimate the between-study variance tau^2.
TAU2_METHODS = ("REML", "PM", "DL")
_MAX_ITER = 1000
_TOL = 1e-10


@dataclass
class MetaResult:
    """Outcome of a single random-effects meta-analysis.

    Estimates (``estimate``, ``ci_low``, ``ci_high``, ``pi_low``, ``pi_high``)
    are on the *analysis scale* given to :func:`meta_analyze` -- i.e. the log
    scale for ratio measures. Use :meth:`on_natural_scale` to back-transform.
    """

    k: int
    estimate: float
    se: float
    ci_low: float
    ci_high: float
    pi_low: float | None
    pi_high: float | None
    z: float
    p_value: float
    tau2: float
    tau: float
    i2: float
    h2: float
    q: float
    q_df: int
    q_p: float
    method: str
    measure: str = "GEN"  # GEN, OR, RR, MD, SMD ... (informational)
    log_scale: bool = False  # True when estimate/CI are on the log scale
    alpha: float = 0.05

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def significant(self) -> bool:
        """Whether the CI excludes the null on the analysis scale.

        Null is 0 on the log / difference scale (ratio of 1 once
        back-transformed). A result is significant when the CI does not span 0.
        """
        return not (self.ci_low <= 0.0 <= self.ci_high)

    def on_natural_scale(self) -> dict:
        """Back-transform the estimate and intervals to the reporting scale.

        For ratio measures (``log_scale=True``) this exponentiates; otherwise it
        returns the values unchanged.
        """
        f = math.exp if self.log_scale else (lambda x: x)
        out = {
            "estimate": f(self.estimate),
            "ci_low": f(self.ci_low),
            "ci_high": f(self.ci_high),
            "pi_low": f(self.pi_low) if self.pi_low is not None else None,
            "pi_high": f(self.pi_high) if self.pi_high is not None else None,
        }
        return out


def _fixed_effect_mu(yi: np.ndarray, vi: np.ndarray) -> float:
    wi = 1.0 / vi
    return float(np.sum(wi * yi) / np.sum(wi))


def _cochran_q(yi: np.ndarray, vi: np.ndarray) -> tuple[float, int]:
    """Cochran's Q and its degrees of freedom using fixed-effect weights."""
    wi = 1.0 / vi
    mu = np.sum(wi * yi) / np.sum(wi)
    q = float(np.sum(wi * (yi - mu) ** 2))
    df = len(yi) - 1
    return q, df


def _tau2_dl(yi: np.ndarray, vi: np.ndarray) -> float:
    """DerSimonian-Laird closed-form estimator (truncated at 0)."""
    k = len(yi)
    if k < 2:
        return 0.0
    wi = 1.0 / vi
    q, df = _cochran_q(yi, vi)
    c = float(np.sum(wi) - np.sum(wi ** 2) / np.sum(wi))
    if c <= 0:
        return 0.0
    return max(0.0, (q - df) / c)


def _tau2_pm(yi: np.ndarray, vi: np.ndarray) -> float:
    """Paule-Mandel estimator: solve the generalised-Q equation for tau^2.

    Robust for small ``k``; bracketed with a monotone bisection because the
    weighted residual Q is strictly decreasing in tau^2.
    """
    k = len(yi)
    if k < 2:
        return 0.0
    df = k - 1

    def gen_q(t2: float) -> float:
        wi = 1.0 / (vi + t2)
        mu = np.sum(wi * yi) / np.sum(wi)
        return float(np.sum(wi * (yi - mu) ** 2))

    # At tau^2 = 0 the statistic is Cochran's Q. If already <= df, tau^2 = 0.
    if gen_q(0.0) <= df:
        return 0.0
    lo, hi = 0.0, 1.0
    # Expand the upper bracket until the generalised Q drops below df.
    while gen_q(hi) > df and hi < 1e12:
        hi *= 2.0
    # Bisect on the bracket width (not just |Q-df|): the generalised Q is flat
    # near the root, so converging in Q-space leaves tau^2 imprecise.
    for _ in range(_MAX_ITER):
        mid = 0.5 * (lo + hi)
        if hi - lo < 1e-10:
            return mid
        if gen_q(mid) > df:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _tau2_reml(yi: np.ndarray, vi: np.ndarray) -> float:
    """Restricted maximum likelihood via the standard fixed-point iteration.

    tau2_{n+1} = [ sum w^2 ((y-mu)^2 - v) ] / sum w^2 + 1 / sum w,
    with ``w = 1/(v + tau2)`` and ``mu`` the weighted mean. Truncated at 0 and
    seeded from the DL estimate for fast, stable convergence.
    """
    k = len(yi)
    if k < 2:
        return 0.0
    t2 = max(0.0, _tau2_dl(yi, vi))
    for _ in range(_MAX_ITER):
        wi = 1.0 / (vi + t2)
        sw = np.sum(wi)
        mu = np.sum(wi * yi) / sw
        wi2 = wi ** 2
        num = float(np.sum(wi2 * ((yi - mu) ** 2 - vi)))
        new = num / float(np.sum(wi2)) + 1.0 / float(sw)
        new = max(0.0, new)
        if abs(new - t2) < _TOL:
            t2 = new
            break
        t2 = new
    return t2


_TAU2_ESTIMATORS = {"DL": _tau2_dl, "PM": _tau2_pm, "REML": _tau2_reml}


def estimate_tau2(yi: Sequence[float], vi: Sequence[float], method: str = "REML") -> float:
    method = method.upper()
    if method not in _TAU2_ESTIMATORS:
        raise ValueError(f"unknown tau^2 method {method!r}; choose from {TAU2_METHODS}")
    yi = np.asarray(yi, dtype=float)
    vi = np.asarray(vi, dtype=float)
    return _TAU2_ESTIMATORS[method](yi, vi)


def meta_analyze(
    yi: Sequence[float],
    vi: Sequence[float],
    method: str = "REML",
    alpha: float = 0.05,
    measure: str = "GEN",
    log_scale: bool = False,
    knha: bool = False,
) -> MetaResult:
    """Run a random-effects meta-analysis.

    Parameters
    ----------
    yi, vi:
        Study effect sizes and their variances on the analysis scale.
    method:
        tau^2 estimator: ``REML`` (default), ``PM`` or ``DL``.
    alpha:
        Two-sided significance level for the confidence interval.
    measure, log_scale:
        Informational labels carried through to :class:`MetaResult`. Set
        ``log_scale=True`` for ratio measures so ``on_natural_scale`` knows to
        exponentiate.
    knha:
        Use the Knapp-Hartung (HKSJ) adjustment for the CI of the pooled effect
        (t distribution with ``k-1`` df, with a variance-inflation floor of 1 so
        the interval is never narrower than the Wald interval).

    A single study (``k == 1``) is returned with tau^2 = 0, no prediction
    interval and the study's own variance.
    """
    yi = np.asarray(yi, dtype=float)
    vi = np.asarray(vi, dtype=float)
    if yi.shape != vi.shape:
        raise ValueError("yi and vi must have the same length")
    k = int(yi.size)
    if k == 0:
        raise ValueError("cannot pool an empty set of studies")
    if np.any(vi <= 0):
        raise ValueError("all variances vi must be strictly positive")

    method = method.upper()

    if k == 1:
        est = float(yi[0])
        se = float(math.sqrt(vi[0]))
        zcrit = float(stats.norm.ppf(1 - alpha / 2))
        z = est / se
        p = float(2 * stats.norm.sf(abs(z)))
        return MetaResult(
            k=1, estimate=est, se=se,
            ci_low=est - zcrit * se, ci_high=est + zcrit * se,
            pi_low=None, pi_high=None, z=z, p_value=p,
            tau2=0.0, tau=0.0, i2=0.0, h2=1.0, q=0.0, q_df=0, q_p=float("nan"),
            method=method, measure=measure, log_scale=log_scale, alpha=alpha,
        )

    q, df = _cochran_q(yi, vi)
    q_p = float(stats.chi2.sf(q, df)) if df > 0 else float("nan")
    tau2 = estimate_tau2(yi, vi, method)

    # Random-effects weights and pooled estimate.
    wi = 1.0 / (vi + tau2)
    sw = float(np.sum(wi))
    mu = float(np.sum(wi * yi) / sw)
    var_mu = 1.0 / sw
    se_mu = math.sqrt(var_mu)

    # Heterogeneity descriptors. We use the generalised, estimator-aware I^2 of
    # Higgins & Thompson: I^2 = tau^2 / (tau^2 + s^2), where s^2 is the "typical"
    # within-study variance s^2 = df / C with C = sum(w) - sum(w^2)/sum(w) (FE
    # weights w = 1/vi). Unlike the Q-based (Q-df)/Q form, this tracks the chosen
    # tau^2 estimator and matches metafor's rma(). I^2 measures the *proportion*
    # of variance due to heterogeneity; tau^2 is reported alongside for magnitude.
    fe_w = 1.0 / vi
    c_const = float(np.sum(fe_w) - np.sum(fe_w ** 2) / np.sum(fe_w))
    if df > 0 and c_const > 0:
        s2_typical = df / c_const
        i2 = 100.0 * tau2 / (tau2 + s2_typical)
        h2 = (tau2 + s2_typical) / s2_typical
    else:
        i2 = 0.0
        h2 = 1.0

    if knha:
        # Knapp-Hartung: scale SE by sqrt of the weighted residual / df, floored
        # at 1 so the interval can only widen relative to the Wald interval.
        resid = float(np.sum(wi * (yi - mu) ** 2)) / df
        scale = math.sqrt(max(1.0, resid))
        se_ci = se_mu * scale
        crit = float(stats.t.ppf(1 - alpha / 2, df))
    else:
        se_ci = se_mu
        crit = float(stats.norm.ppf(1 - alpha / 2))

    ci_low = mu - crit * se_ci
    ci_high = mu + crit * se_ci

    # Test of the pooled effect against the null (0 on the analysis scale).
    z = mu / se_mu
    p_value = float(2 * stats.norm.sf(abs(z)))

    # Prediction interval: t_{k-1} per Cochrane Handbook v6.5. Needs k >= 2.
    pi_low = pi_high = None
    if k >= 2:
        t_pi = float(stats.t.ppf(1 - alpha / 2, df))
        sd_pred = math.sqrt(tau2 + var_mu)
        pi_low = mu - t_pi * sd_pred
        pi_high = mu + t_pi * sd_pred

    return MetaResult(
        k=k, estimate=mu, se=se_mu,
        ci_low=ci_low, ci_high=ci_high,
        pi_low=pi_low, pi_high=pi_high,
        z=z, p_value=p_value,
        tau2=tau2, tau=math.sqrt(tau2), i2=i2, h2=h2,
        q=q, q_df=df, q_p=q_p,
        method=method, measure=measure, log_scale=log_scale, alpha=alpha,
    )
