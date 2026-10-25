"""Persistent state store (SQLite) - replaces the Java-serialized
the original Java client dlclient.localstate / dlclient.history.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sapdlm.client.constants import FileStatus, LocalStatus, MAX_HISTORY_ENTRIES

_SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
  dockey        TEXT PRIMARY KEY,
  text          TEXT NOT NULL DEFAULT '',
  doctype       TEXT NOT NULL DEFAULT '',
  size_kb       INTEGER NOT NULL DEFAULT 0,
  global_status INTEGER NOT NULL DEFAULT 0,
  basket_status INTEGER NOT NULL DEFAULT 0,
  local_status  INTEGER NOT NULL DEFAULT 0,
  local_version INTEGER NOT NULL DEFAULT 0,
  desc_file     TEXT NOT NULL DEFAULT '',
  dir_created   INTEGER NOT NULL DEFAULT 0,
  updated_at    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS components (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  dockey          TEXT NOT NULL,
  seq             INTEGER NOT NULL,
  file_name       TEXT NOT NULL DEFAULT '',
  resource_url    TEXT NOT NULL DEFAULT '',
  token           TEXT NOT NULL DEFAULT '',
  local_file_name TEXT NOT NULL DEFAULT '',
  bytes_committed INTEGER NOT NULL DEFAULT 0,
  file_status     INTEGER NOT NULL DEFAULT 0,
  updated_at      TEXT NOT NULL DEFAULT '',
  UNIQUE(dockey, seq)
);
CREATE TABLE IF NOT EXISTS protocol (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  dockey  TEXT NOT NULL,
  ts      TEXT NOT NULL,
  type    INTEGER NOT NULL,
  message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  dockey  TEXT NOT NULL,
  text    TEXT NOT NULL DEFAULT '',
  doctype TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 0,
  date    TEXT NOT NULL
);
"""

ENTRYTYPE_INFO = 1      # ProtocolEntry.ENTRYTYPE_INFO
ENTRYTYPE_EXCEPTION = 2  # ProtocolEntry.ENTRYTYPE_EXCEPTION


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class ObjectRecord:
    dockey: str
    text: str = ""
    doctype: str = ""
    size_kb: int = 0
    global_status: int = 0
    basket_status: int = 0
    local_status: int = LocalStatus.NOT_STARTED
    local_version: int = 0
    desc_file: str = ""
    dir_created: bool = False


@dataclass
class ComponentRecord:
    id: int | None
    dockey: str
    seq: int
    file_name: str = ""
    resource_url: str = ""
    token: str = ""
    local_file_name: str = ""
    bytes_committed: int = 0
    file_status: int = FileStatus.NOT_STARTED


@dataclass
class ProtocolRecord:
    id: int
    dockey: str
    ts: str
    type: int
    message: str


@dataclass
class HistoryRecord:
    id: int
    dockey: str
    text: str
    doctype: str
    version: int
    date: str


