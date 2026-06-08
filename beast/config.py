"""Beast home directory, paths, and tracked-topics config."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from beast.sources.base import TopicSpec


def beast_home() -> str:
    """Root data directory (env ``BEAST_HOME`` or ``./beast_data``)."""
    return os.environ.get("BEAST_HOME") or os.path.join(os.getcwd(), "beast_data")


@dataclass
class Paths:
    home: str

    @property
    def db(self) -> str:
        return os.path.join(self.home, "beast.db")

    @property
    def topics(self) -> str:
        return os.path.join(self.home, "topics.json")

    @property
    def reports(self) -> str:
        return os.path.join(self.home, "reports")

    @property
    def log(self) -> str:
        return os.path.join(self.home, "beast.log")

    def ensure(self) -> "Paths":
        os.makedirs(self.home, exist_ok=True)
        os.makedirs(self.reports, exist_ok=True)
        return self


def paths(home: str | None = None) -> Paths:
    return Paths(home or beast_home())


def default_topics(sample_csv: str) -> list[TopicSpec]:
    """A ready-to-run starter topic backed by the bundled real Cochrane sample.

    ``sample_csv`` should point at ``tests/fixtures/sample_pairwise70.csv`` (or a
    copy). This lets ``beast init && beast backfill`` produce a real trend with
    zero network and zero extra data.
    """
    return [
        TopicSpec(
            id="htn-elderly-mortality",
            title="Antihypertensive therapy in the elderly: all-cause mortality (60-79y)",
            source="pairwise70",
            measure="OR",
            method="REML",
            params={"csv": sample_csv},
            notes="Real Cochrane CD000028 trial-level data; demonstrates a "
                  "significance flip between 1986 and 1991 as trials accumulated.",
        )
    ]


def load_topics(path: str) -> list[TopicSpec]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("topics", [])
    return [TopicSpec.from_dict(d) for d in data]


def save_topics(path: str, topics: list[TopicSpec]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"topics": [t.to_dict() for t in topics]}, fh, indent=2)
