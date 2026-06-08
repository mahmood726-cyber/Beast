"""Ingestion of newly-published Cochrane reviews into the Pairwise70 dataset.

Two abstractions mirror the existing cochrane-data-extractor pipeline:

* :class:`~beast.ingest.base.CochraneFeed` -- lists currently-published Cochrane
  reviews (id, DOI, title, date), so Beast can find what is *new* since last run.
* :class:`~beast.ingest.base.StudyExtractor` -- turns one review into study-level
  :class:`~beast.effects.Trial` rows (the data-rows format).

The live implementations talk to cochranelibrary.com / shell out to the real
extractor; both are mockable so the test-suite is fully offline.
"""

from beast.ingest.base import (
    CochraneFeed,
    ReviewRef,
    StudyExtractor,
    CallableExtractor,
    NoDataError,
)

__all__ = [
    "CochraneFeed", "ReviewRef", "StudyExtractor", "CallableExtractor", "NoDataError",
]
