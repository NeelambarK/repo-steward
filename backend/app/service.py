from __future__ import annotations

import difflib
import hashlib
import os
import posixpath
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .db import Database, audit, now
from .errors import ServiceError
from .jail import is_blocked, resolve
from .kb import KnowledgeBase

SKIP_DIRS = {"node_modules", "dist", "build", ".next", "coverage"}
INDEX_EXTS = {".tsx", ".ts", ".css"}
CODE_EXTS = (".ts", ".tsx", ".js", ".jsx")
STATUSES = {"pending", "applied", "rejected", "failed", "applied_unindexed"}
IMPORT_RE = re.compile(r"""(?:from|import)\s*\(?\s*['"]([^'"]+)['"]""")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def make_diff(rel: str, old: str | None, new: str | None) -> str:
    a = (old or "").splitlines()
    b = (new or "").splitlines()
    lines = difflib.unified_diff(
        a, b,
        fromfile="/dev/null" if old is None else f"a/{rel}",
        tofile="/dev/null" if new is None else f"b/{rel}",
        lineterm="",
    )
    return "\n".join(lines)


def is_indexable(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in SKIP_DIRS or is_blocked(p) for p in parts[:-1]):
        return False
    name = parts[-1].lower()
    if is_blocked(name):
        return False
    return (
        os.path.splitext(name)[1] in INDEX_EXTS
        or name == "package.json"
        or (name.startswith("tsconfig") and name.endswith(".json"))
        or name.startswith("readme")
    )


