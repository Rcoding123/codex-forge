from pathlib import Path

from codex_forge.config import load_config


def test_load_config_resolves_relative_paths(tmp_path: Path):
    config_path = tmp_path / "forge.toml"
    config_path.write_text(
        """
[paths]
data_root = "data"
workspace_root = "worktrees"
repository_root = "repos"
inbox_root = "inbox"
database_path = "data/forge.db"
evidence_root = "evidence"
knowledge_root = "knowledge"

[codex]
approval_policy = "never"
sandbox = "danger-full-access"
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.data_root == (tmp_path / "data").resolve()
    assert config.workspace_root == (tmp_path / "worktrees").resolve()
    assert config.approval_policy == "never"
    assert config.sandbox == "danger-full-access"
    assert "multi_agent" in config.worker_disabled_features
    assert "browser_use" in config.worker_disabled_features


def test_load_config_rejects_interactive_approval_policy(tmp_path: Path):
    config_path = tmp_path / "forge.toml"
    config_path.write_text(
        """
[paths]
data_root = "data"

[codex]
approval_policy = "on-request"
""",
        encoding="utf-8",
    )
    try:
        load_config(config_path)
    except ValueError as exc:
        assert "approval_policy='never'" in str(exc)
    else:
        raise AssertionError("interactive approval policy was accepted")
