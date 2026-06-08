"""Europe PMC live source -- forward-looking surveillance of the literature.

Queries the Europe PMC REST API (https://www.ebi.ac.uk/europepmc/webservices/rest)
for trials matching a topic query and returns them as :class:`~beast.effects.Trial`
objects. Where an abstract explicitly reports an effect estimate with a confidence
interval (e.g. "OR 0.82, 95% CI 0.70-0.96"), a *generic* effect is parsed and
log-transformed for ratio measures; trials without a parseable estimate are still
returned (they count toward the discovered trial set and "new trial" flags) but
carry no effect and are dropped from the pool. Nothing is fabricated -- an effect
is emitted only when one is literally stated.

For rigorous full-text effect extraction, plug in the rct-extractor-v2 engine
(mahmood726-cyber/rct-extractor-v2); this adapter is intentionally lightweight and
abstract-only.

Network access goes through :meth:`_http_get_json`, which retries with bounded
backoff and fails closed on transport errors or non-JSON / error payloads. Tests
monkeypatch that one method so the suite is fully offline.
"""

from __future__ import annotations

import math
import re
import time
from typing import Optional

from beast.effects import Trial
from beast.sources.base import Source, TopicSpec, register_source

_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_USER_AGENT = "Beast-living-surveillance/0.1 (+https://github.com/mahmood726-cyber/Beast)"

# Conservative effect-with-CI parser: "<measure> <point> (95% CI <lo>-<hi>)".
# Accepts OR/RR/HR and the en-dash / "to" separators. Ratios are log-transformed.
_EFFECT_RE = re.compile(
    r"\b(?P<measure>OR|RR|HR|odds ratio|risk ratio|hazard ratio)\b"
    r"[^0-9]{0,12}(?P<point>\d+\.\d+)"
    r"[^0-9]{0,25}?95%?\s*CI[^0-9]{0,6}"
    r"(?P<lo>\d+\.\d+)\s*(?:-|–|to)\s*(?P<hi>\d+\.\d+)",
    re.IGNORECASE,
)


def _parse_effect(abstract: str) -> Optional[tuple[float, float]]:
    """Return ``(yi, sei)`` on the log scale from a reported ratio + 95% CI, or None.

    SE is recovered from the CI width: ``sei = (log(hi) - log(lo)) / (2 * 1.96)``.
    Only ratio measures with a strictly positive, ordered CI are accepted.
    """
    if not abstract:
        return None
    m = _EFFECT_RE.search(abstract)
    if not m:
        return None
    try:
        lo, hi = float(m.group("lo")), float(m.group("hi"))
    except ValueError:
        return None
    if not (0 < lo < hi):
        return None
    yi = math.log(float(m.group("point")))
    sei = (math.log(hi) - math.log(lo)) / (2.0 * 1.959963984540054)
    if sei <= 0 or not math.isfinite(yi) or not math.isfinite(sei):
        return None
    return yi, sei


@register_source
class EuropePmcSource(Source):
    name = "europepmc"

    def __init__(self, page_size: int = 100, max_pages: int = 10,
                 min_interval: float = 0.34, timeout: int = 30, max_retries: int = 3):
        self.page_size = page_size
        self.max_pages = max_pages
        self.min_interval = min_interval  # >= ~3 req/s politeness
        self.timeout = timeout
        self.max_retries = max_retries
        self._last_call = 0.0

    def fetch(self, topic: TopicSpec, as_of_year: Optional[int] = None) -> list[Trial]:
        params = topic.params or {}
        query = params.get("query")
        if not query:
            raise ValueError(f"topic {topic.id!r}: europepmc source needs params.query")
        # Restrict to RCTs; optionally cap the publication year for back-dating.
        q = f"({query}) AND PUB_TYPE:\"Randomized Controlled Trial\""
        if as_of_year is not None:
            q += f" AND PUB_YEAR:[1900 TO {int(as_of_year)}]"
        elif params.get("min_year"):
            q += f" AND PUB_YEAR:[{int(params['min_year'])} TO 3000]"

        trials, cursor, pages = [], "*", 0
        while pages < self.max_pages:
            data = self._http_get_json(_BASE, {
                "query": q, "format": "json", "pageSize": self.page_size,
                "resultType": "core", "cursorMark": cursor,
            })
            results = (data.get("resultList") or {}).get("result") or []
            for rec in results:
                t = self._record_to_trial(rec)
                if t is not None:
                    trials.append(t)
            next_cursor = data.get("nextCursorMark")
            if not results or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
            pages += 1
        return trials

    @staticmethod
    def _record_to_trial(rec: dict) -> Optional[Trial]:
        ident = rec.get("id") or rec.get("pmid") or rec.get("doi")
        if not ident:
            return None
        year = None
        for key in ("pubYear", "firstPublicationDate"):
            val = rec.get(key)
            if val:
                m = re.search(r"\d{4}", str(val))
                if m:
                    year = int(m.group(0))
                    break
        label = rec.get("authorString", "").split(",")[0].strip() or "study"
        study = f"{label} {year}" if year else label
        t = Trial(study=f"{study} [{ident}]", year=year,
                  extra={"id": str(ident), "title": rec.get("title", "")})
        eff = _parse_effect(rec.get("abstractText", ""))
        if eff is not None:
            t.yi, t.sei = eff
        return t

    def _http_get_json(self, url: str, params: dict) -> dict:
        """GET ``url`` and return parsed JSON; retry with backoff, fail closed.

        Raises on transport failure or a non-JSON / error payload rather than
        returning partial data that downstream code could mistake for an empty
        or shrunken evidence base.
        """
        try:
            import requests
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError(
                "the europepmc source needs 'requests' (pip install requests)"
            ) from exc

        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            # Politeness rate limit between calls.
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = requests.get(
                    url, params=params, timeout=self.timeout,
                    headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
                )
                self._last_call = time.monotonic()
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"Europe PMC transient HTTP {resp.status_code}")
                resp.raise_for_status()
                ctype = resp.headers.get("Content-Type", "")
                if "json" not in ctype.lower():
                    raise ValueError(f"Europe PMC returned non-JSON payload ({ctype!r})")
                return resp.json()
            except Exception as exc:  # noqa: BLE001 - bounded retry then re-raise
                last_err = exc
                self._last_call = time.monotonic()
                time.sleep(min(2.0 ** attempt, 8.0))
        raise RuntimeError(f"Europe PMC request failed after {self.max_retries} attempts") from last_err
