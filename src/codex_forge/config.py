from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


# The sandbox modes Codex accepts. `read-only` and `workspace-write` are enforced on Windows with a
# restricted token; `danger-full-access` is not. The worker mode and the reviewer mode are both
# checked against this set when the configuration is read, so an unsupported name fails there rather
# than as an unexplained Codex launch failure part-way through a task.
SANDBOX_MODES = frozenset({"read-only", "workspace-write", "danger-full-access"})

# The reviewer keeps the least-privilege default it has always had. An operator whose host cannot run
# a useful review under a restricted token may widen it deliberately through `codex.review_sandbox`.
# Widening it is never silent: the requested and effective modes are both recorded in terminal
# evidence, so a review that ran unrestricted says so.
DEFAULT_REVIEW_SANDBOX = "read-only"

# Telemetry is opt-in and local-only. Every bound below exists so a misconfigured `[telemetry]`
# section fails when the configuration is read rather than by emitting something unintended at
# runtime: an unbounded service name would become an unbounded resource attribute, and a
# non-loopback endpoint would turn a local observability slice into network egress.
TELEMETRY_KEYS = frozenset(
    {"enabled", "service_name", "endpoint", "protocol", "exporter", "sample_ratio", "environment"}
)
TELEMETRY_PROTOCOLS = frozenset({"grpc"})
TELEMETRY_EXPORTERS = frozenset({"otlp"})
TELEMETRY_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_SERVICE_NAME_CHARS = 64
MAX_ENVIRONMENT_CHARS = 32
MAX_ENDPOINT_CHARS = 200

# A deliberately narrow charset. It admits the names an operator would choose and rejects the
# shapes a leak would take — whitespace, path separators, and URL punctuation.
_BOUNDED_LABEL = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(slots=True, frozen=True)
class TelemetryConfig:
    """Resolved `[telemetry]` settings. Disabled by default, so an existing forge.toml is unchanged."""

    enabled: bool = False
    service_name: str = "codex-forge"
    endpoint: str = "http://127.0.0.1:4317"
    protocol: str = "grpc"
    exporter: str = "otlp"
    sample_ratio: float = 1.0
    environment: str = "local"


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
    review_sandbox: str = DEFAULT_REVIEW_SANDBOX
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
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)


def _resolve_path(value: str, base: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    path = Path(expanded)
    return path if path.is_absolute() else (base / path).resolve()


def _get(data: dict[str, Any], section: str, key: str, default: Any) -> Any:
    return data.get(section, {}).get(key, default)


def _integer_option(data: dict[str, Any], section: str, key: str, default: int, minimum: int) -> int:
    value = _get(data, section, key, default)
    if type(value) is not int or value < minimum:
        raise ValueError(f"{section}.{key} must be an integer >= {minimum}.")
    return value


def _bounded_label(section: dict[str, Any], key: str, default: str, limit: int, *, allow_empty: bool) -> str:
    """Read one short identifier that is safe to publish as a telemetry resource attribute."""
    value = section.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"telemetry.{key} must be a string.")
    value = value.strip()
    if not value:
        if allow_empty:
            return ""
        raise ValueError(f"telemetry.{key} must not be empty.")
    if len(value) > limit:
        raise ValueError(f"telemetry.{key} must be at most {limit} characters.")
    if not _BOUNDED_LABEL.fullmatch(value):
        raise ValueError(
            f"telemetry.{key} may contain only letters, digits, '.', '_', and '-'; got {value!r}."
        )
    return value


