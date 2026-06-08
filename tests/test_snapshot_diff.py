"""Snapshot construction and the change-detection diff logic."""

import pytest

from beast.diff import DiffThresholds, diff_snapshots
from beast.effects import Trial
from beast.snapshot import Snapshot, compute_snapshot, study_key
from beast.sources.base import TopicSpec


def _topic():
    return TopicSpec(id="t", title="t", source="pairwise70", measure="OR", method="REML")


def _bin_trials(n):
    return [Trial(f"s{i}", year=2000 + i, e_events=5 + i, e_n=80, c_events=10 + i, c_n=80)
            for i in range(n)]


def test_compute_snapshot_basic_fields():
    snap = compute_snapshot(_topic(), _bin_trials(5), "2020-01-01T00:00:00Z")
    assert snap.k == 5
    assert snap.measure == "OR"
    assert snap.log_scale is True
    assert len(snap.study_keys) == 5
    assert snap.natural["estimate"] > 0  # OR on natural scale
    assert snap.n_total == 5 * 160
    assert snap.content_hash


def test_content_hash_is_stable_and_input_sensitive():
    a = compute_snapshot(_topic(), _bin_trials(5), "2020-01-01T00:00:00Z")
    b = compute_snapshot(_topic(), _bin_trials(5), "2099-12-31T00:00:00Z")  # different ts
    # Hash ignores timestamp -> same inputs, same hash (idempotency basis).
    assert a.content_hash == b.content_hash
    c = compute_snapshot(_topic(), _bin_trials(6), "2020-01-01T00:00:00Z")  # one more trial
    assert c.content_hash != a.content_hash


def test_compute_snapshot_fails_closed_on_no_usable_trials():
    with pytest.raises(ValueError):
        compute_snapshot(_topic(), [Trial("header", year=2000)], "2020-01-01T00:00:00Z")


def _snap(**kw):
    base = dict(
        topic_id="t", timestamp="2020-01-01T00:00:00Z", measure="OR", method="REML",
        k=5, estimate=-0.2, se=0.1, ci_low=-0.4, ci_high=0.0, pi_low=-0.6, pi_high=0.2,
        tau2=0.0, i2=0.0, h2=1.0, q=2.0, q_p=0.7, z=-2.0, p_value=0.045,
        significant=False, log_scale=True,
        natural={"estimate": 0.82, "ci_low": 0.67, "ci_high": 1.0, "pi_low": 0.55, "pi_high": 1.22},
        study_keys=[f"s{i}|200{i}" for i in range(5)], n_total=800, as_of_year=None,
    )
    base.update(kw)
    s = Snapshot(**base)
    s.content_hash = s._hash_payload()
    return s


def test_no_change_when_identical():
    a, b = _snap(), _snap()
    assert diff_snapshots(a, b).has_changes is False


def test_new_trials_flagged():
    prev = _snap(k=5, study_keys=[f"s{i}|200{i}" for i in range(5)])
    curr = _snap(k=7, study_keys=[f"s{i}|200{i}" for i in range(7)])
    diff = diff_snapshots(prev, curr)
    types = {c.type for c in diff.changes}
    assert "new_trials" in types
    nt = next(c for c in diff.changes if c.type == "new_trials")
    assert nt.details["k_to"] == 7


def test_significance_flip_is_major():
    prev = _snap(significant=False, ci_low=-0.4, ci_high=0.05)
    curr = _snap(significant=True, ci_low=-0.4, ci_high=-0.05,
                 natural={"estimate": 0.78, "ci_low": 0.67, "ci_high": 0.95})
    diff = diff_snapshots(prev, curr)
    flip = [c for c in diff.changes if c.type == "significance_flip"]
    assert flip and flip[0].severity == "major"
    assert flip[0].details["direction"] == "gained"
    assert diff.max_severity == "major"


def test_effect_shift_threshold():
    prev = _snap(estimate=-0.20)
    small = _snap(estimate=-0.25)   # 0.05 < 0.10 default threshold
    big = _snap(estimate=-0.45)     # 0.25 shift -> major
    assert not any(c.type == "effect_shift" for c in diff_snapshots(prev, small).changes)
    big_changes = [c for c in diff_snapshots(prev, big).changes if c.type == "effect_shift"]
    assert big_changes and big_changes[0].severity == "major"


def test_direction_flip_is_major():
    prev = _snap(estimate=-0.15, natural={"estimate": 0.86, "ci_low": 0.7, "ci_high": 1.05})
    curr = _snap(estimate=0.15, natural={"estimate": 1.16, "ci_low": 0.95, "ci_high": 1.42})
    diff = diff_snapshots(prev, curr)
    assert any(c.type == "direction_flip" and c.severity == "major" for c in diff.changes)


def test_direction_flip_gated_by_magnitude():
    # A sign change while hovering on the null (OR ~0.98 -> ~1.02) is noise, not a
    # real reversal -- it must NOT raise a spurious "major" alert.
    prev = _snap(estimate=-0.02, natural={"estimate": 0.98, "ci_low": 0.9, "ci_high": 1.07})
    curr = _snap(estimate=0.02, natural={"estimate": 1.02, "ci_low": 0.93, "ci_high": 1.11})
    diff = diff_snapshots(prev, curr)
    assert not any(c.type == "direction_flip" for c in diff.changes)


def test_heterogeneity_change_flagged():
    prev = _snap(i2=10.0, tau2=0.01)
    curr = _snap(i2=55.0, tau2=0.08)
    diff = diff_snapshots(prev, curr)
    het = [c for c in diff.changes if c.type == "heterogeneity_change"]
    assert het and het[0].severity == "major"  # 45-point jump


def test_removed_trials_is_major():
    prev = _snap(k=7, study_keys=[f"s{i}|200{i}" for i in range(7)])
    curr = _snap(k=5, study_keys=[f"s{i}|200{i}" for i in range(5)])
    diff = diff_snapshots(prev, curr)
    assert any(c.type == "removed_trials" and c.severity == "major" for c in diff.changes)


def test_custom_thresholds_respected():
    prev, curr = _snap(estimate=-0.20), _snap(estimate=-0.26)
    th = DiffThresholds(effect_shift=0.05)
    assert any(c.type == "effect_shift" for c in diff_snapshots(prev, curr, th).changes)
