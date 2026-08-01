"""ARI-OS Cortex — cwd/path → workspace slug mapping.

Single source of truth for turning a filesystem location into a workspace
slug, shared by retrieve (tunnel-posture gating), ingest (source tagging),
and distill (synthesis grouping). Three layers, checked in order:

1. Configured workspace roots — ``cortex.workspace_roots`` in
   ``$ARI_OS_HOME/config.json`` (list of absolute dirs, ``~`` allowed;
   default ``~/Desktop/ClaudeCode_Projects``). A path under
   ``<root>/<Child>/...`` maps to the normalized ``<Child>``.
2. The memories tree — ``$ARI_OS_HOME/memories/<ws>/...`` maps to ``<ws>``
   (:func:`path_to_workspace` only).
3. Legacy ``workspaces/<name>`` path components (the original heavy-brain
   layout), kept as a fallback.

Slugs are normalized (lowercase, ``_`` → ``-``) so repo folder names like
``EA_Animarek`` / ``EA-xfactor`` land on the existing memory folder names
``ea-animarek`` / ``ea-xfactor``.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .config import _config_value, state_home

# Default project root — override or extend via ``cortex.workspace_roots``.
DEFAULT_WORKSPACE_ROOTS = ("~/Desktop/ClaudeCode_Projects",)

# Claude Code's project-dir encoding collapses every non-alphanumeric char
# (``/``, ``_``, ``.``, spaces) to ``-`` — lossy, so it is only ever matched
# against re-encoded known paths, never decoded.
_CC_ENCODE_RE = re.compile(r"[^A-Za-z0-9-]")


def normalize_workspace(name: str) -> str:
    """Folder name → workspace slug: lowercase, underscores to dashes."""
    return name.strip().lower().replace("_", "-")


def workspace_roots() -> list[Path]:
    """Configured workspace root dirs (expanded), defaulting to the standard root."""
    raw = _config_value("cortex.workspace_roots")
    if not isinstance(raw, list) or not raw or not all(isinstance(r, str) for r in raw):
        raw = list(DEFAULT_WORKSPACE_ROOTS)
    return [Path(os.path.expanduser(r)) for r in raw]


def _roots_workspace(p: Path) -> str | None:
    """Slug for a path under ``<root>/<Child>/...``, else None."""
    for root in workspace_roots():
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if rel.parts:
            return normalize_workspace(rel.parts[0])
    return None


def _legacy_workspace(p: Path) -> str | None:
    """Original heavy-brain layout: the component after ``workspaces/``.

    Returned raw (no normalization) — legacy brains tagged sources with the
    verbatim folder name, and the tunnel gate must keep matching them.
    """
    parts = p.parts
    if "workspaces" in parts:
        i = parts.index("workspaces")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def cwd_to_workspace(cwd: str | None) -> str | None:
    """Map a cwd path to its workspace slug.

    Configured roots win; the legacy ``workspaces/`` layout is the fallback.
    Returns None when the cwd is outside every known layout.
    """
    if not cwd:
        return None
    p = Path(cwd)
    return _roots_workspace(p) or _legacy_workspace(p)


def path_to_workspace(path: str | os.PathLike | None, *, legacy: bool = True) -> str | None:
    """Map an ingested file path to its workspace slug.

    Memory files under ``$ARI_OS_HOME/memories/<ws>/`` tag as ``<ws>``;
    anything else resolves like a cwd. ``legacy=False`` skips the
    ``workspaces/`` fallback for callers that keep their own legacy labels
    (distill grouping).
    """
    if not path:
        return None
    p = Path(path)
    try:
        rel = p.relative_to(state_home() / "memories")
    except ValueError:
        rel = None
    if rel is not None and len(rel.parts) > 1:
        return normalize_workspace(rel.parts[0])
    ws = _roots_workspace(p)
    if ws or not legacy:
        return ws
    return _legacy_workspace(p)


def encode_cc_project_dirname(path_str: str) -> str:
    """Re-encode an absolute path the way CC names ``~/.claude/projects`` dirs."""
    return _CC_ENCODE_RE.sub("-", path_str)


def transcript_dir_to_workspace(jsonl_path: str | os.PathLike) -> str | None:
    """Fallback mapper for ``~/.claude/projects/<encoded-cwd>/*.jsonl`` files.

    The encoded dirname is lossy, so each configured root's real children are
    re-encoded and prefix-matched against it instead of decoding. The longest
    matching child wins (guards ``EA-x`` vs ``EA-x-archive`` siblings). Only
    used when no transcript record carries a usable ``cwd``.
    """
    dirname = Path(jsonl_path).parent.name
    best: str | None = None
    best_len = -1
    for root in workspace_roots():
        prefix = encode_cc_project_dirname(str(root)) + "-"
        if not dirname.startswith(prefix):
            continue
        rest = dirname[len(prefix):]
        try:
            children = [c.name for c in root.iterdir() if c.is_dir()]
        except OSError:
            continue
        for child in children:
            enc = encode_cc_project_dirname(child)
            if len(enc) <= best_len:
                continue
            if rest == enc or rest.startswith(enc + "-"):
                best = normalize_workspace(child)
                best_len = len(enc)
    return best
