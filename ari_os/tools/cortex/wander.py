"""Associative wander for ARI-OS Cortex.

Wander deliberately surfaces a bounded, off-focus memory from the local brain.
It uses the same SQLite substrate and retrieval primitives as normal recall,
but chooses for novelty, distant workspace, and default-mode-network regions
instead of direct query match.
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .db import connect
from .embed import EmbedClient, default_embed_client
from .retrieve import RankedChunk, _hydrate, expand_via_tracts, retrieve, vec_search

DMN_REGIONS = {"hippocampus", "vmpfc"}


@dataclass
class WanderResult:
    focus_ids: list[int]
    seed: RankedChunk | None
    tangent: list[RankedChunk] = field(default_factory=list)
    divergence: float = 0.3
    fired: bool = False


@dataclass
class Candidate:
    chunk_id: int
    region: str
    workspace: str | None
    retrieved_count: int
    coact_to_focus: int


_WANDER_DEFAULTS = {
    "enabled": True,
    "divergence": 0.3,
    "cadence_min": 5,
    "cadence_max": 10,
    "divergence_min": 0.1,
    "divergence_max": 0.6,
}


def adaptive_divergence(base: float, confidence: float, *, lo: float, hi: float) -> float:
    """Scale divergence by retrieval confidence and clamp to ``[lo, hi]``."""
    c = min(max(confidence, 0.0), 1.0)
    if c >= 0.5:
        d = base + (hi - base) * (c - 0.5) * 2.0
    else:
        d = lo + (base - lo) * (c / 0.5)
    return min(max(d, lo), hi)


def resolve_wander_params(mode_name: str) -> dict:
    """Merge a mode YAML's optional ``wander`` block over wander defaults."""
    try:
        from .modes.loader import load_mode

        mode = load_mode(mode_name)
    except Exception:
        return dict(_WANDER_DEFAULTS)
    return {**_WANDER_DEFAULTS, **(mode.get("wander") or {})}


def _candidate_pool(db_path: Path, focus_ids: list[int], exclude_ids: list[int]) -> list[Candidate]:
    excluded = set(focus_ids) | set(exclude_ids)
    con = connect(db_path)
    try:
        coact: dict[int, int] = {}
        if focus_ids:
            placeholders = ",".join("?" * len(focus_ids))
            rows = con.execute(
                f"""SELECT to_chunk, SUM(co_activations)
                      FROM tract_edge
                     WHERE from_chunk IN ({placeholders})
                  GROUP BY to_chunk""",
                focus_ids,
            ).fetchall()
            coact = {int(cid): int(count or 0) for cid, count in rows}
        rows = con.execute(
            """SELECT c.id, c.region, s.workspace, c.retrieved_count
                 FROM chunk c JOIN source s ON c.source_id = s.id"""
        ).fetchall()
    finally:
        con.close()

    out: list[Candidate] = []
    for cid, region, workspace, retrieved_count in rows:
        if cid in excluded:
            continue
        out.append(
            Candidate(
                chunk_id=cid,
                region=region,
                workspace=workspace,
                retrieved_count=retrieved_count,
                coact_to_focus=coact.get(cid, 0),
            )
        )
    return out


def _score_candidate(candidate: Candidate, focus_workspaces: set, divergence: float) -> float:
    region_weight = 2.0 if candidate.region in DMN_REGIONS else 1.0
    novelty = 1.0 / (1.0 + candidate.retrieved_count)
    decoupling = 1.0 / (1.0 + candidate.coact_to_focus)
    same_workspace = candidate.workspace in focus_workspaces
    distance = (1.0 - divergence) + divergence * (0.0 if same_workspace else 1.0)
    return region_weight * novelty * decoupling * distance


def _focus_confidence(distances: list[float], ws_per_hit: list[str | None]) -> float:
    if not distances:
        return 0.5
    mean_distance = sum(distances) / len(distances)
    proximity = 1.0 - min(max(mean_distance, 0.0), 1.0)
    if ws_per_hit:
        counts: dict[str | None, int] = {}
        for workspace in ws_per_hit:
            counts[workspace] = counts.get(workspace, 0) + 1
        concentration = max(counts.values()) / len(ws_per_hit)
    else:
        concentration = 1.0
    return 0.5 * proximity + 0.5 * concentration


def _focus_near_set(db_path: Path, focus_text: str, embed_client: EmbedClient, k: int = 8):
    query_vec = embed_client.embed([focus_text])[0]
    if query_vec is None:  # no embedding backend: wander cannot fire
        return [], set(), [], []
    hits = vec_search(db_path, query_vec, k=k)
    ids = [hit.chunk_id for hit in hits]
    distances = [hit.distance for hit in hits]
    ws_by_id: dict[int, str | None] = {}
    if ids:
        con = connect(db_path)
        try:
            placeholders = ",".join("?" * len(ids))
            rows = con.execute(
                f"""SELECT c.id, s.workspace
                      FROM chunk c JOIN source s ON c.source_id = s.id
                     WHERE c.id IN ({placeholders})""",
                ids,
            ).fetchall()
            ws_by_id = {int(cid): workspace for cid, workspace in rows}
        finally:
            con.close()
    ws_per_hit = [ws_by_id.get(cid) for cid in ids]
    return ids, set(ws_per_hit), distances, ws_per_hit


