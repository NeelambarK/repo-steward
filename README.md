# Repo Steward

Pilot for Lyzr Agent Studio: one agent, one knowledge base, four custom tools and a FastAPI backend.
The agent reads freely but can only *stage* changes; nothing touches the sandbox until a human approves it.
The product requirements doc is kept locally and is not part of this repository.

## Layout

- `backend/app/` FastAPI app (`main.py`), approval/KB logic (`service.py`), path jail (`jail.py`), SQLite (`db.py`), Lyzr clients (`kb.py`), one-file UI (`static/index.html`)
- `backend/tests/` pytest suite covering the PRD scenarios 3-6 and 8-9 against fakes (no Lyzr needed)
- `docs/` evaluation notes, e.g. [rag-value-check.md](docs/rag-value-check.md) (knowledge base attached vs detached)
- `sandbox/` the repo the agent can touch. Git-ignored: it is third-party code that approvals modify. See below.

## Set up the sandbox

The pilot uses [adiletbaimyrza/react-typescript-todo-app](https://github.com/adiletbaimyrza/react-typescript-todo-app) (MIT).
From the project root:

```bash
mkdir -p /tmp/todo-app && curl -fsSL https://codeload.github.com/adiletbaimyrza/react-typescript-todo-app/tar.gz/refs/heads/master | tar -xz -C /tmp/todo-app --strip-components=1
cp -R /tmp/todo-app/. sandbox/ && rm -rf sandbox/blob sandbox/package-lock.json
```

To use another repo, point `SANDBOX_DIR` in `backend/.env` at it, use a fresh `DB_PATH`, and attach a new knowledge base.
Indexing currently covers `.ts`, `.tsx`, `.css`, `package.json`, `tsconfig*.json` and README files (`is_indexable` in `service.py`).

## Run

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # set TOOL_KEY and ADMIN_KEY to two different secrets
pytest
uvicorn app.main:create_app --factory --port 8000
```

Open http://localhost:8000, enter the admin key. `/docs` has the full API; `/tool-openapi.json`
is the spec containing only the four tools, which is what you give Studio. Send `X-Tool-Key` on every tool call.

## Status against the PRD build plan

Steps 1-3, 7 (backend, `/chat`, web page) and 8 (re-index on approve, `/reindex`) are implemented.
Steps 4-6 (tunnel, Studio KB, tools, agent, guardrail) are manual in Studio.
KB sync uses the documented Lyzr endpoints (`POST /v3/train/text/`, `DELETE /v3/rag/{id}/reset/`). Adding text with an
existing `source` (the file path) replaces that document, so approved edits are a single call. There is no per-document
delete, so an approved delete wipes the KB and re-ingests the sandbox. If a KB call fails, the change is still applied
and marked `applied_unindexed`; `/ingest` or `/reindex` recovers.

Additions beyond the PRD tables: `changes.reason` and `changes.error` columns; delete responses include a best-effort
`imported_by` list; rejected tool calls are written to the audit log as `auth_failed`; `PUBLIC_URL` sets the server URL
in the generated tool spec.
