"""Append-only, idempotent writer for the Pairwise70 dataset.

Beast keeps the Pairwise70 dataset (https://github.com/mahmood726-cyber/Pairwise70)
continuously growing by appending newly-published Cochrane meta-analyses. This
module is the *safety boundary* for that: it only ever **adds** new dataset files
and never touches the original 595 (or anything already present).

Guarantees (all tested in tests/test_pairwise70_repo.py):

* **No duplicates** -- a review id already present (as ``data/<id>_data.rda`` or in
  the ledger) is skipped; identical study-level content (by SHA-256) is skipped
  even under a different id.
* **No clobber / no data loss** -- a target path that already exists is never
  overwritten; writes go to a temp file and are atomically renamed into place; no
  file is ever deleted.
* **Idempotent** -- re-running an update adds nothing new.

The original review ids are derived from the existing ``data/*.rda`` filenames and
are treated as immutable. Beast records everything it adds in ``beast_manifest.json``
at the dataset root (the append ledger).
"""

from __future__ import annotations

import csv
import glob
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from typing import Optional

from beast.effects import Trial

MANIFEST_NAME = "beast_manifest.json"
# Tidy data-rows columns Beast writes for an appended dataset.
CSV_COLUMNS = [
    "study", "year", "e_events", "e_n", "c_events", "c_n",
    "e_mean", "e_sd", "c_mean", "c_sd", "yi", "sei",
    "review_id", "review_doi", "measure",
]


def review_id_from_stem(stem: str) -> str:
    """``CD000028_pub4_data`` -> ``CD000028_pub4`` (strip the trailing ``_data``)."""
    return stem[:-5] if stem.endswith("_data") else stem


def _trial_rows(trials: list[Trial], review_id: str, doi: str, measure: str) -> list[dict]:
    rows = []
    for t in trials:
        rows.append({
            "study": t.study, "year": t.year,
            "e_events": t.e_events, "e_n": t.e_n,
            "c_events": t.c_events, "c_n": t.c_n,
            "e_mean": t.e_mean, "e_sd": t.e_sd,
            "c_mean": t.c_mean, "c_sd": t.c_sd,
            "yi": t.yi, "sei": t.sei,
            "review_id": review_id, "review_doi": doi, "measure": measure,
        })
    return rows


def content_sha256(rows: list[dict]) -> str:
    """Stable hash of the study-level content (order-independent on rows)."""
    norm = sorted(
        json.dumps({k: r.get(k) for k in CSV_COLUMNS if k not in ("review_id", "review_doi")},
                   sort_keys=True)
        for r in rows
    )
    return hashlib.sha256("\n".join(norm).encode("utf-8")).hexdigest()


@dataclass
class AddResult:
    review_id: str
    status: str            # added | skipped_exists | skipped_dup_content | error
    n_studies: int = 0
    csv_path: Optional[str] = None
    rda_path: Optional[str] = None
    error: Optional[str] = None


