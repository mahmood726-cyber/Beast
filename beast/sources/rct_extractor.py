"""rct_extractor source -- poolable trial effects from abstracts (issue #1).

This adapter wires Beast to the now pip-installable `rct-extractor-v2` engine
(https://github.com/mahmood726-cyber/rct-extractor-v2), which extracts trial
effects from free-text abstracts across 17 disease specialties. The heavy NLP
lives entirely in that external package; this adapter only:

* loads a *corpus* -- either a JSON list of ``{study, text, year?}`` records or a
  folder of ``*.txt`` files (one abstract per file),
* hands it to ``rct_extractor.integrations.beast.to_beast_trials(...)``, which
  returns Beast ``Trial``-shaped **dicts** (raw 2x2 counts ``e_events,e_n,
  c_events,c_n`` when available, else generic ``yi``/``sei`` already on the
  analysis scale -- log for ratios), and
* materialises those dicts into :class:`~beast.effects.Trial` objects, honouring
  ``as_of_year`` (cumulative reconstruction by publication year).

The external engine does **not** import ``beast`` (no reverse dependency), and is
imported *lazily* here, so Beast neither hard-depends on it nor fails to import
when it is absent -- ``pip install "git+https://github.com/mahmood726-cyber/rct-extractor-v2.git"``
only when you actually use this source.

Fail-closed: raises if the engine is not installed, the corpus is missing or
empty, or no poolable trial is produced -- never an empty pool that downstream
diffing could read as "the evidence vanished".

The single call into the external engine is routed through
:meth:`_extract_trial_dicts`, which the test-suite monkeypatches so the bundled
tests stay fully offline and need no network and no third-party install.
"""

from __future__ import annotations

import glob
import json
import os
from typing import Optional

from beast.effects import Trial
from beast.sources.base import Source, TopicSpec, register_source
from beast.sources.pairwise70 import _row_to_trial


@register_source
class RctExtractorSource(Source):
    name = "rct_extractor"

    def fetch(self, topic: TopicSpec, as_of_year: Optional[int] = None) -> list[Trial]:
        params = topic.params or {}
        corpus = params.get("corpus")
        if not corpus:
            raise ValueError(
                f"topic {topic.id!r}: rct_extractor source needs params.corpus "
                f"(a JSON list of {{study,text,year?}} records or a folder of *.txt)"
            )
        records = self._load_corpus(corpus)
        if not records:
            raise ValueError(f"topic {topic.id!r}: corpus {corpus!r} is empty")

        specialty = params.get("specialty", "auto")
        endpoint = params.get("endpoint")
        measure = topic.measure.upper() if topic.measure else "OR"

        trial_dicts = self._extract_trial_dicts(records, specialty, endpoint, measure)
        trials = []
        for d in trial_dicts:
            t = _row_to_trial(dict(d))
            if t is not None:
                trials.append(t)
        if as_of_year is not None:
            trials = [t for t in trials if t.year is not None and t.year <= as_of_year]
        if not trials:
            # Fail closed: an empty pool must never be silently returned.
            raise ValueError(
                f"topic {topic.id!r}: rct_extractor produced no poolable {measure} "
                f"trials from {len(records)} record(s)"
                + (f" on/before {as_of_year}" if as_of_year is not None else "")
            )
        return trials

    # --- corpus loading -----------------------------------------------
    @staticmethod
    def _load_corpus(corpus: str) -> list[dict]:
        """Return a list of ``{study, text, year?}`` records from a file or folder."""
        if os.path.isdir(corpus):
            records = []
            for path in sorted(glob.glob(os.path.join(corpus, "*.txt"))):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                study = os.path.splitext(os.path.basename(path))[0]
                records.append({"study": study, "text": text})
            return records
        if os.path.isfile(corpus):
            with open(corpus, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                data = data.get("records", data.get("corpus", []))
            if not isinstance(data, list):
                raise ValueError(
                    f"corpus {corpus!r} must be a JSON list of records "
                    f"(or an object with a 'records' list)"
                )
            out = []
            for i, rec in enumerate(data):
                if not isinstance(rec, dict) or not (rec.get("text") or rec.get("abstract")):
                    continue
                out.append({
                    "study": str(rec.get("study") or rec.get("id") or f"record_{i}"),
                    "text": rec.get("text") or rec.get("abstract"),
                    "year": rec.get("year"),
                })
            return out
        raise FileNotFoundError(f"corpus path not found: {corpus!r}")

    # --- external engine boundary (monkeypatched in tests) ------------
    @staticmethod
    def _extract_trial_dicts(records: list[dict], specialty: str,
                             endpoint: Optional[str], measure: str) -> list[dict]:
        """Call the external rct-extractor-v2 engine; return Trial-shaped dicts.

        Lazily imported so Beast does not depend on the engine unless this source
        is actually used. Raises a clear, actionable error if it is missing.
        """
        try:
            from rct_extractor.integrations.beast import to_beast_trials
        except ImportError as exc:  # pragma: no cover - exercised only without the dep
            raise ImportError(
                "the rct_extractor source needs the rct-extractor-v2 engine: "
                'pip install "git+https://github.com/mahmood726-cyber/rct-extractor-v2.git"'
            ) from exc
        result = to_beast_trials(records, specialty=specialty, endpoint=endpoint,
                                 measure=measure)
        return list(result or [])
