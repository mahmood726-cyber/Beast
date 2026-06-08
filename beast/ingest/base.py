"""Feed and extractor abstractions for Cochrane ingestion."""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from beast.effects import Trial


class NoDataError(Exception):
    """Raised by an extractor when a review has no usable pairwise data."""


@dataclass
class ReviewRef:
    """A reference to one Cochrane review, enough to dedupe and fetch it.

    ``id`` is the Beast/Pairwise70 review id (``CDxxxxxx`` or ``CDxxxxxx_pubN``),
    derived from the DOI when not given explicitly.
    """

    id: str
    doi: str = ""
    title: str = ""
    pub_date: str = ""
    measure: str = "OR"
    n_studies: Optional[int] = None
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_doi(cls, doi: str, **kw) -> "ReviewRef":
        return cls(id=review_id_from_doi(doi), doi=doi, **kw)


# Cochrane DOIs look like 10.1002/14651858.CD002042.pub6
_DOI_RE = re.compile(r"(CD\d{4,6})(?:\.(pub\d+))?", re.IGNORECASE)


def review_id_from_doi(doi: str) -> str:
    """Extract the Pairwise70 review id from a Cochrane DOI or URL.

    ``10.1002/14651858.CD002042.pub6`` -> ``CD002042_pub6``;
    ``...CD016001`` (no pub) -> ``CD016001``.
    """
    m = _DOI_RE.search(doi or "")
    if not m:
        raise ValueError(f"could not parse a Cochrane review id from {doi!r}")
    cd = m.group(1).upper()
    pub = m.group(2)
    return f"{cd}_{pub.lower()}" if pub else cd


class CochraneFeed(abc.ABC):
    """Lists currently-published Cochrane reviews."""

    @abc.abstractmethod
    def list_reviews(self, since: Optional[str] = None) -> list[ReviewRef]:
        """Return review references, optionally only those published after ``since``.

        ``since`` is an ISO date string (``YYYY-MM-DD``). Implementations must be
        fail-closed: raise on a transport/parse error rather than returning an
        empty list that would look like "nothing new".
        """
        raise NotImplementedError


class StudyExtractor(abc.ABC):
    """Turns a review into study-level :class:`~beast.effects.Trial` rows."""

    @abc.abstractmethod
    def extract(self, ref: ReviewRef) -> list[Trial]:
        """Return the trials for ``ref``.

        Raise :class:`NoDataError` when the review genuinely has no pairwise data
        (a normal outcome for many reviews); raise other exceptions on failure.
        Never return fabricated rows.
        """
        raise NotImplementedError


class CallableExtractor(StudyExtractor):
    """Wrap a plain callable ``ref -> list[Trial]`` (tests, custom pipelines)."""

    def __init__(self, fn: Callable[[ReviewRef], list[Trial]]):
        self._fn = fn

    def extract(self, ref: ReviewRef) -> list[Trial]:
        trials = self._fn(ref)
        if not trials:
            raise NoDataError(ref.id)
        return trials
