"""The Windows `.cmd` shim must deliver every argv element to the child exactly as written.

Gate 7 failed with `unexpected argument 'with' found` and zero telemetry because the command line
was quoted twice. `_build_process_command` flattened the vector with `subprocess.list2cmdline` and
then handed the flattened line to `subprocess`, which runs `list2cmdline` over whatever it is given.
The second pass rewrote the first pass's real quotes as literal `\\"`, which `cmd.exe` does not undo,
so the shim's child read them as ordinary characters and split

    Reply with exactly CANARY_OK and nothing else.

into `"Reply`, `with`, `exactly`, ... — seven argv elements where the vector had one.

**No test in this file makes a model request.** The shim tests run a recorder that prints its own
argv and exits; the single test that runs the real `codex` binary appends `--help`, which the
argument parser answers before any configuration is loaded or any model is contacted.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import otel_codex_canary as codex_canary
from codex_forge.runner import _build_process_command

windows_only = pytest.mark.skipif(os.name != "nt", reason="the cmd.exe shim only exists on Windows")

# Every shape the canary vector and `CodexRunner` actually carry through the shim. Each one is a
# separate way the previous construction lost an argument boundary.
AWKWARD_ARGUMENTS: tuple[tuple[str, str], ...] = (
    ("spaces", codex_canary.CANARY_PROMPT),
    ("embedded_equals", "model_reasoning_effort=high"),
    ("toml_double_quotes", 'otel.exporter="none"'),
    ("toml_braces", '{ "forge.invocation_id" = "0123456789abcdef" }'),
    (
        "toml_inline_table_override",
        'otel.span_attributes={ "forge.invocation_id" = "0123456789abcdef", '
        '"forge.contract_version" = "1.0.0" }',
    ),
    ("empty_string", ""),
    ("path_with_a_space", r"C:\Program Files\codex-forge\schemas\task.json"),
)


def _recording_shim(directory: Path) -> Path:
    """A `.cmd` shim shaped like npm's: `%*` forwarded to a program that prints its own argv.

    npm's `codex.cmd` forwards to `node`, whose argument parsing is the Windows C runtime's. Python's
    is the same runtime and the same rules, so a recorder written in Python observes exactly what
    Codex observes on the far side of the shim.
    """
    directory.mkdir(parents=True, exist_ok=True)
    recorder = directory / "record_argv.py"
    recorder.write_text(
        "import json, os, sys\n"
        "with open(os.environ['FORGE_RECORDED_ARGV'], 'w', encoding='utf-8') as handle:\n"
        "    json.dump(sys.argv[1:], handle)\n",
        encoding="utf-8",
    )
    shim = directory / "codex.cmd"
    shim.write_text(f'@echo off\r\n"{sys.executable}" "{recorder}" %*\r\n', encoding="utf-8")
    return shim


def _argv_through_shim(tmp_path: Path, arguments: list[str], *, directory: str = "shim") -> list[str]:
    """Run `arguments` through the real builder and the real shim; return the child's argv."""
    shim = _recording_shim(tmp_path / directory)
    recorded = tmp_path / "recorded-argv.json"
    command = _build_process_command(str(shim), arguments)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env={**os.environ, "FORGE_RECORDED_ARGV": str(recorded)},
    )
    assert result.returncode == 0, f"the shim failed: {result.stderr or result.stdout}"
    return json.loads(recorded.read_text(encoding="utf-8"))


@windows_only
def test_the_canary_prompt_reaches_the_child_as_exactly_one_argument(tmp_path: Path):
    """The Gate 7 failure, reproduced end to end: this is the assertion that was false."""
    arguments = ["exec", "--json", codex_canary.CANARY_PROMPT]

    recorded = _argv_through_shim(tmp_path, arguments)

    assert recorded == arguments
    assert recorded[-1] == "Reply with exactly CANARY_OK and nothing else."
    assert "with" not in recorded[:-1], "the prompt was split at its spaces"


@windows_only
@pytest.mark.parametrize(
    "value",
    [value for _, value in AWKWARD_ARGUMENTS],
    ids=[name for name, _ in AWKWARD_ARGUMENTS],
)
def test_an_awkward_argument_survives_the_shim_unchanged(tmp_path: Path, value: str):
    """Spaces, `=`, TOML braces, TOML double quotes, and the empty string, one shape at a time."""
    arguments = ["exec", "-c", value, "-"]

    assert _argv_through_shim(tmp_path, arguments) == arguments


@windows_only
def test_the_whole_canonical_vector_survives_the_shim_unchanged(tmp_path: Path):
    """The canary's own vector, minus the executable, through the builder the canary calls."""
    canonical = list(codex_canary.placeholder_argv())

    assert _argv_through_shim(tmp_path, canonical[1:]) == canonical[1:]


@windows_only
def test_a_space_in_the_shim_path_does_not_mangle_the_vector(tmp_path: Path):
    """`cmd /s` strips the first and last quote of a command line that begins with one.

    A shim path needing quotes therefore used to cost the vector its final quote as well. The
    builder's leading `call` means the line no longer begins with a quote.
    """
    arguments = ["exec", codex_canary.CANARY_PROMPT]

    assert _argv_through_shim(tmp_path, arguments, directory="Program Files") == arguments


def test_the_builder_hands_subprocess_separate_elements_rather_than_a_command_line(monkeypatch):
    """The root cause, asserted directly: nothing is flattened before `subprocess` sees it.

    Runs everywhere, because it is the construction that is being checked and not `cmd.exe`.
    """
    monkeypatch.setattr("codex_forge.runner.os.name", "nt")
    monkeypatch.setattr("codex_forge.runner.shutil.which", lambda _: r"C:\Tools With Space\codex.cmd")
    monkeypatch.setenv("ComSpec", r"C:\Windows\System32\cmd.exe")

    command = _build_process_command("codex", ["exec", codex_canary.CANARY_PROMPT])

    assert command == [
        r"C:\Windows\System32\cmd.exe",
        "/d",
        "/s",
        "/c",
        "call",
        r"C:\Tools With Space\codex.cmd",
        "exec",
        codex_canary.CANARY_PROMPT,
    ]
    assert not any('\\"' in item for item in command), "an element was pre-escaped and will escape twice"
    # `subprocess` quotes this list once, and once is what `cmd.exe` expects.
    assert subprocess.list2cmdline(command).endswith(
        '/d /s /c call "C:\\Tools With Space\\codex.cmd" exec '
        '"Reply with exactly CANARY_OK and nothing else."'
    )


@windows_only
@pytest.mark.skipif(shutil.which("codex") is None, reason="codex is not on PATH")
def test_codex_parses_the_canonical_vector_without_splitting_the_prompt(tmp_path: Path):
    """Parser only. `--help` terminates inside the argument parser: no model, no config, no request.

    With the prompt split, Codex reached `with` as a second positional and refused the invocation
    before `--help` was ever parsed. Reaching the help text is the proof that it no longer does.
    """
    canonical = list(codex_canary.placeholder_argv())
    command = _build_process_command(canonical[0], canonical[1:] + ["--help"])

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(tmp_path),
    )
    output = result.stdout + result.stderr

    assert "unexpected argument" not in output, output[:2000]
    assert result.returncode == 0, output[:2000]
    assert "Usage:" in output, output[:2000]
