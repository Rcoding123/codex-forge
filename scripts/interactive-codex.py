"""Launch the native interactive interface through the Linux adapter."""

import argparse
import os
import sys
from pathlib import Path
import json
import shlex


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", required=True, type=Path)
    parser.add_argument("--codex-home", required=True, type=Path)
    args = parser.parse_args()
    cwd = Path.cwd()
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("Open the interactive adapter in a terminal.")
    from codex_forge.adapter import policy
    spec = {"workspace": {"path": str(cwd)}, "policy": {"sandbox": "danger-full-access"},
            "model": "gpt-5.6-sol", "effort": "medium"}
    parent = dict(os.environ, CODEX_HOME=str(args.codex_home.resolve(strict=True)))
    report = policy.inspect_policy(spec, executable=str(args.native.resolve(strict=True)), env=parent)
    env = report["environment"]
    exe = report["executable"]
    argv = [exe, "--ask-for-approval", "never", "--sandbox", "danger-full-access",
            "--model", spec["model"], "--strict-config"]
    for override in [*report["argv_overrides"], 'model_reasoning_effort="medium"']:
        argv.extend(["-c", override])
    for feature in policy._DISABLED_FEATURES:
        argv.extend(["--disable", feature])
    guidance = (
        "For coding tasks, locate named symbols first with git grep -n or grep -n. "
        "Read just the surrounding lines needed for the edit, using Read offset/limit or sed -n. "
        "Batch independent lookups, reuse unchanged file reads and show only relevant test failures. "
        "Expand context when necessary for correctness. Keep the final handoff concise."
    )
    if (cwd / "CMakeLists.txt").is_file():
        guidance += (" Add permanent regression cases for new public API behavior. "
                     "For a standard CMake project with same-named build and CTest targets, "
                     "run this helper followed by the relevant target names. It configures, "
                     "incrementally builds and actually runs each selected test, returning a short result. "
                     "Reuse it on follow-ups. Use normal project commands for custom builds. Helper: "
                     + shlex.quote(str(Path(__file__).resolve().with_name("adapter-cmake-check"))))
    argv.extend(["-c", "developer_instructions=" + json.dumps(guidance), "-c", "tool_output_token_limit=2000"])
    os.execve(exe, argv, env)


if __name__ == "__main__":
    main()
