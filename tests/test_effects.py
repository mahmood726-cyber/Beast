"""Validate effect-size conversions against metafor's escalc."""

import math

import pytest

from beast.effects import Trial, trial_effect, compute_effects, is_log_scale


def _trial(d):
    return Trial(study="s", **d)


def test_or_matches_escalc(gold):
    g = gold["escalc"]["OR"]
    yi, vi = trial_effect(_trial(g["trial"]), "OR")
    assert yi == pytest.approx(g["yi"], abs=1e-9)
    assert vi == pytest.approx(g["vi"], abs=1e-9)


def test_or_zero_cell_correction_matches_escalc(gold):
    g = gold["escalc"]["OR_zero_cell"]
    yi, vi = trial_effect(_trial(g["trial"]), "OR")
    assert yi == pytest.approx(g["yi"], abs=1e-9)
    assert vi == pytest.approx(g["vi"], abs=1e-9)


def test_rr_matches_escalc(gold):
    g = gold["escalc"]["RR"]
    yi, vi = trial_effect(_trial(g["trial"]), "RR")
    assert yi == pytest.approx(g["yi"], abs=1e-9)
    assert vi == pytest.approx(g["vi"], abs=1e-9)


def test_smd_matches_escalc(gold):
    g = gold["escalc"]["SMD"]
    yi, vi = trial_effect(_trial(g["trial"]), "SMD")
    assert yi == pytest.approx(g["yi"], abs=1e-9)
    assert vi == pytest.approx(g["vi"], abs=1e-9)


def test_md_matches_escalc(gold):
    g = gold["escalc"]["MD"]
    yi, vi = trial_effect(_trial(g["trial"]), "MD")
    assert yi == pytest.approx(g["yi"], abs=1e-9)
    assert vi == pytest.approx(g["vi"], abs=1e-9)


def test_continuity_correction_only_when_a_cell_is_zero():
    # No zero cell -> raw counts, no +0.5 inflation.
    no_zero = Trial("a", e_events=12, e_n=65, c_events=20, c_n=65)
    a, b, c, d = 12, 53, 20, 45
    expected = math.log((a * d) / (b * c))
    yi, _ = trial_effect(no_zero, "OR")
    assert yi == pytest.approx(expected, abs=1e-12)
    # A zero cell -> correction applied, finite result.
    has_zero = Trial("b", e_events=0, e_n=40, c_events=8, c_n=40)
    yi0, vi0 = trial_effect(has_zero, "OR")
    assert math.isfinite(yi0) and vi0 > 0


def test_generic_effect_passthrough():
    t = Trial("g", yi=-0.3, sei=0.2)
    yi, vi = trial_effect(t, "GEN")
    assert yi == -0.3
    assert vi == pytest.approx(0.04, abs=1e-12)


def test_unusable_rows_return_none():
    # A subgroup header row with no data.
    assert trial_effect(Trial("header"), "OR") is None
    # Impossible counts (events > N).
    assert trial_effect(Trial("x", e_events=50, e_n=10, c_events=2, c_n=10), "OR") is None


def test_compute_effects_skips_unusable_and_preserves_order():
    trials = [
        Trial("good1", year=2000, e_events=5, e_n=50, c_events=10, c_n=50),
        Trial("header", year=2001),  # no data -> skipped
        Trial("good2", year=2002, e_events=3, e_n=40, c_events=8, c_n=40),
    ]
    used, yi, vi = compute_effects(trials, "OR")
    assert [t.study for t in used] == ["good1", "good2"]
    assert len(yi) == len(vi) == 2


def test_is_log_scale():
    assert is_log_scale("OR") and is_log_scale("RR")
    assert not is_log_scale("MD") and not is_log_scale("SMD") and not is_log_scale("GEN")
