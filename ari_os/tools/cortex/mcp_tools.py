"""Per-tool handler functions for the ARI-OS Cortex MCP server.

Each function takes db_path as its first argument and returns plain Python
types (str, dict, list) that FastMCP serialises for the client.

Public port + scrub of the private engine's MCP handlers. Media surfaces
are local-first and default-off so fresh installs stay inert until the
user explicitly enables them.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def remember(
    db_path: Path,
    text: str,
    *,
    layer: str = "semantic",
    source: str | None = None,
    embed_client=None,
) -> dict:
    """Store one memory chunk, deduping identical text by content-hash source."""
    from . import index
    from .db import connect

    text = text.strip()
    if not text:
        raise ValueError("remember: text is empty")
    if source is None:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        source = f"remember:{digest}"
    if embed_client is None:
        from .embed import EmbedClient, default_embed_client
        embed_client = default_embed_client()

    cid = index.index_text(
        db_path,
        text,
        source=source,
        layer=layer,
        embed_client=embed_client,
    )

    con = connect(db_path)
    try:
        row = con.execute(
            "SELECT region, importance FROM chunk WHERE id = ?",
            (cid,),
        ).fetchone()
    finally:
        con.close()
    return {
        "id": cid,
        "source": source,
        "region": row[0] if row else None,
        "importance": row[1] if row else None,
    }


def recall(
    db_path: Path,
    query: str,
    *,
    mode: str = "default",
    k: int = 12,
    token_budget: int = 4000,
    session_id: str | None = None,
) -> str:
    """Retrieve relevant context and return as region-grouped markdown."""
    from .retrieve import emit_markdown, retrieve

    result = retrieve(
        db_path,
        query=query,
        mode=mode,
        k_vector=k,
        token_budget=token_budget,
        consumer="mcp",
        session_id=session_id,
    )
    return emit_markdown(result.chunks, mode=mode,
                         sufficiency=result.sufficiency, reason=result.reason)


def lineage(db_path: Path, chunk_id: int) -> dict:
    """Walk parent_chunks back to raw source; return structured tree.

    Returns: {chunk_id, tier, parents: [...same shape...], raw_source_path}
    """
    from .db import connect

    con = connect(db_path)
    try:
        return _lineage_node(con, chunk_id, visited=set())
    finally:
        con.close()


def _lineage_node(con, chunk_id: int, visited: set) -> dict:
    if chunk_id in visited:
        return {"chunk_id": chunk_id, "cycle": True}
    visited.add(chunk_id)

    row = con.execute(
        "SELECT c.id, c.distillation_tier, c.parent_chunks, s.path "
        "FROM chunk c LEFT JOIN source s ON s.id = c.source_id "
        "WHERE c.id = ?",
        (chunk_id,),
    ).fetchone()

    if row is None:
        return {"chunk_id": chunk_id, "not_found": True}

    cid, tier, parents_json, path = row
    parents: list[int] = []
    if parents_json:
        try:
            parents = [int(p) for p in json.loads(parents_json)]
        except (TypeError, ValueError):
            parents = []

    return {
        "chunk_id": cid,
        "tier": tier,
        "raw_source_path": path,
        "parents": [_lineage_node(con, pid, visited) for pid in parents],
    }


def regions(db_path: Path) -> dict:
    """Return {region: count} for all chunks in brain.db."""
    from .db import connect

    con = connect(db_path)
    try:
        rows = con.execute(
            "SELECT region, COUNT(*) FROM chunk GROUP BY region ORDER BY region"
        ).fetchall()
    finally:
        con.close()
    return {r[0]: r[1] for r in rows}


def tracts(db_path: Path, *, n: int = 100) -> list:
    """Return top N Hebbian tract edges ordered by weight descending.

    Each entry: {from_chunk, to_chunk, weight, co_activations}
    """
    from .db import connect

    con = connect(db_path)
    try:
        rows = con.execute(
            """SELECT from_chunk, to_chunk, weight, co_activations
               FROM tract_edge
               WHERE tract = 'hebbian'
               ORDER BY weight DESC
               LIMIT ?""",
            (n,),
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "from_chunk": r[0],
            "to_chunk": r[1],
            "weight": r[2],
            "co_activations": r[3],
        }
        for r in rows
    ]


def entities(db_path: Path, *, kind: str | None = None, k: int = 50) -> list:
    """Return KG entities ordered by mention count and confidence."""
    from .db import connect

    limit = max(1, int(k))
    con = connect(db_path)
    try:
        if kind:
            rows = con.execute(
                """SELECT id, name, kind, confidence, mention_count
                   FROM kg_entity
                   WHERE kind = ?
                   ORDER BY mention_count DESC, confidence DESC, name ASC
                   LIMIT ?""",
                (kind, limit),
            ).fetchall()
        else:
            rows = con.execute(
                """SELECT id, name, kind, confidence, mention_count
                   FROM kg_entity
                   ORDER BY mention_count DESC, confidence DESC, name ASC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
    finally:
        con.close()
    return [
        {
            "id": int(row[0]),
            "name": row[1],
            "kind": row[2],
            "confidence": row[3],
            "mention_count": row[4],
        }
        for row in rows
    ]


