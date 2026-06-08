"""Validate the random-effects engine against R metafor gold values.

Gold values were produced with metafor's ``rma()`` / ``predict()`` (see
tests/fixtures/metafor_gold.json). Tolerances:

* DL is closed-form -> matches to 1e-6.
* REML matches to 1e-4 (fixed-point vs metafor's Fisher scoring).
* PM matches to ~2e-3 on I^2, 1e-4 on tau^2 -- bounded by metafor's own PM
  convergence tolerance, not by our solver.

The prediction interval follows the Cochrane Handbook v6.5 t_{k-1} rule and is
checked against its closed form (metafor's predict() default uses a different
quantile in some versions).
"""

import math

import pytest
from scipy import stats

from beast.meta import meta_analyze, estimate_tau2


def test_dl_matches_metafor_exactly(gold):
    g = gold["rma_heterogeneous"]
    r = meta_analyze(g["yi"], g["vi"], method="DL", log_scale=True)
    d = g["DL"]
    assert r.estimate == pytest.approx(d["est"], abs=1e-6)
    assert r.ci_low == pytest.approx(d["ci_low"], abs=1e-6)
    assert r.ci_high == pytest.approx(d["ci_high"], abs=1e-6)
    assert r.tau2 == pytest.approx(d["tau2"], abs=1e-6)
    assert r.i2 == pytest.approx(d["i2"], abs=1e-5)
    assert r.se == pytest.approx(d["se"], abs=1e-6)
    assert r.q == pytest.approx(g["Q"], abs=1e-6)


def test_reml_matches_metafor(gold):
    g = gold["rma_heterogeneous"]
    r = meta_analyze(g["yi"], g["vi"], method="REML", log_scale=True)
    d = g["REML"]
    assert r.estimate == pytest.approx(d["est"], abs=1e-4)
    assert r.ci_low == pytest.approx(d["ci_low"], abs=1e-4)
    assert r.ci_high == pytest.approx(d["ci_high"], abs=1e-4)
    assert r.tau2 == pytest.approx(d["tau2"], abs=1e-4)
    assert r.i2 == pytest.approx(d["i2"], abs=1e-2)


def test_pm_matches_metafor(gold):
    g = gold["rma_heterogeneous"]
    r = meta_analyze(g["yi"], g["vi"], method="PM", log_scale=True)
    d = g["PM"]
    # PM tau^2 is bounded by metafor's own convergence tolerance.
    assert r.tau2 == pytest.approx(d["tau2"], abs=1e-3)
    assert r.i2 == pytest.approx(d["i2"], abs=5e-3)


def test_knha_matches_metafor(gold):
    g = gold["rma_heterogeneous"]
    r = meta_analyze(g["yi"], g["vi"], method="REML", knha=True, log_scale=True)
    d = g["REML_knha"]
    assert r.ci_low == pytest.approx(d["ci_low"], abs=1e-5)
    assert r.ci_high == pytest.approx(d["ci_high"], abs=1e-5)


def test_knha_never_narrower_than_wald(gold):
    g = gold["rma_heterogeneous"]
    wald = meta_analyze(g["yi"], g["vi"], method="REML", log_scale=True)
    knha = meta_analyze(g["yi"], g["vi"], method="REML", knha=True, log_scale=True)
    assert (knha.ci_high - knha.ci_low) >= (wald.ci_high - wald.ci_low) - 1e-9


def test_prediction_interval_is_cochrane_t_k_minus_1(gold):
    g = gold["rma_heterogeneous"]
    r = meta_analyze(g["yi"], g["vi"], method="REML", log_scale=True)
    k = r.k
    t_crit = float(stats.t.ppf(0.975, k - 1))
    sd_pred = math.sqrt(r.tau2 + r.se ** 2)
    assert r.pi_low == pytest.approx(r.estimate - t_crit * sd_pred, abs=1e-9)
    assert r.pi_high == pytest.approx(r.estimate + t_crit * sd_pred, abs=1e-9)
    # PI must be wider than the CI when tau^2 > 0.
    assert (r.pi_high - r.pi_low) > (r.ci_high - r.ci_low)


def test_homogeneous_data_gives_zero_tau2():
    yi = [-0.30, -0.32, -0.28, -0.31]
    vi = [0.04, 0.05, 0.045, 0.042]
    for m in ("DL", "PM", "REML"):
        assert estimate_tau2(yi, vi, m) == pytest.approx(0.0, abs=1e-9)
    r = meta_analyze(yi, vi, method="REML")
    assert r.i2 == pytest.approx(0.0, abs=1e-9)


def test_single_study_has_no_pi_and_own_variance():
    r = meta_analyze([0.5], [0.04], method="REML")
    assert r.k == 1
    assert r.tau2 == 0.0
    assert r.pi_low is None and r.pi_high is None
    assert r.se == pytest.approx(0.2, abs=1e-12)


def test_significance_flag_tracks_ci():
    sig = meta_analyze([-0.5, -0.6, -0.55], [0.02, 0.03, 0.025])
    assert sig.significant is True
    ns = meta_analyze([0.05, -0.1, 0.02], [0.2, 0.25, 0.22])
    assert ns.significant is False


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        meta_analyze([], [])
    with pytest.raises(ValueError):
        meta_analyze([0.1, 0.2], [0.05])  # mismatched lengths
    with pytest.raises(ValueError):
        meta_analyze([0.1, 0.2], [0.05, 0.0])  # non-positive variance
    with pytest.raises(ValueError):
        estimate_tau2([0.1], [0.2], method="BOGUS")
