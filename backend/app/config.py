from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    sandbox_dir: Path
    db_path: Path
    tool_key: str
    admin_key: str
    lyzr_api_key: str = ""
    lyzr_agent_id: str = ""
    lyzr_kb_id: str = ""
    lyzr_user_id: str = "repo-steward-admin"
    lyzr_chat_url: str = "https://agent-prod.studio.lyzr.ai/v3/inference/chat/"
    lyzr_rag_url: str = "https://rag-prod.studio.lyzr.ai/v3"
    kb_sync_mode: str = "incremental"
    public_url: str = ""
    max_file_bytes: int = 200_000

    @classmethod
    def from_env(cls) -> "Settings":
        from dotenv import load_dotenv

        load_dotenv()
        env = os.environ
        tool_key, admin_key = env.get("TOOL_KEY", ""), env.get("ADMIN_KEY", "")
        if not tool_key or not admin_key:
            raise RuntimeError("TOOL_KEY and ADMIN_KEY must both be set")
        if tool_key == admin_key:
            raise RuntimeError("TOOL_KEY and ADMIN_KEY must be different")
        mode = env.get("KB_SYNC_MODE", "incremental")
        if mode not in ("incremental", "rebuild"):
            raise RuntimeError("KB_SYNC_MODE must be 'incremental' or 'rebuild'")
        defaults = cls.__dataclass_fields__
        return cls(
            sandbox_dir=Path(env.get("SANDBOX_DIR", "../sandbox")).expanduser(),
            db_path=Path(env.get("DB_PATH", "./steward.db")).expanduser(),
            tool_key=tool_key,
            admin_key=admin_key,
            lyzr_api_key=env.get("LYZR_API_KEY", ""),
            lyzr_agent_id=env.get("LYZR_AGENT_ID", ""),
            lyzr_kb_id=env.get("LYZR_KB_ID", ""),
            lyzr_user_id=env.get("LYZR_USER_ID", defaults["lyzr_user_id"].default),
            lyzr_chat_url=env.get("LYZR_CHAT_URL", defaults["lyzr_chat_url"].default),
            lyzr_rag_url=env.get("LYZR_RAG_URL", defaults["lyzr_rag_url"].default),
            kb_sync_mode=mode,
            public_url=env.get("PUBLIC_URL", "").strip().rstrip("/"),
        )
