"""SQLite store: persistence, idempotency and change recording."""

import pytest

from beast.diff import diff_snapshots
from beast.effects import Trial
from beast.snapshot import compute_snapshot
from beast.sources.base import TopicSpec
from beast.store import BeastStore


@pytest.fixture
def store(tmp_path):
    s = BeastStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _topic():
    return TopicSpec(id="t", title="T", source="pairwise70", measure="OR", method="REML")


def _snap(n, ts):
    trials = [Trial(f"s{i}", year=2000 + i, e_events=5 + i, e_n=80, c_events=10, c_n=80)
              for i in range(n)]
    return compute_snapshot(_topic(), trials, ts)


def test_upsert_and_list_topics(store):
    store.upsert_topic(_topic(), "2020-01-01T00:00:00Z")
    store.upsert_topic(_topic(), "2020-01-01T00:00:00Z")  # idempotent upsert
    topics = store.list_topics()
    assert len(topics) == 1 and topics[0].id == "t"


def test_add_snapshot_latest_scope_dedupes_consecutive(store):
    store.upsert_topic(_topic(), "2020-01-01T00:00:00Z")
    id1, ins1 = store.add_snapshot(_snap(5, "2020-01-01T00:00:00Z"))
    id2, ins2 = store.add_snapshot(_snap(5, "2020-02-01T00:00:00Z"))  # identical content
    assert ins1 is True and ins2 is False
    assert id1 == id2
    assert len(store.history("t")) == 1


def test_add_snapshot_records_real_change(store):
    store.upsert_topic(_topic(), "2020-01-01T00:00:00Z")
    store.add_snapshot(_snap(5, "2020-01-01T00:00:00Z"))
    _id, inserted = store.add_snapshot(_snap(7, "2020-02-01T00:00:00Z"))
    assert inserted is True
    assert len(store.history("t")) == 2


def test_topic_scope_dedupe_skips_any_existing_hash(store):
    store.upsert_topic(_topic(), "2020-01-01T00:00:00Z")
    a = _snap(5, "a")
    b = _snap(7, "b")
    store.add_snapshot(a, dedupe="topic")
    store.add_snapshot(b, dedupe="topic")
    # Re-adding an earlier-content snapshot under topic scope is a no-op.
    _id, inserted = store.add_snapshot(_snap(5, "c"), dedupe="topic")
    assert inserted is False
    assert len(store.history("t")) == 2


def test_changes_persisted_and_retrievable(store):
    store.upsert_topic(_topic(), "2020-01-01T00:00:00Z")
    s1 = _snap(5, "2020-01-01T00:00:00Z")
    s2 = _snap(7, "2020-02-01T00:00:00Z")
    id1, _ = store.add_snapshot(s1)
    id2, _ = store.add_snapshot(s2)
    diff = diff_snapshots(s1, s2)
    n = store.add_changes(diff, id1, id2)
    assert n == len(diff.changes) >= 1
    rc = store.recent_changes(topic_id="t")
    assert rc and rc[0]["type"] in {c.type for c in diff.changes}
    assert isinstance(rc[0]["details"], dict)
