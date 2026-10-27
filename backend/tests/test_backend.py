import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import ServiceError
from app.main import create_app

TOOL = {"X-Tool-Key": "tool-secret"}
ADMIN = {"X-Admin-Key": "admin-secret"}


class FakeKB:
    enabled = True

    def __init__(self):
        self.docs = {}  # doc_id -> path
        self.n = 0
        self.fail = False

    def add(self, path, content, content_hash):
        if self.fail:
            raise ServiceError(502, "kb down")
        self.n += 1
        self.docs[f"d{self.n}"] = path
        return f"d{self.n}"

    def remove(self, doc_id):
        if self.fail:
            raise ServiceError(502, "kb down")
        self.docs.pop(doc_id)

    def reset(self):
        self.docs.clear()

    @property
    def paths(self):
        return sorted(self.docs.values())


class FakeAgent:
    def chat(self, message, session_id):
        return f"echo:{message}:{session_id}"


@pytest.fixture
def env(tmp_path):
    sandbox = tmp_path / "sandbox"
    (sandbox / "src/shared/ui").mkdir(parents=True)
    (sandbox / "src/shared/ui/Button.tsx").write_text("export const Button = () => <button className=\"p-2\" />;\n")
    (sandbox / "src/shared/ui/Textarea.tsx").write_text("export const Textarea = () => <textarea />;\n")
    (sandbox / "src/App.tsx").write_text("import { Textarea } from './shared/ui/Textarea';\nimport { Button } from './shared/ui/Button';\n")
    (sandbox / ".env").write_text("SECRET=1\n")
    settings = Settings(sandbox_dir=sandbox, db_path=tmp_path / "t.db", tool_key="tool-secret", admin_key="admin-secret")
    kb = FakeKB()
    client = TestClient(create_app(settings, kb, FakeAgent()))
    assert client.post("/ingest", headers=ADMIN).json()["failed"] == []
    return client, sandbox, kb, settings


def audit_rows(settings):
    return sqlite3.connect(settings.db_path).execute("SELECT actor, action, change_id, result FROM audit_log").fetchall()


def test_keys_are_separate(env):
    c, *_ = env
    assert c.get("/tools/read_file?path=src").status_code == 401
    assert c.get("/tools/read_file?path=src", headers=ADMIN).status_code == 401
    assert c.post("/approve/1", headers=TOOL).status_code == 401
    assert c.get("/changes", headers=TOOL).status_code == 401


def test_tool_spec_lists_only_tools(env):
    c, *_ = env
    paths = set(c.get("/tool-openapi.json").json()["paths"])
    assert paths == {"/tools/read_file", "/tools/write_file", "/tools/edit_file", "/tools/delete_file"}


def test_ingest_skips_env_and_indexes_code(env):
    _, _, kb, _ = env
    assert kb.paths == ["src/App.tsx", "src/shared/ui/Button.tsx", "src/shared/ui/Textarea.tsx"]


def test_edit_approve_updates_file_and_kb(env):  # scenario 3
    c, sandbox, kb, _ = env
    r = c.post("/tools/edit_file", headers=TOOL, json={"path": "src/shared/ui/Button.tsx", "old_text": "p-2", "new_text": "p-4"})
    cid = r.json()["change_id"]
    assert "p-2" in (sandbox / "src/shared/ui/Button.tsx").read_text()  # untouched while pending
    old_doc = next(k for k, v in kb.docs.items() if v.endswith("Button.tsx"))
    assert c.post(f"/approve/{cid}", headers=ADMIN).json()["status"] == "applied"
    assert "p-4" in (sandbox / "src/shared/ui/Button.tsx").read_text()
    assert old_doc not in kb.docs and any(v.endswith("Button.tsx") for v in kb.docs.values())


def test_edit_requires_exactly_one_match(env):
    c, *_ = env
    assert c.post("/tools/edit_file", headers=TOOL, json={"path": "src/App.tsx", "old_text": "import", "new_text": "x"}).status_code == 422
    assert c.post("/tools/edit_file", headers=TOOL, json={"path": "src/App.tsx", "old_text": "nope", "new_text": "x"}).status_code == 422


def test_write_approve(env):  # scenario 4
    c, sandbox, kb, _ = env
    cid = c.post("/tools/write_file", headers=TOOL, json={"path": "src/shared/ui/Badge.tsx", "content": "export const Badge = () => null;\n"}).json()["change_id"]
    assert not (sandbox / "src/shared/ui/Badge.tsx").exists()
    c.post(f"/approve/{cid}", headers=ADMIN)
    assert (sandbox / "src/shared/ui/Badge.tsx").exists()
    assert "src/shared/ui/Badge.tsx" in kb.paths


def test_delete_reject_then_approve(env):  # scenarios 5 and 6
    c, sandbox, kb, settings = env
    r = c.post("/tools/delete_file", headers=TOOL, json={"path": "src/shared/ui/Textarea.tsx", "reason": "unused"}).json()
    assert r["imported_by"] == ["src/App.tsx"]
    c.post(f"/reject/{r['change_id']}", headers=ADMIN)
    assert (sandbox / "src/shared/ui/Textarea.tsx").exists() and "src/shared/ui/Textarea.tsx" in kb.paths

    cid = c.post("/tools/delete_file", headers=TOOL, json={"path": "src/shared/ui/Textarea.tsx"}).json()["change_id"]
    c.post(f"/approve/{cid}", headers=ADMIN)
    assert not (sandbox / "src/shared/ui/Textarea.tsx").exists()
    assert list((sandbox / ".trash").rglob("Textarea.tsx"))
    assert "src/shared/ui/Textarea.tsx" not in kb.paths
    assert ("admin", "approve", cid, "applied (kb updated)") in audit_rows(settings)


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", ".env", "src/../.env", ".git/config", ".ENV", ".trash/x"])
def test_path_jail(env, path):  # scenario 8
    c, *_ = env
    assert c.get("/tools/read_file", params={"path": path}, headers=TOOL).status_code in (403, 404)
    assert c.post("/tools/write_file", headers=TOOL, json={"path": path, "content": "x"}).status_code == 403


