"""workspace_map tests — cwd/path → workspace slug mapping.

Covers the three mapping layers (configured roots, memories tree, legacy
``workspaces/`` fallback), slug normalization, and the lossy CC
project-dirname fallback used by transcript ingest.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ari_os.tools.cortex import workspace_map


# ---------- fixtures --------------------------------------------------------

@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """Isolated $ARI_OS_HOME so real user config never leaks into tests."""
    monkeypatch.setenv("ARI_OS_HOME", str(tmp_path / "arios-home"))
    (tmp_path / "arios-home").mkdir()
    return tmp_path / "arios-home"


@pytest.fixture
def projects_root(tmp_path: Path, home: Path) -> Path:
    """A configured workspace root with two repo-style children."""
    root = tmp_path / "Projects"
    (root / "EA-xfactor").mkdir(parents=True)
    (root / "EA_Animarek").mkdir()
    (home / "config.json").write_text(
        json.dumps({"cortex.workspace_roots": [str(root)]})
    )
    return root


# ---------- workspace_roots -------------------------------------------------

def test_workspace_roots_reads_config(projects_root):
    assert workspace_map.workspace_roots() == [projects_root]


def test_workspace_roots_nested_config_key(home, tmp_path):
    root = tmp_path / "Other"
    (home / "config.json").write_text(
        json.dumps({"cortex": {"workspace_roots": [str(root)]}})
    )
    assert workspace_map.workspace_roots() == [root]


def test_workspace_roots_default_when_unconfigured(home):
    roots = workspace_map.workspace_roots()
    assert roots == [Path(os.path.expanduser("~/Desktop/ClaudeCode_Projects"))]


def test_workspace_roots_malformed_config_falls_back(home):
    (home / "config.json").write_text(
        json.dumps({"cortex.workspace_roots": "not-a-list"})
    )
    default = [Path(os.path.expanduser(r)) for r in workspace_map.DEFAULT_WORKSPACE_ROOTS]
    assert workspace_map.workspace_roots() == default


# ---------- cwd_to_workspace ------------------------------------------------

def test_cwd_under_configured_root_maps_and_normalizes(projects_root):
    assert workspace_map.cwd_to_workspace(str(projects_root / "EA-xfactor")) == "ea-xfactor"
    assert (
        workspace_map.cwd_to_workspace(str(projects_root / "EA_Animarek" / "sub" / "dir"))
        == "ea-animarek"
    )


def test_cwd_at_root_itself_is_not_a_workspace(projects_root):
    assert workspace_map.cwd_to_workspace(str(projects_root)) is None


def test_cwd_legacy_workspaces_fallback_is_verbatim(projects_root):
    # Legacy layout keeps the raw component — old brains tagged sources with
    # the verbatim folder name and the tunnel gate must keep matching.
    assert workspace_map.cwd_to_workspace("/Users/me/workspaces/projA/src") == "projA"
    assert workspace_map.cwd_to_workspace("/Users/me/workspaces/proj") == "proj"


def test_cwd_no_match_returns_none(projects_root):
    assert workspace_map.cwd_to_workspace("/Users/me/elsewhere/repo") is None
    assert workspace_map.cwd_to_workspace(None) is None
    assert workspace_map.cwd_to_workspace("") is None


# ---------- path_to_workspace -----------------------------------------------

def test_memory_file_maps_to_its_workspace_folder(home, projects_root):
    p = home / "memories" / "ea-xfactor" / "2026-08-01-note.md"
    assert workspace_map.path_to_workspace(p) == "ea-xfactor"


def test_memory_file_at_memories_top_level_has_no_workspace(home, projects_root):
    assert workspace_map.path_to_workspace(home / "memories" / "loose.md") is None


def test_path_under_root_maps_like_cwd(projects_root):
    p = projects_root / "EA_Animarek" / "docs" / "x.md"
    assert workspace_map.path_to_workspace(p) == "ea-animarek"


def test_path_legacy_opt_out(projects_root):
    legacy = "/Users/me/workspaces/projA/a.md"
    assert workspace_map.path_to_workspace(legacy) == "projA"
    assert workspace_map.path_to_workspace(legacy, legacy=False) is None


# ---------- transcript_dir_to_workspace -------------------------------------

def _enc(path: Path) -> str:
    return workspace_map.encode_cc_project_dirname(str(path))


def test_transcript_dirname_matches_known_child(projects_root, tmp_path):
    # ~/.claude/projects/<encoded-cwd>/session.jsonl — encoding is lossy
    # (both "/" and "_" become "-"), so EA_Animarek arrives as EA-Animarek.
    proj_dir = tmp_path / f"{_enc(projects_root)}-EA-Animarek"
    proj_dir.mkdir()
    jsonl = proj_dir / "session.jsonl"
    assert workspace_map.transcript_dir_to_workspace(jsonl) == "ea-animarek"


def test_transcript_dirname_longest_child_wins(home, tmp_path):
    root = tmp_path / "Projects"
    (root / "proj").mkdir(parents=True)
    (root / "proj-web").mkdir()
    (home / "config.json").write_text(
        json.dumps({"cortex.workspace_roots": [str(root)]})
    )
    jsonl = tmp_path / f"{_enc(root)}-proj-web" / "s.jsonl"
    jsonl.parent.mkdir()
    assert workspace_map.transcript_dir_to_workspace(jsonl) == "proj-web"


def test_transcript_dirname_unknown_returns_none(projects_root, tmp_path):
    jsonl = tmp_path / "-Users-someone-else-place" / "s.jsonl"
    jsonl.parent.mkdir()
    assert workspace_map.transcript_dir_to_workspace(jsonl) is None
