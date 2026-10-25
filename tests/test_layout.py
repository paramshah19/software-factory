"""The folder layout: code in src/, everything that is yours in workspace/, and the one-time move from the old layout."""

from pathlib import Path

import checks
import common
import factory

REPO = Path(__file__).resolve().parent.parent


def test_user_data_lives_in_workspace_and_code_in_src():
    assert common.ROOT == REPO and common.HERE == REPO / "src"
    assert factory.RUNS_DIR == common.WORKSPACE / "runs"
    assert checks.ALWAYS_DIR == common.WORKSPACE / "always-use" and checks.SKILLS_LIBRARY == common.WORKSPACE / "skills-library"
    assert common.AGENTS_DIR == REPO / "agents" and (REPO / "dashboard" / "index.html").exists()
    top = {p.name for p in REPO.iterdir() if not p.name.startswith(".")} - {"__pycache__"}
    assert not [n for n in top if n.endswith(".py")], "no loose Python files at the top: they belong in src/ or scripts/"


def test_old_layout_is_moved_into_workspace_without_overwriting(tmp_path):
    root, ws = tmp_path / "repo", tmp_path / "repo" / "workspace"
    (root / "runs" / "20260101-a").mkdir(parents=True)
    (root / "runs" / "20260101-a" / "state.json").write_text("{}", encoding="utf-8")
    (root / "always-use").mkdir()
    (root / "always-use" / "brand.md").write_text("old brand", encoding="utf-8")
    (root / "always-use" / "README.md").write_text("old readme", encoding="utf-8")
    (root / "skills-library" / "my-skill").mkdir(parents=True)
    (ws / "always-use").mkdir(parents=True)
    (ws / "always-use" / "README.md").write_text("new readme", encoding="utf-8")   # already there: must win

    moved = common.migrate_legacy_layout(root, ws)

    assert sorted(moved) == ["always-use/brand.md", "runs/20260101-a", "skills-library/my-skill"]
    assert (ws / "runs" / "20260101-a" / "state.json").exists() and (ws / "skills-library" / "my-skill").is_dir()
    assert (ws / "always-use" / "brand.md").read_text(encoding="utf-8") == "old brand"
    assert (ws / "always-use" / "README.md").read_text(encoding="utf-8") == "new readme"
    assert (root / "always-use" / "README.md").exists()          # not moved, so it is not deleted
    assert not (root / "runs").exists() and not (root / "skills-library").exists()   # emptied folders are removed
    assert common.migrate_legacy_layout(root, ws) == []          # running it again does nothing


def test_nothing_to_move_is_fine(tmp_path):
    assert common.migrate_legacy_layout(tmp_path / "none", tmp_path / "none" / "workspace") == []
