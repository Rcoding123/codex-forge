import os
import json
from pathlib import Path
import sys

import pytest

from codex_forge.adapter import policy


@pytest.fixture
def policy_case(tmp_path, monkeypatch):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    (workspace / ".git").touch()
    home = tmp_path / "codex-home"
    home.mkdir()
    native = tmp_path / ("codex.exe" if os.name == "nt" else "codex")
    native.write_bytes((b"MZ" if os.name == "nt" else b"\x7fELF") + b"native-test-image")
    env = {"CODEX_HOME": str(home), "PATH": os.environ.get("PATH", "")}
    request = {
        "workspace": {"path": str(workspace)}, "role": "implementation",
        "policy": {"sandbox": "workspace-write", "tool_network": "unspecified",
                   "search": False, "mcp": False, "plugins": False},
        "model": "caller-model", "effort": "medium", "required_capabilities": [],
    }
    calls = []

    def probe(argv, *, cwd, env):
        calls.append((argv, cwd, env.copy()))
        if argv[-1] == "--version":
            return 0, "codex-cli 0.151.0\n"
        if argv[-2:] == ["exec", "--help"]:
            return 0, "--ignore-user-config --strict-config --ephemeral --json --output-schema danger-full-access"
        if argv[-2:] == ["features", "list"]:
            return 0, "\n".join(f"{feature} stable false" for feature in policy._DISABLED_FEATURES)
        if argv[-2:] == ["login", "status"]:
            return 0, "Logged in using ChatGPT\n"
        raise AssertionError("unexpected native probe")

    monkeypatch.setattr(policy, "_probe", probe)
    monkeypatch.setattr(policy, "_system_paths", lambda env: [])
    return request, str(native), env, calls


def test_sanitizes_child_only_preserves_home_and_policy_variables():
    parent = {
        "CODEX_HOME": "auth-home", "OPENAI_API_KEY": "secret-1", "Anthropic_Auth_Token": "secret-2",
        "OPENAI_BASE_URL": "https://custom", "CODEX_MODEL_PROVIDER": "other",
        "CODEX_PROVIDER_FALLBACK": "other", "AWS_ACCESS_KEY_ID": "secret-3",
        "AZURE_TENANT_ID": "routing", "CUSTOM_API_URL": "custom", "HTTPS_PROXY": "proxy",
        "OTHER_ACCESS_TOKEN": "secret-4", "CODEX_MANAGED_CONFIG_PATH": "required.toml",
        "HF_TOKEN": "secret-5", "CUSTOM_AUTHORIZATION": "secret-6", "CUSTOM_API_BASE": "router",
        "CODEX_SANDBOX_NETWORK_DISABLED": "1", "PATH": "tools",
    }
    original = parent.copy()
    child = policy.sanitized_child_environment(parent)
    assert child == {
        "CODEX_HOME": "auth-home", "CODEX_MANAGED_CONFIG_PATH": "required.toml",
        "CODEX_SANDBOX_NETWORK_DISABLED": "1", "PATH": "tools",
    }
    assert parent == original


@pytest.mark.parametrize(("rc", "raw", "expected"), [
    (0, "Logged in using ChatGPT\n", "chatgpt_subscription"),
    (0, "Logged in using an API key: sk-redacted", "api_key"),
    (0, "Logged in using access token", "access_token"),
    (1, "Logged in using ChatGPT", "unknown"),
    (0, "Warning: different auth\nLogged in using ChatGPT", "unknown"),
    (0, '{"authMethod":"claude.ai"}', "unknown"),
    (0, "Not logged in", "unknown"),
])
def test_auth_requires_exact_native_contract(rc, raw, expected):
    assert policy.classify_auth_status(rc, raw) == expected


def test_qualified_native_preflight_same_child_without_auth_retention(policy_case):
    request, native, env, calls = policy_case
    env["OPENAI_API_KEY"] = "do-not-log"
    result = policy.inspect_policy(request, executable=native, env=env)
    assert result["auth"] == "chatgpt_subscription"
    assert result["version"] == "0.151.0"
    assert "OPENAI_API_KEY" not in result["environment"]
    assert result["environment"]["CODEX_HOME"] == env["CODEX_HOME"]
    assert "OPENAI_API_KEY" in env
    assert all(call[2] == result["environment"] for call in calls)
    assert not any("exec" in call[0] and "--help" not in call[0] for call in calls)
    assert "Logged in" not in str(result)
    assert "do-not-log" not in str(result)
    assert result["capabilities"]["tool_network_deny"]["state"] == "unverified"
    assert result["capabilities"]["managed_cloud_attestation"]["state"] == "unverified"


