"""A timestamped snapshot of a topic's pooled evidence."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, asdict
from typing import Optional

from beast.effects import compute_effects, is_log_scale
from beast.meta import meta_analyze
from beast.sources.base import TopicSpec
from beast.effects import Trial


def study_key(t: Trial) -> str:
    """Stable identity for a trial, used to diff which studies are in the pool."""
    yr = t.year if t.year is not None else "?"
    return f"{t.study.strip()}|{yr}"


@dataclass
class Snapshot:
    """One observation of where a topic's evidence stands at a point in time.

    Effect fields (``estimate``, ``ci_low`` ...) are on the analysis scale (log
    for ratio measures); ``natural`` holds the back-transformed reporting-scale
    values. ``study_keys`` is the sorted set of contributing studies, used for
    "new trial" detection.
    """

    topic_id: str
    timestamp: str           # ISO-8601 UTC
    measure: str
    method: str
    k: int
    estimate: float
    se: float
    ci_low: float
    ci_high: float
    pi_low: Optional[float]
    pi_high: Optional[float]
    tau2: float
    i2: float
    h2: float
    q: float
    q_p: float
    z: float
    p_value: float
    significant: bool
    log_scale: bool
    natural: dict
    study_keys: list[str]
    n_total: Optional[int] = None
    as_of_year: Optional[int] = None
    content_hash: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Snapshot":
        fields = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in fields})

    def _hash_payload(self) -> str:
        """Hash of the inputs+result that defines a *meaningful* snapshot.

        Drives idempotency: re-running with the same studies and the same pooled
        estimate yields the same hash, so no duplicate snapshot is stored.
        """
        basis = {
            "topic_id": self.topic_id,
            "measure": self.measure,
            "method": self.method,
            "study_keys": sorted(self.study_keys),
            "k": self.k,
            # Round to 8 dp so floating-point noise never forges a "change".
            "estimate": round(self.estimate, 8),
            "ci": [round(self.ci_low, 8), round(self.ci_high, 8)],
            "i2": round(self.i2, 6),
            "tau2": round(self.tau2, 8),
        }
        raw = json.dumps(basis, sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def compute_snapshot(
    topic: TopicSpec,
    trials: list[Trial],
    timestamp: str,
    as_of_year: Optional[int] = None,
) -> Snapshot:
    """Pool ``trials`` for ``topic`` and package the result as a Snapshot.

    Raises ``ValueError`` if no trial yields a usable effect for the measure --
    fail closed rather than emit an empty pool that downstream diffing could read
    as "the evidence vanished".
    """
    measure = topic.measure.upper()
    used, yi, vi = compute_effects(trials, measure)
    if not used:
        raise ValueError(
            f"topic {topic.id!r}: no trials produced a usable {measure} effect "
            f"(received {len(trials)} trials)"
        )
    res = meta_analyze(
        yi, vi, method=topic.method, measure=measure, log_scale=is_log_scale(measure)
    )

    n_total = 0
    have_n = False
    for t in used:
        for n in (t.e_n, t.c_n):
            if n is not None:
                n_total += int(n)
                have_n = True

    snap = Snapshot(
        topic_id=topic.id,
        timestamp=timestamp,
        measure=measure,
        method=res.method,
        k=res.k,
        estimate=res.estimate,
        se=res.se,
        ci_low=res.ci_low,
        ci_high=res.ci_high,
        pi_low=res.pi_low,
        pi_high=res.pi_high,
        tau2=res.tau2,
        i2=res.i2,
        h2=res.h2,
        q=res.q,
        q_p=res.q_p,
        z=res.z,
        p_value=res.p_value,
        significant=res.significant,
        log_scale=res.log_scale,
        natural=res.on_natural_scale(),
        study_keys=sorted(study_key(t) for t in used),
        n_total=n_total if have_n else None,
        as_of_year=as_of_year,
    )
    snap.content_hash = snap._hash_payload()
    return snap
