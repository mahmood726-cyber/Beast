"""Pairwise70 (CSV path) and EuropePMC (mocked) source behaviour."""

import math

import pytest

from beast.effects import compute_effects
from beast.meta import meta_analyze
from beast.sources.base import TopicSpec, get_source
from beast.sources.europepmc import EuropePmcSource, _parse_effect


# --- Pairwise70 (offline CSV) -----------------------------------------
def _topic(sample_csv):
    return TopicSpec(id="htn", title="HTN", source="pairwise70", measure="OR",
                     params={"csv": sample_csv})


def test_pairwise70_loads_real_sample(sample_csv):
    src = get_source("pairwise70")
    trials = src.fetch(_topic(sample_csv))
    assert len(trials) == 9  # deduped real CD000028 subgroup
    assert all(t.year is not None for t in trials)


def test_pairwise70_as_of_year_is_cumulative(sample_csv):
    src = get_source("pairwise70")
    assert len(src.fetch(_topic(sample_csv), as_of_year=1986)) == 4
    assert len(src.fetch(_topic(sample_csv), as_of_year=1991)) == 7
    assert len(src.fetch(_topic(sample_csv), as_of_year=1993)) == 9
    # Earlier year is a subset of later year.
    early = {t.study for t in src.fetch(_topic(sample_csv), as_of_year=1986)}
    late = {t.study for t in src.fetch(_topic(sample_csv), as_of_year=1991)}
    assert early.issubset(late)


def test_pairwise70_cumulative_pool_matches_metafor_gold(sample_csv, gold):
    src = get_source("pairwise70")
    g = gold["pairwise70_sample"]
    for yr, key in [(1986, "as_of_1986"), (1991, "as_of_1991"), (1993, "as_of_1993")]:
        used, yi, vi = compute_effects(src.fetch(_topic(sample_csv), as_of_year=yr), "OR")
        r = meta_analyze(yi, vi, method="REML", log_scale=True)
        assert r.k == g[key]["k"]
        assert r.estimate == pytest.approx(g[key]["est"], abs=1e-4)
        assert r.significant == g[key]["significant"]


def test_pairwise70_requires_csv_or_rda():
    src = get_source("pairwise70")
    with pytest.raises(ValueError):
        src.fetch(TopicSpec(id="x", title="x", source="pairwise70", params={}))


# --- EuropePMC effect parser ------------------------------------------
@pytest.mark.parametrize("text,point", [
    ("benefit (OR 0.82, 95% CI 0.70-0.96)", 0.82),
    ("Hazard ratio 0.75 (95% CI 0.60 to 0.93)", 0.75),
    ("RR 1.20, 95%CI 1.05-1.38 for the outcome", 1.20),
])
def test_parse_effect_extracts_log_and_se(text, point):
    yi, sei = _parse_effect(text)
    assert yi == pytest.approx(math.log(point), abs=1e-9)
    assert sei > 0


@pytest.mark.parametrize("text", [
    "", "No quantitative estimate reported.",
    "OR 0.9 with wide uncertainty",            # no CI
    "OR 0.0, 95% CI 0.0-0.0",                   # degenerate
])
def test_parse_effect_returns_none_when_absent(text):
    assert _parse_effect(text) is None


# --- EuropePMC fetch (mocked transport) -------------------------------
def _mock_payload():
    return {"resultList": {"result": [
        {"id": "10", "pubYear": "2019", "authorString": "Smith J, et al",
         "title": "Trial A", "abstractText": "Lower mortality (HR 0.75, 95% CI 0.60 to 0.93)."},
        {"id": "11", "pubYear": "2021", "authorString": "Lee K",
         "title": "Trial B", "abstractText": "No effect estimate stated."},
    ]}, "nextCursorMark": None}


def test_europepmc_fetch_maps_records(monkeypatch):
    src = EuropePmcSource()
    monkeypatch.setattr(src, "_http_get_json", lambda url, params: _mock_payload())
    topic = TopicSpec(id="t", title="t", source="europepmc", measure="GEN",
                      params={"query": "drug AND mortality"})
    trials = src.fetch(topic)
    assert len(trials) == 2
    with_effect = [t for t in trials if t.yi is not None]
    assert len(with_effect) == 1
    assert with_effect[0].year == 2019


def test_europepmc_as_of_year_added_to_query(monkeypatch):
    captured = {}

    def spy(url, params):
        captured["query"] = params["query"]
        return {"resultList": {"result": []}, "nextCursorMark": None}

    src = EuropePmcSource()
    monkeypatch.setattr(src, "_http_get_json", spy)
    src.fetch(TopicSpec(id="t", title="t", source="europepmc", params={"query": "aspirin"}),
              as_of_year=2010)
    assert "PUB_YEAR:[1900 TO 2010]" in captured["query"]
    assert "Randomized Controlled Trial" in captured["query"]


def test_europepmc_requires_query():
    with pytest.raises(ValueError):
        EuropePmcSource().fetch(TopicSpec(id="t", title="t", source="europepmc", params={}))


def test_europepmc_fails_closed_on_transport_error(monkeypatch):
    src = EuropePmcSource(max_retries=2)

    def boom(url, params, **kw):
        raise RuntimeError("network down")

    # Patch the real network method; fetch must propagate, not return [].
    monkeypatch.setattr(src, "_http_get_json", boom)
    with pytest.raises(RuntimeError):
        src.fetch(TopicSpec(id="t", title="t", source="europepmc", params={"query": "x"}))
