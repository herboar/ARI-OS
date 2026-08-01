"""Tiered Cortex memory distillation.

The public ARI-OS distiller is deliberately local-only: it reads and writes the
SQLite brain, creates higher-tier summary chunks, and never deletes source
memories. The LLM is an optional gate. When the configured backend resolves to
``None``, every public pass exits cleanly with no changes.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import logging
import re
import time
from pathlib import Path

from ari_os.tools.cortex import config
from ari_os.tools.cortex.db import connect
from ari_os.tools.cortex.llm import get_llm
from ari_os.tools.cortex.llm.base import BrainLLM
from ari_os.tools.cortex.model_routing import model_for_stage
from ari_os.tools.cortex.workspace_map import path_to_workspace

logger = logging.getLogger(__name__)

HOT_THRESHOLD_SECS = 24 * 3600
MIN_CHUNKS_TO_DISTILL = 2
MAX_SYNTH_INPUT_CHARS = 48_000
_AUTO_LLM = object()


@dataclass(frozen=True)
class _ChunkRow:
    id: int
    source_id: int
    text: str
    last_retrieved_at: int | None
    distilled_at: int | None
    path: str | None
    workspace: str | None


@contextmanager
def distill_lock(db_path: Path):
    """Exclusive SQLite process lock for one distill run."""
    lock_path = Path(db_path).parent / ".distill.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    with lock_path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def region_for_tier(tier: int) -> str:
    """Route tier-1 digests to episodic recall, tier-2+ syntheses to semantics."""
    return "hippocampus" if tier <= 1 else "parietal"


def distill_session_to_digest(
    db_path: Path,
    output_dir: Path | None = None,
    llm: BrainLLM | None | object = _AUTO_LLM,
    max_sources: int | None = None,
) -> int:
    """Group tier-0 chunks by source and create one tier-1 digest per source."""
    resolved_llm = _resolve_llm(llm)
    if resolved_llm is None:
        return 0

    with distill_lock(db_path):
        rows = _chunk_rows_for_tier(db_path, 0)
        existing_sources = _source_ids_with_child_tier(db_path, 1)

        by_source: dict[int, list[_ChunkRow]] = defaultdict(list)
        for row in rows:
            by_source[row.source_id].append(row)

        created = 0
        for source_id, chunk_rows in by_source.items():
            if source_id in existing_sources:
                continue
            cold_rows = [row for row in chunk_rows if not _is_hot(row.last_retrieved_at)]
            if len(cold_rows) < MIN_CHUNKS_TO_DISTILL:
                continue

            texts = [row.text for row in cold_rows]
            parent_ids = [row.id for row in cold_rows]
            try:
                summary = resolved_llm.consolidate(texts)
            except Exception as exc:
                logger.warning("distill 0->1: skipping source %s: %s", source_id, exc)
                continue
            if not summary:
                continue

            summary_id = _insert_distilled_chunk(
                db_path, source_id, summary, tier=1, parent_ids=parent_ids
            )
            _write_artifact(
                _artifact_root(output_dir),
                label=f"source-{source_id}-chunk-{summary_id}",
                text=summary,
                tier_from=0,
                tier_to=1,
            )
            created += 1
            if max_sources is not None and created >= max_sources:
                break

        return created


def distill_daily_synthesis(
    db_path: Path,
    output_dir: Path | None = None,
    llm: BrainLLM | None | object = _AUTO_LLM,
) -> int:
    """Group tier-1 digests by day and workspace, then create tier-2 syntheses."""
    resolved_llm = _resolve_llm(llm)
    if resolved_llm is None:
        return 0

    with distill_lock(db_path):
        existing = {
            (_day_key(parent_ts), _group_workspace(parent_path, parent_workspace))
            for parent_ts, parent_path, parent_workspace in _parent_keys_for_child_tier(
                db_path, 2
            )
            if parent_ts is not None
        }
        groups = _group_rows_for_synthesis(_chunk_rows_for_tier(db_path, 1), _day_key)
        return _distill_synthesis_groups(
            db_path,
            groups,
            existing,
            resolved_llm,
            output_root=_artifact_root(output_dir),
            tier_from=1,
            tier_to=2,
            label_prefix="daily",
        )


def distill_weekly_arc(
    db_path: Path,
    output_dir: Path | None = None,
    llm: BrainLLM | None | object = _AUTO_LLM,
) -> int:
    """Group tier-2 syntheses by ISO week and workspace, then create tier-3 arcs."""
    resolved_llm = _resolve_llm(llm)
    if resolved_llm is None:
        return 0

    with distill_lock(db_path):
        existing = {
            (_week_key(parent_ts), _group_workspace(parent_path, parent_workspace))
            for parent_ts, parent_path, parent_workspace in _parent_keys_for_child_tier(
                db_path, 3
            )
            if parent_ts is not None
        }
        groups = _group_rows_for_synthesis(_chunk_rows_for_tier(db_path, 2), _week_key)
        return _distill_synthesis_groups(
            db_path,
            groups,
            existing,
            resolved_llm,
            output_root=_artifact_root(output_dir),
            tier_from=2,
            tier_to=3,
            label_prefix="weekly",
        )


def _resolve_llm(llm: BrainLLM | None | object) -> BrainLLM | None:
    if llm is _AUTO_LLM:
        return get_llm(model_for_stage("consolidation"))
    if llm is None:
        return None
    if isinstance(llm, BrainLLM):
        return llm
    if hasattr(llm, "consolidate") and hasattr(llm, "distill"):
        return llm
    return None


def _now() -> int:
    return int(time.time())


def _is_hot(last_retrieved_at: int | None) -> bool:
    if last_retrieved_at is None:
        return False
    return _now() - int(last_retrieved_at) < HOT_THRESHOLD_SECS


def _artifact_root(output_dir: Path | None) -> Path:
    base = Path(output_dir) if output_dir is not None else config.state_home()
    return base / "consolidation"


def _write_artifact(
    root: Path, *, label: str, text: str, tier_from: int, tier_to: int
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = _slugify(f"tier{tier_from}to{tier_to}_{label}_{ts}") + ".md"
    path = root / slug
    path.write_text(
        f"---\ntier_from: {tier_from}\ntier_to: {tier_to}\n"
        f"label: {label}\ncreated_at: {ts}\n---\n\n{text}\n"
    )
    return path


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-")
    return slug or "distillation"


def _chunk_rows_for_tier(db_path: Path, tier: int) -> list[_ChunkRow]:
    con = connect(db_path)
    try:
        rows = con.execute(
            """SELECT c.id, c.source_id, c.text, c.last_retrieved_at,
                      c.distilled_at, s.path, s.workspace
                 FROM chunk c
                 LEFT JOIN source s ON s.id = c.source_id
                WHERE c.distillation_tier = ?
             ORDER BY c.source_id, c.ordinal, c.id""",
            (tier,),
        ).fetchall()
    finally:
        con.close()
    return [
        _ChunkRow(
            id=row[0],
            source_id=row[1],
            text=row[2],
            last_retrieved_at=row[3],
            distilled_at=row[4],
            path=row[5],
            workspace=row[6],
        )
        for row in rows
    ]


def _source_ids_with_child_tier(db_path: Path, child_tier: int) -> set[int]:
    con = connect(db_path)
    try:
        rows = con.execute(
            "SELECT DISTINCT source_id FROM chunk WHERE distillation_tier = ?",
            (child_tier,),
        ).fetchall()
    finally:
        con.close()
    return {int(row[0]) for row in rows}


def _insert_distilled_chunk(
    db_path: Path,
    source_id: int,
    text: str,
    *,
    tier: int,
    parent_ids: list[int],
) -> int:
    now = _now()
    con = connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        ordinal = con.execute(
            "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM chunk WHERE source_id = ?",
            (source_id,),
        ).fetchone()[0]
        cur = con.execute(
            """INSERT INTO chunk(
                 source_id, ordinal, text, line_start, line_end, region,
                 importance, distillation_tier, parent_chunks, distilled_at,
                 consolidated_at
               ) VALUES (?, ?, ?, 0, 0, ?, 0.6, ?, ?, ?, ?)""",
            (
                source_id,
                ordinal,
                text,
                region_for_tier(tier),
                tier,
                json.dumps(parent_ids),
                now,
                now,
            ),
        )
        child_id = int(cur.lastrowid)
        for parent_id in parent_ids:
            _upsert_edge(con, parent_id, child_id, "uncinate", 0.8)
            _upsert_edge(con, child_id, parent_id, "uncinate", 0.8)
        con.execute("COMMIT")
        return child_id
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def _upsert_edge(con, from_chunk: int, to_chunk: int, tract: str, weight: float) -> None:
    con.execute(
        """INSERT INTO tract_edge(
             from_chunk, to_chunk, tract, weight, co_activations, last_fired_at
           ) VALUES (?, ?, ?, ?, 1, ?)
           ON CONFLICT(from_chunk, to_chunk, tract) DO UPDATE SET
             weight = excluded.weight,
             co_activations = tract_edge.co_activations + 1,
             last_fired_at = excluded.last_fired_at""",
        (from_chunk, to_chunk, tract, weight, _now()),
    )


def _group_rows_for_synthesis(
    rows: list[_ChunkRow], key_fn
) -> dict[tuple[str, str], list[_ChunkRow]]:
    groups: dict[tuple[str, str], list[_ChunkRow]] = defaultdict(list)
    skipped_null = 0
    for row in rows:
        if row.distilled_at is None:
            skipped_null += 1
            continue
        if _is_hot(row.last_retrieved_at):
            continue
        groups[(key_fn(row.distilled_at), _group_workspace(row.path, row.workspace))].append(row)
    if skipped_null:
        logger.warning("distill synthesis: skipped %d rows without distilled_at", skipped_null)
    return groups


def _distill_synthesis_groups(
    db_path: Path,
    groups: dict[tuple[str, str], list[_ChunkRow]],
    existing: set[tuple[str, str]],
    llm: BrainLLM,
    *,
    output_root: Path,
    tier_from: int,
    tier_to: int,
    label_prefix: str,
) -> int:
    created = 0
    for (period, workspace), rows in sorted(groups.items()):
        if (period, workspace) in existing:
            continue
        if len(rows) < MIN_CHUNKS_TO_DISTILL:
            continue
        parent_ids = [row.id for row in rows]
        texts = [row.text for row in rows]
        try:
            summary = _synthesize(llm, texts, tier=tier_from)
        except Exception as exc:
            logger.warning(
                "distill %s->%s: skipping group %s/%s: %s",
                tier_from,
                tier_to,
                period,
                workspace,
                exc,
            )
            continue
        if not summary:
            continue
        child_id = _insert_distilled_chunk(
            db_path,
            rows[0].source_id,
            summary,
            tier=tier_to,
            parent_ids=parent_ids,
        )
        _write_artifact(
            output_root,
            label=f"{label_prefix}-{period}-{workspace}-chunk-{child_id}",
            text=summary,
            tier_from=tier_from,
            tier_to=tier_to,
        )
        created += 1
    return created


def _synthesize(llm: BrainLLM, texts: list[str], tier: int) -> str:
    joined = "\n---\n".join(texts)
    if len(texts) == 1 or len(joined) <= MAX_SYNTH_INPUT_CHARS:
        return llm.distill(joined, tier=tier)

    batches: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for text in texts:
        projected = current_len + len(text) + 5
        if current and projected > MAX_SYNTH_INPUT_CHARS:
            batches.append(current)
            current = []
            current_len = 0
        current.append(text)
        current_len += len(text) + 5
    if current:
        batches.append(current)

    partials = [llm.distill("\n---\n".join(batch), tier=tier) for batch in batches]
    if len(partials) >= len(texts):
        return llm.distill("\n---\n".join(partials), tier=tier)
    return _synthesize(llm, partials, tier)


def _parent_keys_for_child_tier(
    db_path: Path, child_tier: int
) -> list[tuple[int | None, str | None, str | None]]:
    con = connect(db_path)
    try:
        child_rows = con.execute(
            "SELECT parent_chunks FROM chunk WHERE distillation_tier = ? "
            "AND parent_chunks IS NOT NULL",
            (child_tier,),
        ).fetchall()
        parent_ids: list[int] = []
        for (parent_json,) in child_rows:
            try:
                parents = json.loads(parent_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if parents:
                parent_ids.append(int(parents[0]))
        if not parent_ids:
            return []
        placeholders = ",".join("?" for _ in parent_ids)
        rows = con.execute(
            f"""SELECT c.distilled_at, s.path, s.workspace
                  FROM chunk c
                  LEFT JOIN source s ON s.id = c.source_id
                 WHERE c.id IN ({placeholders})""",
            parent_ids,
        ).fetchall()
    finally:
        con.close()
    return [(row[0], row[1], row[2]) for row in rows]


def _group_workspace(path: str | None, workspace: str | None = None) -> str:
    if workspace:
        return workspace
    if not path:
        return "misc"
    # Configured workspace roots + memories tree first (shared mapper);
    # legacy=False keeps the historical "workspaces/<name>" label below.
    mapped = path_to_workspace(path, legacy=False)
    if mapped:
        return mapped
    parts = [part for part in Path(path).parts if part not in {"", "."}]
    if "workspaces" in parts:
        idx = parts.index("workspaces")
        if idx + 1 < len(parts):
            return f"workspaces/{parts[idx + 1]}"
    return parts[0] if parts else "misc"


def _day_key(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def _week_key(epoch: int) -> str:
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"


__all__ = [
    "distill_daily_synthesis",
    "distill_lock",
    "distill_session_to_digest",
    "distill_weekly_arc",
    "region_for_tier",
]
