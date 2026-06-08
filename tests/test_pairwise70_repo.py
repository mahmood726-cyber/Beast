"""Safety tests for the append-only Pairwise70 writer.

These encode the hard guarantees: no duplicates, no clobber, no data loss,
idempotent. The 595 original datasets are simulated by empty ``data/*.rda`` files
so the suite stays offline and fast (no R, no network).
"""

import os
import threading

import pytest

from beast.effects import Trial
from beast import pairwise70_repo as pw
from beast.pairwise70_repo import (
    ManifestError, Pairwise70Repo, content_sha256, review_id_from_stem,
)


def _seed_existing(root, ids):
    """Create empty data/<id>_data.rda files to simulate the original dataset."""
    data = os.path.join(root, "data")
    os.makedirs(data, exist_ok=True)
    for rid in ids:
        with open(os.path.join(data, f"{rid}_data.rda"), "w", encoding="utf-8") as fh:
            fh.write("ORIGINAL")  # sentinel content we assert is never altered


def _trials(n=3, seed=0):
    return [Trial(f"Study{seed}_{i}", year=2024, e_events=5 + i, e_n=100,
                  c_events=9 + i, c_n=100) for i in range(n)]


@pytest.fixture
def repo(tmp_path):
    _seed_existing(str(tmp_path), ["CD000028_pub4", "CD016001"])
    # rscript=None -> CSV-only, no R dependency in tests.
    return Pairwise70Repo(str(tmp_path), rscript=None)


def test_review_id_from_stem():
    assert review_id_from_stem("CD000028_pub4_data") == "CD000028_pub4"
    assert review_id_from_stem("CD016001_data") == "CD016001"


def test_existing_ids_derived_from_rda(repo):
    assert repo.has("CD000028_pub4")
    assert repo.has("CD016001")
    assert not repo.has("CD999999_pub1")
    assert repo.count_existing_rda() == 2


def test_add_new_dataset_writes_csv_and_manifest(repo):
    res = repo.add_dataset("CD999999_pub1", _trials(), doi="10.1002/14651858.CD999999.pub1",
                           title="New review", measure="OR", added_at="2026-06-08")
    assert res.status == "added"
    assert os.path.exists(res.csv_path)
    assert res.rda_path is None  # rscript disabled
    assert repo.has("CD999999_pub1")  # now present
    man = repo.manifest()
    assert man["added"][0]["review_id"] == "CD999999_pub1"
    assert man["added"][0]["n_studies"] == 3


def test_skip_existing_id_no_write(repo):
    before = set(os.listdir(os.path.join(repo.root, "data-raw", "beast")))
    res = repo.add_dataset("CD000028_pub4", _trials())
    assert res.status == "skipped_exists"
    after = set(os.listdir(os.path.join(repo.root, "data-raw", "beast")))
    assert before == after  # nothing written


def test_idempotent_readd(repo):
    repo.add_dataset("CD999999_pub1", _trials())
    res2 = repo.add_dataset("CD999999_pub1", _trials())
    assert res2.status == "skipped_exists"
    assert len(repo.manifest()["added"]) == 1


def test_duplicate_content_under_new_id_skipped(repo):
    repo.add_dataset("CD999999_pub1", _trials(seed=7))
    # Same study rows, different id -> content dedupe.
    res = repo.add_dataset("CD888888_pub1", _trials(seed=7))
    assert res.status == "skipped_dup_content"
    assert len(repo.manifest()["added"]) == 1


def test_no_clobber_of_preexisting_file(repo):
    # A CSV already on disk that is NOT in the ledger must not be overwritten.
    csv_path = os.path.join(repo.csv_dir, "CD777777_pub1_data.csv")
    with open(csv_path, "w", encoding="utf-8") as fh:
        fh.write("PREEXISTING")
    res = repo.add_dataset("CD777777_pub1", _trials())
    assert res.status == "skipped_exists"
    with open(csv_path, encoding="utf-8") as fh:
        assert fh.read() == "PREEXISTING"  # untouched


def test_original_rda_never_modified_or_deleted(repo):
    data = os.path.join(repo.root, "data")
    before = {f: open(os.path.join(data, f), encoding="utf-8").read()
              for f in os.listdir(data)}
    repo.add_dataset("CD555555_pub2", _trials())
    repo.add_dataset("CD444444_pub1", _trials(seed=2))
    after = {f: open(os.path.join(data, f), encoding="utf-8").read()
             for f in os.listdir(data) if f in before}
    # Every original file still present and byte-identical.
    assert set(before).issubset(set(os.listdir(data)))
    for f, content in before.items():
        assert after[f] == content == "ORIGINAL"


def test_added_files_lists_manifest_and_csv(repo):
    repo.add_dataset("CD333333_pub1", _trials())
    files = repo.added_files()
    assert "beast_manifest.json" in files
    assert any(f.endswith("CD333333_pub1_data.csv") for f in files)


def test_manifest_persists_across_instances(repo):
    repo.add_dataset("CD222222_pub1", _trials())
    # A fresh instance over the same root must see the addition (dedupe survives).
    repo2 = Pairwise70Repo(repo.root, rscript=None)
    assert repo2.has("CD222222_pub1")


