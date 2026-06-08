"""Source abstraction and the tracked-topic spec."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional

from beast.effects import Trial


@dataclass
class TopicSpec:
    """A topic Beast tracks over time.

    ``id`` is a stable slug used as the storage key. ``source`` selects the
    adapter (``pairwise70`` or ``europepmc``). ``measure`` is the effect measure
    to pool (``OR``/``RR``/``MD``/``SMD``/``GEN``). ``params`` carries
    source-specific settings (e.g. the Cochrane review id, or a search query).
    """

    id: str
    title: str
    source: str
    measure: str = "OR"
    method: str = "REML"
    params: dict = field(default_factory=dict)
    notes: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "TopicSpec":
        known = {f for f in ("id", "title", "source", "measure", "method", "params", "notes")}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "source": self.source,
            "measure": self.measure, "method": self.method,
            "params": self.params, "notes": self.notes,
        }


class Source(abc.ABC):
    """Fetches the current trial set for a topic.

    Implementations must be *fail-closed*: on a transport error, malformed
    payload, or error page, raise rather than return a partial/empty set that
    would be mistaken for "the evidence shrank".
    """

    name: str = "base"

    @abc.abstractmethod
    def fetch(self, topic: TopicSpec, as_of_year: Optional[int] = None) -> list[Trial]:
        """Return the trials for ``topic``.

        ``as_of_year`` (when supported) restricts to trials published in or
        before that year, enabling reconstruction of historical snapshots.
        """
        raise NotImplementedError


_REGISTRY: dict[str, type[Source]] = {}


def register_source(cls: type[Source]) -> type[Source]:
    _REGISTRY[cls.name] = cls
    return cls


def get_source(name: str, **kwargs) -> Source:
    """Instantiate a registered source by name."""
    # Imported lazily so optional deps (pyreadr, requests) aren't required unless
    # the corresponding source is actually used.
    import beast.sources.pairwise70  # noqa: F401
    import beast.sources.europepmc  # noqa: F401
    import beast.sources.rct_extractor  # noqa: F401

    if name not in _REGISTRY:
        raise ValueError(f"unknown source {name!r}; available: {sorted(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)
