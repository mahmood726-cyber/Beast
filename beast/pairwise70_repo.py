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
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from beast.effects import Trial

MANIFEST_NAME = "beast_manifest.json"
LOCK_NAME = MANIFEST_NAME + ".lock"
# Tidy data-rows columns Beast writes for an appended dataset.
CSV_COLUMNS = [
    "study", "year", "e_events", "e_n", "c_events", "c_n",
    "e_mean", "e_sd", "c_mean", "c_sd", "yi", "sei",
    "review_id", "review_doi", "measure",
]


class ManifestError(RuntimeError):
    """Raised when the append ledger is unreadable/corrupt.

    Fail closed: we never silently treat a corrupt manifest as empty, because
    that would throw away the dedupe ledger and risk re-appending datasets that
    are already present (a duplicate-content leak under a different id).
    """


class _FileLock:
    """A small cross-platform inter-process lock (exclusive-create lockfile).

    Beast may run as a persistent ``loop`` process *and* under cron at the same
    time, both auto-updating the same Pairwise70 working copy. Without a lock the
    manifest is a read-modify-write across processes and one writer can silently
    clobber another's just-added entry (a lost update), which would drop a real
    addition out of the durable dedupe ledger. This lock serialises the whole
    claim+write+record step so the ledger stays correct and durable.

    Acquisition is bounded; a lockfile left behind by a crashed process is
    reclaimed once it is older than ``stale_after`` so a crash never deadlocks
    future runs.
    """

    def __init__(self, path: str, timeout: float = 60.0, poll: float = 0.05,
                 stale_after: float = 900.0):
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self.stale_after = stale_after
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        start = time.monotonic()
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                try:
                    os.write(self._fd, str(os.getpid()).encode("ascii"))
                except OSError:
                    pass
                return
            except FileExistsError:
                contended = True
            except PermissionError:
                # Windows returns ACCESS_DENIED (not FileExistsError) when the
                # lockfile is held *or* in a pending-delete state during another
                # holder's release. Treat it as contention and retry, never as a
                # hard failure that would abort an update.
                contended = True
            if contended:
                # Reclaim a stale lock left by a crashed/killed process.
                try:
                    age = time.time() - os.path.getmtime(self.path)
                except OSError:
                    age = 0.0
                if age > self.stale_after:
                    try:
                        os.unlink(self.path)
                    except OSError:
                        pass
                    continue
                if time.monotonic() - start > self.timeout:
                    raise TimeoutError(
                        f"could not acquire {self.path} within {self.timeout}s "
                        f"(another Beast update may be running)"
                    )
                time.sleep(self.poll)

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def __enter__(self) -> "_FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


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
        self.lock_path = os.path.join(self.root, LOCK_NAME)
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
            # A concurrent writer's atomic os.replace makes the target briefly
            # inaccessible on Windows (PermissionError); that is NOT corruption,
            # so retry the read before judging. Genuine corruption (bad JSON /
            # wrong structure) still fails closed as a ManifestError.
            raw = None
            last_err: Optional[Exception] = None
            for attempt in range(20):
                try:
                    with open(self.manifest_path, encoding="utf-8") as fh:
                        raw = fh.read()
                    break
                except PermissionError as exc:  # transient sharing violation
                    last_err = exc
                    time.sleep(0.05)
            if raw is None:
                raise ManifestError(
                    f"Pairwise70 manifest at {self.manifest_path} stayed unreadable "
                    f"across retries ({last_err}); refusing to proceed so the dedupe "
                    f"ledger is never silently lost."
                ) from last_err
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ManifestError(
                    f"Pairwise70 manifest at {self.manifest_path} is corrupt "
                    f"({exc}); refusing to proceed so the dedupe ledger is never "
                    f"silently lost. Inspect or restore it, then re-run."
                ) from exc
            if not isinstance(data, dict) or not isinstance(data.get("added"), list):
                raise ManifestError(
                    f"Pairwise70 manifest at {self.manifest_path} has an unexpected "
                    f"structure (expected a JSON object with an 'added' list); "
                    f"refusing to proceed."
                )
            return data
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
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._manifest, fh, indent=2)
            # Writes are serialised by the lock, but a *reader* in another
            # process (e.g. a peer's __init__) can briefly hold the manifest
            # open. On Windows os.replace then raises a sharing violation
            # (WinError 5/32); retry briefly so a concurrent reader never costs
            # us a real addition (the lost-update we are guarding against).
            for attempt in range(20):
                try:
                    os.replace(tmp, self.manifest_path)
                    return
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.05)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

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

        rows = _trial_rows(trials, review_id, doi, measure)
        sha = content_sha256(rows)
        object_name = f"{review_id}_data"
        csv_path = os.path.join(self.csv_dir, f"{object_name}.csv")
        rda_path = os.path.join(self.data_dir, f"{object_name}.rda")

        # The whole claim->write->record step runs under an inter-process lock so a
        # concurrent updater (e.g. `beast loop` overlapping a cron `beast run`)
        # cannot lost-update the manifest. We re-read the ledger *inside* the lock
        # so dedupe sees any peer's additions and stays durable across processes.
        with _FileLock(self.lock_path):
            self._manifest = self._load_manifest()
            if self.has(review_id):
                return AddResult(review_id, "skipped_exists", n_studies=len(trials))
            if sha in self.manifest_shas:
                return AddResult(review_id, "skipped_dup_content", n_studies=len(trials))
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