@pytest.mark.parametrize("bad_status", ["Logged in using API key sk-secret", "Logged in using access token", ""])
def test_api_and_unknown_status_refuse_without_leaking(policy_case, monkeypatch, bad_status):
    request, native, env, calls = policy_case
    original = policy._probe

    def probe(argv, **kwargs):
        return (0, bad_status) if argv[-2:] == ["login", "status"] else original(argv, **kwargs)

    monkeypatch.setattr(policy, "_probe", probe)
    with pytest.raises(policy.PolicyError) as caught:
        policy.inspect_policy(request, executable=native, env=env)
    assert caught.value.code == "subscription_auth_required"
    assert "sk-secret" not in str(caught.value)


@pytest.mark.parametrize("required", ["strict_no_tools", "turn_cap", "file_read_jail", "unknown", "managed_cloud_attestation"])
def test_unverified_required_capability_is_refused(policy_case, required):
    request, native, env, calls = policy_case
    request["required_capabilities"] = [required]
    with pytest.raises(policy.PolicyError, match="required capability"):
        policy.inspect_policy(request, executable=native, env=env)


def test_owned_process_is_deferred_to_mandatory_process_gate(policy_case):
    request, native, env, calls = policy_case
    request["required_capabilities"] = ["owned_process_cleanup", "subscription_auth"]
    result = policy.inspect_policy(request, executable=native, env=env)
    assert result["capabilities"]["owned_process_cleanup"]["state"] == "unverified"


@pytest.mark.parametrize("policy_change", [
    {"sandbox": "unknown-sandbox"}, {"tool_network": "deny"},
    {"mcp": True}, {"search": True}, {"plugins": True},
])
def test_unsupported_policy_never_launches_any_probe(policy_case, policy_change):
    request, native, env, calls = policy_case
    request["policy"].update(policy_change)
    with pytest.raises(policy.PolicyError):
        policy.inspect_policy(request, executable=native, env=env)
    assert calls == []


@pytest.mark.parametrize("name", ["codex.cmd", "codex.py", "proxy.exe"])
def test_native_resolution_refuses_shims(policy_case, name):
    request, native, env, calls = policy_case
    shim = Path(native).with_name(name)
    shim.write_text("placeholder")
    with pytest.raises(policy.PolicyError) as caught:
        policy.inspect_policy(request, executable=str(shim), env=env)
    assert caught.value.code == "native_executable_required"
    assert calls == []


def test_native_name_does_not_make_a_script_native(policy_case):
    request, native, env, calls = policy_case
    Path(native).write_text("#!/usr/bin/env python\nprint('not native')")
    with pytest.raises(policy.PolicyError) as caught:
        policy.inspect_policy(request, executable=native, env=env)
    assert caught.value.code == "native_executable_required"
    assert calls == []


def test_unpinned_version_never_checks_auth(policy_case, monkeypatch):
    request, native, env, calls = policy_case
    original = policy._probe

    def probe(argv, **kwargs):
        return (0, "codex-cli 0.152.0") if argv[-1] == "--version" else original(argv, **kwargs)

    monkeypatch.setattr(policy, "_probe", probe)
    with pytest.raises(policy.PolicyError) as caught:
        policy.inspect_policy(request, executable=native, env=env)
    assert caught.value.code == "version_unsupported"
    assert not any(call[0][-2:] == ["login", "status"] for call in calls)


def test_invalid_config_is_rejected_before_probe(policy_case):
    request, native, env, calls = policy_case
    config_root = Path(request["workspace"]["path"]) / ".codex"
    config_root.mkdir()
    (config_root / "config.toml").write_text("[invalid")
    with pytest.raises(policy.PolicyError) as caught:
        policy.inspect_policy(request, executable=native, env=env)
    assert caught.value.code == "config_invalid"
    assert calls == []


