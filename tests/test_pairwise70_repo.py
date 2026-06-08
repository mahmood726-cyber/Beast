"""Safety tests for the append-only Pairwise70 writer.

These encode the hard guarantees: no duplicates, no clobber, no data loss,
idempotent. The 595 original datasets are simulated by empty ``data/*.rda`` files
so the suite stays offline and fast (no R, no network).
"""

import os

import pytest

from beast.effects import Trial
from beast.pairwise70_repo import Pairwise70Repo, content_sha256, review_id_from_stem


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
