from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ForgeConfig:
    config_path: Path
    workspace_root: Path
    data_root: Path
    repository_root: Path
    inbox_root: Path
    database_path: Path
    evidence_root: Path
    knowledge_root: Path
    codex_executable: str = "codex"
    sandbox: str = "danger-full-access"
    approval_policy: str = "never"
    implementation_model: str = "gpt-5.6-terra"
    escalation_model: str = "gpt-5.6-sol"
    review_model: str = "gpt-5.6-terra"
    implementation_effort: str = "medium"
    escalation_effort: str = "high"
    review_effort: str = "medium"
    max_parallel: int = 2
    poll_seconds: int = 15
    task_timeout_minutes: int = 180
    max_repair_cycles: int = 2
    max_identical_failures: int = 2
    max_task_tokens: int = 2_000_000
    max_output_chars: int = 12_000
    extra_codex_args: list[str] = field(default_factory=list)
    worker_disabled_features: list[str] = field(
        default_factory=lambda: [
            "apps",
            "browser_use",
            "browser_use_external",
            "goals",
            "image_generation",
            "memories",
            "multi_agent",
            "plugins",
            "skill_search",
        ]
    )


def _resolve_path(value: str, base: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    path = Path(expanded)
    return path if path.is_absolute() else (base / path).resolve()


def _get(data: dict[str, Any], section: str, key: str, default: Any) -> Any:
    return data.get(section, {}).get(key, default)


def load_config(path: str | Path | None = None) -> ForgeConfig:
    config_path = Path(path or os.environ.get("CODEX_FORGE_CONFIG", "forge.toml")).resolve()
    if not config_path.exists():
        raise FileNotFoundError(
            f"Forge config not found: {config_path}. Run bootstrap.ps1 or copy forge.example.toml to forge.toml."
        )
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    base = config_path.parent

    data_root = _resolve_path(_get(data, "paths", "data_root", "./.forge-data"), base)
    workspace_root = _resolve_path(_get(data, "paths", "workspace_root", str(data_root / "worktrees")), base)
    repository_root = _resolve_path(_get(data, "paths", "repository_root", str(data_root / "repositories")), base)
    inbox_root = _resolve_path(_get(data, "paths", "inbox_root", str(data_root / "inbox")), base)
    database_path = _resolve_path(_get(data, "paths", "database_path", str(data_root / "forge.db")), base)
    evidence_root = _resolve_path(_get(data, "paths", "evidence_root", str(data_root / "evidence")), base)
    knowledge_root = _resolve_path(_get(data, "paths", "knowledge_root", str(data_root / "knowledge")), base)

    config = ForgeConfig(
        config_path=config_path,
        workspace_root=workspace_root,
        data_root=data_root,
        repository_root=repository_root,
        inbox_root=inbox_root,
        database_path=database_path,
        evidence_root=evidence_root,
        knowledge_root=knowledge_root,
        codex_executable=_get(data, "codex", "executable", "codex"),
        sandbox=_get(data, "codex", "sandbox", "danger-full-access"),
        approval_policy=_get(data, "codex", "approval_policy", "never"),
        implementation_model=_get(data, "models", "implementation", "gpt-5.6-terra"),
        escalation_model=_get(
            data, "models", "escalation", _get(data, "models", "planning", "gpt-5.6-sol")
        ),
        review_model=_get(data, "models", "review", "gpt-5.6-terra"),
        implementation_effort=_get(data, "reasoning", "implementation", "medium"),
        escalation_effort=_get(
            data, "reasoning", "escalation", _get(data, "reasoning", "planning", "high")
        ),
        review_effort=_get(data, "reasoning", "review", "medium"),
        max_parallel=int(_get(data, "workers", "max_parallel", 2)),
        poll_seconds=int(_get(data, "workers", "poll_seconds", 15)),
        task_timeout_minutes=int(_get(data, "limits", "task_timeout_minutes", 180)),
        max_repair_cycles=int(_get(data, "limits", "max_repair_cycles", 2)),
        max_identical_failures=int(_get(data, "limits", "max_identical_failures", 2)),
        max_task_tokens=int(_get(data, "limits", "max_task_tokens", 2_000_000)),
        max_output_chars=int(_get(data, "limits", "max_output_chars", 12_000)),
        extra_codex_args=list(_get(data, "codex", "extra_args", [])),
        worker_disabled_features=list(
            _get(
                data,
                "codex",
                "disabled_features",
                [
                    "apps",
                    "browser_use",
                    "browser_use_external",
                    "goals",
                    "image_generation",
                    "memories",
                    "multi_agent",
                    "plugins",
                    "skill_search",
                ],
            )
        ),
    )
    if config.approval_policy != "never":
        raise ValueError("Codex Forge requires codex.approval_policy='never' for unattended execution.")
    if config.sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
        raise ValueError(f"Unsupported Codex sandbox mode: {config.sandbox}")
    if config.task_timeout_minutes <= 0:
        raise ValueError("limits.task_timeout_minutes must be positive.")
    if config.max_repair_cycles < 0:
        raise ValueError("limits.max_repair_cycles cannot be negative.")
    if config.max_identical_failures < 2:
        raise ValueError("limits.max_identical_failures must be at least 2.")
    if config.max_task_tokens < 0:
        raise ValueError("limits.max_task_tokens cannot be negative.")
    return config