def _weighted_pick(candidates: list[Candidate], weights: list[float], rng: random.Random):
    total = sum(weights)
    if total <= 0:
        return None
    target = rng.random() * total
    acc = 0.0
    for candidate, weight in zip(candidates, weights):
        acc += weight
        if target <= acc:
            return candidate
    return candidates[-1]


def wander(
    db_path: Path,
    focus_text: str,
    *,
    divergence: float = 0.3,
    embed_client: EmbedClient | None = None,
    recent_wander_ids: list[int] | None = None,
    k_focus: int = 8,
    min_pool: int = 5,
    rng_seed: int | None = None,
    adaptive: bool = False,
    divergence_min: float = 0.1,
    divergence_max: float = 0.9,
    enabled: bool | None = None,
    session_id: str | None = None,
) -> WanderResult:
    """Pick one bounded associative memory plus up to two tangent chunks."""
    if enabled is False or (enabled is None and not config.wander_enabled(True)):
        return WanderResult(focus_ids=[], seed=None, divergence=divergence, fired=False)
    if os.environ.get("ARI_OS_WANDER_YIELD") == "1" and session_id:
        try:
            from .council_marker import council_serving

            if council_serving(session_id):
                return WanderResult(focus_ids=[], seed=None, divergence=divergence, fired=False)
        except Exception:
            pass

    rng = random.Random(rng_seed)
    embed_client = embed_client or default_embed_client()
    focus_ids, focus_workspaces, focus_distances, focus_ws_per_hit = _focus_near_set(
        db_path, focus_text, embed_client, k=k_focus
    )
    if adaptive:
        confidence = _focus_confidence(focus_distances, focus_ws_per_hit)
        divergence = adaptive_divergence(
            divergence,
            confidence,
            lo=divergence_min,
            hi=divergence_max,
        )
    pool = _candidate_pool(db_path, focus_ids, recent_wander_ids or [])
    if len(pool) < min_pool:
        return WanderResult(focus_ids=focus_ids, seed=None, divergence=divergence, fired=False)

    weights = [_score_candidate(candidate, focus_workspaces, divergence) for candidate in pool]
    chosen = _weighted_pick(pool, weights, rng)
    if chosen is None:
        return WanderResult(focus_ids=focus_ids, seed=None, divergence=divergence, fired=False)

    con = connect(db_path)
    try:
        hydrated = _hydrate(con, [chosen.chunk_id], {chosen.chunk_id: "wander"})
    finally:
        con.close()
    if not hydrated:
        return WanderResult(focus_ids=focus_ids, seed=None, divergence=divergence, fired=False)
    seed_chunk = hydrated[0]

    tangent_ids = [
        adjacent.chunk_id
        for adjacent in expand_via_tracts(db_path, [chosen.chunk_id], per_seed=2, threshold=0.0)
    ][:2]
    tangent: list[RankedChunk] = []
    if tangent_ids:
        con = connect(db_path)
        try:
            tangent = _hydrate(con, tangent_ids, {chunk_id: "wander" for chunk_id in tangent_ids})
        finally:
            con.close()

    return WanderResult(
        focus_ids=focus_ids,
        seed=seed_chunk,
        tangent=tangent,
        divergence=divergence,
        fired=True,
    )


def _preview(text: str, limit: int = 200) -> str:
    preview = text.strip().split("\n\n", 1)[0]
    if len(preview) > limit:
        return preview[:limit] + "..."
    return preview


def render_wander_block(result: WanderResult, prompt_n: int | None = None) -> str:
    if not result.fired or result.seed is None:
        return ""
    header = "MIND-WANDER" + (f" (prompt {prompt_n})" if prompt_n else "")
    lines = [
        "---",
        header,
        "These chunks are from a distant part of memory. Note whether they rhyme with the current work, then carry on.",
        "",
    ]
    for chunk in [result.seed, *result.tangent]:
        lines.append(
            f"- [{chunk.path}:L{chunk.line_start}-L{chunk.line_end}] {_preview(chunk.text)}"
        )
    lines.append("---")
    return "\n".join(lines)


def render_surface_block(chunks: list[RankedChunk], prompt_n: int | None = None) -> str:
    if not chunks:
        return ""
    header = "SURFACE - global re-orientation"
    if prompt_n:
        header += f" (prompt {prompt_n})"
    lines = [
        "---",
        header,
        "The cadence forced a global pass across memory. Carry back anything useful, then dive back in.",
        "",
    ]
    for chunk in chunks:
        lines.append(
            f"- [{chunk.path}:L{chunk.line_start}-L{chunk.line_end}] {_preview(chunk.text)}"
        )
    lines.append("---")
    return "\n".join(lines)


def surface_pass(
    db_path: Path,
    focus_text: str,
    *,
    cwd: str | None = None,
    embed_client: EmbedClient | None = None,
    prompt_n: int | None = None,
    mode: str = "default",
) -> str:
    """Run a global re-orientation retrieval and render its banner."""
    if not config.wander_enabled(True):
        return ""
    embed_client = embed_client or default_embed_client()
    result = retrieve(
        db_path,
        focus_text,
        embed_client,
        mode=mode,
        cwd=cwd,
        posture="global",
        consumer="wander-surface",
    )
    return render_surface_block(result.chunks, prompt_n=prompt_n)
