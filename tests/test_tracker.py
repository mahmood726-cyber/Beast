"""End-to-end tracker behaviour over the real Cochrane sample.

This is the integration test: it proves the whole pipeline (source -> effects ->
meta -> snapshot -> store -> diff) reproduces the real surveillance story in
CD000028 -- antihypertensive therapy in the elderly went from non-significant in
1986 to significant by 1991 as trials accumulated -- and that re-running is a
no-op.
"""

import pytest

from beast.sources.base import TopicSpec
from beast.store import BeastStore
from beast.tracker import backfill_topic, run_once


@pytest.fixture
def store(tmp_path):
    s = BeastStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _topic(sample_csv):
    return TopicSpec(id="htn", title="HTN elderly", source="pairwise70",
                     measure="OR", method="REML", params={"csv": sample_csv})


def test_backfill_reconstructs_significance_flip(store, sample_csv):
    topic = _topic(sample_csv)
    report = backfill_topic(store, topic, years=[1980, 1986, 1989, 1991, 1992, 1993])
    hist = store.history("htn")
    # One snapshot per distinct cumulative state.
    by_year = {s.as_of_year: s for s in hist}
    assert by_year[1986].significant is False
    assert by_year[1991].significant is True
    # The flip must be recorded as a major change at 1991.
    flips = [c for c in store.recent_changes(topic_id="htn")
             if c["type"] == "significance_flip"]
    assert flips and flips[0]["severity"] == "major"
    # Heterogeneity climbs into a major flag by 1993.
    het = [c for c in store.recent_changes(topic_id="htn")
           if c["type"] == "heterogeneity_change"]
    assert any(c["severity"] == "major" for c in het)


def test_backfill_is_idempotent(store, sample_csv):
    topic = _topic(sample_csv)
    years = [1980, 1986, 1989, 1991, 1992, 1993]
    backfill_topic(store, topic, years)
    n_snap = len(store.history("htn"))
    n_chg = len(store.recent_changes(topic_id="htn", limit=999))
    # Replaying the same years must add nothing.
    report2 = backfill_topic(store, topic, years)
    assert all(r.inserted is False for r in report2.results)
    assert len(store.history("htn")) == n_snap
    assert len(store.recent_changes(topic_id="htn", limit=999)) == n_chg


def test_run_once_isolates_failing_topic(store, sample_csv):
    good = _topic(sample_csv)
    bad = TopicSpec(id="bad", title="bad", source="pairwise70", measure="OR",
                    params={"csv": "does-not-exist.csv"})
    report = run_once(store, [good, bad])
    assert report.n_ok == 1 and report.n_failed == 1
    by_id = {r.topic_id: r for r in report.results}
    assert by_id["good" if "good" in by_id else "htn"].ok is True
    assert by_id["bad"].ok is False and by_id["bad"].error


def test_run_once_idempotent_no_duplicate(store, sample_csv):
    topic = _topic(sample_csv)
    run_once(store, [topic])
    first = len(store.history("htn"))
    run_once(store, [topic])  # nothing changed in the source
    assert len(store.history("htn")) == first
