from __future__ import annotations

import shutil
from pathlib import Path

from .models import Project, Task
from .utils import ensure_dir, run_process, slugify


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str, timeout: int = 300, check: bool = True):
    result = run_process(["git", *args], cwd=repo, timeout=timeout)
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed:\n{result.stderr or result.stdout}")
    return result


def verify_repository(repo: Path) -> None:
    if not repo.exists():
        raise GitError(f"Repository does not exist: {repo}")
    result = _git(repo, "rev-parse", "--is-inside-work-tree", check=False)
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise GitError(f"Not a Git working tree: {repo}")


def clone_repository(url: str, destination: Path) -> None:
    ensure_dir(destination.parent)
    result = run_process(["git", "clone", url, str(destination)], timeout=1800)
    if result.returncode != 0:
        raise GitError(result.stderr or result.stdout)


def prepare_worktree(project: Project, task: Task, workspace_root: Path) -> tuple[str, Path]:
    verify_repository(project.repo_path)
    if project.auto_fetch:
        _git(project.repo_path, "fetch", "--all", "--prune", timeout=900, check=False)

    branch = task.branch or f"forge/{slugify(task.id)}-{slugify(task.title, 36)}"
    worktree = task.worktree_path or (workspace_root / project.name / slugify(task.id))
    ensure_dir(worktree.parent)

    if worktree.exists() and (worktree / ".git").exists():
        return branch, worktree
    if worktree.exists():
        shutil.rmtree(worktree)

    remote_ref = f"refs/remotes/origin/{project.default_branch}"
    remote_check = _git(project.repo_path, "show-ref", "--verify", remote_ref, check=False)
    base_ref = f"origin/{project.default_branch}" if remote_check.returncode == 0 else project.default_branch

    branch_check = _git(project.repo_path, "show-ref", "--verify", f"refs/heads/{branch}", check=False)
    if branch_check.returncode == 0:
        result = _git(project.repo_path, "worktree", "add", str(worktree), branch, timeout=600, check=False)
    else:
        result = _git(
            project.repo_path,
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            base_ref,
            timeout=600,
            check=False,
        )
    if result.returncode != 0:
        raise GitError(result.stderr or result.stdout)
    return branch, worktree


def changed_files(worktree: Path) -> list[str]:
    result = _git(worktree, "status", "--porcelain", check=False)
    files: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) >= 4:
            files.append(line[3:].strip().strip('"'))
    return files


def current_commit(worktree: Path) -> str:
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def changed_files_between(worktree: Path, base: str, candidate: str | None = None) -> list[str]:
    """Return the paths in the authoritative base-to-candidate Git diff.

    A committed candidate is compared directly to ``base``. Projects with automatic commits
    disabled retain the historical worktree behavior, including intent-to-add entries for
    untracked files.
    """
    if candidate is None:
        _git(worktree, "add", "-N", ".", check=False)
        revision = base
    else:
        revision = f"{base}..{candidate}"

    result = _git(worktree, "diff", "--name-status", "-z", revision, "--", check=False)
    if result.returncode != 0:
        raise GitError(result.stderr or result.stdout)

    fields = iter(field for field in result.stdout.split("\0") if field)
    files: list[str] = []
    for status in fields:
        first = next(fields, None)
        if first is None:
            raise GitError("git diff --name-status returned an incomplete path record.")
        if status.startswith(("R", "C")):
            second = next(fields, None)
            if second is None:
                raise GitError("git diff --name-status returned an incomplete rename record.")
            files.append(second)
        else:
            files.append(first)
    return files


def write_patch(worktree: Path, destination: Path, base: str = "HEAD", candidate: str | None = None) -> None:
    ensure_dir(destination.parent)
    if candidate is None:
        _git(worktree, "add", "-N", ".", check=False)
        revision = base
    else:
        revision = f"{base}..{candidate}"
    result = _git(worktree, "diff", "--binary", revision, "--", check=False)
    if result.returncode != 0:
        raise GitError(result.stderr or result.stdout)
    destination.write_text(result.stdout, encoding="utf-8")


def commit_task(worktree: Path, task: Task) -> str | None:
    if not changed_files(worktree):
        return None
    if _git(worktree, "config", "user.name", check=False).returncode != 0:
        _git(worktree, "config", "user.name", "Codex Forge")
    if _git(worktree, "config", "user.email", check=False).returncode != 0:
        _git(worktree, "config", "user.email", "codex-forge@localhost")
    _git(worktree, "add", "-A")
    result = _git(worktree, "commit", "-m", f"forge: {task.id} {task.title}", timeout=600, check=False)
    if result.returncode != 0:
        raise GitError(result.stderr or result.stdout)
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()
