from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .models import Project, Task
from .utils import derived_worktree_path, ensure_dir, run_process, slugify


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str, timeout: int = 300, check: bool = True):
    try:
        result = run_process(["git", *args], cwd=repo, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # A hang is an infrastructure failure; it must surface as a structured Git failure, not
        # escape as an unhandled subprocess exception.
        raise GitError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed:\n{result.stderr or result.stdout}")
    return result


_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


def _plain_directory(info: os.stat_result) -> bool:
    return stat.S_ISDIR(info.st_mode) and not bool(
        getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _canonical(path: Path) -> str:
    """Windows-normalized canonical form for overlap comparison."""
    return os.path.normcase(str(path))


def _canonical_lookup(path: Path) -> str:
    """Canonical form for path lookup, resolving symlink and reparse aliases when possible."""
    try:
        return _canonical(path.resolve())
    except (OSError, RuntimeError, ValueError):
        return _canonical(Path(os.path.normpath(str(path))))


def _canonical_overlap(one: str, other: str) -> bool:
    """Equality, ancestor, or descendant overlap between two canonical paths."""
    return one == other or one.startswith(other + os.sep) or other.startswith(one + os.sep)


def ensure_disposable_checkout(
    candidate: Path, containment_root: Path, protected: Iterable[Path]
) -> None:
    """Prove a directory is a disposable Forge checkout before anything is allowed to delete it.

    The candidate must be a plain directory (never a link or reparse point), must resolve
    strictly inside its owner's containment root — a task checkout lives inside its own
    project's workspace directory, never merely inside the shared workspace root — and must not
    overlap (equality, ancestor, descendant, or resolved alias) any protected path: every
    registered repository, every other project's workspace, and every live task's effective
    checkout. All comparisons use Windows-normalized canonical paths. A stored path is data, not
    authority: refusal raises GitError and nothing is removed on a refusal.
    """
    try:
        info = os.lstat(candidate)
    except OSError as exc:
        raise GitError(f"checkout could not be inspected: {candidate}") from exc
    if not _plain_directory(info):
        raise GitError(f"checkout is not a plain directory and was refused: {candidate}")
    try:
        resolved = _canonical(candidate.resolve(strict=True))
        resolved_root = _canonical(containment_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise GitError(f"checkout containment could not be proven: {candidate}") from exc
    if resolved == resolved_root or not resolved.startswith(resolved_root + os.sep):
        raise GitError(f"checkout is outside its owner's workspace and was refused: {candidate}")
    for path in protected:
        try:
            guarded = _canonical(Path(path).resolve())
        except (OSError, RuntimeError, ValueError):
            # A protected path that cannot be resolved still names something to protect; its
            # normalized lexical form stands in for the canonical one.
            guarded = _canonical(Path(os.path.normpath(str(path))))
        if _canonical_overlap(guarded, resolved):
            raise GitError(f"checkout overlaps protected state and was refused: {candidate}")


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
    worktree = task.worktree_path or derived_worktree_path(workspace_root, project.name, task.id)
    ensure_dir(worktree.parent)

    if worktree.exists() and (worktree / ".git").exists():
        return branch, worktree
    if worktree.exists():
        # A stale non-worktree directory is deleted only after the same disposability proof the
        # cleanup path requires; a stored path is data, not authority.
        ensure_disposable_checkout(worktree, workspace_root / project.name, [project.repo_path])
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


@dataclass(frozen=True)
class DirectoryPin:
    """A plain directory pinned by physical identity and canonical path.

    A lexical path is data, not authority: between proving a directory disposable and mutating
    it, the path could be re-pointed at something else. The pin captures what was proven so
    every later mutation can require the same physical directory.
    """

    identity: tuple[int, int]
    canonical: str


def pin_plain_directory(path: Path) -> DirectoryPin:
    """Prove the path names a plain, non-reparse directory with a usable identity and pin it.

    A symlink or junction is refused before anything reads through it, so a cleanup can never be
    redirected at the link's target.
    """
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise GitError(f"directory could not be inspected: {path}") from exc
    if not _plain_directory(info):
        raise GitError(f"not a plain directory and was refused: {path}")
    if not (info.st_dev and info.st_ino):
        raise GitError(f"directory identity is unavailable: {path}")
    try:
        canonical = _canonical(path.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise GitError(f"directory could not be resolved: {path}") from exc
    return DirectoryPin(identity=(info.st_dev, info.st_ino), canonical=canonical)


def revalidate_pinned_directory(path: Path, pin: DirectoryPin) -> None:
    """Fail closed when the path no longer names the pinned physical directory."""
    if pin_plain_directory(path) != pin:
        raise GitError(f"directory changed while it was being cleaned and was refused: {path}")


def _registered_worktree_paths(repo: Path) -> list[Path]:
    """List every checkout Git knows about.

    A listing failure raises: registration state that cannot be read must never be treated as
    "nothing registered", because that is exactly how a removal gets misreported as complete.
    """
    result = _git(repo, "worktree", "list", "--porcelain", check=False)
    if result.returncode != 0:
        raise GitError(f"git worktree list failed:\n{result.stderr or result.stdout}")
    return [
        Path(line[len("worktree ") :])
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    ]


def _repository_registers(
    repository: Path, matcher: Callable[[Path], bool], *, require_available: bool
) -> bool:
    """Whether a repository's registration inventory selects a checkout via `matcher`.

    A repository directory that does not exist is unavailable: with `require_available` the
    inventory fails closed — ownership cannot be disproven from an unreachable repository — and
    without it the absence itself is the answer, because registration metadata lives inside the
    repository directory. A directory that is verifiably not a Git repository holds no
    registrations and vouches for nothing; an actual repository must list successfully or the
    inventory fails closed.
    """
    if not repository.is_dir():
        if require_available:
            raise GitError(
                f"repository is unavailable; checkout ownership cannot be verified: {repository}"
            )
        return False
    probe = _git(repository, "rev-parse", "--git-dir", check=False)
    if probe.returncode != 0:
        return False
    return any(matcher(path) for path in _registered_worktree_paths(repository))


def _require_no_foreign_owner(
    repo: Path,
    repositories: Iterable[Path],
    matcher: Callable[[Path], bool],
    target: Path,
) -> None:
    """Prove no repository other than `repo` registers a checkout the matcher selects.

    Runs only when a deletion is imminent, so every other registered repository must testify: a
    foreign match fails closed, and an unavailable repository fails closed too, because a
    checkout owned by a currently unreachable repository must never be raw-deleted.
    """
    own = _canonical_lookup(repo)
    for other in repositories:
        other_path = Path(other)
        if _canonical_lookup(other_path) == own:
            continue
        if _repository_registers(other_path, matcher, require_available=True):
            raise GitError(
                f"registered to another repository and was refused: {target}"
            )


def remove_worktree(
    repo: Path,
    worktree: Path,
    containment_root: Path,
    protected: Iterable[Path],
    foreign_repositories: Iterable[Path] = (),
) -> bool:
    """Remove one task's disposable worktree checkout, never its branch or any other ref.

    Returns True only when both the checkout and any Git registration are verifiably gone, and
    False when there was nothing on disk to remove. Before anything is deleted the checkout must
    prove it is disposable via `ensure_disposable_checkout`, so a Git discovery or removal
    failure can never fall through to deleting a non-disposable directory. Registered removal and
    pruning are checked operations with no raw-deletion fall-through — a locked worktree fails
    closed with its directory intact — and success is re-verified against both the filesystem
    and a fresh registration listing. Deleting an on-disk checkout additionally requires every
    other registered repository to testify about ownership: a checkout registered to another
    repository — or to more than one — is never touched, and a repository that cannot testify
    fails the removal closed. The branch is never deleted and no GC ever runs here.
    """

    # Registrations are matched canonically: reaching the same checkout through a directory
    # symlink must still find — and clear — Git's registration of the real path.
    target = _canonical_lookup(worktree)

    def matches(candidate: Path) -> bool:
        return _canonical_lookup(candidate) == target

    checkout_present = os.path.lexists(worktree)
    if not repo.is_dir():
        # A repository that cannot be reached cannot testify about registrations, and that is
        # never the same as "unregistered". With a checkout on disk the removal fails closed
        # rather than raw-deleting a checkout whose registration may survive; with nothing on
        # disk there is nothing to delete and nothing is claimed removed.
        if checkout_present:
            raise GitError(
                f"repository is missing and registration state cannot be verified; "
                f"the checkout was not touched: {repo}"
            )
        return False
    registered = any(matches(path) for path in _registered_worktree_paths(repo))
    if not checkout_present:
        if registered:
            _git(repo, "worktree", "prune")
            if any(matches(path) for path in _registered_worktree_paths(repo)):
                raise GitError(f"worktree registration could not be pruned: {worktree}")
        return False
    _require_no_foreign_owner(repo, foreign_repositories, matches, worktree)
    ensure_disposable_checkout(worktree, containment_root, protected)
    if registered:
        # Registered removal is a checked operation with no raw-deletion fall-through: a locked
        # worktree fails closed with its directory intact rather than losing the directory while
        # the registration survives.
        _git(repo, "worktree", "remove", "--force", str(worktree))
    else:
        shutil.rmtree(worktree)
    if os.path.lexists(worktree):
        raise GitError(f"worktree checkout still present after removal: {worktree}")
    if any(matches(path) for path in _registered_worktree_paths(repo)):
        raise GitError(f"worktree registration still present after removal: {worktree}")
    return True


def remove_registered_checkouts(
    repo: Path,
    containment_root: Path,
    protected: Iterable[Path],
    foreign_repositories: Iterable[Path] = (),
    pin: DirectoryPin | None = None,
) -> None:
    """Clear every Git worktree registration inside a directory that is about to be deleted.

    The registration inventory is read canonically from the repository before anything is
    removed; a repository that cannot be verified fails closed, because unverifiable
    registration state is never "nothing registered". Every other registered repository must
    testify that it owns nothing inside the containment root — a foreign-owned checkout, or a
    repository that cannot testify, fails the cleanup closed before anything is removed. Each
    of the owner's registered checkouts still on disk must prove it is disposable and is then
    removed only through checked Git operations — a locked worktree fails with its directory
    and registration intact. Registrations whose checkouts are already gone are pruned, and the
    inventory is re-read afterwards: when this returns, Git holds no registration inside the
    containment root, so a later rmtree cannot delete a registered checkout or orphan
    registration metadata. When a pin is supplied, the containment root is re-proven to be the
    same physical directory before every Git removal, so a swapped-in link or replacement
    directory can never redirect the cleanup.
    """
    if not repo.is_dir():
        raise GitError(
            f"repository is missing and worktree registrations cannot be inventoried: {repo}"
        )
    protected = list(protected)
    root = _canonical_lookup(containment_root)

    def inside(candidate: Path) -> bool:
        canonical = _canonical_lookup(candidate)
        return canonical == root or canonical.startswith(root + os.sep)

    _require_no_foreign_owner(repo, foreign_repositories, inside, containment_root)
    for path in _registered_worktree_paths(repo):
        if not inside(path):
            continue
        if os.path.lexists(path):
            if pin is not None:
                revalidate_pinned_directory(containment_root, pin)
            ensure_disposable_checkout(path, containment_root, protected)
            _git(repo, "worktree", "remove", "--force", str(path))
    if any(inside(path) for path in _registered_worktree_paths(repo)):
        if pin is not None:
            revalidate_pinned_directory(containment_root, pin)
        _git(repo, "worktree", "prune")
        if any(inside(path) for path in _registered_worktree_paths(repo)):
            raise GitError(
                f"a worktree registration inside the workspace could not be cleared: "
                f"{containment_root}"
            )


def reconcile_missing_workspace(
    repo: Path, containment_root: Path, foreign_repositories: Iterable[Path] = ()
) -> None:
    """Clear stale registrations under a workspace path that no longer exists on disk.

    A missing workspace can still be named by Git registrations, so success must not be
    reported before they are reconciled. Registration metadata lives inside a repository
    directory, so a repository that does not exist holds none and needs no check; a directory
    that is verifiably not a Git repository holds none either; an actual repository must list
    successfully or the reconciliation fails closed. The deleted project's own stale
    registrations are pruned and verified gone — a locked registration that survives pruning
    fails closed — and a foreign registration naming a path under the missing workspace fails
    closed rather than being ignored or touched.
    """
    root = _canonical_lookup(containment_root)

    def inside(candidate: Path) -> bool:
        canonical = _canonical_lookup(candidate)
        return canonical == root or canonical.startswith(root + os.sep)

    own = _canonical_lookup(repo)
    for other in foreign_repositories:
        other_path = Path(other)
        if _canonical_lookup(other_path) == own:
            continue
        if _repository_registers(other_path, inside, require_available=False):
            raise GitError(
                f"a foreign repository registers a checkout under the missing workspace: "
                f"{containment_root}"
            )
    if not _repository_registers(repo, inside, require_available=False):
        return
    _git(repo, "worktree", "prune")
    if any(inside(path) for path in _registered_worktree_paths(repo)):
        raise GitError(
            f"a stale worktree registration under the missing workspace could not be cleared: "
            f"{containment_root}"
        )


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
