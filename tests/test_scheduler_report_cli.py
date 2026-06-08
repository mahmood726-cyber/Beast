"""Scheduler loop, report rendering (no placeholder leaks), and CLI smoke."""

import re
import threading

import pytest

from beast.cli import main
from beast.report import build_report_data, write_html
from beast.scheduler import run_loop
from beast.store import BeastStore


# --- scheduler --------------------------------------------------------
def test_run_loop_respects_max_runs():
    calls = []
    run_loop(lambda: calls.append(1), interval_seconds=0.01, max_runs=3,
             sleep_fn=lambda s: None)
    assert len(calls) == 3


def test_run_loop_continues_after_run_error():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")

    n = run_loop(flaky, interval_seconds=0.01, max_runs=3, sleep_fn=lambda s: None)
    assert n == 3  # loop survived the first run's exception


def test_run_loop_stops_on_event():
    ev = threading.Event()
    ev.set()
    n = run_loop(lambda: None, interval_seconds=0.01, stop_event=ev, sleep_fn=lambda s: None)
    assert n == 0


def test_run_loop_rejects_nonpositive_interval():
    with pytest.raises(ValueError):
        run_loop(lambda: None, interval_seconds=0)


# --- report -----------------------------------------------------------
def test_html_report_has_no_placeholder_leak(tmp_path, sample_csv):
    from beast.sources.base import TopicSpec
    from beast.tracker import backfill_topic

    store = BeastStore(str(tmp_path / "t.db"))
    topic = TopicSpec(id="htn", title="HTN", source="pairwise70", measure="OR",
                      params={"csv": sample_csv}, notes="real sample")
    backfill_topic(store, topic, [1986, 1991, 1993])
    out = write_html(store, str(tmp_path / "index.html"), "2026-06-08T00:00:00Z")
    html = open(out, encoding="utf-8").read()
    store.close()
    # Lessons.md placeholder-leak guards: no bare Python None / NaN / undefined,
    # no URL ending in /None, no unfilled template token.
    for pattern in (r"\bNone\b", r"/None", r"\bNaN\b participants", r"__BEAST_DATA__",
                    r"\bundefined\b", r"\{\{"):
        assert not re.search(pattern, html), f"placeholder leak: {pattern}"
    assert "const DATA" in html and "trendChart" in html


def test_build_report_data_structure(tmp_path, sample_csv):
    from beast.sources.base import TopicSpec
    from beast.tracker import backfill_topic

    store = BeastStore(str(tmp_path / "t.db"))
    topic = TopicSpec(id="htn", title="HTN", source="pairwise70", measure="OR",
                      params={"csv": sample_csv})
    backfill_topic(store, topic, [1986, 1991, 1993])
    data = build_report_data(store, "2026-06-08T00:00:00Z")
    store.close()
    assert data["topics"][0]["topic"]["id"] == "htn"
    assert len(data["topics"][0]["history"]) >= 3
    assert data["topics"][0]["latest"]["k"] == 9


# --- CLI smoke (real, offline) ----------------------------------------
def test_cli_init_backfill_history_report(tmp_home, capsys):
    assert main(["--home", tmp_home, "init"]) == 0
    assert main(["--home", tmp_home, "backfill", "--topic", "htn-elderly-mortality",
                 "--years", "1986,1991,1993", "--no-log-file", "--no-report"]) == 0
    assert main(["--home", tmp_home, "history", "--topic", "htn-elderly-mortality"]) == 0
    out = capsys.readouterr().out
    assert "SIG" in out  # the trend reaches significance
    assert main(["--home", tmp_home, "report"]) == 0
    assert main(["--home", tmp_home, "list"]) == 0


def test_cli_run_idempotent(tmp_home):
    assert main(["--home", tmp_home, "init"]) == 0
    assert main(["--home", tmp_home, "run", "--no-log-file", "--no-report"]) == 0
    # second run: source unchanged -> exit 0, no crash
    assert main(["--home", tmp_home, "run", "--no-log-file", "--no-report"]) == 0


def test_cli_update_discover_only(tmp_home, tmp_path, monkeypatch, capsys):
    """`beast update` in discover-only mode lists new ids and writes nothing."""
    # Seed a tiny fake Pairwise70 dataset.
    pw = tmp_path / "Pairwise70"
    (pw / "data").mkdir(parents=True)
    (pw / "data" / "CD000028_pub4_data.rda").write_text("ORIGINAL", encoding="utf-8")

    # Mock the Crossref feed so no network is touched.
    import beast.cli as cli
    from beast.ingest.base import ReviewRef
    from beast.ingest.cochrane import CrossrefCochraneFeed

    def fake_list(self, since=None):
        return [ReviewRef.from_doi("10.1002/14651858.CD000028.pub4"),   # existing
                ReviewRef.from_doi("10.1002/14651858.CD900900.pub1")]   # new

    monkeypatch.setattr(CrossrefCochraneFeed, "list_reviews", fake_list)
    rc = cli.main(["--home", tmp_home, "update", "--pairwise70", str(pw), "--no-log-file"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "discover-only" in out
    assert "CD900900_pub1" in out          # the new review is surfaced
    # Discover-only must not write any dataset files.
    assert not (pw / "beast_manifest.json").exists()


def test_cli_update_requires_pairwise70(tmp_home):
    # --pairwise70 is required for `update`; argparse exits (SystemExit) without it.
    with pytest.raises(SystemExit):
        main(["--home", tmp_home, "update", "--no-log-file"])
