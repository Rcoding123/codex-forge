"""Closed, subscription-only native CLI launch policy for the one-job adapter.

No authentication response or credential file is persisted by this module.
The returned ``environment`` is private launch material, never result metadata.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from typing import Mapping


TESTED_VERSION = "0.151.0"
PROBE_TIMEOUT_S = 20.0
PROBE_MAX_BYTES = 64 * 1024
_DISABLED_FEATURES = (
    "apps", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "in_app_browser", "goals", "hooks", "memories", "multi_agent", "multi_agent_v2",
    "plugins", "remote_plugin", "skill_search", "skill_mcp_dependency_install",
)
_OVERRIDES = (
    'model_provider="openai"', 'forced_login_method="chatgpt"', 'web_search="disabled"',
    'project_root_markers=[".git"]',
    'shell_environment_policy.inherit="core"',
    'shell_environment_policy.ignore_default_excludes=false',
)
_SENSITIVE_FRAGMENTS = (
    "API_KEY", "AUTH_TOKEN", "ACCESS_TOKEN", "BEARER_TOKEN", "BASE_URL", "BASEURL",
    "API_BASE", "AUTHORIZATION", "AUTH_HEADER",
)
_PROVIDER_PREFIXES = (
    "ANTHROPIC_", "OPENAI_", "AZURE_", "AWS_", "GOOGLE_", "GEMINI_", "VERTEX_",
    "BEDROCK_", "OPENROUTER_", "LITELLM_", "TOGETHER_", "GROQ_", "MISTRAL_",
)
_ROUTING_KEYS = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "CODEX_API_KEY", "API_TOKEN", "API_KEY",
    "CUSTOM_API_URL", "OLLAMA_HOST", "LM_STUDIO_BASE_URL",
    "CODEX_MODEL_PROVIDER", "CODEX_DEFAULT_PROVIDER", "CODEX_PROFILE",
})
_CODEX_ROUTING_PREFIXES = ("CODEX_PROVIDER_", "CODEX_REMOTE_", "CODEX_USE_")


class PolicyError(ValueError):
    """A controller-owned preflight classification; never provider output."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def sanitized_child_environment(parent: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if parent is None else parent
    clean = {}
    for key, value in source.items():
        upper = key.upper()
        if upper == "CODEX_HOME":
            clean[key] = value
            continue
        if (upper.startswith(_CODEX_ROUTING_PREFIXES) or upper.startswith(_PROVIDER_PREFIXES)
                or upper in _ROUTING_KEYS
                or upper.endswith(("_TOKEN", "_SECRET", "_SECRET_KEY"))
                or any(fragment in upper for fragment in _SENSITIVE_FRAGMENTS)):
            continue
        clean[key] = value
    return clean


