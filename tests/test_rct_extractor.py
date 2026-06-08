"""rct_extractor source adapter (issue #1) -- fully offline.

The external rct-extractor-v2 engine is not a test dependency; the single call
into it (:meth:`RctExtractorSource._extract_trial_dicts`) is monkeypatched so the
corpus loading, Trial materialisation, ``as_of_year`` filtering and fail-closed
contract are all exercised without any network or third-party install.
"""

import json

import pytest

from beast.sources.base import TopicSpec, get_source
from beast.sources.rct_extractor import RctExtractorSource


def _topic(params, measure="OR"):
    return TopicSpec(id="rx", title="rx", source="rct_extractor",
                     measure=measure, params=params)


# Trial-shaped dicts, as rct_extractor.integrations.beast.to_beast_trials returns.
_DICTS = [
    {"study": "Trial A", "year": 2018, "e_events": 5, "e_n": 100, "c_events": 9, "c_n": 100},
    {"study": "Trial B", "year": 2021, "e_events": 7, "e_n": 120, "c_events": 12, "c_n": 120},
    {"study": "Trial C", "year": 2023, "yi": -0.25, "sei": 0.12},  # generic effect
]


def test_registered_in_source_registry():
    src = get_source("rct_extractor")
    assert isinstance(src, RctExtractorSource)


def test_fetch_materialises_trials(monkeypatch, tmp_path):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps([
        {"study": "Trial A", "text": "...", "year": 2018},
        {"study": "Trial B", "text": "...", "year": 2021},
        {"study": "Trial C", "text": "...", "year": 2023},
    ]), encoding="utf-8")
    captured = {}

    def fake_extract(records, specialty, endpoint, measure):
        captured["records"] = records
        captured["specialty"] = specialty
        captured["measure"] = measure
        return _DICTS

    src = RctExtractorSource()
    monkeypatch.setattr(src, "_extract_trial_dicts", staticmethod(fake_extract))
    trials = src.fetch(_topic({"corpus": str(corpus), "specialty": "diabetes"}))
    assert len(trials) == 3
    assert {t.study for t in trials} == {"Trial A", "Trial B", "Trial C"}
    assert captured["specialty"] == "diabetes"
    assert captured["measure"] == "OR"
    assert len(captured["records"]) == 3


def test_fetch_honours_as_of_year(monkeypatch, tmp_path):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps([{"study": "x", "text": "t", "year": 2018}]), encoding="utf-8")
    src = RctExtractorSource()
    monkeypatch.setattr(src, "_extract_trial_dicts",
                        staticmethod(lambda *a, **k: _DICTS))
    trials = src.fetch(_topic({"corpus": str(corpus)}), as_of_year=2021)
    assert {t.study for t in trials} == {"Trial A", "Trial B"}  # 2023 excluded


def test_fetch_reads_txt_folder(monkeypatch, tmp_path):
    (tmp_path / "Trial A.txt").write_text("abstract A", encoding="utf-8")
    (tmp_path / "Trial B.txt").write_text("abstract B", encoding="utf-8")
    seen = {}

    def fake_extract(records, specialty, endpoint, measure):
        seen["studies"] = sorted(r["study"] for r in records)
        seen["texts"] = sorted(r["text"] for r in records)
        return _DICTS[:2]

    src = RctExtractorSource()
    monkeypatch.setattr(src, "_extract_trial_dicts", staticmethod(fake_extract))
    trials = src.fetch(_topic({"corpus": str(tmp_path)}))
    assert seen["studies"] == ["Trial A", "Trial B"]
    assert seen["texts"] == ["abstract A", "abstract B"]
    assert len(trials) == 2


def test_fetch_requires_corpus():
    with pytest.raises(ValueError):
        RctExtractorSource().fetch(_topic({}))


def test_fetch_fails_closed_on_empty_corpus(monkeypatch, tmp_path):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(ValueError):
        RctExtractorSource().fetch(_topic({"corpus": str(corpus)}))


def test_fetch_fails_closed_on_no_poolable_trials(monkeypatch, tmp_path):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps([{"study": "x", "text": "t"}]), encoding="utf-8")
    src = RctExtractorSource()
    # Engine returns dicts with no usable effect data -> empty pool -> raise.
    monkeypatch.setattr(src, "_extract_trial_dicts",
                        staticmethod(lambda *a, **k: [{"study": "header only"}]))
    with pytest.raises(ValueError):
        src.fetch(_topic({"corpus": str(corpus)}))


def test_missing_corpus_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        RctExtractorSource().fetch(_topic({"corpus": str(tmp_path / "nope.json")}))


def test_engine_absent_raises_helpful_importerror(tmp_path):
    # rct_extractor is not a test dependency; the real boundary must raise a clear
    # ImportError (never silently return an empty pool).
    with pytest.raises(ImportError):
        RctExtractorSource._extract_trial_dicts(
            [{"study": "x", "text": "t"}], "auto", None, "OR")
