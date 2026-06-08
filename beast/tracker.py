"""Orchestration: turn tracked topics into stored snapshots and change flags.

A *run* fetches each topic's current trials, computes a snapshot, stores it
idempotently, and -- when the content actually changed -- diffs against the
previous snapshot and records the flagged changes. A *backfill* replays a
Pairwise70 topic year-by-year to reconstruct the historical trend from real data
in a single command.

Every per-topic step is wrapped so one failing topic never aborts the whole run
(fail-closed and isolated): the error is logged and recorded in the run report.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import Optional

from beast.config import Paths
from beast.diff import DiffThresholds, diff_snapshots
from beast.snapshot import Snapshot, compute_snapshot
from beast.sources.base import Source, TopicSpec, get_source
from beast.store import BeastStore

log = logging.getLogger("beast.tracker")


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class TopicRunResult:
    topic_id: str
    ok: bool
    inserted: bool = False
    k: Optional[int] = None
    estimate_natural: Optional[float] = None
    significant: Optional[bool] = None
    n_changes: int = 0
    severities: list[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class RunReport:
    timestamp: str
    results: list[TopicRunResult] = field(default_factory=list)

    @property
    def n_ok(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def n_failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    @property
    def n_with_changes(self) -> int:
        return sum(1 for r in self.results if r.n_changes)


def _source_for(topic: TopicSpec, source_factory) -> Source:
    return source_factory(topic.source) if source_factory else get_source(topic.source)


def process_topic(
    store: BeastStore,
    topic: TopicSpec,
    timestamp: str,
    source_factory=None,
    thresholds: Optional[DiffThresholds] = None,
    as_of_year: Optional[int] = None,
) -> TopicRunResult:
    """Run one topic end-to-end. Never raises; failures are captured in the result."""
    try:
        store.upsert_topic(topic, timestamp)
        source = _source_for(topic, source_factory)
        trials = source.fetch(topic, as_of_year=as_of_year)
        snap = compute_snapshot(topic, trials, timestamp, as_of_year=as_of_year)

        prev = store.latest_snapshot(topic.id)
        snap_id, inserted = store.add_snapshot(snap)

        result = TopicRunResult(
            topic_id=topic.id, ok=True, inserted=inserted, k=snap.k,
            estimate_natural=snap.natural.get("estimate"), significant=snap.significant,
        )
        if inserted and prev is not None:
            diff = diff_snapshots(prev[1], snap, thresholds)
            if diff.has_changes:
                store.add_changes(diff, from_snapshot_id=prev[0], to_snapshot_id=snap_id)
                result.n_changes = len(diff.changes)
                result.severities = [c.severity for c in diff.changes]
                for ch in diff.changes:
                    log.info("[%s] %s change: %s", topic.id, ch.severity, ch.message)
        if inserted:
            log.info("[%s] snapshot stored: k=%d estimate=%.4f %s significant=%s",
                     topic.id, snap.k, result.estimate_natural or float("nan"),
                     snap.measure, snap.significant)
        else:
            log.info("[%s] no change since last snapshot (idempotent skip)", topic.id)
        return result
    except Exception as exc:  # noqa: BLE001 - isolate per-topic failures
        log.error("[%s] run failed: %s", topic.id, exc)
        return TopicRunResult(topic_id=topic.id, ok=False, error=str(exc))


def run_once(
    store: BeastStore,
    topics: list[TopicSpec],
    source_factory=None,
    thresholds: Optional[DiffThresholds] = None,
    timestamp: Optional[str] = None,
) -> RunReport:
    """Process every topic once and return a summary report."""
    ts = timestamp or utc_now_iso()
    report = RunReport(timestamp=ts)
    log.info("run started: %d topic(s) @ %s", len(topics), ts)
    for topic in topics:
        report.results.append(
            process_topic(store, topic, ts, source_factory, thresholds)
        )
    log.info("run finished: %d ok, %d failed, %d with changes",
             report.n_ok, report.n_failed, report.n_with_changes)
    return report


def backfill_topic(
    store: BeastStore,
    topic: TopicSpec,
    years: list[int],
    source_factory=None,
    thresholds: Optional[DiffThresholds] = None,
    base_timestamp: Optional[str] = None,
) -> RunReport:
    """Reconstruct a topic's historical trend by replaying ``as_of_year`` snapshots.

    Each year becomes its own snapshot (timestamped deterministically from the
    year) and is diffed against the prior year, so a single command yields the
    full evolution -- e.g. how the pooled estimate and significance changed as
    trials accumulated. Idempotent: re-running skips years whose content is
    unchanged from the previous stored snapshot.
    """
    base = base_timestamp or utc_now_iso()
    report = RunReport(timestamp=base)
    store.upsert_topic(topic, base)
    source = _source_for(topic, source_factory)
    log.info("backfill [%s]: replaying %d year(s)", topic.id, len(years))
    for yr in sorted(years):
        ts = f"{yr:04d}-12-31T00:00:00Z"
        try:
            trials = source.fetch(topic, as_of_year=yr)
        except Exception as exc:  # noqa: BLE001
            log.warning("backfill [%s] year %d skipped: %s", topic.id, yr, exc)
            report.results.append(TopicRunResult(topic_id=topic.id, ok=False, error=str(exc)))
            continue
        try:
            snap = compute_snapshot(topic, trials, ts, as_of_year=yr)
        except ValueError as exc:
            # No usable trials yet for this year -- expected early in a trend.
            log.info("backfill [%s] year %d: %s", topic.id, yr, exc)
            continue
        prev = store.latest_snapshot(topic.id)
        snap_id, inserted = store.add_snapshot(snap, dedupe="topic")
        res = TopicRunResult(
            topic_id=topic.id, ok=True, inserted=inserted, k=snap.k,
            estimate_natural=snap.natural.get("estimate"), significant=snap.significant,
        )
        if inserted and prev is not None:
            diff = diff_snapshots(prev[1], snap, thresholds)
            if diff.has_changes:
                store.add_changes(diff, prev[0], snap_id)
                res.n_changes = len(diff.changes)
                res.severities = [c.severity for c in diff.changes]
        report.results.append(res)
        log.info("backfill [%s] %d: k=%d estimate=%.4f significant=%s changes=%d",
                 topic.id, yr, snap.k, res.estimate_natural or float("nan"),
                 snap.significant, res.n_changes)
    return report
