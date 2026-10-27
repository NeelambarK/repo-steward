from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Security
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from .config import Settings
from .db import Database
from .errors import ServiceError
from .kb import Agent, KnowledgeBase, LyzrAgent, LyzrKB, NullKB
from .service import ChangeService

STATIC = Path(__file__).parent / "static"


class WriteReq(BaseModel):
    path: str = Field(min_length=1, max_length=500, description="Repo-relative path of the new file")
    content: str = Field(max_length=500_000)


class EditReq(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    old_text: str = Field(min_length=1, max_length=500_000, description="Exact text to replace; must appear exactly once")
    new_text: str = Field(max_length=500_000)


class DeleteReq(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    reason: str = Field(default="", max_length=1000)


class ChatReq(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    session_id: str = Field(min_length=1, max_length=200)


def _check(supplied: str | None, expected: str) -> None:
    if not supplied or not secrets.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid or missing key")


def create_app(settings: Settings | None = None, kb: KnowledgeBase | None = None, agent: Agent | None = None) -> FastAPI:
    s = settings or Settings.from_env()
    db = Database(s.db_path)
    if kb is None:
        real = LyzrKB(s)
        kb = real if real.enabled else NullKB()
    agent = agent or LyzrAgent(s)
    svc = ChangeService(s, db, kb)

    tool_key = APIKeyHeader(name="X-Tool-Key", auto_error=False)
    admin_key = APIKeyHeader(name="X-Admin-Key", auto_error=False)

    def require_tool(request: Request, key: str | None = Security(tool_key)) -> None:
        try:
            _check(key, s.tool_key)
        except HTTPException:
            # Rejected tool calls are visible in the audit log, so a wrong header in Studio is diagnosable.
            reason = "missing key" if not key else "wrong key"
            db.record("agent", "auth_failed", request.url.path, None, f"401 {reason}")
            raise

    def require_admin(key: str | None = Security(admin_key)) -> None:
        _check(key, s.admin_key)

    app = FastAPI(title="Repo Steward", version="0.1.0")

    @app.exception_handler(ServiceError)
    async def service_error(_, exc: ServiceError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    # ---- agent tools (X-Tool-Key) ----
    tools = APIRouter(prefix="/tools", tags=["tools"], dependencies=[Depends(require_tool)])

    @tools.get("/read_file", operation_id="read_file", summary="Read a file, or list a folder")
    def read_file(path: str = ""):
        return svc.read_file(path)

    @tools.post("/write_file", operation_id="write_file", summary="Stage a new file for human approval")
    def write_file(req: WriteReq, x_session_id: str | None = Header(default=None)):
        return svc.stage_write(req.path, req.content, x_session_id)

    @tools.post("/edit_file", operation_id="edit_file", summary="Stage an exact-match edit for human approval")
    def edit_file(req: EditReq, x_session_id: str | None = Header(default=None)):
        return svc.stage_edit(req.path, req.old_text, req.new_text, x_session_id)

    @tools.post("/delete_file", operation_id="delete_file", summary="Stage a soft delete for human approval")
    def delete_file(req: DeleteReq, x_session_id: str | None = Header(default=None)):
        return svc.stage_delete(req.path, req.reason, x_session_id)

    app.include_router(tools)

    # ---- admin (X-Admin-Key) ----
    admin = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])

    @admin.post("/chat")
    def chat(req: ChatReq):
        reply = agent.chat(req.message, req.session_id)
        db.record("admin", "chat", None, None, "ok")
        return {"reply": reply}

    @admin.get("/changes")
    def changes(status: str = "pending"):
        return svc.list_changes(status)

    @admin.post("/approve/{change_id}")
    def approve(change_id: int):
        return svc.approve(change_id)

    @admin.post("/reject/{change_id}")
    def reject(change_id: int):
        return svc.reject(change_id)

    @admin.post("/ingest")
    def ingest():
        return svc.ingest()

    @admin.post("/reindex")
    def reindex():
        return svc.reindex()

    @admin.get("/files/tree")
    def files_tree():
        return {"files": svc.tree()}

    @admin.get("/files/read")
    def files_read(path: str):
        return svc.admin_read(path)

    app.include_router(admin)

    # ---- public ----
    @app.get("/health", include_in_schema=False)
    def health():
        return {"ok": True, "kb": kb.enabled}

    @app.get("/tool-openapi.json", include_in_schema=False)
    def tool_openapi(request: Request):
        """OpenAPI spec containing ONLY the four agent tools; give this to Studio."""
        spec = get_openapi(title="Repo Steward tools", version="0.1.0", routes=tools.routes)
        spec.setdefault("components", {}).setdefault("securitySchemes", {})["ToolKey"] = {
            "type": "apiKey", "in": "header", "name": "X-Tool-Key",
        }
        spec["security"] = [{"ToolKey": []}]
        # Studio needs an absolute base URL; without `servers` its tool runner has none to call.
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
        spec["servers"] = [{"url": s.public_url or f"{proto}://{host}"}]
        return spec

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    return app
