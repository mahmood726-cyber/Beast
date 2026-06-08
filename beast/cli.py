"""Command-line interface: ``beast <command>``.

Commands
--------
* ``init``      create the home dir + a starter topics.json (real Cochrane sample)
* ``add``       add / update a tracked topic
* ``list``      list tracked topics and their latest pooled state
* ``run``       fetch, recompute, snapshot and diff every topic once
* ``backfill``  reconstruct a Pairwise70 topic's historical trend (as-of-year)
* ``loop``      self-running scheduler: ``run`` every N seconds
* ``history``   print a topic's snapshot trend
* ``report``    (re)write the JSON + HTML dashboards
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from typing import Optional

from beast import __version__
from beast.config import Paths, default_topics, load_topics, paths, save_topics
from beast.logging_setup import configure_logging
from beast.report import write_html, write_json
from beast.sources.base import TopicSpec
from beast.store import BeastStore
from beast.tracker import backfill_topic, run_once, utc_now_iso

_FIXTURE = os.path.join(os.path.dirname(__file__), os.pardir, "tests", "fixtures",
                        "sample_pairwise70.csv")


def _store(p: Paths) -> BeastStore:
    p.ensure()
    return BeastStore(p.db)


def _emit_reports(store: BeastStore, p: Paths, ts: str) -> tuple[str, str]:
    jp = write_json(store, os.path.join(p.reports, "beast.json"), ts)
    hp = write_html(store, os.path.join(p.reports, "index.html"), ts)
    return jp, hp


def cmd_init(args) -> int:
    p = paths(args.home).ensure()
    if os.path.exists(p.topics) and not args.force:
        print(f"topics already exist at {p.topics} (use --force to overwrite)")
        return 0
    sample_dest = os.path.join(p.home, "sample_pairwise70.csv")
    fixture = os.path.abspath(_FIXTURE)
    if os.path.exists(fixture):
        shutil.copyfile(fixture, sample_dest)
        topics = default_topics(sample_dest)
    else:  # pragma: no cover - fixture always shipped
        topics = default_topics(os.path.abspath(_FIXTURE))
    save_topics(p.topics, topics)
    with _store(p) as store:
        for t in topics:
            store.upsert_topic(t, utc_now_iso())
    print(f"initialised Beast home at {p.home}")
    print(f"  topics:  {p.topics} ({len(topics)} starter topic)")
    print(f"  db:      {p.db}")
    print("next: `beast backfill --topic htn-elderly-mortality` then open the dashboard "
          "(`beast report`).")
    return 0


def cmd_add(args) -> int:
    p = paths(args.home).ensure()
    topics = load_topics(p.topics) if os.path.exists(p.topics) else []
    params = json.loads(args.params) if args.params else {}
    topic = TopicSpec(id=args.id, title=args.title, source=args.source,
                      measure=args.measure, method=args.method, params=params,
                      notes=args.notes or "")
    topics = [t for t in topics if t.id != topic.id] + [topic]
    save_topics(p.topics, topics)
    with _store(p) as store:
        store.upsert_topic(topic, utc_now_iso())
    print(f"added/updated topic {topic.id!r} ({topic.source}/{topic.measure})")
    return 0


def cmd_list(args) -> int:
    p = paths(args.home)
    if not os.path.exists(p.db):
        print("no Beast home yet; run `beast init`.")
        return 1
    with _store(p) as store:
        topics = store.list_topics()
        if not topics:
            print("no topics tracked.")
            return 0
        for t in topics:
            latest = store.latest_snapshot(t.id)
            if latest:
                s = latest[1]
                est = s.natural.get("estimate")
                print(f"  {t.id:<28} {t.measure} k={s.k:<3} "
                      f"{est:.3f} [{s.natural.get('ci_low'):.3f},{s.natural.get('ci_high'):.3f}] "
                      f"I2={s.i2:.0f}% {'SIG' if s.significant else 'ns'}  {t.title}")
            else:
                print(f"  {t.id:<28} {t.measure} (no snapshots yet)  {t.title}")
    return 0


def _run_update_step(args) -> Optional[object]:
    """Run the Pairwise70 auto-update if --pairwise70 is configured.

    With an extractor command it appends new datasets; without one it runs in
    safe discover-only mode (lists what is new, writes nothing). Returns the
    UpdateReport (or a discover-only summary dict), or None if not configured.
    """
    if not getattr(args, "pairwise70", None):
        return None
    from beast.ingest.cochrane import CrossrefCochraneFeed, ProcessExtractor
    from beast.pairwise70_repo import Pairwise70Repo
    from beast.updater import commit_and_push, update_pairwise70

    repo = Pairwise70Repo(args.pairwise70)
    feed = CrossrefCochraneFeed()
    ts = utc_now_iso()

    if not getattr(args, "extractor_cmd", None):
        # Discover-only: never writes. Reports new ids for the operator.
        refs = feed.list_reviews(since=getattr(args, "since", None))
        new = [r for r in refs if not repo.has(r.id)]
        print(f"discover-only: {len(refs)} review(s) in feed, {len(new)} new, "
              f"{repo.count_existing_rda()} already in dataset "
              f"(pass --extractor-cmd to append them)")
        for r in new[: getattr(args, "limit", None) or 20]:
            print(f"  NEW {r.id:<16} {r.pub_date or '????':<10} {r.title[:70]}")
        return {"discover_only": True, "new": [r.id for r in new]}

    extractor = ProcessExtractor(shlex.split(args.extractor_cmd), cwd=getattr(args, "extractor_cwd", None))
    report = update_pairwise70(repo, feed, extractor, since=getattr(args, "since", None),
                               limit=getattr(args, "limit", None), timestamp=ts)
    print(f"pairwise70 update: +{report.n_added} added, {report.n_skipped} skipped, "
          f"{report.n_no_data} no-data, {report.n_failed} failed")
    if report.n_added and (getattr(args, "commit", False) or getattr(args, "push", False)):
        msg = commit_and_push(repo, report, push=getattr(args, "push", False))
        if msg:
            print(f"  committed{' + pushed' if getattr(args, 'push', False) else ''}: {msg}")
    return report


def cmd_update(args) -> int:
    p = paths(args.home).ensure()
    configure_logging(args.log_level, None if args.no_log_file else p.log)
    if not args.pairwise70:
        print("error: --pairwise70 PATH is required (a local clone of the Pairwise70 dataset)")
        return 1
    _run_update_step(args)
    return 0


def cmd_run(args) -> int:
    p = paths(args.home).ensure()
    configure_logging(args.log_level, None if args.no_log_file else p.log)
    # Living-updater core: refresh the Pairwise70 dataset first (if configured),
    # so trend-tracking runs on the most current evidence.
    try:
        _run_update_step(args)
    except Exception as exc:  # noqa: BLE001 - update must not block tracking
        print(f"  ! pairwise70 update step failed (continuing to track): {exc}")
    topics = load_topics(p.topics) if os.path.exists(p.topics) else []
    if not topics:
        print("no topics to run; add some with `beast add` or `beast init`.")
        return 1
    with _store(p) as store:
        report = run_once(store, topics)
        ts = report.timestamp
        if not args.no_report:
            _emit_reports(store, p, ts)
    print(f"run @ {report.timestamp}: {report.n_ok} ok, {report.n_failed} failed, "
          f"{report.n_with_changes} topic(s) with new changes")
    for r in report.results:
        if not r.ok:
            print(f"  ! {r.topic_id}: {r.error}")
        elif r.n_changes:
            print(f"  * {r.topic_id}: {r.n_changes} change(s) [{','.join(r.severities)}]")
    return 0 if report.n_failed == 0 else 2


def cmd_backfill(args) -> int:
    p = paths(args.home).ensure()
    configure_logging(args.log_level, None if args.no_log_file else p.log)
    topics = load_topics(p.topics)
    topic = next((t for t in topics if t.id == args.topic), None)
    if topic is None:
        print(f"unknown topic {args.topic!r}")
        return 1
    if args.years:
        years = [int(y) for y in args.years.split(",")]
    else:
        years = list(range(args.start, args.end + 1))
    with _store(p) as store:
        report = backfill_topic(store, topic, years)
        if not args.no_report:
            _emit_reports(store, p, report.timestamp)
    inserted = sum(1 for r in report.results if r.inserted)
    changed = sum(r.n_changes for r in report.results)
    print(f"backfill {topic.id}: {inserted} distinct snapshot(s) across "
          f"{len(years)} year(s), {changed} change(s) flagged")
    return 0


def cmd_loop(args) -> int:
    p = paths(args.home).ensure()
    configure_logging(args.log_level, None if args.no_log_file else p.log)
    from beast.scheduler import run_loop

    def one_run():
        try:
            _run_update_step(args)
        except Exception as exc:  # noqa: BLE001 - update must not kill the loop
            print(f"  ! pairwise70 update step failed (continuing to track): {exc}")
        topics = load_topics(p.topics) if os.path.exists(p.topics) else []
        with _store(p) as store:
            rep = run_once(store, topics)
            if not args.no_report:
                _emit_reports(store, p, rep.timestamp)
        return rep

    print(f"starting Beast scheduler (every {args.interval}s; Ctrl-C to stop)")
    run_loop(one_run, interval_seconds=args.interval, max_runs=args.max_runs)
    return 0


def cmd_history(args) -> int:
    p = paths(args.home)
    with _store(p) as store:
        hist = store.history(args.topic)
        if not hist:
            print(f"no history for {args.topic!r}")
            return 1
        print(f"trend for {args.topic} ({len(hist)} snapshot(s)):")
        for s in hist:
            tag = s.as_of_year if s.as_of_year is not None else s.timestamp[:10]
            print(f"  {str(tag):<12} k={s.k:<3} {s.natural.get('estimate'):.3f} "
                  f"[{s.natural.get('ci_low'):.3f},{s.natural.get('ci_high'):.3f}] "
                  f"I2={s.i2:5.1f}% tau2={s.tau2:.4f} {'SIG' if s.significant else 'ns'}")
    return 0


def cmd_report(args) -> int:
    p = paths(args.home).ensure()
    with _store(p) as store:
        ts = utc_now_iso()
        jp, hp = _emit_reports(store, p, ts)
    print(f"wrote {jp}\nwrote {hp}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="beast",
        description="Self-running surveillance of how meta-analysis evidence evolves over time.")
    ap.add_argument("--version", action="version", version=f"beast {__version__}")
    ap.add_argument("--home", default=None, help="Beast data dir (default: $BEAST_HOME or ./beast_data)")

    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--log-level", default="INFO")
        sp.add_argument("--no-log-file", action="store_true")
        sp.add_argument("--no-report", action="store_true", help="skip writing JSON/HTML reports")

    def update_flags(sp, required=False):
        sp.add_argument("--pairwise70", required=required,
                        help="path to a local clone of the Pairwise70 dataset to auto-update")
        sp.add_argument("--since", help="only consider reviews published on/after this date (YYYY-MM-DD)")
        sp.add_argument("--limit", type=int, help="cap how many new reviews to append per run")
        sp.add_argument("--extractor-cmd",
                        help="command to extract a review's data-rows CSV; placeholders "
                             "{doi} {review_id} {out}. Omit for safe discover-only mode.")
        sp.add_argument("--extractor-cwd", help="working dir for the extractor command")
        sp.add_argument("--commit", action="store_true", help="git-commit appended datasets")
        sp.add_argument("--push", action="store_true", help="git push appended datasets (implies commit)")

    sp = sub.add_parser("init", help="create home dir + starter topic"); sp.set_defaults(func=cmd_init)
    sp.add_argument("--force", action="store_true")

    sp = sub.add_parser("add", help="add/update a tracked topic"); sp.set_defaults(func=cmd_add)
    sp.add_argument("--id", required=True); sp.add_argument("--title", required=True)
    sp.add_argument("--source", required=True, choices=["pairwise70", "europepmc"])
    sp.add_argument("--measure", default="OR", choices=["OR", "RR", "MD", "SMD", "GEN"])
    sp.add_argument("--method", default="REML", choices=["REML", "PM", "DL"])
    sp.add_argument("--params", help="JSON params, e.g. '{\"csv\":\"data.csv\"}'")
    sp.add_argument("--notes", default="")

    sp = sub.add_parser("list", help="list tracked topics"); sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("update", help="auto-update the Pairwise70 dataset with new Cochrane reviews")
    common(sp); update_flags(sp, required=True); sp.set_defaults(func=cmd_update)

    sp = sub.add_parser("run", help="(optionally update Pairwise70 then) run all topics once")
    common(sp); update_flags(sp); sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("backfill", help="reconstruct a topic's historical trend")
    common(sp); sp.set_defaults(func=cmd_backfill)
    sp.add_argument("--topic", required=True)
    sp.add_argument("--start", type=int, default=1970); sp.add_argument("--end", type=int, default=2025)
    sp.add_argument("--years", help="explicit comma list, e.g. 1986,1991,1993 (overrides start/end)")

    sp = sub.add_parser("loop", help="self-running scheduler (update + track each tick)")
    common(sp); update_flags(sp); sp.set_defaults(func=cmd_loop)
    sp.add_argument("--interval", type=float, default=86400.0, help="seconds between runs (default 1 day)")
    sp.add_argument("--max-runs", type=int, default=None)

    sp = sub.add_parser("history", help="print a topic's trend"); sp.set_defaults(func=cmd_history)
    sp.add_argument("--topic", required=True)

    sp = sub.add_parser("report", help="(re)write JSON + HTML dashboards"); sp.set_defaults(func=cmd_report)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
