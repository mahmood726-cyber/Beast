"""Orchestrate the Pairwise70 auto-update: discover new reviews, extract, append.

This is Beast's primary, reliable core. On each run it:

1. asks the :class:`~beast.ingest.base.CochraneFeed` for currently-published
   reviews (optionally only those after ``since``),
2. keeps only ids not already in the dataset (the original 595 + anything Beast
   added) -- the idempotent dedupe,
3. extracts study-level rows for each new review and appends them via
   :class:`~beast.pairwise70_repo.Pairwise70Repo` (append-only, no clobber),
4. optionally commits (and pushes) the additions to the Pairwise70 git repo.

Every per-review step is isolated: one failure or a no-data review never aborts
the batch. The function is safe to re-run -- already-present reviews are skipped.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from beast.ingest.base import CochraneFeed, NoDataError, ReviewRef, StudyExtractor
from beast.pairwise70_repo import Pairwise70Repo

log = logging.getLogger("beast.updater")


@dataclass
class ReviewUpdate:
    review_id: str
    status: str            # added | skipped_exists | skipped_dup_content | no_data | failed
    n_studies: int = 0
    error: Optional[str] = None


@dataclass
class UpdateReport:
    timestamp: str
    results: list[ReviewUpdate] = field(default_factory=list)
    existing_before: int = 0

    def _count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def n_added(self) -> int:
        return self._count("added")

    @property
    def n_skipped(self) -> int:
        return self._count("skipped_exists") + self._count("skipped_dup_content")

    @property
    def n_no_data(self) -> int:
        return self._count("no_data")

    @property
    def n_failed(self) -> int:
        return self._count("failed")

    @property
    def added_ids(self) -> list[str]:
        return [r.review_id for r in self.results if r.status == "added"]


def update_pairwise70(
    repo: Pairwise70Repo,
    feed: CochraneFeed,
    extractor: StudyExtractor,
    since: Optional[str] = None,
    limit: Optional[int] = None,
    timestamp: str = "",
) -> UpdateReport:
    """Discover and append newly-published Cochrane reviews. Idempotent."""
    report = UpdateReport(timestamp=timestamp, existing_before=repo.count_existing_rda())
    refs = feed.list_reviews(since=since)
    # Record already-present reviews as skipped BEFORE any expensive extraction.
    new_refs = []
    for r in refs:
        if repo.has(r.id):
            report.results.append(ReviewUpdate(r.id, "skipped_exists"))
        else:
            new_refs.append(r)
    log.info("update: feed returned %d review(s), %d new (existing dataset: %d .rda)",
             len(refs), len(new_refs), report.existing_before)
    if limit is not None:
        new_refs = new_refs[:limit]

    for ref in new_refs:
        # Re-check inside the loop: an earlier iteration may have added the same id.
        if repo.has(ref.id):
            report.results.append(ReviewUpdate(ref.id, "skipped_exists"))
            continue
        try:
            trials = extractor.extract(ref)
        except NoDataError:
            report.results.append(ReviewUpdate(ref.id, "no_data"))
            log.info("update [%s]: no pairwise data", ref.id)
            continue
        except Exception as exc:  # noqa: BLE001 - isolate per-review failures
            report.results.append(ReviewUpdate(ref.id, "failed", error=str(exc)))
            log.warning("update [%s]: extraction failed: %s", ref.id, exc)
            continue
        try:
            res = repo.add_dataset(
                ref.id, trials, doi=ref.doi, title=ref.title, pub_date=ref.pub_date,
                measure=ref.measure, added_at=timestamp,
            )
        except Exception as exc:  # noqa: BLE001 - a write/ledger error on one review
            # (disk full, permission, lock timeout, corrupt manifest) must not abort
            # the whole batch; record it and move on.
            report.results.append(ReviewUpdate(ref.id, "failed", error=str(exc)))
            log.warning("update [%s]: append failed: %s", ref.id, exc)
            continue
        status = "failed" if res.status == "error" else res.status
        report.results.append(ReviewUpdate(ref.id, status, n_studies=res.n_studies,
                                           error=res.error))
        if res.status == "added":
            log.info("update [%s]: added (%d studies)", ref.id, res.n_studies)

    log.info("update finished: +%d added, %d skipped, %d no-data, %d failed",
             report.n_added, report.n_skipped, report.n_no_data, report.n_failed)
    return report


def commit_and_push(
    repo: Pairwise70Repo, report: UpdateReport, push: bool = False,
    runner=subprocess.run,
) -> Optional[str]:
    """Stage Beast's additions, commit, and optionally push (fast-forward only).

    Returns the commit message on success, or ``None`` if there was nothing to
    commit. Only the files Beast added (manifest + new CSV/.rda) are staged -- the
    original dataset is never touched. Idempotent: a no-addition report is a no-op.
    """
    if report.n_added == 0:
        return None
    files = repo.added_files()
    msg = (f"Beast auto-update: add {report.n_added} new Cochrane dataset(s) "
           f"[{', '.join(report.added_ids[:8])}{'...' if report.n_added > 8 else ''}]")

    def git(*args):
        proc = runner(["git", "-C", repo.root, *args], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {proc.stderr.strip()[:300]}")
        return proc

    git("add", *files)
    # Nothing staged (e.g. files already committed) -> no-op.
    status = git("status", "--porcelain")
    if not status.stdout.strip():
        return None
    git("commit", "-m", msg)
    if push:
        git("push", "origin", "HEAD")
    log.info("committed %d dataset(s) to Pairwise70%s", report.n_added,
             " and pushed" if push else "")
    return msg
