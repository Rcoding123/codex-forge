from __future__ import annotations

import tomllib
from pathlib import Path

from codex_forge import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_release_version_is_consistent():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        metadata = tomllib.load(handle)
    assert metadata["project"]["version"] == "0.2.0"
    assert __version__ == "0.2.0"


def test_gitignore_excludes_publication_sensitive_state():
    patterns = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    required = {
        ".venv/",
        "__pycache__/",
        ".pytest_cache/",
        ".ruff_cache/",
        "build/",
        "dist/",
        "forge.toml",
        "*.db",
        "logs/",
        "evidence/",
        "worktrees/",
        "sessions/",
        ".codex/",
        "auth.json",
        "*.zip",
        ".idea/",
        ".vscode/",
    }
    assert required <= patterns


def test_github_publication_helper_is_private_only():
    script = (ROOT / "publish-to-github.ps1").read_text(encoding="utf-8")
    assert "--private" in script
    assert "--public" not in script
    assert "Refusing to publish a dirty working tree" in script