def test_plain_application_config_is_not_a_codex_policy_layer(policy_case):
    request, native, env, calls = policy_case
    workspace = Path(request["workspace"]["path"])
    (workspace / "config.toml").write_text('[database]\nname="application"')
    report = policy.inspect_policy(request, executable=native, env=env)
    assert report["auth"] == "chatgpt_subscription"
    assert not any(source["path"] == str(workspace / "config.toml") for source in report["config_sources"])


@pytest.mark.parametrize("config", [
    'model_provider="external"', 'chatgpt_base_url="https://proxy"',
    '[mcp_servers.external]\ncommand="anything"', '[features]\nhooks=true',
    '[plugins.external]\nenabled=true', '[profiles.alternate]\nmodel_provider="other"',
    'config_server="https://cloud"', '[permissions]\ndefault="full"',
])
def test_project_config_escape_rejected_before_probe(policy_case, config):
    request, native, env, calls = policy_case
    config_root = Path(request["workspace"]["path"]) / ".codex"
    config_root.mkdir()
    (config_root / "config.toml").write_text(config)
    with pytest.raises(policy.PolicyError) as caught:
        policy.inspect_policy(request, executable=native, env=env)
    assert caught.value.code == "config_extension"
    assert calls == []


def test_ancestor_layer_cannot_hide_outside_git_root(policy_case):
    request, native, env, calls = policy_case
    workspace = Path(request["workspace"]["path"])
    (workspace / ".git").unlink()
    (workspace.parent / ".git").touch()
    config_root = workspace.parent / ".codex"
    config_root.mkdir()
    (config_root / "config.toml").write_text('[mcp_servers.external]\ncommand="external"')
    with pytest.raises(policy.PolicyError):
        policy.inspect_policy(request, executable=native, env=env)
    assert calls == []


def test_system_managed_source_rejected_and_not_bypassed(policy_case, monkeypatch):
    request, native, env, calls = policy_case
    managed = Path(env["CODEX_HOME"]).parent / "requirements.toml"
    managed.write_text('model_provider="external"')
    monkeypatch.setattr(policy, "_system_paths", lambda env: [managed])
    with pytest.raises(policy.PolicyError):
        policy.inspect_policy(request, executable=native, env=env)
    assert calls == []


def test_environment_selected_managed_source_is_refused_not_erased(policy_case):
    request, native, env, calls = policy_case
    env["CODEX_MANAGED_CONFIG_PATH"] = "required.toml"
    with pytest.raises(policy.PolicyError) as caught:
        policy.inspect_policy(request, executable=native, env=env)
    assert caught.value.code == "config_source_unverified"
    assert calls == []


def test_user_config_is_skipped_but_hooks_are_not_assumed_skipped(policy_case):
    request, native, env, calls = policy_case
    home = Path(env["CODEX_HOME"])
    (home / "config.toml").write_text('[mcp_servers.user]\ncommand="anything"')
    result = policy.inspect_policy(request, executable=native, env=env)
    argv = policy.build_argv(result, request, schema_path=home / "schema", final_path=home / "final")
    assert "--ignore-user-config" in argv
    calls.clear()
    (home / "hooks.json").write_text('{"SessionStart":[{"command":"anything"}]}')
    with pytest.raises(policy.PolicyError) as caught:
        policy.inspect_policy(request, executable=native, env=env)
    assert caught.value.code == "config_extension"
    assert calls == []


def test_closed_argv_preserves_global_options_and_never_uses_extra_args(policy_case):
    request, native, env, calls = policy_case
    report = policy.inspect_policy(request, executable=native, env=env)
    report["argv_overrides"] = ["model_provider=external"]
    request["extra_args"] = ["--oss"]
    root = Path(env["CODEX_HOME"])
    argv = policy.build_argv(report, request, schema_path=root / "schema", final_path=root / "final")
    assert argv.index("--ask-for-approval") < argv.index("exec")
    assert argv[argv.index("--ask-for-approval") + 1] == "never"
    assert '--oss' not in argv
    assert "--ignore-rules" not in argv
    assert '--dangerously-bypass-approvals-and-sandbox' not in argv
    assert 'model_provider="openai"' in argv
    assert 'forced_login_method="chatgpt"' in argv
    assert 'web_search="disabled"' in argv
    assert "model_provider=external" not in argv
    assert argv[-1] == "-"


