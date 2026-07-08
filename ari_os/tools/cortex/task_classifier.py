"""Task-type centroid classifier (council S2, spec §3a).

Locked task-type vocabulary — six labels, one fallback (``reason``):

    build, voice, ideate, reason, research, visual

:class:`CentroidClassifier` ranks a query against per-task-type centroids via
cosine and returns the top label, with two abstention triggers:

- ``top_score < min_confidence`` → fallback, ``low_confidence=True``.
- ``top_score - second_score <= tie_epsilon`` → fallback, ``fallback_reason="tie"``.

The shipped centroid artifact is the **neutral seed** (``task_centroids
.neutral.json``): per-task-type uniform-axis unit vectors (one-hot in a
6-dim space). This means at default confidence 0.18 + tie 0.01, *any* query
embedding triggers a tie and returns the fallback — by design. Real
per-task centroids are rebuilt locally by the user from their own query
history via ``python -m ari_os.tools.cortex.task_classifier rebuild-centroids``.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import EMBED_MODEL

TASK_TYPES = ("build", "voice", "ideate", "reason", "research", "visual")
DEFAULT_FALLBACK = "reason"
DEFAULT_CENTROIDS_PATH = (
    Path(__file__).resolve().parent.parent / "seeds" / "task_centroids.neutral.json"
)

# Seed queries per task type. Scrubbed: generic task archetypes; no private
# voice / identity references. Used only by the offline rebuild-centroids
# command — never evaluated online.
SEED_QUERIES: dict[str, list[str]] = {
    "build": [
        "fix the bug in this module",
        "add a feature to the plugin",
        "wire the poller as a child process",
        "merge the branch onto main",
        "write a unit test for the classifier",
    ],
    "voice": [
        "draft a post in this voice",
        "rewrite this caption in that voice",
        "summarize the retrieval notes in voice",
        "brand-check this copy",
        "write the about page in a consistent voice",
    ],
    "ideate": [
        "brainstorm names for the plugin",
        "fresh angle for this piece",
        "riff on the council-of-regions idea",
        "creative direction for the shoot",
        "concept ideas for the visual identity",
    ],
    "reason": [
        "should we flip this switch on",
        "is this the right metric to use",
        "work through the worktree tangle",
        "floor or blend for vmpfc",
        "decompose this architecture tradeoff",
    ],
    "research": [
        "what do we know about Global Workspace Theory",
        "summarize the recon map",
        "recall the prior findings",
        "state of the brain schema",
        "find prior work on this",
    ],
    "visual": [
        "describe this card",
        "the look of this frame",
        "image prompt for the scene",
        "analyze the colour grade",
        "visual references for the mood",
    ],
}


class TaskClassifier(Protocol):
    def classify(self, query: str, query_embedding: list[float] | None) -> str:
        """Return one locked task-type label for a query."""


@dataclass(frozen=True)
class ClassificationResult:
    task_type: str
    confidence: float
    low_confidence: bool = False
    fallback_reason: str | None = None
    scores: dict[str, float] | None = None


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    an = math.sqrt(sum(x * x for x in a))
    bn = math.sqrt(sum(y * y for y in b))
    if an == 0.0 or bn == 0.0:
        return 0.0
    return dot / (an * bn)


class CentroidClassifier:
    def __init__(
        self,
        centroids: dict[str, list[float]],
        *,
        fallback: str = DEFAULT_FALLBACK,
        min_confidence: float = 0.18,
        tie_epsilon: float = 0.01,
    ) -> None:
        unknown = set(centroids) - set(TASK_TYPES)
        missing = set(TASK_TYPES) - set(centroids)
        if unknown or missing:
            raise ValueError(
                f"centroids must cover exactly {TASK_TYPES}; "
                f"missing={sorted(missing)} unknown={sorted(unknown)}"
            )
        if fallback not in TASK_TYPES:
            raise ValueError(f"fallback must be one of {TASK_TYPES}, got {fallback!r}")
        self.centroids = centroids
        self.fallback = fallback
        self.min_confidence = min_confidence
        self.tie_epsilon = tie_epsilon
        self.last_result: ClassificationResult | None = None
        self.last_confidence: float | None = None

    def classify(self, query: str, query_embedding: list[float] | None) -> str:
        result = self.classify_result(query, query_embedding)
        self.last_result = result
        self.last_confidence = result.confidence
        return result.task_type

    def classify_result(self, query: str, query_embedding: list[float] | None) -> ClassificationResult:
        _ = query  # Reserved for the LLM seam and diagnostics; centroid uses embedding only.
        if query_embedding is None:
            return ClassificationResult(
                self.fallback, 0.0, low_confidence=True,
                fallback_reason="missing_embedding", scores={},
            )

        scores = {
            task_type: _cosine(query_embedding, centroid)
            for task_type, centroid in self.centroids.items()
        }
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        top_type, top_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else float("-inf")

        if top_score < self.min_confidence:
            return ClassificationResult(
                self.fallback, top_score, low_confidence=True,
                fallback_reason="low_confidence", scores=scores,
            )
        if (top_score - second_score) <= self.tie_epsilon:
            return ClassificationResult(
                self.fallback, top_score, fallback_reason="tie", scores=scores,
            )
        return ClassificationResult(top_type, top_score, scores=scores)


class LLMClassifier:
    """Future seam for a local task classifier LLM."""

    def __init__(self, model: str = "ollama:gemma3:4b") -> None:
        self.model = model
        self.last_confidence: float | None = None

    def classify(self, query: str, query_embedding: list[float] | None) -> str:
        _ = (query, query_embedding)
        raise NotImplementedError("LLMClassifier is a seam only; use CentroidClassifier online.")


def load_centroid_artifact(path: Path = DEFAULT_CENTROIDS_PATH) -> dict:
    return json.loads(path.read_text())


def classifier_from_artifact(path: Path = DEFAULT_CENTROIDS_PATH) -> CentroidClassifier:
    artifact = load_centroid_artifact(path)
    centroids = {task_type: artifact["centroids"][task_type] for task_type in TASK_TYPES}
    return CentroidClassifier(
        centroids,
        fallback=artifact.get("fallback", DEFAULT_FALLBACK),
        min_confidence=float(artifact.get("min_confidence", 0.18)),
        tie_epsilon=float(artifact.get("tie_epsilon", 0.01)),
    )


_DEFAULT_CLASSIFIER: CentroidClassifier | None = None


def get_default_classifier() -> CentroidClassifier:
    global _DEFAULT_CLASSIFIER
    if _DEFAULT_CLASSIFIER is None:
        _DEFAULT_CLASSIFIER = classifier_from_artifact()
    return _DEFAULT_CLASSIFIER


def rebuild_centroids(
    *,
    embed_client=None,
    output_path: Path | None = None,
) -> dict:
    """Rebuild the versioned centroid artifact from the locked seed queries.

    If ``output_path`` is None, writes the rebuilt artifact alongside the
    neutral seed at the default path — which OVERWRITES the neutral seed.
    Pass an explicit path under the user's ARI_OS_HOME to preserve the
    shipped neutral defaults.
    """
    if embed_client is None:
        from .embed import EmbedClient, default_embed_client
        embed_client = default_embed_client()

    centroids: dict[str, list[float]] = {}
    for task_type in TASK_TYPES:
        embeddings = embed_client.embed(SEED_QUERIES[task_type])
        if not embeddings:
            raise ValueError(f"no embeddings generated for {task_type}")
        dim = len(embeddings[0])
        totals = [0.0] * dim
        for emb in embeddings:
            if len(emb) != dim:
                raise ValueError(f"embedding dimension mismatch for {task_type}")
            for i, value in enumerate(emb):
                totals[i] += float(value)
        centroids[task_type] = [v / len(embeddings) for v in totals]

    artifact = {
        "version": 1,
        "model": EMBED_MODEL,
        "fallback": DEFAULT_FALLBACK,
        "min_confidence": 0.18,
        "tie_epsilon": 0.01,
        "task_types": list(TASK_TYPES),
        "seed_queries": SEED_QUERIES,
        "centroids": centroids,
    }
    target = output_path if output_path is not None else DEFAULT_CENTROIDS_PATH
    target.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    return artifact


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Task-type centroid utilities")
    sub = parser.add_subparsers(dest="command", required=True)
    rebuild = sub.add_parser("rebuild-centroids")
    rebuild.add_argument("--output", type=Path, default=None,
                         help="Defaults to the neutral seed path (overwrites it). "
                              "Pass an explicit ARI_OS_HOME path to keep the neutral seed intact.")
    args = parser.parse_args()

    if args.command == "rebuild-centroids":
        rebuild_centroids(output_path=args.output)
        out = args.output or DEFAULT_CENTROIDS_PATH
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
