"""Updater orchestration: discovery, idempotent append, and git staging.

The feed and extractor are mocked so the suite is fully offline -- no Cochrane
Library, no Crossref, no subprocess.
"""

import os

import pytest

from beast.effects import Trial
from beast.ingest.base import CallableExtractor, CochraneFeed, NoDataError, ReviewRef, review_id_from_doi
from beast.pairwise70_repo import Pairwise70Repo
from beast.updater import commit_and_push, update_pairwise70


def _seed_existing(root, ids):
    data = os.path.join(root, "data")
    os.makedirs(data, exist_ok=True)
    for rid in ids:
        with open(os.path.join(data, f"{rid}_data.rda"), "w", encoding="utf-8") as fh:
            fh.write("ORIGINAL")


class MockFeed(CochraneFeed):
    def __init__(self, refs):
        self.refs = refs
        self.calls = 0

    def list_reviews(self, since=None):
        self.calls += 1
        return list(self.refs)


def _trials(n=3, seed=0):
    return [Trial(f"S{seed}_{i}", year=2024, e_events=4 + i, e_n=80,
                  c_events=8 + i, c_n=80) for i in range(n)]


@pytest.fixture
def repo(tmp_path):
    _seed_existing(str(tmp_path), ["CD000028_pub4", "CD016001"])
    return Pairwise70Repo(str(tmp_path), rscript=None)


def test_review_id_from_doi():
    assert review_id_from_doi("10.1002/14651858.CD002042.pub6") == "CD002042_pub6"
    assert review_id_from_doi("https://doi.org/10.1002/14651858.CD016001") == "CD016001"
    with pytest.raises(ValueError):
        review_id_from_doi("10.1002/not-a-cochrane-doi")


def test_update_appends_only_new_reviews(repo):
    refs = [
        ReviewRef.from_doi("10.1002/14651858.CD000028.pub4"),  # existing -> skip
        ReviewRef.from_doi("10.1002/14651858.CD900001.pub1"),  # new
        ReviewRef.from_doi("10.1002/14651858.CD900002.pub1"),  # new
    ]
    feed = MockFeed(refs)

    def extract(ref):
        return _trials(seed=ref.id)

    report = update_pairwise70(repo, feed, CallableExtractor(extract), timestamp="2026-06-08")
    assert report.n_added == 2
    assert set(report.added_ids) == {"CD900001_pub1", "CD900002_pub1"}
    assert repo.has("CD900001_pub1") and repo.has("CD900002_pub1")
    # The pre-existing review was never extracted/written.
    assert "CD000028_pub4" not in report.added_ids


def test_update_is_idempotent(repo):
    refs = [ReviewRef.from_doi("10.1002/14651858.CD900001.pub1")]
    feed = MockFeed(refs)
    extractor = CallableExtractor(lambda r: _trials())
    r1 = update_pairwise70(repo, feed, extractor, timestamp="t1")
    assert r1.n_added == 1
    r2 = update_pairwise70(repo, feed, extractor, timestamp="t2")
    assert r2.n_added == 0
    assert r2.n_skipped == 1
    assert len(repo.manifest()["added"]) == 1


def test_update_handles_no_data_and_failures(repo):
    refs = [
        ReviewRef.from_doi("10.1002/14651858.CD900010.pub1"),  # ok
        ReviewRef.from_doi("10.1002/14651858.CD900011.pub1"),  # no data
        ReviewRef.from_doi("10.1002/14651858.CD900012.pub1"),  # raises
    ]
    feed = MockFeed(refs)

    def extract(ref):
        if ref.id == "CD900011_pub1":
            raise NoDataError(ref.id)
        if ref.id == "CD900012_pub1":
            raise RuntimeError("boom")
        return _trials()

    report = update_pairwise70(repo, feed, CallableExtractor(extract), timestamp="t")
    assert report.n_added == 1
    assert report.n_no_data == 1
    assert report.n_failed == 1
    # A failure/no-data review must not be recorded as present.
    assert not repo.has("CD900011_pub1")
    assert not repo.has("CD900012_pub1")


def test_update_respects_limit(repo):
    refs = [ReviewRef.from_doi(f"10.1002/14651858.CD90010{i}.pub1") for i in range(5)]
    report = update_pairwise70(repo, MockFeed(refs),
                               CallableExtractor(lambda r: _trials(seed=r.id)),
                               limit=2, timestamp="t")
    assert report.n_added == 2


def test_commit_and_push_stages_only_added_files(repo):
    refs = [ReviewRef.from_doi("10.1002/14651858.CD900020.pub1")]
    report = update_pairwise70(repo, MockFeed(refs),
                               CallableExtractor(lambda r: _trials()), timestamp="t")
    calls = []

    class FakeProc:
        returncode = 0
        stdout = " M beast_manifest.json\n"
        stderr = ""

    def fake_runner(args, **kw):
        calls.append(args)
        return FakeProc()

    msg = commit_and_push(repo, report, push=False, runner=fake_runner)
    assert msg and "CD900020_pub1" in msg
    add_calls = [c for c in calls if "add" in c]
    assert add_calls and "beast_manifest.json" in add_calls[0]
    # push=False -> no push call
    assert not any("push" in c for c in calls)


def test_commit_and_push_noop_when_nothing_added(repo):
    report = update_pairwise70(repo, MockFeed([]),
                               CallableExtractor(lambda r: _trials()), timestamp="t")
    assert report.n_added == 0
    calls = []
    msg = commit_and_push(repo, report, runner=lambda a, **k: calls.append(a))
    assert msg is None
    assert calls == []  # no git invoked at all


def test_commit_and_push_invokes_push_when_requested(repo):
    refs = [ReviewRef.from_doi("10.1002/14651858.CD900030.pub1")]
    report = update_pairwise70(repo, MockFeed(refs),
                               CallableExtractor(lambda r: _trials()), timestamp="t")
    calls = []

    class FakeProc:
        returncode = 0
        stdout = " M beast_manifest.json\n"
        stderr = ""

    def fake_runner(args, **kw):
        calls.append(args)
        return FakeProc()

    commit_and_push(repo, report, push=True, runner=fake_runner)
    assert any("push" in c for c in calls)