def test_full_access_requires_matching_subscription_preflight(policy_case):
    request, native, env, _ = policy_case
    root = Path(env["CODEX_HOME"])
    args = {"schema_path": root / "schema", "final_path": root / "final"}
    report = policy.inspect_policy(request, executable=native, env=env)
    request["policy"]["sandbox"] = "danger-full-access"
    with pytest.raises(policy.PolicyError, match="matching preflight"):
        policy.build_argv(report, request, **args)
    report = policy.inspect_policy(request, executable=native, env=env)
    argv = policy.build_argv(report, request, **args)
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
    assert argv[argv.index("--ask-for-approval") + 1] == "never"
    assert 'forced_login_method="chatgpt"' in argv


@pytest.mark.parametrize("changed", ["version", "auth"])
def test_build_requires_qualified_preflight(policy_case, changed):
    request, native, env, calls = policy_case
    report = policy.inspect_policy(request, executable=native, env=env)
    report[changed] = "other"
    with pytest.raises(policy.PolicyError):
        policy.build_argv(report, request, schema_path=Path("schema"), final_path=Path("final"))


def test_missing_feature_switch_is_not_reported_as_disabled(policy_case, monkeypatch):
    request, native, env, calls = policy_case
    original = policy._probe

    def probe(argv, **kwargs):
        return (0, "plugins stable true") if argv[-2:] == ["features", "list"] else original(argv, **kwargs)

    monkeypatch.setattr(policy, "_probe", probe)
    with pytest.raises(policy.PolicyError):
        policy.inspect_policy(request, executable=native, env=env)
    assert not any(call[0][-2:] == ["login", "status"] for call in calls)


def test_native_probe_has_output_and_time_bounds(tmp_path, monkeypatch):
    # Use a real harmless child; no Codex execution or inference.
    monkeypatch.setattr(policy, "PROBE_MAX_BYTES", 1024)
    with pytest.raises(policy.PolicyError) as caught:
        policy._probe([sys.executable, "-c", "print('x'*100000)"], cwd=tmp_path, env=dict(os.environ))
    assert caught.value.code == "probe_bounded_failure"
    monkeypatch.setattr(policy, "PROBE_TIMEOUT_S", 0.1)
    with pytest.raises(policy.PolicyError) as caught:
        policy._probe([sys.executable, "-c", "import time; time.sleep(10)"], cwd=tmp_path, env=dict(os.environ))
    assert caught.value.code == "probe_bounded_failure"


def _linux_npm_fixture(tmp_path, monkeypatch, *, optional=True):
    monkeypatch.setattr(policy.platform, "machine", lambda: "x86_64")
    root = tmp_path / "lib" / "node_modules" / "@openai" / "codex"
    launcher = root / "bin" / "codex.js"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/usr/bin/env node\nthrow new Error('wrapper must never run');\n")
    launcher.chmod(0o755)
    (root / "package.json").write_text(json.dumps({"name": "@openai/codex", "version": "0.151.0",
                                                  "bin": {"codex": "bin/codex.js"}}))
    vendor = root / "vendor"
    platform_root = root.parent / "codex-linux-x64"
    if optional:
        platform_root.mkdir()
        (platform_root / "package.json").write_text(json.dumps({
            "name": "@openai/codex", "version": "0.151.0-linux-x64", "os": ["linux"], "cpu": ["x64"],
        }))
        vendor = platform_root / "vendor"
    native = vendor / "x86_64-unknown-linux-musl" / "bin" / "codex"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"\x7fELFfixture-only-not-executable-code")
    native.chmod(0o755)
    return launcher, native, platform_root


@pytest.mark.parametrize("optional", [False, True], ids=["bundled-vendor", "optional-platform-package"])
def test_linux_npm_layout_resolves_native_without_running_launcher(tmp_path, monkeypatch, optional):
    launcher, native, _ = _linux_npm_fixture(tmp_path, monkeypatch, optional=optional)
    assert policy._linux_npm_native(launcher) == native.resolve()


def test_linux_npm_wrong_platform_metadata_fails_closed(tmp_path, monkeypatch):
    launcher, _, platform_root = _linux_npm_fixture(tmp_path, monkeypatch)
    (platform_root / "package.json").write_text(json.dumps({
        "name": "@openai/codex", "version": "0.151.0-linux-arm64", "os": ["linux"], "cpu": ["arm64"],
    }))
    with pytest.raises(policy.PolicyError) as caught:
        policy._linux_npm_native(launcher)
    assert caught.value.code == "native_package_unverified"