def _telemetry_endpoint(section: dict[str, Any], default: str) -> str:
    """Read the collector endpoint, rejecting anything that is not a plain loopback OTLP address.

    This slice exports to a Collector on the same machine and carries no credentials, so a remote
    host, a TLS endpoint, embedded userinfo, or a query string is a configuration error rather than
    something to accept and then try to redact later.
    """
    value = section.get("endpoint", default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("telemetry.endpoint must be a non-empty string.")
    value = value.strip()
    if len(value) > MAX_ENDPOINT_CHARS:
        raise ValueError(f"telemetry.endpoint must be at most {MAX_ENDPOINT_CHARS} characters.")
    parsed = urlsplit(value if "://" in value else f"//{value}")
    if parsed.scheme not in {"", "http"}:
        raise ValueError(
            f"telemetry.endpoint must use http:// for a local collector; got scheme {parsed.scheme!r}."
        )
    if parsed.username or parsed.password:
        raise ValueError("telemetry.endpoint must not embed credentials.")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("telemetry.endpoint must be a bare host and port.")
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("telemetry.endpoint is not a valid host and port.") from exc
    if (host or "").lower() not in TELEMETRY_LOOPBACK_HOSTS:
        raise ValueError(
            "telemetry.endpoint must address the loopback interface "
            f"({', '.join(sorted(TELEMETRY_LOOPBACK_HOSTS))}); Forge exports only to a local collector."
        )
    if port is None:
        raise ValueError("telemetry.endpoint must include an explicit port.")
    return value


def _telemetry_sample_ratio(section: dict[str, Any]) -> float:
    value = section.get("sample_ratio", 1.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("telemetry.sample_ratio must be a number between 0.0 and 1.0.")
    ratio = float(value)
    if not math.isfinite(ratio) or not 0.0 <= ratio <= 1.0:
        raise ValueError("telemetry.sample_ratio must be between 0.0 and 1.0.")
    return ratio


def _telemetry_choice(section: dict[str, Any], key: str, default: str, supported: frozenset[str]) -> str:
    value = section.get(key, default)
    if not isinstance(value, str) or value.strip() not in supported:
        raise ValueError(
            f"telemetry.{key} must be one of: {', '.join(sorted(supported))}; got {value!r}."
        )
    return value.strip()


def load_telemetry_config(data: dict[str, Any]) -> TelemetryConfig:
    """Resolve `[telemetry]`, or return the disabled default when the section is absent.

    An unknown key is an error rather than something ignored: a typo in a privacy-relevant setting
    must not silently resolve to a default the operator did not choose.
    """
    if "telemetry" not in data:
        return TelemetryConfig()
    section = data.get("telemetry")
    if not isinstance(section, dict):
        raise ValueError("[telemetry] must be a table.")
    unknown = sorted(set(section) - TELEMETRY_KEYS)
    if unknown:
        raise ValueError(
            f"Unknown telemetry option(s): {', '.join(unknown)}. "
            f"Supported options: {', '.join(sorted(TELEMETRY_KEYS))}."
        )
    defaults = TelemetryConfig()
    enabled = section.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        raise ValueError("telemetry.enabled must be true or false.")
    return TelemetryConfig(
        enabled=enabled,
        service_name=_bounded_label(
            section, "service_name", defaults.service_name, MAX_SERVICE_NAME_CHARS, allow_empty=False
        ),
        endpoint=_telemetry_endpoint(section, defaults.endpoint),
        protocol=_telemetry_choice(section, "protocol", defaults.protocol, TELEMETRY_PROTOCOLS),
        exporter=_telemetry_choice(section, "exporter", defaults.exporter, TELEMETRY_EXPORTERS),
        sample_ratio=_telemetry_sample_ratio(section),
        environment=_bounded_label(
            section, "environment", defaults.environment, MAX_ENVIRONMENT_CHARS, allow_empty=True
        ),
    )


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
        review_sandbox=_get(data, "codex", "review_sandbox", DEFAULT_REVIEW_SANDBOX),
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
        max_parallel=_integer_option(data, "workers", "max_parallel", 2, 1),
        poll_seconds=_integer_option(data, "workers", "poll_seconds", 15, 1),
        task_timeout_minutes=_integer_option(data, "limits", "task_timeout_minutes", 180, 1),
        max_repair_cycles=_integer_option(data, "limits", "max_repair_cycles", 2, 0),
        max_identical_failures=_integer_option(data, "limits", "max_identical_failures", 2, 2),
        max_task_tokens=_integer_option(data, "limits", "max_task_tokens", 2_000_000, 0),
        max_output_chars=_integer_option(data, "limits", "max_output_chars", 12_000, 1),
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
        telemetry=load_telemetry_config(data),
    )
    if config.approval_policy != "never":
        raise ValueError("Codex Forge requires codex.approval_policy='never' for unattended execution.")
    # A TOML array or table is unhashable, so a bare membership test against `SANDBOX_MODES` would
    # raise `TypeError` instead of the configuration error every other malformed value here gets.
    # Check the type first so any non-string mode fails the same controlled way an unknown name does.
    if not isinstance(config.sandbox, str) or config.sandbox not in SANDBOX_MODES:
        raise ValueError(f"Unsupported Codex sandbox mode: {config.sandbox}")
    if not isinstance(config.review_sandbox, str) or config.review_sandbox not in SANDBOX_MODES:
        raise ValueError(f"Unsupported Codex review sandbox mode: {config.review_sandbox}")
    return config
