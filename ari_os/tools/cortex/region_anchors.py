"""Region anchors + per-region z-calibration (council S2, spec §3a/§3b).

The s4 gate found cosine is text-type-biased: prose regions (broca/wernicke)
score high against prose queries even when irrelevant, abstract regions
(vmpfc/parietal) score low even when used. Cross-region comparison of raw
cosine is therefore forbidden; instead each candidate is scored as a z-score
against its OWN region's global cosine distribution:

    ẑ(c) = (cos(q_r, c) − μ_r) / σ_r

with (μ_r, σ_r) measured OFFLINE against each region's global distribution
(not per-query — a region with no relevant content must be able to LOSE).

``region_anchors.neutral.json`` holds per-region template-family centroids
plus the (μ_r, σ_r) constants, measured at a grid of β values so the anchor-
pull knob can move in S3 without an artifact rebuild. The shipped default is
β = 0 (q_r = qvec): pure calibration, no query bending — β earns movement via
its pre-registered instrument (spec §5) like every other knob.

The shipped artifact is **neutral** (uniform-axis anchors, identity
calibration). Real per-region centroids are rebuilt locally by the user from
their own data via ``python -m ari_os.tools.cortex.region_anchors
rebuild-anchors`` — mirroring ``task_classifier rebuild-centroids``. Fail-static
posture: artifact missing/invalid → callers get None and the council falls
back to within-pool percentile stand-ins (S1 behaviour). Revert = delete the
artifact.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from .config import EMBED_MODEL

REGIONS = (
    "wernicke", "broca", "occipital", "parietal",
    "hippocampus", "vmpfc", "frontoparietal",
)

# The neutral seed ships alongside the package; the real (per-user) artifact
# lives under the user's ARI_OS_HOME and is loaded on demand.
DEFAULT_ANCHORS_PATH = (
    Path(__file__).resolve().parent.parent / "seeds" / "region_anchors.neutral.json"
)
BETA_GRID = (0.0, 0.15, 0.30, 0.50)
DEFAULT_BETA = 0.0

# Template families per region — the kinds of question each region exists to
# answer, grounded in classify.py's routing semantics (what actually lands in
# each region at ingest). Scrubbed: published cognitive-science terms only;
# no private identity / voice references.
REGION_TEMPLATES: dict[str, list[str]] = {
    "wernicke": [
        "what was said in that conversation",
        "the exact words used previously",
        "what the user asked for last time",
        "the request made about this tool",
        "the prior phrasing in this thread",
    ],
    "broca": [
        "how this was explained previously",
        "the phrasing used in the prior answer",
        "the earlier response wording",
        "the assistant's explanation of the design",
        "the wording of the past reply",
    ],
    "occipital": [
        "the visual composition of this frame",
        "colour grade and contrast of the reference",
        "the look and feel of the scene",
        "describe the image layout",
        "mood board references for the scene",
    ],
    "parietal": [
        "current state of the workspace",
        "what was decided for this project",
        "the project context and manifest",
        "where this workspace is at right now",
        "the overview of the repository structure",
    ],
    "hippocampus": [
        "what happened in the last session",
        "the ship report for that feature",
        "when did the migration run",
        "the brief for the campaign",
        "what was shipped recently",
    ],
    "vmpfc": [
        "the preferred response format",
        "the rules about committing this code",
        "what matters in this decision",
        "the voice and identity guidelines",
        "feedback about how to work",
    ],
    "frontoparietal": [
        "how to run the test suite",
        "the command line to rebuild the index",
        "the steps to dispatch a worker",
        "how this skill is invoked",
        "the procedure for the deploy",
    ],
}


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    if n == 0.0:
        return list(v)
    return [x / n for x in v]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    an = math.sqrt(sum(x * x for x in a))
    bn = math.sqrt(sum(y * y for y in b))
    if an == 0.0 or bn == 0.0:
        return 0.0
    return dot / (an * bn)


def probe_vector(qvec: list[float], anchor: list[float], beta: float) -> list[float]:
    """Affine anchor probe q_r = normalize((1−β)·qvec + β·anchor_r) — §3a."""
    if beta == 0.0:
        return qvec
    return _normalize([(1.0 - beta) * q + beta * a for q, a in zip(qvec, anchor)])


def _beta_key(beta: float) -> str:
    return f"{beta:.2f}"


def load_anchor_artifact(path: Path = DEFAULT_ANCHORS_PATH) -> dict | None:
    """Load + structurally validate the artifact; None on any defect."""
    try:
        artifact = json.loads(path.read_text())
        anchors = artifact["anchors"]
        calib = artifact["calibration"]
        beta = _beta_key(float(artifact.get("default_beta", DEFAULT_BETA)))
        if beta not in calib:
            return None
        for r in REGIONS:
            if r not in anchors or len(anchors[r]) == 0:
                return None
            c = calib[beta].get(r)
            if not c or float(c["sigma"]) <= 0.0:
                return None
        return artifact
    except Exception:
        return None


_ARTIFACT: dict | None = None
_ARTIFACT_LOADED = False


def get_default_artifact() -> dict | None:
    global _ARTIFACT, _ARTIFACT_LOADED
    if not _ARTIFACT_LOADED:
        _ARTIFACT = load_anchor_artifact()
        _ARTIFACT_LOADED = True
    return _ARTIFACT


def zscores_for_pool(
    db_path: Path,
    qvec: list[float],
    pool: list,
    *,
    artifact: dict | None = None,
    beta: float | None = None,
) -> dict[int, float] | None:
    """Calibrated ẑ for every candidate in the pool, or None (fail-static).

    ``pool`` is the reranked candidate list (objects with .chunk_id/.region).
    All-or-nothing: cross-region comparability is the entire point, so a pool
    that can't be fully scored (missing embedding, unknown region) returns
    None and the caller keeps the percentile stand-ins.
    """
    artifact = artifact if artifact is not None else get_default_artifact()
    if artifact is None or not pool:
        return None
    beta = float(artifact.get("default_beta", DEFAULT_BETA)) if beta is None else beta
    calib = artifact["calibration"].get(_beta_key(beta))
    if calib is None:
        return None

    from .db import connect
    from .embed import unpack_embedding, default_embed_client

    ids = [c.chunk_id for c in pool]
    con = connect(db_path)
    try:
        ph = ",".join("?" * len(ids))
        rows = con.execute(
            f"SELECT rowid, embedding FROM chunk_vec WHERE rowid IN ({ph})", ids
        ).fetchall()
    finally:
        con.close()
    embs = {r[0]: unpack_embedding(r[1]) for r in rows}

    probes: dict[str, list[float]] = {}
    zmap: dict[int, float] = {}
    for c in pool:
        emb = embs.get(c.chunk_id)
        cal = calib.get(c.region)
        anchor = artifact["anchors"].get(c.region)
        if emb is None or cal is None or anchor is None:
            return None
        if c.region not in probes:
            probes[c.region] = probe_vector(qvec, anchor, beta)
        zmap[c.chunk_id] = (
            _cosine(probes[c.region], emb) - float(cal["mu"])
        ) / float(cal["sigma"])
    return zmap


def rebuild_anchors(
    db_path: Path,
    queries: list[str],
    *,
    embed_client=None,
    output_path: Path | None = None,
    sample_per_region: int = 2000,
    betas: tuple = BETA_GRID,
    seed: int = 42,
) -> dict:
    """Rebuild the versioned anchors artifact (offline; numpy; never hot path).

    Anchors = normalized mean of each region's template-family embeddings.
    (μ_r, σ_r) = moments of cos(q_r, chunk) pooled over ``queries`` × a random
    sample of region r's chunks — the GLOBAL distribution (relevant and not),
    per β in the grid. If ``output_path`` is None, writes the rebuilt artifact
    alongside the neutral seed at the default path — which OVERWRITES the
    neutral seed. Pass an explicit path under the user's ARI_OS_HOME to
    preserve the shipped neutral defaults.
    """
    import numpy as np

    if embed_client is None:
        from .embed import EmbedClient, default_embed_client
        embed_client = default_embed_client()

    from .db import connect
    from .embed import unpack_embedding, default_embed_client

    anchors: dict[str, list[float]] = {}
    for region in REGIONS:
        embs = embed_client.embed(REGION_TEMPLATES[region])
        if not embs:
            raise ValueError(f"no embeddings for {region} templates")
        anchors[region] = _normalize(
            np.asarray(embs, dtype=np.float64).mean(axis=0).tolist())

    if not queries:
        raise ValueError("rebuild_anchors needs a non-empty query population")
    qvecs = np.asarray(embed_client.embed(queries), dtype=np.float64)
    qvecs /= np.linalg.norm(qvecs, axis=1, keepdims=True)

    rng = np.random.default_rng(seed)
    calibration: dict[str, dict] = {_beta_key(b): {} for b in betas}
    con = connect(db_path)
    try:
        for region in REGIONS:
            ids = [r[0] for r in con.execute(
                "SELECT id FROM chunk WHERE region = ?", (region,)).fetchall()]
            if not ids:
                raise ValueError(f"region {region} has no chunks in {db_path}")
            if len(ids) > sample_per_region:
                ids = [ids[i] for i in rng.choice(
                    len(ids), size=sample_per_region, replace=False)]
            ph = ",".join("?" * len(ids))
            rows = con.execute(
                f"SELECT embedding FROM chunk_vec WHERE rowid IN ({ph})", ids
            ).fetchall()
            mat = np.asarray(
                [unpack_embedding(r[0]) for r in rows], dtype=np.float64)
            mat /= np.linalg.norm(mat, axis=1, keepdims=True)
            avec = np.asarray(anchors[region], dtype=np.float64)
            for beta in betas:
                probes = (1.0 - beta) * qvecs + beta * avec
                probes = probes / np.linalg.norm(probes, axis=1, keepdims=True)
                cosines = (mat @ probes.T).ravel()
                calibration[_beta_key(beta)][region] = {
                    "mu": float(cosines.mean()),
                    "sigma": float(cosines.std(ddof=1)),
                    "n_chunks": int(mat.shape[0]),
                }
    finally:
        con.close()

    artifact = {
        "version": 1,
        "model": EMBED_MODEL,
        "default_beta": DEFAULT_BETA,
        "betas": [float(b) for b in betas],
        "n_queries": len(queries),
        "source_db": str(db_path),
        "templates": REGION_TEMPLATES,
        "anchors": anchors,
        "calibration": calibration,
    }
    target = output_path if output_path is not None else DEFAULT_ANCHORS_PATH
    target.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    return artifact


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Region anchor utilities")
    sub = parser.add_subparsers(dest="command", required=True)
    rebuild = sub.add_parser("rebuild-anchors")
    rebuild.add_argument("--db", type=Path, required=True)
    rebuild.add_argument("--queries-file", type=Path, required=True,
                         help='JSON: ["q", ...] or {"rows": [{"query": ...}]}')
    rebuild.add_argument("--output", type=Path, default=None,
                         help="Defaults to the neutral seed path (overwrites it). "
                              "Pass an explicit ARI_OS_HOME path to keep the neutral seed intact.")
    rebuild.add_argument("--sample-per-region", type=int, default=2000)
    rebuild.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    if args.command == "rebuild-anchors":
        data = json.loads(args.queries_file.read_text())
        rows = data.get("rows", data) if isinstance(data, dict) else data
        queries = [r["query"] if isinstance(r, dict) else r for r in rows]
        rebuild_anchors(
            args.db, queries,
            output_path=args.output,
            sample_per_region=args.sample_per_region,
            seed=args.seed,
        )
        out = args.output or DEFAULT_ANCHORS_PATH
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