def test_linux_npm_unrelated_wrapper_cannot_redirect_to_a_native_payload(tmp_path, monkeypatch):
    launcher, _, _ = _linux_npm_fixture(tmp_path, monkeypatch)
    (launcher.parent.parent / "package.json").write_text(json.dumps({
        "name": "unrelated-launcher", "version": "0.151.0", "bin": {"codex": "bin/codex.js"},
    }))
    with pytest.raises(policy.PolicyError) as caught:
        policy._linux_npm_native(launcher)
    assert caught.value.code == "native_package_unverified"


def test_linux_npm_missing_native_payload_is_refused(tmp_path, monkeypatch):
    launcher, native, _ = _linux_npm_fixture(tmp_path, monkeypatch)
    native.unlink()
    with pytest.raises(policy.PolicyError) as caught:
        policy._linux_npm_native(launcher)
    assert caught.value.code == "native_executable_required"


@pytest.mark.skipif(os.name == "nt", reason="Unix executable permission bits")
def test_linux_npm_nonexecutable_payload_is_refused(tmp_path, monkeypatch):
    launcher, native, _ = _linux_npm_fixture(tmp_path, monkeypatch)
    native.chmod(0o644)
    with pytest.raises(policy.PolicyError) as caught:
        policy._linux_npm_native(launcher)
    assert caught.value.code == "native_executable_required"


def test_linux_npm_payload_cannot_escape_resolved_package(tmp_path, monkeypatch):
    launcher, native, _ = _linux_npm_fixture(tmp_path, monkeypatch)
    outside = tmp_path / "outside" / "codex"
    outside.parent.mkdir()
    outside.write_bytes(native.read_bytes())
    outside.chmod(0o755)
    native.unlink()
    try:
        native.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(policy.PolicyError) as caught:
        policy._linux_npm_native(launcher)
    assert caught.value.code == "native_package_escape"


def test_launch_boundary_rechecks_qualified_native_bytes(policy_case):
    request, native, env, _calls = policy_case
    report = policy.inspect_policy(request, executable=native, env=env)
    Path(native).write_bytes(b"changed after qualified preflight")
    with pytest.raises(policy.PolicyError) as caught:
        policy.build_argv(report, request, schema_path=Path("schema"), final_path=Path("final"))
    assert caught.value.code == "native_binary_changed"


