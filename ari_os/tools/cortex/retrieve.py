"""ARI-OS Cortex — hybrid retrieval engine.

This is the live retrieval spine of the ARI-OS brain: FTS5 / BM25 sparse recall
unioned with sqlite-vec dense recall, fused via Reciprocal Rank Fusion (RRF),
reranked with divisive-normalization diversity, monotropic per-cwd workspace
gating, and optional knowledge-graph expansion. Public port + scrub of the
private engine's retrieve module.

The single-machine SQLite substrate is the only backend — every code path
that branched on backend in the source engine is collapsed here.

Public surface:

- :class:`RankedChunk` / :class:`VecHit` / :class:`SparseHit` / :class:`AdjacentHit`
  — small dataclasses returned by the search/rerank helpers.
- :class:`RetrievalResult` — what :func:`retrieve` returns (chunk list, query,
  mode, ranked ids, posture offer).
- :func:`vec_search`, :func:`fts_search`, :func:`rrf_fuse` — primitive layers.
- :func:`expand_via_tracts`, :func:`kg_expand` — structural / KG expansion.
- :func:`rerank` — scoring + divisive normalization.
- :func:`retrieve` — top-level entry point.
- :func:`emit_markdown` / :func:`format_for_harness` — renderers.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .config import ASSEMBLER_ENABLED
from .db import connect
from .embed import EmbedClient, pack_embedding

# Sufficiency gate parked: the shipped L2 distances are un-normalized, so dense
# distance is an unreliable sufficiency signal. Hybrid recall (the proven half)
# ships now; the gate revives after embedding normalization lands. Flip to
# True to re-activate.
GATE_ENABLED = False


@dataclass
class VecHit:
    chunk_id: int
    distance: float


def vec_search(db_path: Path, query_vec: list[float], k: int = 12) -> list[VecHit]:
    con = connect(db_path)
    try:
        rows = con.execute(
            """SELECT rowid, distance
               FROM chunk_vec
               WHERE embedding MATCH ?
               ORDER BY distance
               LIMIT ?""",
            (pack_embedding(query_vec), k),
        ).fetchall()
    finally:
        con.close()
    return [VecHit(chunk_id=r[0], distance=r[1]) for r in rows]


@dataclass
class SparseHit:
    chunk_id: int
    bm25: float  # lower (more negative) = stronger lexical match


# FTS5 OR-union cost grows superlinearly in term count; document-length queries
# (SessionStart pre-fetches) are the offenders. Real typed queries sit far
# below the cap.
_FTS_MAX_TERMS = 64


def _fts_query(raw: str) -> str:
    """Turn arbitrary user text into a safe FTS5 OR-of-terms query.

    Strips FTS5 syntax by keeping only alphanumeric/underscore tokens and
    quoting each. Terms are deduped case-insensitively (first occurrence wins)
    and capped at :data:`_FTS_MAX_TERMS`.
    """
    seen: set[str] = set()
    out: list[str] = []
    for t in re.findall(r"\w+", raw):
        tl = t.lower()
        if tl in seen:
            continue
        seen.add(tl)
        out.append(t)
        if len(out) >= _FTS_MAX_TERMS:
            break
    return " OR ".join(f'"{t}"' for t in out)


def fts_search(db_path: Path, query: str, k: int = 12) -> list[SparseHit]:
    q = _fts_query(query)
    if not q:
        return []
    con = connect(db_path)
    try:
        rows = con.execute(
            """SELECT rowid, bm25(chunk_fts) AS score
                 FROM chunk_fts
                WHERE chunk_fts MATCH ?
                ORDER BY score
                LIMIT ?""",
            (q, k),
        ).fetchall()
    except sqlite3.OperationalError:
        # Sparse recall is best-effort: a brain not yet migrated has no
        # chunk_fts table. Degrade to dense-only rather than break retrieval
        # on the SessionStart path.
        return []
    finally:
        con.close()
    return [SparseHit(chunk_id=r[0], bm25=r[1]) for r in rows]


def rrf_fuse(ranked_lists: list[list[int]], c: int = 60) -> dict[int, float]:
    """Reciprocal Rank Fusion. Each list is chunk_ids in rank order (best first).
    Returns {chunk_id: fused_score}; higher = better.
    """
    scores: dict[int, float] = {}
    for lst in ranked_lists:
        for rank, cid in enumerate(lst, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (c + rank)
    return scores


@dataclass
class AdjacentHit:
    chunk_id: int
    via_tract: str
    weight: float
    from_seed: int


def expand_via_tracts(
    db_path: Path,
    seed_ids: list[int],
    per_seed: int = 3,
    threshold: float = 0.3,
    tracts: tuple[str, ...] | None = None,
) -> list[AdjacentHit]:
    """For each seed chunk, pull the top-N tract-edge neighbours above threshold."""
    if not seed_ids:
        return []
    con = connect(db_path)
    try:
        out: list[AdjacentHit] = []
        for seed in seed_ids:
            params: list = [seed, threshold]
            sql = """SELECT to_chunk, tract, weight FROM tract_edge
                     WHERE from_chunk = ? AND weight >= ?"""
            if tracts:
                placeholders = ",".join("?" * len(tracts))
                sql += f" AND tract IN ({placeholders})"
                params.extend(tracts)
            sql += " ORDER BY weight DESC LIMIT ?"
            params.append(per_seed)
            rows = con.execute(sql, tuple(params)).fetchall()
            for to_cid, tract, w in rows:
                if to_cid in seed_ids:
                    continue
                out.append(AdjacentHit(chunk_id=to_cid, via_tract=tract, weight=w, from_seed=seed))
        return out
    finally:
        con.close()


@dataclass
class RankedChunk:
    chunk_id: int
    distance: float
    region: str
    tier: int
    importance: float
    retrieved_count: int
    text: str
    path: str
    line_start: int
    line_end: int
    workspace: str | None
    source: str  # "vec" | "sparse" | "adjacent" | "kg"
    score: float = 0.0
    zone: str | None = None


# Cognitive-science region names (published terms). The shipped
# task_classifier and region_anchors modules share this vocabulary.
DEFAULT_REGION_WEIGHTS = {
    "vmpfc": 1.5, "frontoparietal": 1.2, "wernicke": 1.0,
    "broca": 1.0, "occipital": 1.0, "parietal": 1.0, "hippocampus": 0.9,
}
DEFAULT_TIER_WEIGHTS = {0: 1.0, 1: 0.95, 2: 0.9, 3: 0.95, 4: 1.1}

# A direct retrieval hit (dense vector or sparse/BM25) outranks a chunk that
# only surfaced structurally (tract adjacency or kg-entity expansion).
# Adjacency carries no query-similarity signal of its own.
SOURCE_WEIGHTS = {"vec": 1.0, "sparse": 1.0, "adjacent": 0.4, "kg": 0.5}


def cwd_to_workspace(cwd: str | None) -> str | None:
    """Map a cwd path to its workspace name.

    Looks for 'workspaces' in path parts and returns the next component.
    Returns None if cwd is not under a workspaces/ subtree.
    """
    if not cwd:
        return None
    parts = Path(cwd).parts
    if "workspaces" in parts:
        i = parts.index("workspaces")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


_TOKEN_RE = re.compile(r"\w+")


def _tokens(text: str) -> set[str]:
    """Lowercased word tokens of length >= 2 (single chars carry no redundancy signal)."""
    return {t.lower() for t in _TOKEN_RE.findall(text) if len(t) > 1}


def _redundancy(a: "RankedChunk", b: "RankedChunk",
                ta: set[str] | None = None, tb: set[str] | None = None) -> float:
    """Redundancy in [0,1] between two candidate chunks, for divisive normalization.

    Same source path => maximally redundant (two chunks from one file). Otherwise
    the lexical Jaccard of their text tokens. ta/tb take precomputed token sets
    so O(n²) callers tokenize each text once, not per pair.
    """
    if a.path and a.path == b.path:
        return 1.0
    if ta is None:
        ta = _tokens(a.text)
    if tb is None:
        tb = _tokens(b.text)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def rerank(
    chunks: list[RankedChunk],
    region_weights: dict[str, float] | None = None,
    tier_weights: dict[int, float] | None = None,
    *,
    fsrs_db: Path | None = None,
    fsrs_now: int | None = None,
    path_boost: dict[str, float] | None = None,
    relevance_by_id: dict[int, float] | None = None,
    dn_strength: float = 0.0,
    dn_sigma: float = 1.0,
) -> list[RankedChunk]:
    rw = region_weights or DEFAULT_REGION_WEIGHTS
    tw = tier_weights or DEFAULT_TIER_WEIGHTS

    # FSRS boosting is sqlite-only and not yet shipped in ARI-OS; keep the
    # seam and fail-soft so callers can wire it in later.
    fsrs_boosts: dict[int, float] = {}
    if fsrs_db is not None and fsrs_now is not None and chunks:
        try:
            from .fsrs_sweep import fsrs_boost_for_chunks
            fsrs_boosts = fsrs_boost_for_chunks(
                fsrs_db, chunk_ids=[c.chunk_id for c in chunks], now=fsrs_now,
            )
        except Exception:
            fsrs_boosts = {}

    # Normalize relevance_by_id to [0,1] so fused RRF scores (which are
    # inherently tiny, ~0.01-0.03) don't get dwarfed by the distance-based
    # proximity fallback (~0.5) used for adjacent/kg chunks.
    _norm_relevance: dict[int, float] | None = None
    if relevance_by_id:
        _max_rel = max(relevance_by_id.values()) if relevance_by_id else 1.0
        if _max_rel > 0:
            _norm_relevance = {k: v / _max_rel for k, v in relevance_by_id.items()}
        else:
            _norm_relevance = relevance_by_id

    for c in chunks:
        if _norm_relevance is not None and c.chunk_id in _norm_relevance:
            proximity = _norm_relevance[c.chunk_id]
        else:
            proximity = 1.0 / (1.0 + max(c.distance, 0.0))
        importance_boost = 1.0 + c.importance
        region_w = rw.get(c.region, 1.0)
        tier_w = tw.get(c.tier, 1.0)
        freq_boost = 1.0 + math.log(1.0 + c.retrieved_count)
        fsrs_w = fsrs_boosts.get(c.chunk_id, 1.0)
        source_w = SOURCE_WEIGHTS.get(c.source, 1.0)
        c.score = proximity * importance_boost * region_w * tier_w * freq_boost * fsrs_w * source_w
        if path_boost:
            for substr, multiplier in path_boost.items():
                if substr in c.path:
                    c.score *= multiplier
    # Divisive normalization: divide each chunk's score by the pooled,
    # redundancy-weighted activity of the rest of the candidate set. dn_strength
    # is the E/I knob — alpha->0 leaves scores hyper-reactive (detail-saturated
    # / tunnel); larger alpha suppresses redundant chunks (diverse / global).
    # Snapshot base scores so the pass is order-independent. Reorders only —
    # never filters.
    if dn_strength > 0.0 and len(chunks) > 1:
        base = {id(c): c.score for c in chunks}
        # O(n²) pairs below — tokenize once per chunk, not per pair.
        toks = {id(c): _tokens(c.text) for c in chunks}
        for c in chunks:
            suppression = 0.0
            for other in chunks:
                if other is c:
                    continue
                # MMR-style: a chunk is suppressed only by redundancy with chunks
                # at least as relevant as itself. This keeps DN's diversification
                # among similarly-scored candidates while guaranteeing the single
                # strongest semantic match is never buried by its own lower-scored
                # siblings — semantic relevance dominates.
                if base[id(other)] < base[id(c)]:
                    continue
                suppression += _redundancy(
                    c, other, toks[id(c)], toks[id(other)]) * base[id(other)]
            c.score = c.score / (dn_sigma + dn_strength * suppression)

    return sorted(chunks, key=lambda c: c.score, reverse=True)


def update_hebbian_edges(db_path: Path, returned_ids: list[int]) -> int:
    """Increment co_activations on edges between every returned chunk pair;
    create the edge at weight 0.3 if it doesn't exist.
    """
    if len(returned_ids) < 2:
        return 0
    con = connect(db_path)
    now = int(time.time())
    try:
        count = 0
        con.execute("BEGIN IMMEDIATE")
        for i, a in enumerate(returned_ids):
            for b in returned_ids[i + 1:]:
                for from_, to_ in ((a, b), (b, a)):
                    con.execute(
                        """INSERT INTO tract_edge(from_chunk, to_chunk, tract, weight, co_activations, last_fired_at)
                           VALUES (?, ?, 'hebbian', 0.3, 1, ?)
                           ON CONFLICT(from_chunk, to_chunk, tract) DO UPDATE SET
                             co_activations = co_activations + 1,
                             weight = MIN(0.95, CAST(co_activations + 1 AS REAL) / (CAST(co_activations + 1 AS REAL) + 5.0)),
                             last_fired_at = excluded.last_fired_at""",
                        (from_, to_, now),
                    )
                    count += 1
        con.execute("COMMIT")
        return count
    finally:
        con.close()


def record_retrieval_event(
    db_path: Path,
    query_text: str,
    cwd: str | None,
    branch: str | None,
    mode: str,
    consumer: str,
    chunk_ids: list[int],
    top_distance: float | None = None,
    session_id: str | None = None,
) -> int | None:
    con = connect(db_path)
    try:
        lastrowid = None
        try:
            cur = con.execute(
                """INSERT INTO retrieval_event(ts, query_text, cwd, branch, mode, consumer,
                       chunk_ids, top_distance, session_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (int(time.time()), query_text, cwd, branch, mode, consumer,
                 json.dumps(chunk_ids), top_distance, session_id),
            )
            lastrowid = cur.lastrowid
        except sqlite3.OperationalError:
            # Pre-v4 brain: retrieval_event has no top_distance column yet.
            cur = con.execute(
                """INSERT INTO retrieval_event(ts, query_text, cwd, branch, mode, consumer, chunk_ids)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (int(time.time()), query_text, cwd, branch, mode, consumer, json.dumps(chunk_ids)),
            )
            lastrowid = cur.lastrowid
        now = int(time.time())
        con.executemany(
            "UPDATE chunk SET retrieved_count = retrieved_count + 1, last_retrieved_at = ? WHERE id = ?",
            [(now, cid) for cid in chunk_ids],
        )
        return lastrowid
    finally:
        con.close()


def record_retrieval_metrics(
    db_path: Path,
    *,
    retrieval_event_id: int | None,
    intent: str | None,
    confidence: float | None,
    tokens_budget: int,
    tokens_packed: int,
    n_zones: int,
    per_zone: dict,
    assembler_on: bool,
    latency_ms: float | None = None,
) -> None:
    tokens_wasted = tokens_budget - tokens_packed
    con = connect(db_path)
    try:
        try:
            con.execute(
                """INSERT INTO retrieval_metrics
                   (retrieval_event_id, ts, intent, confidence, tokens_budget, tokens_packed,
                    tokens_wasted, n_zones, per_zone_json, assembler_on, latency_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (retrieval_event_id, int(time.time()), intent, confidence,
                 tokens_budget, tokens_packed, tokens_wasted,
                 n_zones, json.dumps(per_zone), 1 if assembler_on else 0,
                 latency_ms),
            )
        except sqlite3.OperationalError:
            # Defensive: latency_ms column may not be migrated yet.
            con.execute(
                """INSERT INTO retrieval_metrics
                   (retrieval_event_id, ts, intent, confidence, tokens_budget, tokens_packed,
                    tokens_wasted, n_zones, per_zone_json, assembler_on)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (retrieval_event_id, int(time.time()), intent, confidence,
                 tokens_budget, tokens_packed, tokens_wasted,
                 n_zones, json.dumps(per_zone), 1 if assembler_on else 0),
            )
    finally:
        con.close()


@dataclass
class RetrievalResult:
    chunks: list[RankedChunk]
    query: str
    mode: str
    cwd: str | None
    branch: str | None
    sufficiency: str = "strong"
    top_distance: float | None = None
    reason: str = ""
    posture_offer: str | None = None
    ranked_ids: list[int] = field(default_factory=list)  # pre-token-budget ranked ids


def _hydrate(con: sqlite3.Connection, chunk_ids: list[int], source_kind: dict[int, str]) -> list[RankedChunk]:
    if not chunk_ids:
        return []
    placeholders = ",".join("?" * len(chunk_ids))
    rows = con.execute(
        f"""SELECT c.id, c.text, c.region, c.distillation_tier, c.importance, c.retrieved_count,
                   c.line_start, c.line_end, s.path, s.workspace
            FROM chunk c JOIN source s ON c.source_id = s.id
            WHERE c.id IN ({placeholders})""",
        chunk_ids,
    ).fetchall()
    by_id = {r[0]: r for r in rows}
    out: list[RankedChunk] = []
    for cid in chunk_ids:
        if cid not in by_id:
            continue
        cid_, text, region, tier, importance, rc, ls, le, path, ws = by_id[cid]
        out.append(RankedChunk(
            chunk_id=cid_, distance=0.0, region=region, tier=tier,
            importance=importance, retrieved_count=rc, text=text, path=path,
            line_start=ls, line_end=le, workspace=ws,
            source=source_kind.get(cid, "vec"),
        ))
    return out


def _kg_expand_impl(
    db_path: Path,
    seed_chunk_ids: list[int],
    *,
    per_seed: int = 4,
) -> list[RankedChunk]:
    """For each seed chunk, return chunks that share at least one kg_entity.
    Returns RankedChunks with source='kg' so the reranker can up-weight them.
    Seeds themselves are excluded. Empty-safe (returns [] when kg tables are
    empty or the seed set is empty).
    """
    if not seed_chunk_ids:
        return []
    seeds = set(seed_chunk_ids)
    out_ids: list[int] = []
    seen: set[int] = set(seeds)
    con = connect(db_path)
    try:
        for sid in seed_chunk_ids:
            ent_ids = [r[0] for r in con.execute(
                "SELECT entity_id FROM kg_entity_chunk WHERE chunk_id=?", (sid,)
            ).fetchall()]
            if not ent_ids:
                continue
            placeholders = ",".join("?" * len(ent_ids))
            rows = con.execute(
                f"""SELECT chunk_id, MAX(confidence) AS conf
                      FROM kg_entity_chunk
                     WHERE entity_id IN ({placeholders})
                  GROUP BY chunk_id
                  ORDER BY conf DESC""",
                ent_ids,
            ).fetchall()
            added = 0
            for cid, _ in rows:
                if cid in seen:
                    continue
                seen.add(cid)
                out_ids.append(cid)
                added += 1
                if added >= per_seed:
                    break
        if not out_ids:
            return []
        return _hydrate(con, out_ids, {cid: "kg" for cid in out_ids})
    finally:
        con.close()


# Public alias for direct callers / tests
kg_expand = _kg_expand_impl


def retrieve(
    db_path: Path,
    query: str,
    embed_client: EmbedClient | None = None,
    *,
    k_vector: int | None = None,
    k_sparse: int | None = None,
    k_adjacent: int | None = None,
    k_kg: int | None = None,
    kg_expand: bool | None = None,
    token_budget: int | None = None,
    mode: str = "default",
    cwd: str | None = None,
    branch: str | None = None,
    consumer: str = "retrieve",
    region_weights: dict[str, float] | None = None,
    tier_weights: dict[int, float] | None = None,
    fsrs_enabled: bool | None = None,
    dn_strength: float | None = None,
    dn_sigma: float | None = None,
    posture: str | None = None,
    session_id: str | None = None,
) -> RetrievalResult:
    _t_start = time.monotonic()  # instrument: wall-clock of the whole path
    # Load mode YAML; apply as defaults for any param the caller left as None.
    # Explicit caller-passed values (non-None) always win.
    _mode_params: dict = {}
    try:
        from .modes.loader import load_mode as _load_mode
        _mode_params = _load_mode(mode)
    except Exception:
        pass

    _kv: int = k_vector if k_vector is not None else _mode_params.get("k_vector", 12)
    _ksp: int = k_sparse if k_sparse is not None else _mode_params.get("k_sparse", 12)
    _ka: int = k_adjacent if k_adjacent is not None else _mode_params.get("k_adjacent", 6)
    _kkg: int = k_kg if k_kg is not None else _mode_params.get("k_kg", 4)
    _kge: bool = kg_expand if kg_expand is not None else _mode_params.get("kg_expand", False)
    _tb: int = token_budget if token_budget is not None else _mode_params.get("token_budget", 4000)
    _fsrs: bool = fsrs_enabled if fsrs_enabled is not None else _mode_params.get("fsrs_enabled", True)
    _dns: float = dn_strength if dn_strength is not None else _mode_params.get("dn_strength", 0.0)
    _dnsig: float = dn_sigma if dn_sigma is not None else _mode_params.get("dn_sigma", 1.0)
    _rw: dict[str, float] | None = region_weights if region_weights is not None else _mode_params.get("region_weights")
    _tw: dict[int, float] | None = tier_weights if tier_weights is not None else _mode_params.get("tier_weights")
    _pb: dict[str, float] | None = _mode_params.get("path_boost")

    _posture: str = posture if posture in ("tunnel", "global") else "tunnel"
    _active_ws: str | None = cwd_to_workspace(cwd) if _posture == "tunnel" else None

    try:
        from .mode_routing import load_routing_config as _load_posture_rc
        _posture_cfg = _load_posture_rc().posture_defaults
    except Exception:
        _posture_cfg = {}
    if dn_strength is None:
        if _posture == "tunnel":
            _dns = _posture_cfg.get("tunnel_dn_strength", _dns)
        elif _posture == "global":
            _dns = _posture_cfg.get("global_dn_strength", _dns)
    if dn_sigma is None:
        if _posture == "tunnel":
            _dnsig = _posture_cfg.get("tunnel_dn_sigma", _dnsig)
        elif _posture == "global":
            _dnsig = _posture_cfg.get("global_dn_sigma", _dnsig)

    from .embed import EmbedError, default_embed_client
    embed_client = embed_client or default_embed_client()
    try:
        qvec = embed_client.embed([query])[0]
    except EmbedError:
        qvec = None  # degrade to lexical-only retrieval
    _task_type = mode
    _task_confidence = None
    _task_abstained = False  # tie / low-confidence / missing embedding
    try:
        from . import task_classifier as _task_classifier
        _classifier = _task_classifier.get_default_classifier()
        _task_type = _classifier.classify(query, qvec)
        if _task_type not in _task_classifier.TASK_TYPES:
            raise ValueError(f"invalid task type: {_task_type!r}")
        _task_confidence = getattr(_classifier, "last_confidence", None)
        _last_result = getattr(_classifier, "last_result", None)
        _task_abstained = bool(
            _last_result is not None
            and getattr(_last_result, "fallback_reason", None) is not None
        )
    except Exception:
        # Task typing is observability only. Retrieval must keep working even if
        # the artifact is missing, stale, or the classifier seam changes.
        _task_type = mode
        _task_confidence = None
        _task_abstained = False
    # Layer 1 x Layer 2: learned per-task-type base x posture delta. Applies
    # ONLY when the caller didn't pin region_weights (explicit weights always
    # win). Without the task_region_weights.json artifact this is a no-op
    # (fail-static).
    if region_weights is None:
        try:
            from .task_region_weights import compose_region_weights
            # On abstain the predicted label is the classifier's fallback
            # dumping ground — compose from the aggregate row instead.
            _composed = compose_region_weights(
                None if _task_abstained else _task_type, _rw)
            if _composed is not None:
                _rw = _composed
        except Exception:
            pass

    _overfetch = _kv * 3 if _active_ws else _kv
    vec_hits = vec_search(db_path, qvec, k=_overfetch) if qvec is not None else []
    seed_ids = [h.chunk_id for h in vec_hits]
    source_kind: dict[int, str] = {cid: "vec" for cid in seed_ids}

    sparse_hits = fts_search(db_path, query, k=_ksp)
    sparse_ids = [h.chunk_id for h in sparse_hits]
    fused = rrf_fuse([seed_ids, sparse_ids])
    for sid in sparse_ids:
        source_kind.setdefault(sid, "sparse")

    adjacent = expand_via_tracts(db_path, seed_ids, per_seed=_ka or 0)
    for adj in adjacent:
        source_kind.setdefault(adj.chunk_id, "adjacent")
    all_ids = list(dict.fromkeys(seed_ids + sparse_ids + [a.chunk_id for a in adjacent]))

    if _kge and seed_ids and config.kg_enabled(False):
        kg_chunks = _kg_expand_impl(db_path, seed_ids, per_seed=_kkg)
        for kc in kg_chunks:
            if kc.chunk_id not in source_kind:
                source_kind[kc.chunk_id] = "kg"
                all_ids.append(kc.chunk_id)

    con = connect(db_path)
    try:
        chunks = _hydrate(con, all_ids, source_kind)
    finally:
        con.close()

    dist_by_id = {h.chunk_id: h.distance for h in vec_hits}
    for c in chunks:
        c.distance = dist_by_id.get(c.chunk_id, 1.0)

    if _active_ws:
        adj_ids = {a.chunk_id for a in adjacent}
        chunks = [c for c in chunks if c.workspace == _active_ws or c.chunk_id in adj_ids]

    _posture_offer: str | None = None
    if _posture == "tunnel" and _active_ws:
        try:
            from .mode_routing import load_routing_config as _load_rc
            _rc = _load_rc()
        except Exception:
            _rc = None
        _coverage_floor: int = 3
        if _rc:
            _coverage_floor = int(_rc.posture_defaults.get("local_coverage_floor", 3))

        local_count = sum(1 for c in chunks if c.workspace == _active_ws)
        # Sparse-recall strong-hit check — FTS/BM25 lexical match counts as
        # adequate coverage.
        local_has_sparse = any(
            c.workspace == _active_ws and c.source == "sparse"
            and c.chunk_id in (fused or {})
            for c in chunks
        )
        thin_coverage = local_count < _coverage_floor and not local_has_sparse

        kw_widen = False
        if _rc and query:
            from .mode_router import classify_posture as _cp
            pd = _cp(query, _rc.keywords)
            kw_widen = pd.widen_signal

        if thin_coverage:
            _posture_offer = ('🔍 Local coverage thin — say "go global" or pass '
                              '`--posture global` to widen beyond this workspace.')
        elif kw_widen:
            _posture_offer = '🔍 Cross-workspace query detected — say "go global" to widen retrieval.'

    now = int(time.time())
    ranked = rerank(
        chunks,
        region_weights=_rw,
        tier_weights=_tw,
        fsrs_db=db_path if (_fsrs) else None,
        fsrs_now=now if (_fsrs) else None,
        path_boost=_pb,
        relevance_by_id=fused,
        dn_strength=_dns,
        dn_sigma=_dnsig,
    )
    # Capture pre-budget ranking for eval anti-gaming.
    _pre_budget_ranked_ids = [c.chunk_id for c in ranked]

    # Pack the ranked chunks into the token budget. ASSEMBLER_ENABLED is a
    # public toggle (consumers can run the council/assembler through the
    # returned list themselves); the public package ships with the assembler
    # OFF by default.
    if ASSEMBLER_ENABLED:
        from . import context_assembler
        from .mode_router import zone_ratios_for
        packed = context_assembler.assemble(ranked, token_budget=_tb,
                                            ratios=zone_ratios_for(mode))
        if packed is None:
            packed = []
            used = 0
            for c in ranked:
                approx = int(len(c.text.split()) * 1.3)
                if used + approx > _tb and packed:
                    break
                packed.append(c)
                used += approx
    else:
        packed = []
        used = 0
        for c in ranked:
            approx = int(len(c.text.split()) * 1.3)
            if used + approx > _tb and packed:
                break
            packed.append(c)
            used += approx

    returned_ids = [c.chunk_id for c in packed]

    dense_distances = [h.distance for h in vec_hits]
    d_top = min(dense_distances) if dense_distances else None

    # Sufficiency gate is parked (see GATE_ENABLED). top_distance is still
    # logged below so a revived gate can calibrate on real query-distance
    # history.
    suff, reason = "strong", ""

    if consumer != "mcp":
        _event_id = record_retrieval_event(
            db_path, query, cwd, branch, mode, consumer, returned_ids,
            top_distance=d_top, session_id=session_id)
        _tokens_packed = sum(int(len(c.text.split()) * 1.3) for c in packed)
        _per_zone: dict[str, int] = {}
        for c in packed:
            if c.zone:
                _per_zone[c.zone] = _per_zone.get(c.zone, 0) + 1
        # Accrual gate: per-task stats may only count rows whose label carries
        # authority. Classifier abstains and synthetic `cwd=` SessionStart
        # pre-fetches log intent=NULL. Confidence still logs.
        _intent_accruable = not _task_abstained and not query.startswith("cwd=")
        record_retrieval_metrics(
            db_path,
            retrieval_event_id=_event_id,
            intent=_task_type if _intent_accruable else None,
            confidence=_task_confidence,
            tokens_budget=_tb,
            tokens_packed=_tokens_packed,
            n_zones=len(_per_zone),
            per_zone=_per_zone,
            assembler_on=ASSEMBLER_ENABLED,
            latency_ms=(time.monotonic() - _t_start) * 1000.0,
        )
        if session_id:
            try:
                from . import council, council_marker

                council_marker.record_tier(
                    session_id,
                    "normal" if council.enabled() else "static",
                )
            except Exception:
                pass
        update_hebbian_edges(db_path, returned_ids)
    return RetrievalResult(
        chunks=packed, query=query, mode=mode, cwd=cwd, branch=branch,
        sufficiency=suff, top_distance=d_top, reason=reason,
        posture_offer=_posture_offer, ranked_ids=_pre_budget_ranked_ids,
    )


REGION_LABELS = {
    "vmpfc": "Orbitofrontal — identity",
    "frontoparietal": "Frontoparietal — procedural",
    "hippocampus": "Hippocampus — episodic",
    "wernicke": "Wernicke — what you've said",
    "broca": "Broca — what we've produced",
    "occipital": "Occipital — visual",
    "parietal": "Parietal — workspace",
}
REGION_ORDER = ["vmpfc", "frontoparietal", "hippocampus", "wernicke", "broca", "occipital", "parietal"]


def filter_by_harness(chunks, harness):
    """Drop chunks tagged ``harness: claude-only`` when the active harness isn't claude.

    Untagged chunks are left in (lenient — better to surface than hide). Works
    with both dict-shaped chunks (used by tests / future skill-aware indexer)
    and :class:`RankedChunk` dataclass instances (used by the live retrieve
    path).
    """
    if harness is None or harness == "claude":
        return chunks

    def _harness_tag(c):
        if isinstance(c, dict):
            return c.get("harness")
        return getattr(c, "harness", None)

    return [c for c in chunks if _harness_tag(c) != "claude-only"]


def emit_markdown(chunks: list[RankedChunk], *, mode: str = "default",
                  sufficiency: str = "strong", reason: str = "",
                  posture_offer: str | None = None) -> str:
    if sufficiency == "empty":
        lines = [f"## Brain — coverage gap (mode={mode})",
                 f"No strong match{f' — {reason}' if reason else ''}. "
                 "Proceeding without retrieved context.", ""]
        if chunks:
            best = chunks[0]
            preview = best.text.strip().split("\n\n", 1)[0]
            if len(preview) > 280:
                preview = preview[:280] + "…"
            lines += ["_best guess (low confidence):_",
                      f"- [{best.path}:L{best.line_start}-L{best.line_end}] {preview}", ""]
        return "\n".join(lines)

    if not chunks:
        return f"## Brain — no context retrieved (mode={mode})\n"
    total_tokens = sum(int(len(c.text.split()) * 1.3) for c in chunks)
    lines = [f"## Brain — auto-retrieved context (mode={mode}, k={len(chunks)}, ~{total_tokens} tokens)\n"]
    if sufficiency == "weak":
        lines.insert(1, f"> ⚠ Thin context (weak match){f' — {reason}' if reason else ''}; "
                        "treat as a lead, verify before trusting.\n")
    if posture_offer:
        lines.append(f"> {posture_offer}\n")
    by_region: dict[str, list[RankedChunk]] = {}
    for c in chunks:
        by_region.setdefault(c.region, []).append(c)
    for region in REGION_ORDER:
        items = by_region.get(region)
        if not items:
            continue
        lines.append(f"### {REGION_LABELS.get(region, region)}")
        for c in items:
            marker = " (adjacent)" if c.source == "adjacent" else ""
            preview = c.text.strip().split("\n\n", 1)[0]
            if len(preview) > 280:
                preview = preview[:280] + "…"
            lines.append(f"- [{c.path}:L{c.line_start}-L{c.line_end}]{marker} (score={c.score:.2f})")
            lines.append(f"  {preview}")
        lines.append("")
    return "\n".join(lines)


def format_for_harness(chunks, harness, *, mode: str = "default",
                       sufficiency: str = "strong", reason: str = "",
                       posture_offer: str | None = None) -> str:
    """Render retrieved chunks for the active harness.

    Two input shapes are supported (matching :func:`filter_by_harness`):
    - ``list[dict]`` — used by tests and any future skill-aware indexer
    - ``list[RankedChunk]`` — produced by the live retrieve path

    For ``RankedChunk`` input we delegate to :func:`emit_markdown` so the rich
    region-grouped renderer is preserved. For dict input we emit a minimal
    ``### path`` + body section per chunk.
    """
    if chunks and isinstance(chunks[0], RankedChunk):
        body = emit_markdown(chunks, mode=mode, sufficiency=sufficiency, reason=reason,
                             posture_offer=posture_offer)
    else:
        lines: list[str] = []
        for c in chunks:
            path = c.get("path", "?") if isinstance(c, dict) else getattr(c, "path", "?")
            text = c.get("text", "") if isinstance(c, dict) else getattr(c, "text", "")
            lines.append(f"### {path}")
            lines.append("")
            lines.append(text)
            lines.append("")
        body = "\n".join(lines)
    return body
