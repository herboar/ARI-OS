"""ARI-OS Cortex — single-machine ingest pipeline.

Scans for changed source files, chunks them, embeds the chunks, and writes
them to the local brain DB along with their vectors. ARI-OS is a
single-machine SQLite brain — no mesh, no router.

Default ingest source: ``~/.claude/projects/**/*.jsonl`` (the user's own
Claude Code transcripts). Repo-file (markdown) indexing is a flag,
**default off** — it must be opted into explicitly so a personal
ARI-OS install never silently sweeps the local repo.

Three entry points:

- ``index_file(db_path, path, layer, embed_client)`` — ingest a single file
  (auto-detects ``.jsonl`` vs markdown by extension). Markdown ingest
  requires ``markdown_indexing_enabled()`` to be True; it is **off by
  default** for personal installs.
- ``index_text(db_path, text, *, source, embed_client)`` — explicit-text
  ingest for ``/remember`` (single chunk, no file path).
- ``index_sweep(db_path, roots, excludes, embed_client)`` — incremental
  sweep over the default transcript roots.
"""
from __future__ import annotations

import fnmatch
import glob
import hashlib
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import db as _db
from . import vec_sidecar
from .chunker import chunk_markdown
from .config import state_home
from .transcript_filter import filter_jsonl


# --- Defaults ---------------------------------------------------------------

# ARI-OS is a personal brain. Default ingest is the user's own CC transcripts.
DEFAULT_TRANSCRIPT_GLOB = "~/.claude/projects/**/*.jsonl"
DEFAULT_TRANSCRIPT_LAYER = "episodic"

# All ARI-OS regions (kept here so the classifier is self-contained for the
# ar.t3 task — the full region_anchors loader is added in ar.t6).
REGIONS = (
    "wernicke", "broca", "occipital", "parietal",
    "hippocampus", "vmpfc", "frontoparietal",
)

# Markdown indexing is OFF by default — a personal install must opt in.
_MARKDOWN_INDEXING = False


def markdown_indexing_enabled() -> bool:
    return _MARKDOWN_INDEXING


def set_markdown_indexing(enabled: bool) -> None:
    """Opt-in switch for repo-file (markdown) ingest. Off by default."""
    global _MARKDOWN_INDEXING
    _MARKDOWN_INDEXING = bool(enabled)


def default_ingest_roots() -> list[tuple[str, str]]:
    """Default (glob, layer) pairs: the user's CC transcript trees."""
    return [(str(Path(DEFAULT_TRANSCRIPT_GLOB).expanduser()), DEFAULT_TRANSCRIPT_LAYER)]


# --- Scan result ------------------------------------------------------------

@dataclass
class ScanResult:
    path: Path
    layer: str
    workspace: str | None
    sha256: str
    mtime: int
    is_changed: bool


# --- Helpers ----------------------------------------------------------------

def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def _matches_any(p: Path, patterns: list[str]) -> bool:
    s = str(p)
    return any(fnmatch.fnmatch(s, pat) for pat in patterns)


def _pack(vec: list[float]) -> bytes:
    import struct
    return struct.pack(f"{len(vec)}f", *vec)


def _iso_to_epoch(iso: str, fallback: int) -> int:
    if not iso:
        return fallback
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00"))
                   .replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return fallback


def _classify_region(path: Path, *, layer: str, content_kind: str | None) -> str:
    """Minimal region routing — sufficient for ar.t3 (CC transcripts + /remember).

    jsonl: user → wernicke (language comprehension), assistant → broca (production).
    Other extensions / text: by layer.
    """
    if path.suffix == ".jsonl":
        if content_kind == "user":
            return "wernicke"
        if content_kind == "assistant":
            return "broca"
    return {
        "core": "frontoparietal",
        "semantic": "wernicke",
        "procedural": "frontoparietal",
        "episodic": "hippocampus",
        "short_term": "hippocampus",
    }.get(layer, "wernicke")


def _importance_for(layer: str, region: str, text: str, mtime: int) -> float:
    base = {
        "core": 1.0,
        "procedural": 0.8,
        "semantic": 0.7,
        "episodic": 0.5,
        "short_term": 0.5,
    }.get(layer, 0.5)
    bonus = 0.0
    if "🚨" in text:
        bonus += 0.1
    if time.time() - mtime < 7 * 86400:
        bonus += 0.1
    return max(0.0, min(1.0, base + bonus))


# --- Source bookkeeping -----------------------------------------------------

def known_sources(db_path: Path) -> dict[str, tuple[int, str]]:
    """{path: (mtime, sha256)} for every source row."""
    con = sqlite3.connect(db_path)
    try:
        return {
            r[0]: (r[1], r[2])
            for r in con.execute("SELECT path, mtime, sha256 FROM source")
        }
    finally:
        con.close()


