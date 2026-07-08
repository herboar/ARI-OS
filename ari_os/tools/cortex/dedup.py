"""Memory deduplication helpers for ARI-OS Cortex.

The helper compares a new memory description or markdown note against existing
memory files and returns the nearest frontmatter ``description:`` entries by
cosine similarity. It only reads files; callers decide whether a result is a
duplicate, extension, contradiction, or genuinely new memory.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .embed import EmbedClient, default_embed_client
from .similarity import cosine


@dataclass(frozen=True)
class DedupEntry:
    """One candidate memory description to compare."""

    label: str
    description: str


_FRONTMATTER_DESC = re.compile(r"^description:\s*(?P<desc>.+?)\s*$", re.MULTILINE)
_EXCLUDE_NAMES = {"MEMORY.md", "MEMORY.md.bak"}
_EXCLUDE_DIRS = {"drafts", "archive", "abandoned"}


def parse_memory_description(path: Path) -> str | None:
    """Return a markdown file's frontmatter ``description:`` value, if present."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_description_from_text(text)


def _parse_description_from_text(text: str) -> str | None:
    if not text.startswith("---"):
        return None
    end_idx = text.find("\n---", 3)
    if end_idx == -1:
        return None
    frontmatter = text[3:end_idx]
    match = _FRONTMATTER_DESC.search(frontmatter)
    if not match:
        return None
    return match.group("desc").strip().strip("'\"")


def _iter_memory_files(memory_dir: Path) -> Iterator[Path]:
    if not memory_dir.exists():
        return
    for path in sorted(memory_dir.rglob("*.md")):
        if not path.is_file():
            continue
        if path.name in _EXCLUDE_NAMES:
            continue
        rel_parts = set(path.relative_to(memory_dir).parts)
        if rel_parts & _EXCLUDE_DIRS:
            continue
        yield path


def _looks_like_path(value: str) -> bool:
    if "\n" in value or len(value) > 1024:
        return False
    try:
        return Path(value).exists()
    except OSError:
        return False


def _read_candidate_input(new_text_or_draft: str | Path) -> tuple[str, str]:
    if isinstance(new_text_or_draft, Path):
        return new_text_or_draft.name, new_text_or_draft.read_text(encoding="utf-8")
    if _looks_like_path(new_text_or_draft):
        path = Path(new_text_or_draft)
        return path.name, path.read_text(encoding="utf-8")
    return "input", new_text_or_draft


def _candidate_entries(new_text_or_draft: str | Path) -> list[DedupEntry]:
    label, text = _read_candidate_input(new_text_or_draft)
    description = _parse_description_from_text(text) or text.strip()
    if not description:
        return []
    return [DedupEntry(label=label, description=description)]


def run_dedup_check(
    new_text_or_draft: str | Path,
    memory_dir: str | Path,
    top_k: int = 5,
    *,
    embed_client: EmbedClient | None = None,
) -> dict:
    """Return nearest existing memory descriptions for each candidate input.

    ``new_text_or_draft`` may be plain text, markdown text, or a path to a
    markdown file. Existing memories are read from ``memory_dir`` and compared
    using frontmatter ``description:`` values only.
    """
    candidates = _candidate_entries(new_text_or_draft)
    if not candidates:
        return {"entries": []}

    memory_root = Path(memory_dir)
    memory_descs: list[tuple[Path, str]] = []
    for path in _iter_memory_files(memory_root):
        description = parse_memory_description(path)
        if description:
            memory_descs.append((path, description))

    if not memory_descs:
        return {
            "entries": [
                {
                    "label": entry.label,
                    "description": entry.description,
                    "nearest": [],
                }
                for entry in candidates
            ],
        }

    client = embed_client or default_embed_client()
    all_texts = [entry.description for entry in candidates] + [
        description for _, description in memory_descs
    ]
    vectors = client.embed(all_texts)
    candidate_vectors = vectors[: len(candidates)]
    memory_vectors = vectors[len(candidates) :]

    entries = []
    for candidate, candidate_vector in zip(candidates, candidate_vectors):
        nearest = [
            {
                "path": str(path),
                "description": description,
                "similarity": cosine(candidate_vector, memory_vector),
            }
            for (path, description), memory_vector in zip(memory_descs, memory_vectors)
        ]
        nearest.sort(key=lambda item: (-item["similarity"], item["path"]))
        entries.append(
            {
                "label": candidate.label,
                "description": candidate.description,
                "nearest": nearest[:top_k],
            }
        )
    return {"entries": entries}
