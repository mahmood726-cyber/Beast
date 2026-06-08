"""SQLite persistence for topics, snapshots and detected changes.

Idempotent by design: :meth:`BeastStore.add_snapshot` only inserts a new row when
the topic's pooled content actually changed (compared by ``content_hash`` to the
latest stored snapshot). Re-running ``beast run`` on an unchanged evidence base
therefore creates no duplicate rows and raises no spurious change flags.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Optional

from beast.diff import Change, SnapshotDiff
from beast.snapshot import Snapshot
from beast.sources.base import TopicSpec

_SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    source      TEXT NOT NULL,
    measure     TEXT NOT NULL,
    method      TEXT NOT NULL,
    params      TEXT NOT NULL,
    notes       TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id      TEXT NOT NULL REFERENCES topics(id),
    timestamp     TEXT NOT NULL,
    as_of_year    INTEGER,
    k             INTEGER NOT NULL,
    estimate      REAL NOT NULL,
    ci_low        REAL NOT NULL,
    ci_high       REAL NOT NULL,
    i2            REAL NOT NULL,
    tau2          REAL NOT NULL,
    significant   INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    payload       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snap_topic ON snapshots(topic_id, id);
CREATE TABLE IF NOT EXISTS changes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id         TEXT NOT NULL REFERENCES topics(id),
    from_snapshot_id INTEGER,
    to_snapshot_id   INTEGER NOT NULL,
    timestamp        TEXT NOT NULL,
    type             TEXT NOT NULL,
    severity         TEXT NOT NULL,
    message          TEXT NOT NULL,
    details          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_change_topic ON changes(topic_id, id);
"""


class BeastStore:
    def __init__(self, path: str):
        self.path = path
        # check_same_thread=False so a scheduler thread can share the connection;
        # access is serialized by the GIL + short transactions here.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "BeastStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- topics -------------------------------------------------------
    def upsert_topic(self, topic: TopicSpec, created_at: str) -> None:
        self._conn.execute(
            """INSERT INTO topics(id,title,source,measure,method,params,notes,created_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title, source=excluded.source, measure=excluded.measure,
                 method=excluded.method, params=excluded.params, notes=excluded.notes""",
            (topic.id, topic.title, topic.source, topic.measure, topic.method,
             json.dumps(topic.params), topic.notes, created_at),
        )
        self._conn.commit()

    def list_topics(self) -> list[TopicSpec]:
        rows = self._conn.execute("SELECT * FROM topics ORDER BY id").fetchall()
        return [
            TopicSpec(
                id=r["id"], title=r["title"], source=r["source"], measure=r["measure"],
                method=r["method"], params=json.loads(r["params"]), notes=r["notes"] or "",
            )
            for r in rows
        ]

    def get_topic(self, topic_id: str) -> Optional[TopicSpec]:
        r = self._conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()
        if not r:
            return None
        return TopicSpec(
            id=r["id"], title=r["title"], source=r["source"], measure=r["measure"],
            method=r["method"], params=json.loads(r["params"]), notes=r["notes"] or "",
        )

    # --- snapshots ----------------------------------------------------
    def latest_snapshot(self, topic_id: str) -> Optional[tuple[int, Snapshot]]:
        r = self._conn.execute(
            "SELECT id,payload FROM snapshots WHERE topic_id=? ORDER BY id DESC LIMIT 1",
            (topic_id,),
        ).fetchone()
        if not r:
            return None
        return r["id"], Snapshot.from_dict(json.loads(r["payload"]))

    def find_by_hash(self, topic_id: str, content_hash: str) -> Optional[int]:
        """Return the id of any existing snapshot with this content, or None."""
        r = self._conn.execute(
            "SELECT id FROM snapshots WHERE topic_id=? AND content_hash=? ORDER BY id LIMIT 1",
            (topic_id, content_hash),
        ).fetchone()
        return r["id"] if r else None

    def add_snapshot(
        self, snap: Snapshot, force: bool = False, dedupe: str = "latest"
    ) -> tuple[Optional[int], bool]:
        """Insert ``snap`` unless it duplicates existing content (idempotency).

        Returns ``(snapshot_id, inserted)``. ``dedupe`` controls the scope:

        * ``"latest"`` (default, for forward runs): skip only if the content
          matches the *most recent* snapshot, so a genuine return to a prior
          state is still recorded as a new event in time.
        * ``"topic"`` (for historical backfill replay): skip if *any* snapshot
          with this content already exists, so re-running a backfill is a no-op.
        """
        if not force:
            if dedupe == "topic":
                existing = self.find_by_hash(snap.topic_id, snap.content_hash)
                if existing is not None:
                    return existing, False
            else:
                latest = self.latest_snapshot(snap.topic_id)
                if latest and latest[1].content_hash == snap.content_hash:
                    return latest[0], False
        cur = self._conn.execute(
            """INSERT INTO snapshots(topic_id,timestamp,as_of_year,k,estimate,ci_low,
                                     ci_high,i2,tau2,significant,content_hash,payload)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (snap.topic_id, snap.timestamp, snap.as_of_year, snap.k, snap.estimate,
             snap.ci_low, snap.ci_high, snap.i2, snap.tau2, int(snap.significant),
             snap.content_hash, json.dumps(snap.as_dict())),
        )
        self._conn.commit()
        return cur.lastrowid, True

    def history(self, topic_id: str) -> list[Snapshot]:
        rows = self._conn.execute(
            "SELECT payload FROM snapshots WHERE topic_id=? ORDER BY id",
            (topic_id,),
        ).fetchall()
        return [Snapshot.from_dict(json.loads(r["payload"])) for r in rows]

    # --- changes ------------------------------------------------------
    def add_changes(
        self, diff: SnapshotDiff, from_snapshot_id: Optional[int], to_snapshot_id: int
    ) -> int:
        n = 0
        for ch in diff.changes:
            self._conn.execute(
                """INSERT INTO changes(topic_id,from_snapshot_id,to_snapshot_id,timestamp,
                                       type,severity,message,details)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (diff.topic_id, from_snapshot_id, to_snapshot_id, diff.to_ts,
                 ch.type, ch.severity, ch.message, json.dumps(ch.details)),
            )
            n += 1
        self._conn.commit()
        return n

    def recent_changes(self, limit: int = 50, topic_id: Optional[str] = None) -> list[dict]:
        if topic_id:
            rows = self._conn.execute(
                "SELECT * FROM changes WHERE topic_id=? ORDER BY id DESC LIMIT ?",
                (topic_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM changes ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["details"] = json.loads(d["details"])
            out.append(d)
        return out