def relations(db_path: Path, *, entity: str | None = None, k: int = 50) -> list:
    """Return KG relations, optionally constrained to one entity name."""
    from .db import connect
    from .kg.query import find_entity_by_name

    limit = max(1, int(k))
    params: tuple[object, ...]
    where = ""
    if entity:
        hit = find_entity_by_name(db_path, entity)
        if hit is None:
            return []
        where = "WHERE r.subject_id = ? OR r.object_id = ?"
        params = (hit["id"], hit["id"], limit)
    else:
        params = (limit,)

    con = connect(db_path)
    try:
        rows = con.execute(
            f"""SELECT r.id, se.name, r.predicate, oe.name,
                      r.confidence, r.evidence_count
                 FROM kg_relation r
                 JOIN kg_entity se ON se.id = r.subject_id
                 JOIN kg_entity oe ON oe.id = r.object_id
                 {where}
                 ORDER BY r.evidence_count DESC, r.confidence DESC, r.id ASC
                 LIMIT ?""",
            params,
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "id": int(row[0]),
            "subject": row[1],
            "predicate": row[2],
            "object": row[3],
            "confidence": row[4],
            "evidence_count": row[5],
        }
        for row in rows
    ]


def modes(db_path: Path) -> list:
    """List mode YAML files from the modes/ directory beside the cortex package.

    Returns list of {name, path} dicts. Tolerates missing or empty directory.
    """
    modes_dir = Path(__file__).parent / "modes"
    if not modes_dir.exists():
        return []
    result = []
    for yaml_file in sorted(modes_dir.glob("*.yaml")):
        result.append({"name": yaml_file.stem, "path": str(yaml_file)})
    return result


def see_image(
    db_path: Path,
    image_path: str,
    *,
    mode: str = "visual",
    vision_client=None,
    embed_client=None,
) -> dict:
    """Image captioning + similar brain chunks (bimodal).

    Returns: {caption, similar_chunks: [...], note}
    """
    from . import config

    if not config.lens_enabled():
        return _see_off_response(
            "brain.see is off because cortex.lens is off. "
            "Enable it with `ari-os cortex lens on` after installing a "
            "local vision backend."
        )
    if not _cortex_llm_enabled():
        return _see_off_response(
            "brain.see is off because cortex.llm is off. "
            "Enable a local LLM backend before using image captioning."
        )

    from .media.vision_bridge import VisionUnavailable, see

    try:
        result = see(
            db_path,
            Path(image_path),
            mode=mode,
            vision_client=vision_client,
            embed_client=embed_client,
        )
    except VisionUnavailable as exc:
        return _see_off_response(
            f"brain.see is unavailable: {exc}. "
            "Start local Ollama with the llava model to enable image captioning."
        )

    return {
        "caption": result.caption,
        "similar_chunks": [
            {
                "chunk_id": c.chunk_id,
                "region": c.region,
                "score": round(c.score, 4),
                "text": c.text[:280],
                "path": c.path,
            }
            for c in result.retrieval.chunks
        ],
    }


def _see_off_response(note: str) -> dict:
    return {"caption": None, "similar_chunks": [], "note": note}


def _cortex_llm_enabled() -> bool:
    from . import config

    env = os.environ.get("ARI_OS_LLM")
    if env is not None:
        return _llm_spec_enabled(env)

    data = config.runtime_config()
    value = data.get("cortex.llm")
    if value is None:
        cortex = data.get("cortex")
        if isinstance(cortex, dict):
            value = cortex.get("llm")
    if value is None:
        value = config.DEFAULT_LLM
    return _llm_spec_enabled(value)


def _llm_spec_enabled(value) -> bool:
    if isinstance(value, bool):
        return value
    spec = str(value or "").strip().lower()
    if not spec:
        return False
    provider = spec.split(":", 1)[0]
    return provider not in {"0", "false", "off", "none", "unavailable"}


def lens(db_path: Path, slug: str) -> str:
    """Retrieve a LENS card by slug from the user's local state home."""
    from . import config
    from .media.lens_adapter import find_card

    if not config.lens_enabled():
        return (
            "## brain.lens is off\n\n"
            "Enable it with `ari-os cortex lens on` after adding local "
            "cards under your ARI-OS state home.\n"
        )

    card = find_card(config.state_home() / "lens", slug)
    if card is not None:
        return _format_lens_card(card)

    indexed = _lens_from_indexed_chunks(db_path, slug)
    if indexed is not None:
        return indexed

    return f"## LENS card not found: {slug}\n"


def _format_lens_card(card) -> str:
    title = card.title or card.slug
    body = card.body.strip()
    return f"# {title}\n\n{body}\n" if body else f"# {title}\n"


def _lens_from_indexed_chunks(db_path: Path, slug: str) -> str | None:
    if not db_path.exists():
        return None
    try:
        from .db import connect

        con = connect(db_path)
        try:
            row = con.execute(
                "SELECT c.text FROM chunk c JOIN source s ON s.id = c.source_id "
                "WHERE s.path LIKE ? OR s.path LIKE ? "
                "OR (s.workspace = 'lens' AND s.path LIKE ?) "
                "ORDER BY c.ordinal LIMIT 1",
                (f"%/lens/%/{slug}.md", f"%/lens/{slug}.md", f"%{slug}.md"),
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if row is None:
        return None
    return f"# {slug}\n\n{row[0].strip()}\n"