def test_symlink_escape_is_blocked(env, tmp_path):
    c, sandbox, *_ = env
    (tmp_path / "outside.txt").write_text("secret")
    (sandbox / "link.txt").symlink_to(tmp_path / "outside.txt")
    assert c.get("/tools/read_file", params={"path": "link.txt"}, headers=TOOL).status_code == 403


def test_stale_change_fails(env):  # scenario 9
    c, sandbox, *_ = env
    cid = c.post("/tools/edit_file", headers=TOOL, json={"path": "src/shared/ui/Button.tsx", "old_text": "p-2", "new_text": "p-4"}).json()["change_id"]
    (sandbox / "src/shared/ui/Button.tsx").write_text("changed outside the app p-2\n")
    assert c.post(f"/approve/{cid}", headers=ADMIN).status_code == 409
    assert c.get("/changes?status=failed", headers=ADMIN).json()[0]["id"] == cid
    assert "outside the app" in (sandbox / "src/shared/ui/Button.tsx").read_text()


def test_staging_is_idempotent(env):
    c, *_ = env
    body = {"path": "src/shared/ui/Button.tsx", "old_text": "p-2", "new_text": "p-4"}
    a = c.post("/tools/edit_file", headers=TOOL, json=body).json()["change_id"]
    b = c.post("/tools/edit_file", headers=TOOL, json=body).json()["change_id"]
    assert a == b and len(c.get("/changes", headers=ADMIN).json()) == 1


def test_kb_failure_marks_applied_unindexed(env):
    c, sandbox, kb, _ = env
    cid = c.post("/tools/edit_file", headers=TOOL, json={"path": "src/shared/ui/Button.tsx", "old_text": "p-2", "new_text": "p-4"}).json()["change_id"]
    kb.fail = True
    assert c.post(f"/approve/{cid}", headers=ADMIN).json()["status"] == "applied_unindexed"
    assert "p-4" in (sandbox / "src/shared/ui/Button.tsx").read_text()


def test_double_approve_rejected(env):
    c, *_ = env
    cid = c.post("/tools/write_file", headers=TOOL, json={"path": "a.ts", "content": "x"}).json()["change_id"]
    assert c.post(f"/approve/{cid}", headers=ADMIN).status_code == 200
    assert c.post(f"/approve/{cid}", headers=ADMIN).status_code == 409


def test_every_tool_call_is_audited(env):
    c, _, _, settings = env
    c.get("/tools/read_file", params={"path": "src"}, headers=TOOL)
    c.get("/tools/read_file", params={"path": "../x"}, headers=TOOL)
    agent_rows = [r for r in audit_rows(settings) if r[0] == "agent"]
    assert len(agent_rows) == 2 and agent_rows[1][3].startswith("error")


def test_audit_log_is_append_only(env):
    _, _, _, settings = env
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(settings.db_path).execute("DELETE FROM audit_log")


def test_chat_proxy_needs_admin(env):
    c, *_ = env
    assert c.post("/chat", json={"message": "hi", "session_id": "s1"}).status_code == 401
    assert c.post("/chat", headers=ADMIN, json={"message": "hi", "session_id": "s1"}).json() == {"reply": "echo:hi:s1"}


def test_edit_upserts_when_kb_replaces_on_add(env):
    c, _, kb, _ = env
    kb.supports_remove, kb.replaces_on_add = False, True
    calls = []
    orig_reset = kb.reset
    kb.reset = lambda: (calls.append("reset"), orig_reset())
    cid = c.post("/tools/edit_file", headers=TOOL, json={"path": "src/shared/ui/Button.tsx", "old_text": "p-2", "new_text": "p-4"}).json()["change_id"]
    assert c.post(f"/approve/{cid}", headers=ADMIN).json()["status"] == "applied"
    assert calls == []  # no wipe needed for an edit


def test_delete_rebuilds_kb_when_remove_is_unsupported(env):
    c, _, kb, _ = env
    kb.supports_remove, kb.replaces_on_add = False, True
    cid = c.post("/tools/delete_file", headers=TOOL, json={"path": "src/shared/ui/Textarea.tsx"}).json()["change_id"]
    assert c.post(f"/approve/{cid}", headers=ADMIN).json()["status"] == "applied"
    assert kb.paths == ["src/App.tsx", "src/shared/ui/Button.tsx"]


def test_rejected_tool_calls_are_audited(env):
    c, _, _, settings = env
    c.get("/tools/read_file?path=src")
    c.get("/tools/read_file?path=src", headers={"X-Tool-Key": "nope"})
    rows = [r for r in audit_rows(settings) if r[1] == "auth_failed"]
    assert [r[3] for r in rows] == ["401 missing key", "401 wrong key"]


def test_tool_spec_has_absolute_server_url(env):
    c, *_ = env
    spec = c.get("/tool-openapi.json", headers={"X-Forwarded-Host": "abc.trycloudflare.com", "X-Forwarded-Proto": "https"}).json()
    assert spec["servers"] == [{"url": "https://abc.trycloudflare.com"}]
