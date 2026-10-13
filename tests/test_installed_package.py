"""Exercise the real installed CLI outside the source tree with a local fake worker."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]


def checked(args, *, cwd, env=None):
    process = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8")
    assert process.returncode == 0, process.stdout + process.stderr
    return process.stdout


def test_wheel_resources_and_complete_task_outside_checkout(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(
        ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist", "*.egg-info",
    ))
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    # Build only in the scratch copy; no source-tree or network modifications.
    checked([sys.executable, "-c", f"from setuptools.build_meta import build_wheel; build_wheel({str(wheels)!r})"], cwd=source)
    wheel = next(wheels.glob("*.whl"))
    installed = tmp_path / "installed"
    checked([sys.executable, "-m", "pip", "install", "--no-deps", "--no-cache-dir", "--target", str(installed), str(wheel)], cwd=tmp_path)
    from zipfile import ZipFile

    with ZipFile(wheel) as archive:
        assert "codex_forge/schemas/review-result.schema.json" in archive.namelist()
        assert "codex_forge/schemas/task-result.schema.json" in archive.namelist()

    worker = tmp_path / "fake-codex.py"
    worker.write_text(
        "import json,pathlib,sys\n"
        "args=sys.argv[1:]\n"
        "schema=pathlib.Path(args[args.index('--output-schema')+1])\n"
        "assert schema.is_file()\n"
        "json.loads(schema.read_text(encoding='utf-8'))\n"
        "if schema.name.startswith('review'):\n"
        " result={'verdict':'pass','summary':'reviewed','acceptance_criteria':[],'binding_requirements':[],'findings':[]}\n"
        "else:\n"
        " pathlib.Path('output.txt').write_text('verified work\\n',encoding='utf-8')\n"
        " result={'status':'completed','summary':'done','changed_files':['output.txt'],'validation_performed':[],"
        "'assumptions':[],'blocking_reason':None,'decisions_needed':[],'reusable_patterns':[]}\n"
        "pathlib.Path(args[args.index('-o')+1]).write_text(json.dumps(result),encoding='utf-8')\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':2}}))\n",
        encoding="utf-8",
    )
    repo = tmp_path / "project"
    repo.mkdir()
    checked(["git", "init", "-b", "main"], cwd=repo)
    checked(["git", "config", "user.name", "Forge Package Test"], cwd=repo)
    checked(["git", "config", "user.email", "test@example.invalid"], cwd=repo)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    checked(["git", "add", "README.md"], cwd=repo)
    checked(["git", "commit", "-m", "base"], cwd=repo)
    config = tmp_path / "forge.toml"
    # JSON string literals are also valid TOML basic strings for these paths.
    config.write_text(
        "[paths]\ndata_root = " + json.dumps(str(tmp_path / "data")) +
        "\n[codex]\nexecutable = " + json.dumps(str(worker)) +
        '\nreview_sandbox = "danger-full-access"\n', encoding="utf-8",
    )
    entry = (
        f"import sys; sys.path.insert(0,{str(installed)!r}); "
        "import codex_forge; "
        f"assert str(codex_forge.__file__).startswith({str(installed)!r}); "
        "from codex_forge.cli import main; raise SystemExit(main())"
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    def cli(*args):
        return checked([sys.executable, "-I", "-c", entry, "--config", str(config), *args], cwd=tmp_path, env=environment)

    cli("init")
    cli("project", "add", "demo", "--repo", str(repo), "--default-branch", "main")
    cli("task", "add", "--project", "demo", "--id", "PACKAGE-1", "--title", "Package smoke", "--objective", "Write output.")
    cli("run", "--once")
    document = json.loads(cli("task", "show", "PACKAGE-1", "--json"))
    assert document["data"]["task"]["state"] == "COMPLETED"
    # The two independent worker calls both executed from packaged schema paths.
    logs = list((tmp_path / "data" / "evidence").rglob("*.events.jsonl"))
    assert len(logs) == 2