def test_empty_trials_is_error(repo):
    res = repo.add_dataset("CD111111_pub1", [])
    assert res.status == "error"


def test_content_sha_is_order_independent():
    a = [{"study": "X", "year": 2020, "e_events": 1, "review_id": "r1"}]
    b = [{"study": "X", "year": 2020, "e_events": 1, "review_id": "r2"}]
    # review_id excluded from the hash -> same content, same sha.
    assert content_sha256(a) == content_sha256(b)


# --- concurrency / durability ----------------------------------------
def test_concurrent_instances_do_not_lose_manifest_entries(repo):
    """The lost-update scenario: two processes each load the manifest, then each
    appends a different new dataset. Both additions must survive (no clobber of
    the peer's just-recorded entry) so the dedupe ledger stays durable."""
    # Two independent instances over the SAME root, each with its own in-memory
    # manifest snapshot -- exactly what two OS processes would have.
    repo_a = Pairwise70Repo(repo.root, rscript=None)
    repo_b = Pairwise70Repo(repo.root, rscript=None)
    assert repo_a.add_dataset("CD900001_pub1", _trials(seed=1)).status == "added"
    assert repo_b.add_dataset("CD900002_pub1", _trials(seed=2)).status == "added"
    fresh = Pairwise70Repo(repo.root, rscript=None)
    ids = fresh.manifest_ids
    assert "CD900001_pub1" in ids and "CD900002_pub1" in ids
    assert len(fresh.manifest()["added"]) == 2


def test_threaded_adds_all_persist(repo):
    """Hammer the lock with many threads adding distinct ids; every one survives."""
    n = 12
    errors = []

    def worker(i):
        try:
            r = Pairwise70Repo(repo.root, rscript=None)
            r.add_dataset(f"CD90{i:04d}_pub1", _trials(seed=100 + i))
        except Exception as exc:  # pragma: no cover - surfaced via assert below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    fresh = Pairwise70Repo(repo.root, rscript=None)
    assert len(fresh.manifest()["added"]) == n
    # The original sentinels are still byte-identical.
    data = os.path.join(repo.root, "data")
    for f in os.listdir(data):
        if f.endswith(".rda"):
            assert open(os.path.join(data, f), encoding="utf-8").read() == "ORIGINAL"


def test_concurrent_same_content_under_new_id_deduped(repo):
    """A peer adding identical content under a different id is still caught after
    a separate instance recorded it first (content dedupe survives processes)."""
    repo_a = Pairwise70Repo(repo.root, rscript=None)
    repo_b = Pairwise70Repo(repo.root, rscript=None)
    assert repo_a.add_dataset("CD900001_pub1", _trials(seed=9)).status == "added"
    res = repo_b.add_dataset("CD900002_pub1", _trials(seed=9))
    assert res.status == "skipped_dup_content"
    assert len(Pairwise70Repo(repo.root, rscript=None).manifest()["added"]) == 1


# --- corrupt-manifest fail-closed ------------------------------------
def test_manifest_save_retries_transient_windows_sharing_violation(repo, monkeypatch):
    """On Windows a concurrent reader in another process makes os.replace raise a
    sharing violation (WinError 5). The save must retry rather than drop the
    addition -- the exact cross-process lost-update guarded against here."""
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        if os.path.basename(dst) == "beast_manifest.json" and calls["n"] < 3:
            calls["n"] += 1
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(pw.os, "replace", flaky_replace)
    monkeypatch.setattr(pw.time, "sleep", lambda *_: None)  # no real waiting
    res = repo.add_dataset("CD900001_pub1", _trials())
    assert res.status == "added"
    assert calls["n"] == 3  # it actually retried through the transient failures
    assert Pairwise70Repo(repo.root, rscript=None).has("CD900001_pub1")


def test_corrupt_manifest_fails_closed_on_construct(repo):
    repo.add_dataset("CD900001_pub1", _trials())
    with open(repo.manifest_path, "w", encoding="utf-8") as fh:
        fh.write("{not valid json")
    with pytest.raises(ManifestError):
        Pairwise70Repo(repo.root, rscript=None)


def test_structurally_invalid_manifest_fails_closed(repo):
    with open(repo.manifest_path, "w", encoding="utf-8") as fh:
        fh.write('{"added": "not a list"}')
    with pytest.raises(ManifestError):
        Pairwise70Repo(repo.root, rscript=None)


def test_corrupt_manifest_during_run_does_not_destroy_it(repo):
    """If the manifest is corrupted after construction, add_dataset fails closed
    rather than silently resetting the ledger or clobbering the file."""
    repo.add_dataset("CD900001_pub1", _trials())
    good = open(repo.manifest_path, encoding="utf-8").read()
    with open(repo.manifest_path, "w", encoding="utf-8") as fh:
        fh.write("CORRUPT")
    with pytest.raises(ManifestError):
        repo.add_dataset("CD900002_pub1", _trials(seed=3))
    # The corrupt file is left as-is for the operator to inspect; we did not
    # overwrite it with a fresh empty ledger (which would lose dedupe history).
    assert open(repo.manifest_path, encoding="utf-8").read() == "CORRUPT"
    assert good  # sanity: we had a real ledger before corruption
