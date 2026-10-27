from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL CHECK (type IN ('write', 'edit', 'delete')),
    path        TEXT NOT NULL,
    old_text    TEXT,
    new_text    TEXT,
    reason      TEXT,
    diff        TEXT NOT NULL,
    base_hash   TEXT,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'applied', 'rejected', 'failed', 'applied_unindexed')),
    error       TEXT,
    session_id  TEXT,
    created_at  TEXT NOT NULL,
    decided_at  TEXT
);
CREATE TABLE IF NOT EXISTS kb_documents (
    path           TEXT PRIMARY KEY,
    kb_document_id TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    indexed_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    actor     TEXT NOT NULL CHECK (actor IN ('agent', 'admin')),
    action    TEXT NOT NULL,
    path      TEXT,
    change_id INTEGER,
    result    TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit(conn, actor, action, path=None, change_id=None, result="ok") -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, actor, action, path, change_id, result) VALUES (?, ?, ?, ?, ?, ?)",
        (now(), actor, action, (path or "")[:500] or None, change_id, result[:1000]),
    )


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        with self.tx() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record(self, actor, action, path=None, change_id=None, result="ok") -> None:
        with self.tx() as conn:
            audit(conn, actor, action, path, change_id, result)