@pytest.mark.skipif(sys.platform != "linux", reason="Native Linux PATH and ELF selection")
def test_linux_default_npm_path_discovery_preserves_native_auth_and_policy_checks(tmp_path, monkeypatch):
    launcher, native, _ = _linux_npm_fixture(tmp_path, monkeypatch)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "codex").symlink_to(launcher)
    env = {"HOME": str(tmp_path / "home"), "PATH": str(bin_dir), "LOCALAPPDATA": str(tmp_path / "windows")}
    assert policy._resolve_executable(None, env) == native.resolve()
    calls = []

    def probe(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        if argv[-1] == "--version":
            return 0, "codex-cli 0.151.0\n"
        if argv[-2:] == ["exec", "--help"]:
            return 0, "--ignore-user-config --strict-config --ephemeral --json --output-schema"
        if argv[-2:] == ["features", "list"]:
            return 0, "\n".join(f"{feature} stable false" for feature in policy._DISABLED_FEATURES)
        if argv[-2:] == ["login", "status"]:
            return 0, "Logged in using API key sk-fixture-only"
        raise AssertionError("No inference or wrapper may execute")

    monkeypatch.setattr(policy, "_probe", probe)
    monkeypatch.setattr(policy, "_system_paths", lambda _env: [])
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    (workspace / ".git").touch()
    request = {"workspace": {"path": str(workspace)}, "role": "implementation",
               "policy": {"sandbox": "workspace-write", "tool_network": "unspecified",
                          "search": False, "mcp": False, "plugins": False}}
    with pytest.raises(policy.PolicyError) as caught:
        policy.inspect_policy(request, env=env)
    assert caught.value.code == "subscription_auth_required"
    assert all(argv[0] == str(native.resolve()) for argv, _env in calls)
    assert all(not ("exec" in argv and "--help" not in argv) for argv, _env in calls)


@pytest.mark.skipif(os.name == "nt", reason="Unix child HOME config lookup")
def test_unix_config_audit_uses_child_home_not_adapter_home(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").touch()
    child_home = tmp_path / "child-home"
    hooks = child_home / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text('{"unsafe":"must be rejected"}')
    monkeypatch.setattr(policy, "_system_paths", lambda _env: [])
    with pytest.raises(policy.PolicyError) as caught:
        policy._audit_config(workspace, {"HOME": str(child_home)})
    assert caught.value.code == "config_extension"


def test_relative_codex_home_is_rejected_before_auth_probe(policy_case):
    request, native, env, calls = policy_case
    env["CODEX_HOME"] = "relative-home"
    with pytest.raises(policy.PolicyError) as caught:
        policy.inspect_policy(request, executable=native, env=env)
    assert caught.value.code == "config_home_unverified"
    assert calls == []


@pytest.mark.skipif(sys.platform != "linux", reason="Native Linux executable PATH semantics")
def test_linux_executable_search_binds_relative_path_to_caller_workspace(tmp_path, monkeypatch):
    caller, adapter = tmp_path / "caller" / "worktree", tmp_path / "adapter" / "worktree"
    for directory in (caller, adapter):
        directory.mkdir(parents=True)
        (directory.parent / "tools").mkdir()
        executable = directory.parent / "tools" / "codex"
        executable.write_bytes(b"\x7fELFfixture-only")
        executable.chmod(0o755)
    monkeypatch.chdir(adapter)
    expected = caller.parent / "tools" / "codex"
    assert policy._resolve_executable(None, {"PATH": "../tools"}, cwd=caller) == expected
    assert policy._resolve_executable("../tools/codex", {"PATH": ""}, cwd=caller) == expected


def test_missing_child_path_does_not_search_ambient_adapter_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(policy.PolicyError) as caught:
        policy._resolve_executable(None, {})
    assert caught.value.code == "executable_missing"


def test_editable_workspace_native_cli_is_rejected_before_any_probe(policy_case):
    request, native, env, calls = policy_case
    workspace = Path(request["workspace"]["path"])
    local = workspace / Path(native).name
    local.write_bytes(Path(native).read_bytes())
    local.chmod(0o755)
    with pytest.raises(policy.PolicyError) as caught:
        policy.inspect_policy(request, executable=str(local), env=env)
    assert caught.value.code == "executable_in_workspace"
    assert calls == []


@pytest.mark.skipif(sys.platform != "linux", reason="Linux worktree PATH and npm launcher rejection")
@pytest.mark.parametrize("form", ["native-relative-path", "empty-path-component", "npm-launcher"])
def test_linux_editable_path_cannot_supply_native_auth_probe(tmp_path, monkeypatch, form):
    launcher, native, _ = _linux_npm_fixture(tmp_path, monkeypatch)
    if form == "npm-launcher":
        workspace = launcher.parent.parent
        path_dir = workspace / "tools"
        path_dir.mkdir()
        (path_dir / "codex").symlink_to(launcher)
        child_path = "tools"
    else:
        workspace = tmp_path / "worktree"
        workspace.mkdir()
        directory = workspace / "tools" if form == "native-relative-path" else workspace
        directory.mkdir(exist_ok=True)
        local = directory / "codex"
        local.write_bytes(native.read_bytes())
        local.chmod(0o755)
        child_path = "tools" if form == "native-relative-path" else os.pathsep
    (workspace / ".git").touch()
    request = {"workspace": {"path": str(workspace)}, "role": "implementation",
               "policy": {"sandbox": "workspace-write", "tool_network": "unspecified",
                          "search": False, "mcp": False, "plugins": False}}
    monkeypatch.setattr(policy, "_system_paths", lambda _env: [])
    probes = []
    monkeypatch.setattr(policy, "_probe", lambda *args, **kwargs: probes.append(args))
    with pytest.raises(policy.PolicyError) as caught:
        policy.inspect_policy(request, env={"HOME": str(tmp_path / "home"), "PATH": child_path})
    assert caught.value.code == "executable_in_workspace"
    assert probes == []
