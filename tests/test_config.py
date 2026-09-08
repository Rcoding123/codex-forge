from pathlib import Path

from codex_forge.config import DEFAULT_REVIEW_SANDBOX, load_config


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
    # A configuration written before `codex.review_sandbox` existed keeps the reviewer it had.
    assert config.review_sandbox == "read-only"
    assert config.review_sandbox == DEFAULT_REVIEW_SANDBOX
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


def test_load_config_accepts_an_explicit_review_sandbox(tmp_path: Path):
    """An operator can widen the reviewer without touching the worker sandbox."""
    config_path = tmp_path / "forge.toml"
    config_path.write_text(
        """
[paths]
data_root = "data"

[codex]
sandbox = "danger-full-access"
review_sandbox = "danger-full-access"
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.review_sandbox == "danger-full-access"
    # Widening the reviewer is the only thing that changed; the worker mode is read independently.
    assert config.sandbox == "danger-full-access"


def test_load_config_accepts_a_restricted_review_sandbox(tmp_path: Path):
    """The reviewer mode is read independently of the worker mode, not copied from it."""
    config_path = tmp_path / "forge.toml"
    config_path.write_text(
        """
[paths]
data_root = "data"

[codex]
sandbox = "danger-full-access"
review_sandbox = "workspace-write"
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.review_sandbox == "workspace-write"
    assert config.sandbox == "danger-full-access"


def test_load_config_rejects_an_unsupported_review_sandbox(tmp_path: Path):
    """An unsupported reviewer mode fails when the configuration is read, not mid-task."""
    config_path = tmp_path / "forge.toml"
    config_path.write_text(
        """
[paths]
data_root = "data"

[codex]
review_sandbox = "full-access"
""",
        encoding="utf-8",
    )
    try:
        load_config(config_path)
    except ValueError as exc:
        assert "Unsupported Codex review sandbox mode: full-access" in str(exc)
    else:
        raise AssertionError("unsupported review sandbox mode was accepted")


def _load_with_codex_section(tmp_path: Path, body: str):
    """Read a minimal configuration whose `[codex]` section carries `body`."""
    config_path = tmp_path / "forge.toml"
    config_path.write_text(
        f"""
[paths]
data_root = "data"

[codex]
{body}
""",
        encoding="utf-8",
    )
    return load_config(config_path)


def test_load_config_rejects_non_string_review_sandbox_values(tmp_path: Path):
    """A malformed reviewer mode is a configuration error, not an unhandled TypeError.

    A TOML array or table is unhashable, so testing one against the mode set directly raises
    `TypeError` and escapes the configuration-error path an operator can act on. Numbers and
    booleans are hashable and already failed correctly; they are pinned so the type guard does
    not quietly change how they are reported.
    """
    for name, body in {
        "array": 'review_sandbox = ["read-only"]',
        "table": 'review_sandbox = { mode = "read-only" }',
        "number": "review_sandbox = 3",
        "bool": "review_sandbox = true",
    }.items():
        try:
            _load_with_codex_section(tmp_path, body)
        except ValueError as exc:
            assert "Unsupported Codex review sandbox mode" in str(exc)
        else:
            raise AssertionError(f"malformed review sandbox ({name}) was accepted")


def test_load_config_rejects_non_string_worker_sandbox_values(tmp_path: Path):
    """The worker mode is checked against the same set, so it fails the same controlled way."""
    for name, body in {
        "array": 'sandbox = ["danger-full-access"]',
        "table": 'sandbox = { mode = "read-only" }',
        "number": "sandbox = 7",
        "bool": "sandbox = false",
    }.items():
        try:
            _load_with_codex_section(tmp_path, body)
        except ValueError as exc:
            assert "Unsupported Codex sandbox mode" in str(exc)
        else:
            raise AssertionError(f"malformed worker sandbox ({name}) was accepted")


def test_load_config_accepts_every_supported_sandbox_mode(tmp_path: Path):
    """Guarding the value type must not narrow the modes either field accepts."""
    for mode in ("read-only", "workspace-write", "danger-full-access"):
        config = _load_with_codex_section(tmp_path, f'sandbox = "{mode}"\nreview_sandbox = "{mode}"')
        assert config.sandbox == mode
        assert config.review_sandbox == mode
