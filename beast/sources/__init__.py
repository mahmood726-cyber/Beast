"""Data sources Beast can track.

A source knows how to fetch the current set of :class:`~beast.effects.Trial`
objects for a tracked topic. Two are bundled:

* :class:`~beast.sources.pairwise70.Pairwise70Source` -- offline, real Cochrane
  trial-level data, with ``as_of_year`` cumulative surveillance (reconstruct how
  the evidence looked at any past year).
* :class:`~beast.sources.europepmc.EuropePmcSource` -- live PubMed / Europe PMC
  search for forward-looking surveillance (network; mocked in tests).
"""

from beast.sources.base import Source, TopicSpec, get_source

__all__ = ["Source", "TopicSpec", "get_source"]
