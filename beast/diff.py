"""Detect meaningful changes between two consecutive snapshots.

This is the surveillance core: given the previous and current pooled state of a
topic, decide what (if anything) materially changed and how alarming it is. Five
families of change are flagged:

* ``new_trials`` / ``removed_trials`` -- the study set grew or shrank
* ``effect_shift``                    -- the pooled estimate moved appreciably
* ``direction_flip``                  -- the estimate crossed the null (sign change)
* ``significance_flip``               -- the CI started/stopped excluding the null
* ``heterogeneity_change``            -- I-squared moved appreciably

Severity is ``info`` < ``notable`` < ``major``. A significance flip or a
direction change is always ``major`` -- those are the events that change a
clinical conclusion.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from beast.snapshot import Snapshot

SEVERITY_ORDER = {"info": 0, "notable": 1, "major": 2}


@dataclass
class DiffThresholds:
    """Tunable thresholds for what counts as a meaningful change.

    ``effect_shift`` is on the *analysis* scale (log units for ratio measures);
    0.10 log-OR is ~10% on the odds-ratio scale. ``i2_shift`` is in percentage
    points. ``effect_shift_major`` escalates a large estimate move to ``major``.
    """

    effect_shift: float = 0.10
    effect_shift_major: float = 0.25
    i2_shift: float = 15.0
    i2_shift_major: float = 30.0


@dataclass
class Change:
    type: str
    severity: str
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class SnapshotDiff:
    topic_id: str
    from_ts: str
    to_ts: str
    changes: list[Change] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)

    @property
    def max_severity(self) -> str:
        if not self.changes:
            return "none"
        return max(self.changes, key=lambda c: SEVERITY_ORDER[c.severity]).severity


def _fmt(x: Optional[float], nd: int = 3) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def diff_snapshots(
    prev: Snapshot,
    curr: Snapshot,
    thresholds: Optional[DiffThresholds] = None,
) -> SnapshotDiff:
    """Compare ``prev`` -> ``curr`` and return the list of flagged changes."""
    th = thresholds or DiffThresholds()
    diff = SnapshotDiff(topic_id=curr.topic_id, from_ts=prev.timestamp, to_ts=curr.timestamp)

    # --- study set ----------------------------------------------------
    prev_keys, curr_keys = set(prev.study_keys), set(curr.study_keys)
    added = sorted(curr_keys - prev_keys)
    removed = sorted(prev_keys - curr_keys)
    if added:
        diff.changes.append(Change(
            type="new_trials",
            severity="notable" if len(added) >= 2 else "info",
            message=f"{len(added)} new trial(s) entered the pool (k {prev.k} -> {curr.k}).",
            details={"added": added, "k_from": prev.k, "k_to": curr.k},
        ))
    if removed:
        # Studies disappearing is unusual and worth a hard look (data issue or
        # a corrected/retracted trial); never silently swallow it.
        diff.changes.append(Change(
            type="removed_trials",
            severity="major",
            message=f"{len(removed)} trial(s) left the pool (k {prev.k} -> {curr.k}). "
                    f"Investigate -- evidence sets normally only grow.",
            details={"removed": removed, "k_from": prev.k, "k_to": curr.k},
        ))

    # --- effect size --------------------------------------------------
    delta = curr.estimate - prev.estimate
    nat_prev = prev.natural.get("estimate")
    nat_curr = curr.natural.get("estimate")
    if abs(delta) >= th.effect_shift:
        sev = "major" if abs(delta) >= th.effect_shift_major else "notable"
        diff.changes.append(Change(
            type="effect_shift",
            severity=sev,
            message=(
                f"Pooled effect moved by {delta:+.3f} on the analysis scale "
                f"({_fmt(nat_prev)} -> {_fmt(nat_curr)} {curr.measure})."
            ),
            details={
                "delta_analysis": delta,
                "from": prev.estimate, "to": curr.estimate,
                "from_natural": nat_prev, "to_natural": nat_curr,
            },
        ))

    # --- direction (crossing the null) --------------------------------
    # Null is 0 on the analysis scale. A sign change means the pooled direction
    # of benefit/harm reversed -- always major. But gate it by magnitude: an
    # estimate hovering on the null flips sign on pure noise (e.g. OR 0.999 ->
    # 1.001), which is not a meaningful reversal. Require at least one side to be
    # an appreciable distance from the null (the same effect_shift threshold)
    # before calling it a direction flip.
    if prev.estimate != 0 and curr.estimate != 0 and \
            math.copysign(1, prev.estimate) != math.copysign(1, curr.estimate) and \
            max(abs(prev.estimate), abs(curr.estimate)) >= th.effect_shift:
        diff.changes.append(Change(
            type="direction_flip",
            severity="major",
            message=(
                f"Direction of the pooled effect reversed "
                f"({_fmt(nat_prev)} -> {_fmt(nat_curr)} {curr.measure})."
            ),
            details={"from": prev.estimate, "to": curr.estimate},
        ))

    # --- statistical significance -------------------------------------
    if prev.significant != curr.significant:
        gained = curr.significant and not prev.significant
        diff.changes.append(Change(
            type="significance_flip",
            severity="major",
            message=(
                "Result became statistically significant"
                if gained else
                "Result lost statistical significance"
            ) + f" (CI {_fmt(curr.natural.get('ci_low'))} to "
                f"{_fmt(curr.natural.get('ci_high'))} {curr.measure}).",
            details={
                "from_significant": prev.significant,
                "to_significant": curr.significant,
                "direction": "gained" if gained else "lost",
            },
        ))

    # --- heterogeneity ------------------------------------------------
    i2_delta = curr.i2 - prev.i2
    if abs(i2_delta) >= th.i2_shift:
        sev = "major" if abs(i2_delta) >= th.i2_shift_major else "notable"
        diff.changes.append(Change(
            type="heterogeneity_change",
            severity=sev,
            message=(
                f"Heterogeneity I-squared moved {i2_delta:+.1f} points "
                f"({prev.i2:.1f}% -> {curr.i2:.1f}%); tau-squared "
                f"{prev.tau2:.4f} -> {curr.tau2:.4f}."
            ),
            details={
                "i2_from": prev.i2, "i2_to": curr.i2, "i2_delta": i2_delta,
                "tau2_from": prev.tau2, "tau2_to": curr.tau2,
            },
        ))

    return diff