class ChangeService:
    def __init__(self, settings: Settings, db: Database, kb: KnowledgeBase):
        self.s, self.db, self.kb = settings, db, kb
        self.root = settings.sandbox_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # serialises approvals and KB rebuilds

    # ---------- filesystem helpers ----------

    def iter_files(self):
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not is_blocked(d))
            for name in sorted(filenames):
                full = Path(dirpath, name)
                if is_blocked(name) or full.is_symlink():
                    continue
                yield full.relative_to(self.root).as_posix()

    def _read_text(self, target: Path, rel: str) -> tuple[str, str]:
        if not target.is_file():
            raise ServiceError(404, f"{rel} does not exist or is not a file")
        if target.stat().st_size > self.s.max_file_bytes:
            raise ServiceError(413, f"{rel} is larger than {self.s.max_file_bytes} bytes")
        raw = target.read_bytes()
        try:
            return raw.decode("utf-8"), sha(raw)
        except UnicodeDecodeError:
            raise ServiceError(415, f"{rel} is not a UTF-8 text file") from None

    @staticmethod
    def _write_atomic(target: Path, content: str, like: Path | None = None) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.steward-tmp")
        tmp.write_bytes(content.encode("utf-8"))
        if like is not None and like.exists():
            shutil.copymode(like, tmp)
        os.replace(tmp, target)

    def importers(self, rel: str) -> list[str]:
        """Best-effort list of sandbox files that import `rel` (relative imports only)."""
        noext = posixpath.splitext(rel)[0]
        hits = []
        for other in self.iter_files():
            if other == rel or not other.endswith(CODE_EXTS):
                continue
            try:
                text = (self.root / other).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for spec in IMPORT_RE.findall(text):
                if not spec.startswith("."):
                    continue
                res = posixpath.normpath(posixpath.join(posixpath.dirname(other), spec))
                if res in (rel, noext) or (posixpath.basename(noext) == "index" and res == posixpath.dirname(noext)):
                    hits.append(other)
                    break
        return hits

    # ---------- agent-facing tools ----------

    def _tool(self, action: str, raw_path: str, fn):
        try:
            with self.db.tx() as conn:
                result, change_id, note = fn(conn)
                audit(conn, "agent", action, result.get("path") or raw_path, change_id, note)
                return result
        except ServiceError as e:
            self.db.record("agent", action, raw_path, None, f"error: {e.message}")
            raise

    def read_file(self, path: str) -> dict:
        def run(conn):
            target, rel = resolve(self.root, path, allow_root=True)
            if target.is_dir():
                entries = [
                    {"name": c.name, "type": "directory" if c.is_dir() else "file"}
                    for c in sorted(target.iterdir())
                    if not is_blocked(c.name) and c.name not in SKIP_DIRS
                ]
                return {"path": rel or ".", "type": "directory", "entries": entries}, None, "ok"
            text, _ = self._read_text(target, rel)
            return {"path": rel, "type": "file", "content": text}, None, "ok"

        return self._tool("read_file", path, run)

    def _staged(self, cid: int, ctype: str, rel: str, diff: str, **extra) -> dict:
        return {
            "change_id": cid,
            "type": ctype,
            "path": rel,
            "status": "pending",
            "message": f"Change {cid} is PENDING human approval. The file has NOT been changed.",
            "diff": diff,
            **extra,
        }

    def _find_pending(self, conn, ctype, rel, old, new, base_hash):
        row = conn.execute(
            "SELECT id FROM changes WHERE status = 'pending' AND type = ? AND path = ? "
            "AND old_text IS ? AND new_text IS ? AND base_hash IS ?",
            (ctype, rel, old, new, base_hash),
        ).fetchone()
        return row["id"] if row else None

    def _insert(self, conn, ctype, rel, old, new, reason, diff, base_hash, session_id) -> int:
        cur = conn.execute(
            "INSERT INTO changes (type, path, old_text, new_text, reason, diff, base_hash, status, session_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (ctype, rel, old, new, reason, diff, base_hash, session_id, now()),
        )
        return cur.lastrowid

    def stage_write(self, path: str, content: str, session_id: str | None = None) -> dict:
        def run(conn):
            target, rel = resolve(self.root, path)
            if target.exists():
                raise ServiceError(409, f"{rel} already exists; use edit_file")
            if len(content.encode("utf-8")) > self.s.max_file_bytes:
                raise ServiceError(413, "content is too large")
            existing = self._find_pending(conn, "write", rel, None, content, None)
            if existing:
                return self._staged(existing, "write", rel, "", duplicate=True), existing, "duplicate"
            diff = make_diff(rel, None, content)
            cid = self._insert(conn, "write", rel, None, content, None, diff, None, session_id)
            return self._staged(cid, "write", rel, diff), cid, "staged"

        return self._tool("write_file", path, run)

    def stage_edit(self, path: str, old_text: str, new_text: str, session_id: str | None = None) -> dict:
        def run(conn):
            target, rel = resolve(self.root, path)
            text, h = self._read_text(target, rel)
            n = text.count(old_text)
            if n == 0:
                raise ServiceError(422, "old_text was not found in the file; read_file again and copy it exactly")
            if n > 1:
                raise ServiceError(422, f"old_text appears {n} times; include more surrounding context so it is unique")
            if old_text == new_text:
                raise ServiceError(422, "new_text is identical to old_text")
            existing = self._find_pending(conn, "edit", rel, old_text, new_text, h)
            if existing:
                return self._staged(existing, "edit", rel, "", duplicate=True), existing, "duplicate"
            diff = make_diff(rel, text, text.replace(old_text, new_text, 1))
            cid = self._insert(conn, "edit", rel, old_text, new_text, None, diff, h, session_id)
            return self._staged(cid, "edit", rel, diff), cid, "staged"

        return self._tool("edit_file", path, run)

    def stage_delete(self, path: str, reason: str = "", session_id: str | None = None) -> dict:
        def run(conn):
            target, rel = resolve(self.root, path)
            text, h = self._read_text(target, rel)
            importers = self.importers(rel)
            existing = self._find_pending(conn, "delete", rel, None, None, h)
            if existing:
                return self._staged(existing, "delete", rel, "", duplicate=True, imported_by=importers), existing, "duplicate"
            diff = make_diff(rel, text, None)
            cid = self._insert(conn, "delete", rel, None, None, reason, diff, h, session_id)
            return self._staged(cid, "delete", rel, diff, imported_by=importers), cid, "staged"

        return self._tool("delete_file", path, run)

    # ---------- admin: queue ----------

    def list_changes(self, status: str | None) -> list[dict]:
        if status and status != "all" and status not in STATUSES:
            raise ServiceError(400, f"status must be one of {sorted(STATUSES)} or 'all'")
        with self.db.tx() as conn:
            if status and status != "all":
                rows = conn.execute("SELECT * FROM changes WHERE status = ? ORDER BY id DESC", (status,))
            else:
                rows = conn.execute("SELECT * FROM changes ORDER BY id DESC")
            return [dict(r) for r in rows]

    def _get(self, conn, change_id: int):
        row = conn.execute("SELECT * FROM changes WHERE id = ?", (change_id,)).fetchone()
        if not row:
            raise ServiceError(404, f"change {change_id} not found")
        return row

    def _decide(self, change_id: int, status: str, action: str, row, error: str | None = None, result: str | None = None):
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE changes SET status = ?, error = ?, decided_at = ? WHERE id = ?",
                (status, error, now(), change_id),
            )
            audit(conn, "admin", action, row["path"], change_id, result or status)
            return dict(self._get(conn, change_id))

    def reject(self, change_id: int) -> dict:
        with self._lock:
            with self.db.tx() as conn:
                row = self._get(conn, change_id)
            if row["status"] != "pending":
                raise ServiceError(409, f"change {change_id} is already {row['status']}")
            return self._decide(change_id, "rejected", "reject", row)

    def approve(self, change_id: int) -> dict:
        with self._lock:
            with self.db.tx() as conn:
                row = self._get(conn, change_id)
            if row["status"] != "pending":
                raise ServiceError(409, f"change {change_id} is already {row['status']}")
            try:
                self._apply(row)
            except ServiceError as e:
                self._decide(change_id, "failed", "approve", row, error=e.message, result=f"failed: {e.message}")
                raise ServiceError(409, f"change {change_id} failed: {e.message}") from e
            except OSError as e:
                self._decide(change_id, "failed", "approve", row, error=str(e), result=f"failed: {e}")
                raise ServiceError(500, f"change {change_id} failed: {e}") from e
            try:
                note = self._sync_kb(row["type"], row["path"])
                return self._decide(change_id, "applied", "approve", row, result=f"applied ({note})")
            except ServiceError as e:
                return self._decide(
                    change_id, "applied_unindexed", "approve", row,
                    error=e.message, result=f"applied_unindexed: {e.message}",
                )

    def _apply(self, row) -> None:
        target, rel = resolve(self.root, row["path"])
        ctype = row["type"]
        if ctype == "write":
            if target.exists():
                raise ServiceError(409, f"{rel} now exists")
            self._write_atomic(target, row["new_text"])
            return
        text, h = self._read_text(target, rel)
        if h != row["base_hash"]:
            raise ServiceError(409, f"stale change: {rel} was modified after this change was staged")
        if ctype == "edit":
            if text.count(row["old_text"]) != 1:
                raise ServiceError(409, "old_text no longer matches exactly once")
            self._write_atomic(target, text.replace(row["old_text"], row["new_text"], 1), like=target)
        else:  # delete (soft)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            dest = self.root / ".trash" / stamp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(dest))

    # ---------- knowledge-base sync ----------

    def _kb_add(self, rel: str) -> None:
        text, h = self._read_text(self.root / rel, rel)
        doc_id = self.kb.add(rel, text, h)
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO kb_documents (path, kb_document_id, content_hash, indexed_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET kb_document_id = excluded.kb_document_id, "
                "content_hash = excluded.content_hash, indexed_at = excluded.indexed_at",
                (rel, doc_id, h, now()),
            )

    def _kb_remove(self, rel: str) -> None:
        with self.db.tx() as conn:
            row = conn.execute("SELECT kb_document_id FROM kb_documents WHERE path = ?", (rel,)).fetchone()
        if not row:
            return
        self.kb.remove(row["kb_document_id"])
        with self.db.tx() as conn:
            conn.execute("DELETE FROM kb_documents WHERE path = ?", (rel,))

    def _sync_kb(self, ctype: str, rel: str) -> str:
        if not self.kb.enabled:
            return "kb disabled"
        if not is_indexable(rel):
            return "not indexable"
        upserts = getattr(self.kb, "replaces_on_add", False)
        can_remove = getattr(self.kb, "supports_remove", True)
        needs_rebuild = (ctype == "delete" and not can_remove) or (ctype == "edit" and not upserts and not can_remove)
        if self.s.kb_sync_mode == "rebuild" or needs_rebuild:
            self._reindex_locked()
            return "kb rebuilt"
        if ctype == "delete" or (ctype == "edit" and not upserts):
            self._kb_remove(rel)
        if ctype in ("write", "edit"):
            self._kb_add(rel)
        return "kb updated"

    def ingest(self) -> dict:
        """Index every indexable file whose content is not already in the KB."""
        with self._lock:
            return self._ingest_locked()

    def _ingest_locked(self) -> dict:
        if not self.kb.enabled:
            raise ServiceError(503, "knowledge base is not configured")
        with self.db.tx() as conn:
            known = {r["path"]: r["content_hash"] for r in conn.execute("SELECT path, content_hash FROM kb_documents")}
        indexed, skipped, failed = 0, 0, []
        for rel in self.iter_files():
            if not is_indexable(rel):
                continue
            try:
                _, h = self._read_text(self.root / rel, rel)
                if known.get(rel) == h:
                    skipped += 1
                    continue
                if rel in known:
                    self._kb_remove(rel)
                self._kb_add(rel)
                indexed += 1
            except ServiceError as e:
                failed.append({"path": rel, "error": e.message})
        self.db.record("admin", "ingest", None, None, f"indexed={indexed} skipped={skipped} failed={len(failed)}")
        return {"indexed": indexed, "skipped": skipped, "failed": failed}

    def reindex(self) -> dict:
        """Wipe the KB and rebuild it from the sandbox (recovery path)."""
        with self._lock:
            return self._reindex_locked()

    def _reindex_locked(self) -> dict:
        if not self.kb.enabled:
            raise ServiceError(503, "knowledge base is not configured")
        self.kb.reset()
        with self.db.tx() as conn:
            conn.execute("DELETE FROM kb_documents")
        out = self._ingest_locked()
        self.db.record("admin", "reindex", None, None, f"indexed={out['indexed']} failed={len(out['failed'])}")
        return out

    # ---------- admin: file viewer ----------

    def tree(self) -> list[str]:
        return list(self.iter_files())

    def admin_read(self, path: str) -> dict:
        target, rel = resolve(self.root, path)
        text, _ = self._read_text(target, rel)
        return {"path": rel, "content": text}
