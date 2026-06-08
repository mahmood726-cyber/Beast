"""Convert study-level trial data into effect sizes ``(yi, vi)`` for pooling.

A :class:`Trial` is the universal unit Beast tracks. It can carry binary
(2x2 count) data, continuous (mean/SD) data, or a pre-computed generic-inverse-
variance estimate. :func:`compute_effects` turns a list of trials into the
``yi``/``vi`` arrays that :func:`beast.meta.meta_analyze` consumes, on the
appropriate analysis scale for the requested measure.

Conventions:

* Ratio measures (OR, RR) are computed on the *log* scale and the caller pools
  there, back-transforming afterwards (Simpson's paradox lurks if you average
  ratios on the natural scale under a random-effects model).
* A 0.5 continuity correction is applied to a 2x2 table *only* when at least one
  cell is zero (an unconditional correction biases the OR toward 1).
* SMD uses Hedges' g with the small-sample bias correction J.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

MEASURES = ("OR", "RR", "MD", "SMD", "GEN")
RATIO_MEASURES = ("OR", "RR")


@dataclass
class Trial:
    """One trial / study contributing to a meta-analysis.

    Provide exactly one of the three data shapes:

    * binary: ``e_events, e_n, c_events, c_n``
    * continuous: ``e_mean, e_sd, e_n, c_mean, c_sd, c_n``
    * generic: ``yi, sei`` (effect size and its standard error, already on the
      analysis scale -- e.g. log-HR with its SE)

    ``study`` and ``year`` identify the trial; ``year`` drives Beast's
    cumulative-over-time surveillance and the diffing of "new trials".
    """

    study: str
    year: Optional[int] = None

    # Binary 2x2.
    e_events: Optional[float] = None
    e_n: Optional[float] = None
    c_events: Optional[float] = None
    c_n: Optional[float] = None

    # Continuous.
    e_mean: Optional[float] = None
    e_sd: Optional[float] = None
    c_mean: Optional[float] = None
    c_sd: Optional[float] = None

    # Generic inverse variance (already an effect size + SE on analysis scale).
    yi: Optional[float] = None
    sei: Optional[float] = None

    # Shared by continuous/binary if the arm Ns are reused.
    e_n_cont: Optional[float] = None
    c_n_cont: Optional[float] = None

    extra: dict = field(default_factory=dict)

    # --- introspection -------------------------------------------------
    def has_binary(self) -> bool:
        return None not in (self.e_events, self.e_n, self.c_events, self.c_n)

    def has_continuous(self) -> bool:
        n_e = self.e_n if self.e_n is not None else self.e_n_cont
        n_c = self.c_n if self.c_n is not None else self.c_n_cont
        return None not in (self.e_mean, self.e_sd, n_e, self.c_mean, self.c_sd, n_c)

    def has_generic(self) -> bool:
        return self.yi is not None and self.sei is not None and self.sei > 0


def _binary_effect(t: Trial, measure: str) -> Optional[tuple[float, float]]:
    a, n1, c, n2 = t.e_events, t.e_n, t.c_events, t.c_n
    b = n1 - a  # experimental non-events
    d = n2 - c  # control non-events
    if min(a, b, c, d) < 0 or n1 <= 0 or n2 <= 0:
        return None
    # Continuity correction only if a cell is zero (conditional correction).
    if min(a, b, c, d) == 0:
        a, b, c, d = a + 0.5, b + 0.5, c + 0.5, d + 0.5
    if measure == "OR":
        yi = math.log((a * d) / (b * c))
        vi = 1.0 / a + 1.0 / b + 1.0 / c + 1.0 / d
    elif measure == "RR":
        yi = math.log((a / (a + b)) / (c / (c + d)))
        vi = 1.0 / a - 1.0 / (a + b) + 1.0 / c - 1.0 / (c + d)
    else:  # pragma: no cover - guarded by caller
        raise ValueError(f"binary data cannot produce measure {measure}")
    if not math.isfinite(yi) or not math.isfinite(vi) or vi <= 0:
        return None
    return yi, vi


def _continuous_effect(t: Trial, measure: str) -> Optional[tuple[float, float]]:
    n1 = t.e_n if t.e_n is not None else t.e_n_cont
    n2 = t.c_n if t.c_n is not None else t.c_n_cont
    m1, sd1, m2, sd2 = t.e_mean, t.e_sd, t.c_mean, t.c_sd
    if None in (n1, n2, m1, sd1, m2, sd2) or n1 < 2 or n2 < 2 or sd1 < 0 or sd2 < 0:
        return None
    if measure == "MD":
        yi = m1 - m2
        vi = sd1 ** 2 / n1 + sd2 ** 2 / n2
    elif measure == "SMD":
        dof = n1 + n2 - 2
        sp2 = ((n1 - 1) * sd1 ** 2 + (n2 - 1) * sd2 ** 2) / dof
        if sp2 <= 0:
            return None
        d = (m1 - m2) / math.sqrt(sp2)
        # Exact Hedges small-sample correction J = gamma(m/2) / (sqrt(m/2)*gamma((m-1)/2))
        # via lgamma for stability (matches metafor's escalc to machine precision).
        j = math.exp(
            math.lgamma(dof / 2.0)
            - 0.5 * math.log(dof / 2.0)
            - math.lgamma((dof - 1.0) / 2.0)
        )
        yi = j * d
        vi = (n1 + n2) / (n1 * n2) + yi ** 2 / (2.0 * (n1 + n2))
    else:  # pragma: no cover - guarded by caller
        raise ValueError(f"continuous data cannot produce measure {measure}")
    if not math.isfinite(yi) or not math.isfinite(vi) or vi <= 0:
        return None
    return yi, vi


def trial_effect(t: Trial, measure: str) -> Optional[tuple[float, float]]:
    """Return ``(yi, vi)`` for one trial on the analysis scale, or ``None``.

    ``None`` means the trial lacks usable data for the requested measure (e.g. a
    subgroup header row, or impossible counts) and should be dropped.
    """
    measure = measure.upper()
    if measure not in MEASURES:
        raise ValueError(f"unknown measure {measure!r}; choose from {MEASURES}")
    if measure == "GEN":
        return (t.yi, t.sei ** 2) if t.has_generic() else None
    # Prefer the data shape that matches the measure, falling back to generic.
    if measure in RATIO_MEASURES and t.has_binary():
        return _binary_effect(t, measure)
    if measure in ("MD", "SMD") and t.has_continuous():
        return _continuous_effect(t, measure)
    if t.has_generic():
        return (t.yi, t.sei ** 2)
    return None


def compute_effects(trials: list[Trial], measure: str):
    """Map trials to parallel ``(used_trials, yi, vi)`` arrays.

    Trials without usable data for ``measure`` are skipped; ``used_trials`` is the
    subset that contributed, in order, so callers can line up studies with their
    effect sizes.
    """
    used, yi, vi = [], [], []
    for t in trials:
        eff = trial_effect(t, measure)
        if eff is None:
            continue
        used.append(t)
        yi.append(eff[0])
        vi.append(eff[1])
    return used, yi, vi


def is_log_scale(measure: str) -> bool:
    return measure.upper() in RATIO_MEASURES