class Pairwise70Repo:
    """A local working copy of the Pairwise70 dataset that Beast appends to."""

    def __init__(self, root: str, rscript: Optional[str] = "Rscript"):
        self.root = os.path.abspath(root)
        self.data_dir = os.path.join(self.root, "data")
        self.csv_dir = os.path.join(self.root, "data-raw", "beast")
        self.manifest_path = os.path.join(self.root, MANIFEST_NAME)
        self.rscript = rscript
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.csv_dir, exist_ok=True)
        self._manifest = self._load_manifest()
        # Immutable set of pre-existing review ids (the original 595 + any .rda).
        self._existing_rda_ids = self._scan_existing_rda_ids()

    # --- discovery ----------------------------------------------------
    def _scan_existing_rda_ids(self) -> set[str]:
        ids = set()
        for path in glob.glob(os.path.join(self.data_dir, "*.rda")):
            stem = os.path.splitext(os.path.basename(path))[0]
            ids.add(review_id_from_stem(stem))
        return ids

    def _load_manifest(self) -> dict:
        if os.path.exists(self.manifest_path):
            with open(self.manifest_path, encoding="utf-8") as fh:
                return json.load(fh)
        return {
            "managed_by": "Beast living updater (https://github.com/mahmood726-cyber/Beast)",
            "dataset": "Pairwise70",
            "schema_version": 1,
            "added": [],
        }

    @property
    def manifest_ids(self) -> set[str]:
        return {e["review_id"] for e in self._manifest["added"]}

    @property
    def manifest_shas(self) -> set[str]:
        return {e.get("content_sha256") for e in self._manifest["added"]}

    def existing_ids(self) -> set[str]:
        """All review ids that already exist (original .rda + Beast-added)."""
        return set(self._existing_rda_ids) | self.manifest_ids

    def has(self, review_id: str) -> bool:
        return review_id in self.existing_ids()

    def count_existing_rda(self) -> int:
        return len(self._existing_rda_ids)

    # --- atomic, no-clobber write helpers -----------------------------
    @staticmethod
    def _atomic_write(path: str, write_fn) -> None:
        """Write via a temp file in the same dir, then rename into place.

        Refuses to overwrite an existing target (no clobber / no data loss).
        """
        if os.path.exists(path):
            raise FileExistsError(path)
        d = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                write_fn(fh)
            if os.path.exists(path):  # someone created it meanwhile -> abort, keep theirs
                raise FileExistsError(path)
            os.rename(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def _write_csv(self, path: str, rows: list[dict]) -> None:
        def _w(fh):
            w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            w.writeheader()
            for r in rows:
                w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in CSV_COLUMNS})
        self._atomic_write(path, _w)

    def _save_manifest(self) -> None:
        # Manifest is rewritten in place (it is Beast-owned); use a temp+replace
        # that DOES replace the manifest only (never a dataset file).
        d = os.path.dirname(self.manifest_path)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._manifest, fh, indent=2)
        os.replace(tmp, self.manifest_path)

    def _try_build_rda(self, csv_path: str, rda_path: str, object_name: str) -> Optional[str]:
        """Best-effort CSV -> .rda using Rscript (faithful to create_rda_files.R).

        Returns the rda path on success, else None (the CSV stays authoritative).
        Never overwrites an existing .rda.
        """
        if not self.rscript or os.path.exists(rda_path):
            return None
        r_code = (
            f'd <- read.csv({json.dumps(csv_path)}, stringsAsFactors=FALSE); '
            f'assign({json.dumps(object_name)}, d); '
            f'save(list={json.dumps(object_name)}, file={json.dumps(rda_path)})'
        )
        try:
            proc = subprocess.run(
                [self.rscript, "-e", r_code],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode == 0 and os.path.exists(rda_path):
                return rda_path
        except (OSError, subprocess.SubprocessError):
            pass
        return None

    # --- the append operation -----------------------------------------
    def add_dataset(
        self, review_id: str, trials: list[Trial], doi: str = "", title: str = "",
        pub_date: str = "", measure: str = "OR", added_at: str = "",
    ) -> AddResult:
        """Append one new dataset. Idempotent and append-only.

        Skips (without writing) if the id already exists or if identical content
        is already present under any id. Never overwrites or deletes.
        """
        if not trials:
            return AddResult(review_id, "error", error="no study rows to add")
        if self.has(review_id):
            return AddResult(review_id, "skipped_exists", n_studies=len(trials))

        rows = _trial_rows(trials, review_id, doi, measure)
        sha = content_sha256(rows)
        if sha in self.manifest_shas:
            return AddResult(review_id, "skipped_dup_content", n_studies=len(trials))

        object_name = f"{review_id}_data"
        csv_path = os.path.join(self.csv_dir, f"{object_name}.csv")
        rda_path = os.path.join(self.data_dir, f"{object_name}.rda")
        try:
            self._write_csv(csv_path, rows)
        except FileExistsError:
            # A file is on disk but not in our ledger -> treat as pre-existing,
            # do not clobber.
            return AddResult(review_id, "skipped_exists", n_studies=len(trials))

        built = self._try_build_rda(csv_path, rda_path, object_name)

        entry = {
            "review_id": review_id, "doi": doi, "title": title, "pub_date": pub_date,
            "measure": measure, "n_studies": len(trials),
            "csv": os.path.relpath(csv_path, self.root).replace(os.sep, "/"),
            "rda": (os.path.relpath(built, self.root).replace(os.sep, "/") if built else None),
            "content_sha256": sha, "added_at": added_at,
        }
        self._manifest["added"].append(entry)
        self._save_manifest()
        return AddResult(review_id, "added", n_studies=len(trials),
                         csv_path=csv_path, rda_path=built)

    def manifest(self) -> dict:
        return self._manifest

    def added_files(self) -> list[str]:
        """Repo-relative paths of every file Beast has added (for git staging)."""
        out = [MANIFEST_NAME]
        for e in self._manifest["added"]:
            out.append(e["csv"])
            if e.get("rda"):
                out.append(e["rda"])
        return out