def source_id_for_path(db_path: Path, path) -> int | None:
    """source.id for a path, or None."""
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT id FROM source WHERE path = ?", (str(path),)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def mark_indexed(
    con: sqlite3.Connection,
    path: Path,
    sha: str,
    mtime: int,
    layer: str,
    workspace: str | None,
) -> int:
    """Upsert source row, return source_id."""
    now = int(time.time())
    con.execute(
        """INSERT INTO source(path, layer, workspace, mtime, sha256, last_indexed_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET
             layer=excluded.layer, workspace=excluded.workspace,
             mtime=excluded.mtime, sha256=excluded.sha256,
             last_indexed_at=excluded.last_indexed_at""",
        (str(path), layer, workspace, mtime, sha, now),
    )
    row = con.execute("SELECT id FROM source WHERE path = ?", (str(path),)).fetchone()
    return row[0]


# --- Scan changed files -----------------------------------------------------

def scan_changed_files(
    db_path: Path,
    roots: list[tuple[str, str]],
    excludes: list[str],
) -> Iterator[ScanResult]:
    known: dict[str, tuple[int, str]] = known_sources(db_path)
    seen: set[str] = set()
    for pattern, layer in roots:
        for match in glob.glob(str(pattern), recursive=True):
            p = Path(match)
            if str(p) in seen:
                continue
            seen.add(str(p))
            if _matches_any(p, excludes):
                continue
            try:
                if not p.is_file():
                    continue
                mtime = int(p.stat().st_mtime)
            except OSError:
                continue
            known_mtime, known_sha = known.get(str(p), (None, None))
            if known_mtime == mtime:
                continue  # unchanged short-circuit
            sha = _sha256(p)
            is_changed = known_sha != sha
            yield ScanResult(
                path=p,
                layer=layer,
                workspace=None,
                sha256=sha,
                mtime=mtime,
                is_changed=is_changed,
            )


# --- index_file: per-extension dispatch -------------------------------------