def _resolve_executable(executable: str | None, env: Mapping[str, str], *, cwd: Path | None = None) -> Path:
    search_path = env.get("PATH", "")
    search_root = Path.cwd() if cwd is None else cwd
    if os.name != "nt" and search_path:
        search_path = os.pathsep.join(str(Path(entry) if Path(entry).is_absolute() else search_root / entry)
                                      for entry in search_path.split(os.pathsep))
    if executable is None:
        local = env.get("LOCALAPPDATA") if os.name == "nt" else None
        installed = Path(local) / "Programs/OpenAI/Codex/bin/codex.exe" if local else None
        executable = str(installed) if installed and installed.is_file() else (
            shutil.which("codex", path=search_path) if search_path else None
        )
    if not executable:
        raise PolicyError("executable_missing", "A native Codex executable was not found")
    path = Path(executable).expanduser()
    if not path.is_absolute():
        if os.name != "nt" and os.path.dirname(str(path)):
            resolved = str(search_root / path)
        else:
            resolved = shutil.which(str(path), path=search_path) if search_path else None
        if not resolved:
            raise PolicyError("executable_missing", "The configured executable was not found")
        path = Path(resolved)
    path = path.resolve()
    if cwd is not None and path.is_relative_to(search_root.resolve()):
        raise PolicyError("executable_in_workspace", "Native CLI and launcher must be outside the editable worktree")
    if sys.platform == "linux" and path.name == "codex.js":
        path = _linux_npm_native(path)
        if cwd is not None and path.is_relative_to(search_root.resolve()):
            raise PolicyError("executable_in_workspace", "Native CLI payload must be outside the editable worktree")
    expected_name = "codex.exe" if os.name == "nt" else "codex"
    if path.name.lower() != expected_name or not path.is_file():
        raise PolicyError("native_executable_required", "A native codex executable is required; shims are refused")
    try:
        with path.open("rb") as image:
            header = image.read(4)
    except OSError as exc:
        raise PolicyError("executable_unreadable", "The native executable could not be read") from exc
    native_headers = {b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
                      b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"}
    if (os.name == "nt" and not header.startswith(b"MZ")) or (
            os.name != "nt" and header not in native_headers):
        raise PolicyError("native_executable_required", "An executable script cannot stand in for the native CLI")
    return path


def _package_json(path: Path) -> dict:
    """Read installation metadata, never execute an npm/Node launcher."""
    try:
        if not path.is_file() or path.stat().st_size > PROBE_MAX_BYTES:
            raise ValueError("unbounded_package_metadata")
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("invalid_package_metadata")
        return document
    except (OSError, ValueError) as exc:
        raise PolicyError("native_package_unverified", "The native npm installation layout could not be verified") from exc


def _linux_npm_native(launcher: Path) -> Path:
    """Resolve the pinned official Linux npm layout to its ELF payload.

    rust-v0.151.0 codex-cli/bin/codex.js resolves an optional platform package,
    then vendor/<target>/bin/codex. This implements only that filesystem lookup;
    no wrapper, npm command, SDK, registry request or alternate provider runs.
    The resulting binary still passes header, version, policy and auth checks.
    """
    architectures = {
        "x86_64": ("x86_64-unknown-linux-musl", "x64"),
        "amd64": ("x86_64-unknown-linux-musl", "x64"),
        "aarch64": ("aarch64-unknown-linux-musl", "arm64"),
        "arm64": ("aarch64-unknown-linux-musl", "arm64"),
    }
    selected = architectures.get(platform.machine().lower())
    if selected is None:
        raise PolicyError("native_platform_unsupported", "The native Linux architecture is unsupported")
    target, cpu = selected
    package_root = launcher.parent.parent
    package = _package_json(package_root / "package.json")
    if (launcher.parent.name != "bin" or package.get("name") != "@openai/codex"
            or package.get("version") != TESTED_VERSION
            or package.get("bin") != {"codex": "bin/codex.js"}):
        raise PolicyError("native_package_unverified", "The launcher is not the pinned official npm layout")
    alias = f"codex-linux-{cpu}"
    payload_root = package_root.resolve()
    vendor = package_root / "vendor"
    # Node's package lookup walks ancestor node_modules directories. pnpm's
    # dependency links resolve here too; unrelated wrappers remain unsupported.
    for ancestor in (package_root, *package_root.parents):
        candidate = ancestor / "node_modules" / "@openai" / alias
        metadata = candidate / "package.json"
        if not metadata.is_file():
            continue
        platform_package = _package_json(metadata)
        if (platform_package.get("name") != "@openai/codex"
                or platform_package.get("version") != f"{TESTED_VERSION}-linux-{cpu}"
                or platform_package.get("os") != ["linux"] or platform_package.get("cpu") != [cpu]):
            raise PolicyError("native_package_unverified", "The native platform package does not match the pinned CLI")
        vendor = candidate / "vendor"
        payload_root = candidate.resolve()
        break
    native = (vendor / target / "bin" / "codex").resolve()
    if not native.is_relative_to(payload_root):
        raise PolicyError("native_package_escape", "The native payload escapes the selected package directory")
    if not native.is_file() or not os.access(native, os.X_OK):
        raise PolicyError("native_executable_required", "The installed Linux native Codex payload is missing or not executable")
    return native


def _probe(argv: list[str], *, cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    """Read a fixed, non-inference command with a time and in-memory byte bound."""
    chunks: list[bytes] = []
    size = 0
    lock = threading.Lock()
    overflow = threading.Event()
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=creationflags,
        )
    except OSError as exc:
        raise PolicyError("probe_launch_failed", "Native capability inspection could not start") from exc

    def consume(pipe):
        nonlocal size
        try:
            while chunk := pipe.read(4096):
                with lock:
                    remaining = PROBE_MAX_BYTES - size
                    if remaining > 0:
                        chunks.append(chunk[:remaining])
                    size += len(chunk)
                    if size > PROBE_MAX_BYTES:
                        overflow.set()
        finally:
            pipe.close()

    readers = [threading.Thread(target=consume, args=(pipe,), daemon=True)
               for pipe in (process.stdout, process.stderr)]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + PROBE_TIMEOUT_S
    timed_out = False
    while process.poll() is None:
        if overflow.is_set() or time.monotonic() >= deadline:
            timed_out = not overflow.is_set()
            process.kill()
            break
        time.sleep(0.01)
    process.wait(timeout=5)
    for reader in readers:
        reader.join(timeout=1)
    if overflow.is_set() or timed_out or any(reader.is_alive() for reader in readers):
        raise PolicyError("probe_bounded_failure", "Native inspection exceeded its output or time bound")
    return process.returncode, b"".join(chunks).decode("utf-8", errors="replace")


def classify_auth_status(exit_code: int, text: str) -> str:
    """Conservative parser for the tested native status wording, not Claude JSON."""
    if exit_code != 0:
        return "unknown"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines == ["Logged in using ChatGPT"]:
        return "chatgpt_subscription"
    lowered = text.lower()
    if "api key" in lowered or "api-key" in lowered:
        return "api_key"
    if "access token" in lowered or "access-token" in lowered:
        return "access_token"
    return "unknown"


def _system_paths(env: Mapping[str, str]) -> list[Path]:
    if os.name == "nt":
        # Include both the conventional system location and environment descriptor;
        # a caller changing ProgramData must not hide the conventional location.
        roots = {Path("C:/ProgramData/OpenAI/Codex")}
        if env.get("ProgramData") or env.get("PROGRAMDATA"):
            roots.add(Path(env.get("ProgramData", env.get("PROGRAMDATA"))) / "OpenAI/Codex")
        return [root / name for root in roots for name in ("config.toml", "requirements.toml")]
    return [Path("/etc/codex") / name for name in ("config.toml", "requirements.toml", "managed_config.toml")]


def _project_roots(workspace: Path) -> list[Path]:
    # The launch pins the native root marker to .git. Include every layer from
    # cwd through that root, without reclassifying the skipped user config as a
    # project layer when its home is an ancestor of the repository.
    roots = []
    for root in [workspace, *workspace.parents]:
        roots.append(root)
        if (root / ".git").exists():
            break
    return roots


def _audit_config(workspace: Path, env: Mapping[str, str]) -> tuple[str, list[dict]]:
    # The native Unix CLI resolves its default home from the child HOME. Using
    # the adapter process's Path.home() can audit a different user's config.
    default_home = Path(env["HOME"]) if os.name != "nt" and env.get("HOME") else Path.home()
    codex_home = Path(env.get("CODEX_HOME", str(default_home / ".codex")))
    if not codex_home.is_absolute():
        raise PolicyError("config_home_unverified", "The child Codex configuration home must be absolute")
    paths: list[tuple[Path, str]] = [(path, "system") for path in _system_paths(env)]
    paths += [(codex_home / "managed_config.toml", "managed")]
    paths += [(codex_home / "hooks.json", "hooks")]
    for root in _project_roots(workspace):
        paths.extend(((root / ".codex/config.toml", "project"),
                      (root / ".codex/hooks.json", "hooks")))
    # rust-v0.151.0 config/src/loader/mod.rs project discovery loads
    # .codex/config.toml, not an application's plain ./config.toml. The loader's
    # older header prose names the latter, but its executable discovery does not.
    records = []
    seen = set()
    for path, source in paths:
        identity = os.path.normcase(str(path.absolute()))
        if identity in seen:
            continue
        seen.add(identity)
        try:
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise PolicyError("config_alias", "A configuration source is an unsupported filesystem alias")
            if not path.exists():
                records.append({"path": str(path), "source": source, "present": False})
                continue
            if not path.is_file() or path.stat().st_size > PROBE_MAX_BYTES:
                raise PolicyError("config_unreadable", "A configuration source is not a bounded regular file")
            raw = path.read_bytes()
        except OSError as exc:
            raise PolicyError("config_unreadable", "A configuration source could not be audited") from exc
        records.append({"path": str(path), "source": source, "present": True,
                        "sha256": hashlib.sha256(raw).hexdigest()})
        # Hooks are explicitly disabled on argv, but refuse installed hook files
        # rather than claiming trust/management overrides were proven neutralized.
        if source == "hooks":
            if raw.strip() not in (b"", b"{}"):
                raise PolicyError("config_extension", "Installed hooks require a separately verified policy")
            continue
        try:
            config = tomllib.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise PolicyError("config_invalid", "A configuration source is not valid TOML") from exc
        # A small fail-closed launch surface: project/system layers may contribute
        # instructions and a trust declaration, but no executable extension,
        # permission/profile override, provider or managed routing source.
        permitted = {"developer_instructions", "project_doc_max_bytes", "projects"}
        if set(config) - permitted:
            raise PolicyError("config_extension", "A project or managed configuration requires unsupported policy review")
    digest = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    return digest, records


def inspect_policy(request: dict, *, executable: str | None = None,
                   env: dict | None = None) -> dict:
    child_env = sanitized_child_environment(env)
    if os.name != "nt":
        child_env.setdefault("HOME", str(Path.home()))
        child_env.setdefault("CODEX_HOME", str(Path(child_env["HOME"]) / ".codex"))
    if any(key.upper().startswith("CODEX_") and "CONFIG" in key.upper()
           for key in child_env):
        raise PolicyError("config_source_unverified", "An environment-selected configuration source cannot be audited")
    workspace = Path(request["workspace"]["path"]).resolve()
    policy = request["policy"]
    if policy["sandbox"] not in {"workspace-write", "read-only"}:
        raise PolicyError("sandbox_unsupported", "The adapter accepts only restricted native sandboxes")
    if request.get("role", "implementation") != "implementation" or request.get("resume"):
        raise PolicyError("lane_unavailable", "Review, proposal and resume lanes are deferred")
    if policy.get("tool_network") == "deny":
        raise PolicyError("capability_unverified", "Exec-level command network denial is not verified")
    if any(policy.get(key, False) for key in ("search", "mcp", "plugins")):
        raise PolicyError("capability_unsupported", "External search, MCP and plugins are unavailable in this adapter")
    config_digest, config_sources = _audit_config(workspace, child_env)
    native = _resolve_executable(executable, child_env, cwd=workspace)
    rc, version_text = _probe([str(native), "--version"], cwd=workspace, env=child_env)
    version = re.fullmatch(r"codex-cli (\d+\.\d+\.\d+)\s*", version_text)
    if rc != 0 or not version or version.group(1) != TESTED_VERSION:
        raise PolicyError("version_unsupported", "Native CLI version is outside the tested allowlist")
    rc, help_text = _probe([str(native), "exec", "--help"], cwd=workspace, env=child_env)
    flags = ("--ignore-user-config", "--strict-config", "--ephemeral", "--json", "--output-schema")
    if rc != 0 or any(flag not in help_text for flag in flags):
        raise PolicyError("capability_unverified", "The native CLI does not advertise required launch controls")
    global_args = [arg for value in _OVERRIDES for arg in ("-c", value)]
    feature_args = [arg for feature in _DISABLED_FEATURES for arg in ("--disable", feature)]
    rc, feature_text = _probe([str(native), *global_args, *feature_args, "features", "list"],
                              cwd=workspace, env=child_env)
    features = {}
    for line in feature_text.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[-1] in {"true", "false"}:
            features[fields[0]] = fields[-1]
    if rc != 0 or any(features.get(feature) != "false" for feature in _DISABLED_FEATURES):
        raise PolicyError("capability_unverified", "Native feature inspection did not establish disabled extensions")
    rc, auth_text = _probe([str(native), *global_args, "login", "status"], cwd=workspace, env=child_env)
    auth = classify_auth_status(rc, auth_text)
    del auth_text
    if auth != "chatgpt_subscription":
        raise PolicyError("subscription_auth_required", "Native status did not prove ChatGPT subscription authentication")
    verified = {
        "subscription_auth": "Same sanitized child environment; exact native status classification; forced ChatGPT login",
        "native_provider": "Pinned native CLI; explicit openai provider and ChatGPT login; alternate routing variables removed",
        "structured_output": "Pinned native exec help advertises JSONL, schema and final-message output",
        "selected_context": "Host validates source bindings and bounds serialized context; not a read jail",
        "config_file_audit": "Conservative project/ancestor/system source audit and runtime overrides; user config skipped",
        "search_disabled": "Explicit web_search=disabled; search is separate from command network",
        "external_extensions_disabled": "External extensions rejected in config and native feature switches disabled",
    }
    unverified = {
        "owned_process_cleanup": "The process owner must verify lifecycle ownership at launch; not OS containment",
        "tool_network_deny": "No verified exec-level network-denial switch",
        "strict_no_tools": "No verified native no-tools lane",
        "file_read_jail": "Selected context and cwd do not restrict file reads",
        "generation_token_cap": "No verified native generation ceiling",
        "turn_cap": "One CLI launch does not bound internal turns",
        "managed_cloud_attestation": "Static file audit is not an attestation of cloud-managed account policy",
    }
    capabilities = {key: {"state": "verified", "evidence": value} for key, value in verified.items()}
    capabilities.update({key: {"state": "unverified", "evidence": value} for key, value in unverified.items()})
    # Process ownership is established by run_owned, rather than by a CLI help
    # query. Its mandatory launch gate is handled there, before child execution.
    for required in request.get("required_capabilities", []):
        if required == "owned_process_cleanup":
            continue
        if capabilities.get(required, {}).get("state") != "verified":
            raise PolicyError("capability_unverified", "A required capability is unsupported or unverified")
    with native.open("rb") as image:
        executable_hash = hashlib.file_digest(image, "sha256").hexdigest()
    return {
        "executable": str(native), "version": TESTED_VERSION, "auth": auth,
        "environment": child_env, "capabilities": capabilities,
        "config_digest": config_digest, "config_sources": config_sources,
        "executable_sha256": executable_hash,
        "argv_overrides": list(_OVERRIDES),
    }


def build_argv(report: dict, request: dict, *, schema_path: Path, final_path: Path) -> list[str]:
    """No free-form flags, profile, provider, fallback, resume, or full-access lane."""
    if report.get("auth") != "chatgpt_subscription" or report.get("version") != TESTED_VERSION:
        raise PolicyError("unqualified_launch", "A qualified native subscription preflight is required")
    try:
        with Path(report["executable"]).open("rb") as image:
            executable_hash = hashlib.file_digest(image, "sha256").hexdigest()
    except (OSError, KeyError, TypeError) as exc:
        raise PolicyError("native_binary_changed", "The qualified native executable is no longer available") from exc
    if executable_hash != report.get("executable_sha256"):
        raise PolicyError("native_binary_changed", "The native executable changed after preflight")
    sandbox = request["policy"]["sandbox"]
    if sandbox not in {"read-only", "workspace-write"}:
        raise PolicyError("sandbox_unsupported", "The requested sandbox is unsupported")
    if request["effort"] not in {"low", "medium", "high", "xhigh"}:
        raise PolicyError("effort_unsupported", "The requested effort is unsupported")
    # Never consume arbitrary argv_overrides supplied by an external report.
    args = [report["executable"], "--ask-for-approval", "never", "--sandbox", sandbox,
            "--model", request["model"], "--cd", request["workspace"]["path"]]
    for value in (*_OVERRIDES, f'model_reasoning_effort="{request["effort"]}"'):
        args.extend(("-c", value))
    for feature in _DISABLED_FEATURES:
        args.extend(("--disable", feature))
    return [*args, "exec", "--ignore-user-config", "--strict-config", "--ephemeral", "--json",
            "--color", "never", "--output-schema", str(schema_path), "-o", str(final_path), "-"]
