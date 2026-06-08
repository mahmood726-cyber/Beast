"""Crossref feed parsing and ProcessExtractor data-rows parsing (offline)."""

import os

import pytest

from beast.ingest.base import ReviewRef
from beast.ingest.cochrane import CrossrefCochraneFeed, ProcessExtractor


def _crossref_page(items, next_cursor=None):
    msg = {"items": items}
    if next_cursor:
        msg["next-cursor"] = next_cursor
    return {"message": msg}


def test_crossref_feed_parses_items(monkeypatch):
    items = [
        {"DOI": "10.1002/14651858.CD900100.pub2", "title": ["A new review"],
         "published": {"date-parts": [[2026, 5, 1]]}},
        {"DOI": "10.1002/14651858.ED000150", "title": ["An editorial"]},  # not a CD review
        {"DOI": "10.1002/14651858.CD900101", "title": ["Another"],
         "issued": {"date-parts": [[2026]]}},
    ]
    feed = CrossrefCochraneFeed()
    monkeypatch.setattr(feed, "_http_get_json", lambda url, params: _crossref_page(items))
    refs = feed.list_reviews(since="2026-01-01")
    ids = [r.id for r in refs]
    assert ids == ["CD900100_pub2", "CD900101"]
    assert refs[0].pub_date == "2026-05-01"
    assert refs[0].title == "A new review"


def test_crossref_feed_paginates(monkeypatch):
    pages = [
        _crossref_page([{"DOI": "10.1002/14651858.CD900200.pub1"}], next_cursor="c2"),
        _crossref_page([{"DOI": "10.1002/14651858.CD900201.pub1"}], next_cursor="c3"),
        _crossref_page([]),  # empty page stops pagination
    ]
    feed = CrossrefCochraneFeed()
    seq = iter(pages)
    monkeypatch.setattr(feed, "_http_get_json", lambda url, params: next(seq))
    refs = feed.list_reviews()
    assert {r.id for r in refs} == {"CD900200_pub1", "CD900201_pub1"}


def test_crossref_feed_fails_closed(monkeypatch):
    feed = CrossrefCochraneFeed(max_retries=1)

    def boom(url, params):
        raise RuntimeError("crossref down")

    monkeypatch.setattr(feed, "_http_get_json", boom)
    with pytest.raises(RuntimeError):
        feed.list_reviews()


def test_process_extractor_parses_cochrane_data_rows(tmp_path):
    # Simulate the real extractor: a command that writes a Cochrane data-rows CSV.
    out_csv = tmp_path / "out.csv"
    rows = (
        "Study,Study.year,Experimental.cases,Experimental.N,Control.cases,Control.N\n"
        "Trial A,2020,5,100,9,100\n"
        "Trial B,2022,7,120,12,120\n"
    )

    script = tmp_path / "fake_extractor.py"
    script.write_text(
        "import sys, shutil\n"
        f"open(sys.argv[sys.argv.index('--out')+1],'w',encoding='utf-8').write({rows!r})\n",
        encoding="utf-8",
    )
    ext = ProcessExtractor(["python", str(script), "--doi", "{doi}", "--out", "{out}"])
    trials = ext.extract(ReviewRef.from_doi("10.1002/14651858.CD900300.pub1"))
    assert len(trials) == 2
    assert trials[0].study == "Trial A" and trials[0].year == 2020
    assert trials[0].e_events == 5 and trials[0].c_n == 100


def test_process_extractor_requires_out_placeholder():
    # A template that never writes to {out} would silently yield no-data for every
    # review; fail fast instead.
    with pytest.raises(ValueError):
        ProcessExtractor(["python", "extract.py", "--doi", "{doi}"])
    with pytest.raises(ValueError):
        ProcessExtractor([])


def test_process_extractor_no_data_raises(tmp_path):
    from beast.ingest.base import NoDataError
    # Command that writes nothing -> NoDataError.
    script = tmp_path / "empty.py"
    script.write_text("import sys\n", encoding="utf-8")
    ext = ProcessExtractor(["python", str(script), "{out}"])
    with pytest.raises(NoDataError):
        ext.extract(ReviewRef.from_doi("10.1002/14651858.CD900301.pub1"))