class StateStore:
    """Thread-safe SQLite persistence (engine + API run in different threads)."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------- objects

    def upsert_object(self, record: ObjectRecord) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO objects (dockey, text, doctype, size_kb, global_status,
                       basket_status, local_status, local_version, desc_file,
                       dir_created, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(dockey) DO UPDATE SET
                     text=excluded.text, doctype=excluded.doctype,
                     size_kb=excluded.size_kb, global_status=excluded.global_status,
                     basket_status=excluded.basket_status,
                     desc_file=CASE WHEN excluded.desc_file='' THEN objects.desc_file
                                    ELSE excluded.desc_file END,
                     updated_at=excluded.updated_at""",
                (
                    record.dockey, record.text, record.doctype, record.size_kb,
                    record.global_status, record.basket_status, record.local_status,
                    record.local_version, record.desc_file, int(record.dir_created),
                    _now(),
                ),
            )
            self._db.commit()

    def patch_object(self, dockey: str, **fields) -> None:
        if not fields:
            return
        allowed = {
            "global_status", "basket_status", "local_status", "local_version",
            "desc_file", "dir_created", "text", "doctype", "size_kb",
        }
        columns = ", ".join(f"{k}=?" for k in fields if k in allowed)
        values = [v for k, v in fields.items() if k in allowed]
        with self._lock:
            self._db.execute(
                f"UPDATE objects SET {columns}, updated_at=? WHERE dockey=?",  # noqa: S608
                (*values, _now(), dockey),
            )
            self._db.commit()

    def get_object(self, dockey: str) -> ObjectRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM objects WHERE dockey=?", (dockey,)
            ).fetchone()
        return _row_to_object(row) if row else None

    def list_objects(self) -> list[ObjectRecord]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM objects ORDER BY dockey").fetchall()
        return [_row_to_object(r) for r in rows]

    def delete_object_components(self, dockey: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM components WHERE dockey=?", (dockey,))
            self._db.commit()

    def delete_object(self, dockey: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM components WHERE dockey=?", (dockey,))
            self._db.execute("DELETE FROM objects WHERE dockey=?", (dockey,))
            self._db.commit()

    # ---------------------------------------------------------------- components

    def components(self, dockey: str) -> list[ComponentRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM components WHERE dockey=? ORDER BY seq", (dockey,)
            ).fetchall()
        return [_row_to_component(r) for r in rows]

    def sync_components(
        self, dockey: str, components: list[ComponentRecord]
    ) -> list[ComponentRecord]:
        """Mirror of LocalStatusItem.prepareLocalFileInfo: new component list
        replaces entries with the same file name but a different URL (resetting
        progress), keeps completed entries, appends new ones."""
        existing = self.components(dockey)
        result: list[ComponentRecord] = []
        for comp in components:
            match = next(
                (e for e in existing if e.file_name == comp.file_name
                 and e.resource_url != comp.resource_url),
                None,
            )
            keep = next(
                (e for e in existing if e.file_name == comp.file_name
                 and e.resource_url == comp.resource_url),
                None,
            )
            if match is not None:          # URL changed -> reset progress
                comp = ComponentRecord(
                    id=match.id, dockey=dockey, seq=comp.seq,
                    file_name=comp.file_name, resource_url=comp.resource_url,
                    token=comp.token, local_file_name="",
                    bytes_committed=0, file_status=FileStatus.NOT_STARTED,
                )
            elif keep is not None:        # same URL -> keep progress
                comp = keep
                comp.seq = comp.seq  # keep stored order
            result.append(comp)
        with self._lock:
            self._db.execute("DELETE FROM components WHERE dockey=?", (dockey,))
            for i, comp in enumerate(result):
                comp.seq = i
                self._db.execute(
                    """INSERT INTO components (dockey, seq, file_name, resource_url,
                           token, local_file_name, bytes_committed, file_status,
                           updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (dockey, i, comp.file_name, comp.resource_url, comp.token,
                     comp.local_file_name, comp.bytes_committed, comp.file_status,
                     _now()),
                )
            self._db.commit()
        return self.components(dockey)

    def patch_component(self, comp: ComponentRecord, **fields) -> None:
        with self._lock:
            columns = ", ".join(f"{k}=?" for k in fields)
            self._db.execute(
                f"UPDATE components SET {columns}, updated_at=? WHERE id=?",  # noqa: S608
                (*fields.values(), _now(), comp.id),
            )
            self._db.commit()

    # ------------------------------------------------------------------ protocol

    def add_protocol(self, dockey: str, message: str,
                     entry_type: int = ENTRYTYPE_INFO) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO protocol (dockey, ts, type, message) VALUES (?,?,?,?)",
                (dockey, _now(), entry_type, message),
            )
            self._db.commit()

    def protocol(self, dockey: str) -> list[ProtocolRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM protocol WHERE dockey=? ORDER BY id", (dockey,)
            ).fetchall()
        return [
            ProtocolRecord(r["id"], r["dockey"], r["ts"], r["type"], r["message"])
            for r in rows
        ]

    # ------------------------------------------------------------------- history

    def add_history(self, dockey: str, text: str, doctype: str, version: int) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO history (dockey, text, doctype, version, date)
                   VALUES (?,?,?,?,?)""",
                (dockey, text, doctype, version, _now()),
            )
            # DownloadHistory.MAX_ENTRIES cap (oldest entries are dropped)
            self._db.execute(
                "DELETE FROM history WHERE id NOT IN "
                "(SELECT id FROM history ORDER BY id DESC LIMIT ?)",
                (MAX_HISTORY_ENTRIES,),
            )
            self._db.commit()

    def history(self) -> list[HistoryRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM history ORDER BY id DESC LIMIT ?",
                (MAX_HISTORY_ENTRIES,),
            ).fetchall()
        return [
            HistoryRecord(r["id"], r["dockey"], r["text"], r["doctype"],
                          r["version"], r["date"])
            for r in rows
        ]

    # -------------------------------------------------------------------- helper

    def iter_object_keys(self) -> Iterator[str]:
        for record in self.list_objects():
            yield record.dockey


def _row_to_object(row: sqlite3.Row) -> ObjectRecord:
    return ObjectRecord(
        dockey=row["dockey"], text=row["text"], doctype=row["doctype"],
        size_kb=row["size_kb"], global_status=row["global_status"],
        basket_status=row["basket_status"], local_status=row["local_status"],
        local_version=row["local_version"], desc_file=row["desc_file"],
        dir_created=bool(row["dir_created"]),
    )


def _row_to_component(row: sqlite3.Row) -> ComponentRecord:
    return ComponentRecord(
        id=row["id"], dockey=row["dockey"], seq=row["seq"],
        file_name=row["file_name"], resource_url=row["resource_url"],
        token=row["token"], local_file_name=row["local_file_name"],
        bytes_committed=row["bytes_committed"], file_status=row["file_status"],
    )