def index_file(
    db_path: Path,
    path: Path,
    layer: str,
    embed_client,
    *,
    region_hint: str | None = None,
) -> list[int]:
    """Read, chunk, embed, write one file. Returns inserted chunk_ids.

    .jsonl → transcript ingest (always allowed).
    anything else → markdown chunking, **gated by markdown_indexing_enabled()**.
    """
    if path.suffix == ".jsonl":
        return _index_jsonl(db_path, path, layer, embed_client)

    if not markdown_indexing_enabled():
        raise PermissionError(
            "markdown/repo-file indexing is disabled. Call "
            "ari_os.tools.cortex.index.set_markdown_indexing(True) to enable."
        )

    mtime = int(path.stat().st_mtime)
    sha = _sha256(path)
    text = path.read_text(errors="replace")
    region = region_hint or _classify_region(path, layer=layer, content_kind=None)
    chunks = chunk_markdown(text)

    con = _db.connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        source_id = mark_indexed(con, path, sha, mtime, layer, None)
        # Drop any previous chunks for this source
        old_ids = [r[0] for r in con.execute(
            "SELECT id FROM chunk WHERE source_id = ?", (source_id,)
        ).fetchall()]
        for oid in old_ids:
            con.execute("DELETE FROM chunk_vec WHERE rowid = ?", (oid,))
            vec_sidecar.dual_delete(con, oid)
        con.execute("DELETE FROM chunk WHERE source_id = ?", (source_id,))
        chunk_ids: list[int] = []
        texts = [c.text for c in chunks]
        if texts:
            embeddings = embed_client.embed(texts)
            for chunk, vec in zip(chunks, embeddings):
                imp = _importance_for(layer, region, chunk.text, mtime)
                cur = con.execute(
                    """INSERT INTO chunk(source_id, ordinal, text, line_start, line_end,
                                         region, importance, distillation_tier)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                    (source_id, len(chunk_ids), chunk.text, chunk.line_start,
                     chunk.line_end, region, imp),
                )
                cid = cur.lastrowid
                if vec is not None:
                    packed = _pack(vec)
                    con.execute(
                        "INSERT INTO chunk_vec(rowid, embedding) VALUES (?, ?)",
                        (cid, packed),
                    )
                    vec_sidecar.dual_write(con, cid, packed, region)
                chunk_ids.append(cid)
        con.execute("COMMIT")
        return chunk_ids
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def _index_jsonl(
    db_path: Path,
    path: Path,
    layer: str,
    embed_client,
) -> list[int]:
    """Ingest a Claude Code .jsonl transcript."""
    mtime = int(path.stat().st_mtime)
    sha = _sha256(path)

    # Drop empty/whitespace segments: they embed to dim-0 and would fail the
    # whole file (mirrors the empty-chunk guard in chunker._finalize_chunks).
    segments = [s for s in filter_jsonl(path) if s.text.strip()]

    seg_meta: list[tuple[int, str, str, float, str | None, int]] = []
    for i, seg in enumerate(segments):
        region = _classify_region(path, layer=layer, content_kind=seg.kind)
        imp = _importance_for(layer, region, seg.text, mtime)
        cts = _iso_to_epoch(seg.timestamp_iso, mtime)
        seg_meta.append((i, seg.text, region, imp, seg.session_id or None, cts))

    # Batch-embed BEFORE opening the write transaction. Per-segment embed
    # calls inside BEGIN IMMEDIATE would hold the writer lock for tens of
    # seconds on long transcripts.
    embeddings = embed_client.embed([m[1] for m in seg_meta]) if seg_meta else []

    con = _db.connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        source_id = mark_indexed(con, path, sha, mtime, layer, None)
        old_ids = [r[0] for r in con.execute(
            "SELECT id FROM chunk WHERE source_id = ?", (source_id,)
        ).fetchall()]
        for oid in old_ids:
            con.execute("DELETE FROM chunk_vec WHERE rowid = ?", (oid,))
            vec_sidecar.dual_delete(con, oid)
        con.execute("DELETE FROM chunk WHERE source_id = ?", (source_id,))
        chunk_ids: list[int] = []
        for (i, text, region, imp, sess, cts), vec in zip(seg_meta, embeddings):
            cur = con.execute(
                """INSERT INTO chunk(source_id, ordinal, text, line_start, line_end,
                                     region, importance, session_id, created_ts,
                                     distillation_tier)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (source_id, i, text, i, i, region, imp, sess, cts),
            )
            cid = cur.lastrowid
            if vec is not None:
                packed = _pack(vec)
                con.execute(
                    "INSERT INTO chunk_vec(rowid, embedding) VALUES (?, ?)",
                    (cid, packed),
                )
                vec_sidecar.dual_write(con, cid, packed, region)
            chunk_ids.append(cid)
        con.execute("COMMIT")
        return chunk_ids
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def index_text(
    db_path: Path,
    text: str,
    *,
    source: str,
    layer: str = "semantic",
    embed_client,
) -> int:
    """Explicit-text ingest for /remember: one chunk, no file path.

    The source string is stored verbatim as ``source.path`` (the test and
    ``/remember`` use a stable id like ``remember:<ts>`` or similar).
    """
    text = text.strip()
    if not text:
        raise ValueError("index_text: text is empty")
    embeddings = embed_client.embed([text])
    vec = embeddings[0]
    p = Path(source)
    region = _classify_region(p, layer=layer, content_kind=None)
    imp = _importance_for(layer, region, text, int(time.time()))

    con = _db.connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        source_id = mark_indexed(con, p, _sha256_of_text(text), int(time.time()), layer, None)
        old_ids = [r[0] for r in con.execute(
            "SELECT id FROM chunk WHERE source_id = ?", (source_id,)
        ).fetchall()]
        for oid in old_ids:
            con.execute("DELETE FROM chunk_vec WHERE rowid = ?", (oid,))
            vec_sidecar.dual_delete(con, oid)
        con.execute("DELETE FROM chunk WHERE source_id = ?", (source_id,))
        cur = con.execute(
            """INSERT INTO chunk(source_id, ordinal, text, line_start, line_end,
                                 region, importance, distillation_tier)
               VALUES (?, 0, ?, 1, 1, ?, ?, 0)""",
            (source_id, text, region, imp),
        )
        cid = cur.lastrowid
        if vec is not None:
            packed = _pack(vec)
            con.execute(
                "INSERT INTO chunk_vec(rowid, embedding) VALUES (?, ?)",
                (cid, packed),
            )
            vec_sidecar.dual_write(con, cid, packed, region)
        con.execute("COMMIT")
        return cid
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def _sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Full sweep -------------------------------------------------------------

def index_sweep(
    db_path: Path,
    roots: list[tuple[str, str]] | None = None,
    excludes: list[str] | None = None,
    embed_client=None,
) -> dict[str, int]:
    """Incremental sweep: scan → reindex changed files.

    Defaults to the user's CC transcript tree. Markdown ingest is **off by
    default**; if it stays off, this sweep never touches repo files.
    """
    if roots is None:
        roots = default_ingest_roots()
    if excludes is None:
        excludes = [
            "**/.git/**", "**/node_modules/**", "**/__pycache__/**",
            "**/*.log", "**/_archived/**", "**/_trash/**",
        ]
    if embed_client is None:
        from .embed import EmbedClient  # late import: embed.py lands in ar.t5
        embed_client = EmbedClient()

    stats: dict[str, int] = {"scanned": 0, "indexed": 0, "errors": 0}
    for change in scan_changed_files(db_path, roots, excludes):
        stats["scanned"] += 1
        if not change.is_changed:
            continue
        try:
            index_file(db_path, change.path, change.layer, embed_client)
            stats["indexed"] += 1
        except Exception as exc:
            stats["errors"] += 1
            print(f"index error: {change.path}: {type(exc).__name__}: {exc}")
    return stats
