from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_installs_python_build_backend_before_editable_package():
    script = (REPO_ROOT / "bootstrap.ps1").read_text(encoding="utf-8")
    build_tools = script.index("pip install --upgrade pip setuptools wheel")
    editable_install = script.index('pip install -e "${RepoRoot}[dev]"')
    assert build_tools < editable_install
    assert "Failed to install Codex Forge and its development dependencies." in script
    assert 'escalation = "gpt-5.6-sol"' in script
    assert 'validation = "gpt-5.6-luna"' not in script
    assert "max_identical_failures = 2" in script
    assert "max_task_tokens = 2000000" in script
    assert "disabled_features = [" in script
    assert "schtasks.exe /Run /TN CodexForge" not in script


def test_scheduled_task_installer_handles_missing_pwsh_without_null_property_access():
    script = (REPO_ROOT / "install-service.ps1").read_text(encoding="utf-8")
    assert "$pwshCommand = Get-Command pwsh -ErrorAction SilentlyContinue" in script
    assert "if ($pwshCommand)" in script
    assert "(Get-Command pwsh -ErrorAction SilentlyContinue).Source" not in script
    assert "-LogonType S4U" in script
    assert "-AtStartup" in script
    assert "-RestartCount 3" in script
    assert "-MultipleInstances IgnoreNew" in script
    assert "-WorkingDirectory $RepoRoot" in script
    assert "Start-ScheduledTask -TaskName $TaskName" in script


def test_daemon_launcher_is_unbuffered_and_propagates_exit_code():
    script = (REPO_ROOT / "run-daemon.ps1").read_text(encoding="utf-8")
    assert "$Python -u -m codex_forge" in script
    assert "exit $LASTEXITCODE" in script


def test_restart_service_targets_only_verified_forge_daemon_processes():
    script = (REPO_ROOT / "restart-service.ps1").read_text(encoding="utf-8")
    assert "[regex]::Escape($Config)" in script
    assert "-m\\s+codex_forge" in script
    assert "\\srun\\s+--daemon" in script
    assert "Get-DescendantProcessIds" in script
    assert "Stop-Process -Id $processId -Force" in script
    assert "Start-ScheduledTask -TaskName $TaskName" in script
    assert "$daemons = @(Get-ForgeDaemonProcesses)" in script
    assert "@(Get-ForgeDaemonProcesses).Count" in script


def test_codex_configuration_disables_global_multi_agent_by_default():
    script = (REPO_ROOT / "scripts" / "configure-codex.ps1").read_text(encoding="utf-8")
    assert "multi_agent = false" in script
    assert "multi_agent = true" not in script


def test_bootstrap_recreates_validated_rtk_policy_after_rtk_init():
    bootstrap = (REPO_ROOT / "bootstrap.ps1").read_text(encoding="utf-8")
    policy_call = '& (Join-Path $RepoRoot "scripts\\configure-rtk-policy.ps1")'
    assert bootstrap.index("rtk init -g --codex", bootstrap.index("configure-codex.ps1")) < bootstrap.index(
        policy_call
    )
    policy = (REPO_ROOT / "scripts" / "configure-rtk-policy.ps1").read_text(encoding="utf-8")
    assert "rtk proxy pytest ..." in policy
    assert "direct read-only command" in policy
    assert "RTK 0.44.2" in policy
