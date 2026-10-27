from __future__ import annotations

from pathlib import Path, PurePosixPath

from .errors import ServiceError

BLOCKED_DIRS = {".git", ".trash"}


def is_blocked(part: str) -> bool:
    # Lower-cased because the default macOS filesystem is case-insensitive.
    p = part.lower()
    return p in BLOCKED_DIRS or p == ".env" or p.startswith(".env.")


def resolve(root: Path, rel: str, *, allow_root: bool = False) -> tuple[Path, str]:
    """Map a requested path to (absolute path, canonical sandbox-relative path).

    Rejects absolute paths, `..`, `.git`, `.trash`, any `.env*` file, and symlinks
    that leave the sandbox root.
    """
    if not isinstance(rel, str) or "\x00" in rel:
        raise ServiceError(400, "invalid path")
    rel = rel.strip().replace("\\", "/")
    root_real = root.resolve()
    if rel in ("", "."):
        if allow_root:
            return root_real, ""
        raise ServiceError(400, "a file path is required")
    if rel.startswith(("/", "~")) or PurePosixPath(rel).is_absolute():
        raise ServiceError(403, "absolute paths are not allowed")
    parts = tuple(p for p in PurePosixPath(rel).parts if p != ".")
    if ".." in parts:
        raise ServiceError(403, "path escapes the repo")
    if any(is_blocked(p) for p in parts):
        raise ServiceError(403, "path is not accessible")
    target = root_real.joinpath(*parts).resolve()
    if target != root_real and not target.is_relative_to(root_real):
        raise ServiceError(403, "path escapes the repo")
    resolved_parts = target.relative_to(root_real).parts
    if any(is_blocked(p) for p in resolved_parts):
        raise ServiceError(403, "path is not accessible")
    return target, "/".join(resolved_parts)
