"""Live Cochrane feed (Crossref) and a process-based study extractor.

``CrossrefCochraneFeed`` lists Cochrane Database of Systematic Reviews works via
the Crossref REST API (journal ISSN ``1469-493X``) -- a stable, documented JSON
API, far more robust than scraping the Cochrane Library HTML. It yields DOIs,
titles and publication dates, from which Beast derives review ids and decides
what is new.

``ProcessExtractor`` reuses the existing cochrane-data-extractor pipeline: it runs
a configured command that produces a Cochrane *data-rows* CSV for a review, then
parses that CSV into :class:`~beast.effects.Trial` rows (the same column mapping
the Pairwise70 source uses). Nothing is fabricated -- if the command yields no
data file, a :class:`NoDataError` is raised.

Both classes route I/O through small, monkeypatchable methods so the test-suite is
fully offline.
"""

from __future__ import annotations

import csv
import os
import subprocess
import tempfile
import time
from typing import Optional

from beast.effects import Trial
from beast.ingest.base import CochraneFeed, NoDataError, ReviewRef, StudyExtractor, review_id_from_doi
from beast.sources.pairwise70 import _row_to_trial

_CROSSREF = "https://api.crossref.org/journals/1469-493X/works"
_USER_AGENT = "Beast-living-surveillance/0.1 (mailto:noreply@example.com)"


class CrossrefCochraneFeed(CochraneFeed):
    """Cochrane CDSR review feed backed by the Crossref REST API."""

    def __init__(self, rows: int = 200, max_pages: int = 20, timeout: int = 30,
                 max_retries: int = 3, min_interval: float = 0.5):
        self.rows = rows
        self.max_pages = max_pages
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = min_interval
        self._last = 0.0

    def list_reviews(self, since: Optional[str] = None) -> list[ReviewRef]:
        out: list[ReviewRef] = []
        cursor = "*"
        for _ in range(self.max_pages):
            params = {
                "rows": self.rows, "cursor": cursor,
                "select": "DOI,title,published,published-online,issued",
            }
            if since:
                params["filter"] = f"from-pub-date:{since}"
            data = self._http_get_json(_CROSSREF, params)
            msg = data.get("message", {})
            items = msg.get("items", [])
            for it in items:
                ref = self._item_to_ref(it)
                if ref is not None:
                    out.append(ref)
            cursor = msg.get("next-cursor")
            if not items or not cursor:
                break
        return out

    @staticmethod
    def _item_to_ref(item: dict) -> Optional[ReviewRef]:
        doi = item.get("DOI", "")
        try:
            rid = review_id_from_doi(doi)
        except ValueError:
            return None  # not a CD review DOI (editorials etc.)
        title = ""
        if item.get("title"):
            title = item["title"][0] if isinstance(item["title"], list) else str(item["title"])
        pub = ""
        for key in ("published", "published-online", "issued"):
            dp = (item.get(key) or {}).get("date-parts")
            if dp and dp[0]:
                pub = "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(dp[0]))
                break
        return ReviewRef(id=rid, doi=doi, title=title, pub_date=pub)

    def _http_get_json(self, url: str, params: dict) -> dict:
        try:
            import requests
        except ImportError as exc:  # pragma: no cover
            raise ImportError("CrossrefCochraneFeed needs 'requests' (pip install requests)") from exc
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = requests.get(url, params=params, timeout=self.timeout,
                                    headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
                self._last = time.monotonic()
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"Crossref transient HTTP {resp.status_code}")
                resp.raise_for_status()
                if "json" not in resp.headers.get("Content-Type", "").lower():
                    raise ValueError("Crossref returned non-JSON payload")
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                self._last = time.monotonic()
                time.sleep(min(2.0 ** attempt, 8.0))
        raise RuntimeError(f"Crossref request failed after {self.max_retries} attempts") from last_err


class ProcessExtractor(StudyExtractor):
    """Extract study-level rows by running the real cochrane-data-extractor.

    ``command_template`` is a list of args with ``{review_id}``, ``{doi}`` and
    ``{out}`` placeholders. The command must write a Cochrane *data-rows* CSV to
    ``{out}``; this class then parses it. Example::

        ProcessExtractor(["python", "bulk_downloader.py", "--doi", "{doi}",
                          "--out", "{out}"], cwd="C:/CochraneDataExtractor")

    If the command exits non-zero or writes no rows, a :class:`NoDataError` is
    raised -- never a fabricated dataset.
    """

    def __init__(self, command_template: list[str], cwd: Optional[str] = None,
                 timeout: int = 600):
        self.command_template = command_template
        self.cwd = cwd
        self.timeout = timeout

    def extract(self, ref: ReviewRef) -> list[Trial]:
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, f"{ref.id}-data-rows.csv")
            cmd = [a.format(review_id=ref.id, doi=ref.doi, out=out) for a in self.command_template]
            try:
                proc = subprocess.run(cmd, cwd=self.cwd, capture_output=True,
                                      text=True, timeout=self.timeout)
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError(f"extractor command failed for {ref.id}: {exc}") from exc
            if proc.returncode != 0:
                raise RuntimeError(
                    f"extractor exited {proc.returncode} for {ref.id}: {proc.stderr[:300]}"
                )
            if not os.path.exists(out):
                raise NoDataError(ref.id)
            trials = self._parse_data_rows(out)
        if not trials:
            raise NoDataError(ref.id)
        return trials

    @staticmethod
    def _parse_data_rows(path: str) -> list[Trial]:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        out = [_row_to_trial(r) for r in rows]
        return [t for t in out if t is not None]
