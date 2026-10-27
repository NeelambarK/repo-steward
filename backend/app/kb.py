"""Lyzr Studio clients (knowledge base + agent chat).

The PRD lists the Studio API shapes as unverified assumptions. Everything that
talks to Lyzr lives here so that fixing an endpoint or payload is a one-file
change. Any failure surfaces as a ServiceError, which /approve turns into the
`applied_unindexed` status and /reindex can recover from.
"""
from __future__ import annotations

import re
from typing import Protocol

import httpx

from .config import Settings
from .errors import ServiceError


class KnowledgeBase(Protocol):
    enabled: bool

    def add(self, path: str, content: str, content_hash: str) -> str: ...
    def remove(self, doc_id: str) -> None: ...
    def reset(self) -> None: ...


class Agent(Protocol):
    def chat(self, message: str, session_id: str) -> str: ...


class NullKB:
    enabled = False

    def add(self, path, content, content_hash):
        raise ServiceError(503, "knowledge base is not configured")

    def remove(self, doc_id):
        raise ServiceError(503, "knowledge base is not configured")

    def reset(self):
        raise ServiceError(503, "knowledge base is not configured")


class LyzrKB:
    """Endpoints from the Lyzr KB API docs: POST /train/text/ and DELETE /rag/{id}/reset/.

    Observed behaviour: posting text with a `source` that already exists replaces that
    document, so an edit is a single add() with the file path as the source. There is no
    documented per-document delete, so an approved delete rebuilds the whole KB
    (reset + re-ingest).
    """

    replaces_on_add = True
    supports_remove = False
    TRAIN_PATH = "/train/text/"
    RESET_PATH = "/rag/{rag_id}/reset/"
    CHUNK_SIZE = 1000
    CHUNK_OVERLAP = 100

    def __init__(self, s: Settings):
        self.enabled = bool(s.lyzr_api_key and s.lyzr_kb_id)
        self._base = s.lyzr_rag_url.rstrip("/")
        self._kb_id = s.lyzr_kb_id
        self._headers = {"x-api-key": s.lyzr_api_key}

    def _check_id(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{24}", self._kb_id):
            raise ServiceError(500, "LYZR_KB_ID must be the 24-character hex ID only (no 'kb=' prefix)")

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            r = httpx.request(method, url, headers=self._headers, timeout=60, **kw)
            r.raise_for_status()
            return r
        except httpx.HTTPStatusError as e:
            detail = e.response.text[:300]
            raise ServiceError(502, f"Lyzr KB {method} failed: HTTP {e.response.status_code} {detail}") from e
        except httpx.HTTPError as e:
            raise ServiceError(502, f"Lyzr KB request failed: {e}") from e

    def add(self, path, content, content_hash):
        self._check_id()
        # Prefix the path so retrieved chunks can be cited; `source` is the upsert key.
        body = {
            "data": [{"text": f"// FILE: {path}\n{content}", "source": path}],
            "chunk_size": self.CHUNK_SIZE,
            "chunk_overlap": self.CHUNK_OVERLAP,
        }
        self._request("POST", self._base + self.TRAIN_PATH, params={"rag_id": self._kb_id}, json=body)
        return path  # the API returns no document id; the path is the identity

    def remove(self, doc_id):
        raise ServiceError(501, "Lyzr KB has no per-document delete; a rebuild is required")

    def reset(self):
        self._check_id()
        self._request("DELETE", self._base + self.RESET_PATH.format(rag_id=self._kb_id))


class LyzrAgent:
    def __init__(self, s: Settings):
        self.enabled = bool(s.lyzr_api_key and s.lyzr_agent_id)
        self._s = s

    def chat(self, message, session_id):
        if not self.enabled:
            raise ServiceError(503, "Lyzr agent is not configured (LYZR_API_KEY / LYZR_AGENT_ID)")
        payload = {
            "user_id": self._s.lyzr_user_id,
            "agent_id": self._s.lyzr_agent_id,
            "session_id": session_id,
            "message": message,
        }
        try:
            r = httpx.post(
                self._s.lyzr_chat_url,
                headers={"x-api-key": self._s.lyzr_api_key},
                json=payload,
                timeout=120,
            )
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ServiceError(502, f"Lyzr chat request failed: {e}") from e
        if isinstance(data, dict):
            return str(data.get("response") or data.get("message") or data)
        return str(data)
