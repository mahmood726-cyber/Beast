"""Pairwise70 source -- real Cochrane trial-level data, offline.

Reads either:

* a tidy CSV (columns ``study, year`` plus a data shape: binary
  ``e_events,e_n,c_events,c_n``; continuous ``e_mean,e_sd,e_n,c_mean,c_sd,c_n``;
  or generic ``yi,sei``), or
* a Cochrane ``*.rda`` file from the Pairwise70 dataset
  (https://github.com/mahmood726-cyber/Pairwise70), filtered to one analysis /
  subgroup, using the dataset's native column names.

Because every trial carries a publication ``year``, this source supports
``as_of_year``: ask for "the evidence as it stood in 1991" and Beast reconstructs
the cumulative meta-analysis from real data -- the backbone of its surveillance.

``pyreadr`` is imported lazily and only needed for the ``.rda`` path; the CSV
path (and the bundled tests) need nothing beyond the standard library.
"""

from __future__ import annotations

import csv
from typing import Optional

from beast.effects import Trial
from beast.sources.base import Source, TopicSpec, register_source

# Mapping from Pairwise70 (Cochrane RevMan) column names to Trial fields.
_RDA_BINARY = {
    "e_events": "Experimental.cases", "e_n": "Experimental.N",
    "c_events": "Control.cases", "c_n": "Control.N",
}
_RDA_CONT = {
    "e_mean": "Experimental.mean", "e_sd": "Experimental.SD", "e_n": "Experimental.N",
    "c_mean": "Control.mean", "c_sd": "Control.SD", "c_n": "Control.N",
}
_RDA_GENERIC = {"yi": "GIV.Mean", "sei": "GIV.SE"}


def _num(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        if v == "" or v.upper() in ("NA", "NAN", "NULL"):
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # pandas/pyreadr render missing numerics as NaN.
    return None if f != f else f


# Each Trial field may arrive under a tidy name or its Cochrane (RevMan) name.
_FIELD_ALIASES = {
    "e_events": ("e_events", "Experimental.cases"),
    "e_n": ("e_n", "Experimental.N"),
    "c_events": ("c_events", "Control.cases"),
    "c_n": ("c_n", "Control.N"),
    "e_mean": ("e_mean", "Experimental.mean"),
    "e_sd": ("e_sd", "Experimental.SD"),
    "c_mean": ("c_mean", "Control.mean"),
    "c_sd": ("c_sd", "Control.SD"),
    "yi": ("yi", "GIV.Mean"),
    "sei": ("sei", "GIV.SE"),
}


def _row_to_trial(row: dict) -> Optional[Trial]:
    """Build a Trial from a row dict using tidy *or* Cochrane column names."""
    study = (row.get("study") or row.get("Study") or "").strip()
    if not study:
        return None
    year = _num(row.get("year") or row.get("Study.year"))
    t = Trial(study=study, year=int(year) if year is not None else None)

    def g(key):  # value by tidy or Cochrane column name
        for name in _FIELD_ALIASES[key]:
            v = _num(row.get(name))
            if v is not None:
                return v
        return None

    t.e_events, t.e_n = g("e_events"), g("e_n")
    t.c_events, t.c_n = g("c_events"), g("c_n")
    t.e_mean, t.e_sd = g("e_mean"), g("e_sd")
    t.c_mean, t.c_sd = g("c_mean"), g("c_sd")
    t.yi, t.sei = g("yi"), g("sei")
    # Continuous arm Ns share the binary N columns in Cochrane data.
    t.e_n_cont, t.c_n_cont = t.e_n, t.c_n
    if not (t.has_binary() or t.has_continuous() or t.has_generic()):
        return None
    return t


def _dedupe(trials: list[Trial]) -> list[Trial]:
    """Drop exact-duplicate trial rows (Pairwise70 double-enters some studies)."""
    seen, out = set(), []
    for t in trials:
        key = (t.study, t.year, t.e_events, t.e_n, t.c_events, t.c_n,
               t.e_mean, t.e_sd, t.c_mean, t.c_sd, t.yi, t.sei)
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


@register_source
class Pairwise70Source(Source):
    name = "pairwise70"

    def fetch(self, topic: TopicSpec, as_of_year: Optional[int] = None) -> list[Trial]:
        params = topic.params or {}
        if params.get("csv"):
            trials = self._from_csv(params["csv"])
        elif params.get("rda"):
            trials = self._from_rda(
                params["rda"],
                analysis_number=params.get("analysis_number"),
                subgroup=params.get("subgroup"),
            )
        else:
            raise ValueError(
                f"topic {topic.id!r}: pairwise70 source needs params.csv or params.rda"
            )
        trials = _dedupe(trials)
        if as_of_year is not None:
            trials = [t for t in trials if t.year is not None and t.year <= as_of_year]
        return trials

    @staticmethod
    def _from_csv(path: str) -> list[Trial]:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        out = [_row_to_trial(r) for r in rows]
        trials = [t for t in out if t is not None]
        if not trials:
            raise ValueError(f"no usable trial rows in {path!r}")
        return trials

    @staticmethod
    def _from_rda(
        path: str, analysis_number=None, subgroup: Optional[str] = None
    ) -> list[Trial]:
        try:
            import pyreadr
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError(
                "reading Pairwise70 .rda files needs 'pyreadr' (pip install pyreadr); "
                "or point params.csv at a tidy CSV export instead"
            ) from exc
        result = pyreadr.read_r(path)
        if not result:
            raise ValueError(f"{path!r} contained no R objects")
        df = next(iter(result.values()))
        if analysis_number is not None and "Analysis.number" in df.columns:
            df = df[df["Analysis.number"] == analysis_number]
        if subgroup is not None and "Subgroup" in df.columns:
            df = df[df["Subgroup"].astype(str).str.strip() == str(subgroup).strip()]
        cols = set(df.columns)
        wanted = {"study": "Study", "year": "Study.year",
                  **_RDA_BINARY, **_RDA_CONT, **_RDA_GENERIC}
        trials = []
        for _, r in df.iterrows():
            row = {tidy: r[src] for tidy, src in wanted.items() if src in cols}
            t = _row_to_trial(row)
            if t is not None:
                trials.append(t)
        if not trials:
            raise ValueError(
                f"no usable trials in {path!r} for analysis={analysis_number} subgroup={subgroup!r}"
            )
        return trials
