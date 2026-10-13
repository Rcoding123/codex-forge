from __future__ import annotations

import os
import re
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


class BaseCommitError(GitError):
    """A requested admission base is not a canonical commit object in the repository.

    Raised at admission only. A branch name, a tag, a revision expression, an abbreviated object
    name and an object that is not a commit are all refused here, because none of them is durable
    identity: each can name a different commit later than it does now.
    """


class BaseCommitUnavailable(GitError):
    """A task's persisted admission base cannot supply this execution's lineage.

    Raised at execution only, and always a repository/infrastructure failure rather than a finding
    about the task's code. It covers both forms of the same fault: the admission base is absent
    from the repository that must supply it, and a retained branch or checkout for the task no
    longer descends from it. Neither is ever repaired by substituting another commit.
    """


class WorktreeUnavailable(GitError):
    """An exact-base checkout cannot be safely reconstructed in its current repository."""


class CandidateStateUnavailable(GitError):
    """The final checkout is not one clean commit descended from its admission base."""


# A Git object name as Git itself writes one. Object-format neutral on purpose: the length is
# never asserted here, because the only accepted proof that a value is a canonical object id is
# that Git resolves it to exactly itself.
_OBJECT_NAME = re.compile(r"\A[0-9a-fA-F]+\Z")


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


def _resolved_commit(repo: Path, value: str) -> str | None:
    """Return the canonical commit id `value` names, or None when it names no commit.

    `^{commit}` is what makes the answer a commit rather than any object: a tree or blob id
    dereferences to no commit and is refused here, and an annotated tag is peeled rather than
    stored. `--end-of-options` keeps a caller-supplied value from being read as an option.
    """
    probe = _git(
        repo,
        "rev-parse",
        "--verify",
        "--quiet",
        "--end-of-options",
        f"{value}^{{commit}}",
        check=False,
    )
    if probe.returncode != 0:
        return None
    return probe.stdout.strip() or None


def resolve_admission_base_commit(repo: Path, value: str) -> str:
    """Prove a requested admission base already *is* a commit object id, and return Git's form.

    Only one thing is accepted: a value Git resolves to exactly itself. That single rule refuses
    every non-identity a caller might send -- a branch or tag name, `HEAD~1` or any other revision
    expression, and an abbreviated object name -- because each of those resolves to something
    other than what was written, and each can name a different commit tomorrow than it does today.
    The comparison is case-insensitive and the value Git returns is what is persisted, so the
    durable form is always Git's own canonical object id. Nothing here assumes an object format or
    a hash length; the repository answers that by resolving.
    """
    candidate = value.strip()
    if not candidate or not _OBJECT_NAME.match(candidate):
        raise BaseCommitError(
            "An admission base must be a full Git commit object id, not a branch, tag, "
            "abbreviation or revision expression."
        )
    resolved = _resolved_commit(repo, candidate)
    if resolved is None:
        raise BaseCommitError(
            "The requested admission base does not identify a commit in the registered repository."
        )
    if resolved.lower() != candidate.lower():
        raise BaseCommitError(
            "The requested admission base is not a canonical commit object id; supply the full "
            "object id Git resolves it to."
        )
    return resolved


def require_admission_base_commit(repo: Path, commit: str) -> None:
    """Fail closed when a task's persisted admission base is not present as a commit.

    Execution never substitutes another commit for a missing base: the default branch may have
    advanced, and the repository may have been relocated to a path that never held this history.
    In both cases the honest outcome is a repository failure the operator resolves.
    """
    if _resolved_commit(repo, commit) != commit:
        raise BaseCommitUnavailable(
            f"The task's admission base commit is not present in the repository: {commit}"
        )


def require_base_lineage(where: Path, commit: str, revision: str) -> None:
    """Fail closed when a retained branch or checkout does not descend from the admission base.

    A retried task reuses whatever branch and checkout its earlier attempt left behind. That reuse
    is only safe while the lineage still starts at the admission base, so the descent is proven
    rather than assumed: a branch rewritten underneath the task is a repository failure, never a
    reason to continue from wherever it now points.
    """
    probe = _git(where, "merge-base", "--is-ancestor", commit, revision, check=False)
    if probe.returncode != 0:
        raise BaseCommitUnavailable(
            f"The task's retained {revision} no longer descends from its admission base commit "
            f"{commit}."
        )


def _common_git_dir(where: Path) -> str | None:
    """The absolute, canonical common Git directory `where` belongs to, or None if Git cannot say.

    A linked worktree stores a pointer to the repository that created it, so this is Git's own
    answer to "which repository owns this checkout" rather than an inference from the path.
    """
    probe = _git(where, "rev-parse", "--git-common-dir", check=False)
    if probe.returncode != 0:
        return None
    value = probe.stdout.strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = where / path
    return _canonical_lookup(path)


def checkout_is_owned_by(repository: Path, worktree: Path) -> bool:
    """Whether Git says this checkout belongs to the repository the project is registered at now.

    Registration metadata is the authority; a stored path never is. A project may be relocated to
    a different repository while a task's checkout is still on disk, and that checkout keeps
    naming the repository that created it -- or names a repository that has since moved away, in
    which case Git can answer nothing at all. Reusing such a checkout would run the task against
    a repository the registration no longer points at, so anything short of a proven match is
    treated as not owned.
    """
    theirs = _common_git_dir(worktree)
    if theirs is None:
        return False
    ours = _common_git_dir(repository)
    return ours is not None and theirs == ours


def _discard_unowned_checkout(
    worktree: Path,
    containment_root: Path,
    repository: Path,
    branch: str,
) -> None:
    """Remove a checkout that the currently registered repository does not own.

    Ownership has already been disproven. The checkout is still deregistered through the
    repository that actually owns it whenever that repository is reachable, so a relocation does
    not leave a live registration pointing into a directory that is about to be rebuilt. When the
    old repository is gone -- the ordinary result of moving one -- no registration survives to
    clear, and the directory is removed only after the same disposability proof every other
    removal here requires. A directory that survives both is a refusal, never a silent reuse.
    """
    try:
        owner_dir = _common_git_dir(worktree)
        owner = Path(owner_dir).parent if owner_dir and Path(owner_dir).name == ".git" else None
        if owner is not None and owner.is_dir():
            remove_worktree(
                owner,
                worktree,
                containment_root,
                [repository, owner],
                foreign_repositories=[repository],
            )

        # A physical repository move leaves the registration inside the moved repository even
        # though the checkout's absolute `.git` pointer no longer resolves. Filesystem existence
        # therefore cannot answer whether the target is reusable. Git's inventory is authoritative:
        # only the exact target registration, on the task's expected branch, may be removed.
        _remove_target_worktree_registration(
            repository,
            worktree,
            containment_root,
            [repository],
            expected_branch=branch,
        )
        if os.path.lexists(worktree):
            ensure_disposable_checkout(worktree, containment_root, [repository])
            shutil.rmtree(worktree)
        if os.path.lexists(worktree):
            raise GitError(
                f"a checkout owned by another repository could not be discarded: {worktree}"
            )
    except GitError as exc:
        raise WorktreeUnavailable(
            f"the task checkout could not be safely reconstructed: {worktree}: {exc}"
        ) from exc


def _default_base_ref(project: Project) -> str:
    """The implicit base a task without an admission base still starts from.

    Reached only for such a task. An explicit admission base never consults this, so neither a
    project's default branch nor its remote can move a task admitted at an exact commit.
    """
    remote_ref = f"refs/remotes/origin/{project.default_branch}"
    remote_check = _git(project.repo_path, "show-ref", "--verify", remote_ref, check=False)
    return (
        f"origin/{project.default_branch}"
        if remote_check.returncode == 0
        else project.default_branch
    )


def prepare_worktree(project: Project, task: Task, workspace_root: Path) -> tuple[str, Path]:
    """Give the task a checkout, based on its admission base when it has one.

    A task carrying `admission_base_commit` is branched from exactly that commit and from nothing
    else, on the first attempt and on every later one. The configured fetch still runs first and
    is unchanged -- a base that only becomes present with that fetch is usable -- but a fetch can
    never change which commit is chosen, because the choice is a persisted object id rather than a
    ref. A task without an admission base keeps the project's existing base selection exactly.

    Every reusable checkout and branch must prove it still belongs to the registered repository
    and still descends from the admission base. Whatever cannot prove both is discarded and
    rebuilt from the base, or fails closed; no path here ever falls back to another base.
    """
    verify_repository(project.repo_path)
    if project.auto_fetch:
        _git(project.repo_path, "fetch", "--all", "--prune", timeout=900, check=False)

    admission_base = task.admission_base_commit
    if admission_base:
        require_admission_base_commit(project.repo_path, admission_base)

    branch = task.branch or f"forge/{slugify(task.id)}-{slugify(task.title, 36)}"
    worktree = task.worktree_path or derived_worktree_path(workspace_root, project.name, task.id)
    ensure_dir(worktree.parent)

    if worktree.exists() and (worktree / ".git").exists():
        if not admission_base:
            return branch, worktree
        # Two proofs before an existing checkout is reused, never one: the repository registered
        # now must own it, and its lineage must still start at the admission base. A checkout that
        # cannot prove both is discarded and rebuilt from the base rather than executed against.
        if checkout_is_owned_by(project.repo_path, worktree):
            require_base_lineage(worktree, admission_base, "HEAD")
            return branch, worktree
        _discard_unowned_checkout(
            worktree,
            workspace_root / project.name,
            project.repo_path,
            branch,
        )
    if worktree.exists():
        # A stale non-worktree directory is deleted only after the same disposability proof the
        # cleanup path requires; a stored path is data, not authority.
        try:
            ensure_disposable_checkout(
                worktree, workspace_root / project.name, [project.repo_path]
            )
            shutil.rmtree(worktree)
        except GitError as exc:
            if admission_base:
                raise WorktreeUnavailable(
                    f"the stale task checkout could not be safely removed: {worktree}: {exc}"
                ) from exc
            raise
    if admission_base:
        # The directory may already have disappeared while Git still registers its exact path.
        # Reconcile that one target before `worktree add`; filesystem absence is not evidence that
        # the registry is clear.
        try:
            _remove_target_worktree_registration(
                project.repo_path,
                worktree,
                workspace_root / project.name,
                [project.repo_path],
                expected_branch=branch,
            )
        except GitError as exc:
            if isinstance(exc, WorktreeUnavailable):
                raise
            raise WorktreeUnavailable(
                f"the stale task worktree registration could not be cleared: {worktree}: {exc}"
            ) from exc

    branch_check = _git(project.repo_path, "show-ref", "--verify", f"refs/heads/{branch}", check=False)
    if branch_check.returncode == 0:
        if admission_base:
            require_base_lineage(project.repo_path, admission_base, branch)
        result = _git(project.repo_path, "worktree", "add", str(worktree), branch, timeout=600, check=False)
    else:
        # Short-circuit, not a fallback: with an admission base the default-branch resolution is
        # never evaluated at all.
        base_ref = admission_base or _default_base_ref(project)
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
        error = GitError(result.stderr or result.stdout)
        if admission_base:
            raise WorktreeUnavailable(
                f"the exact-base task checkout could not be created: {worktree}: {error}"
            ) from error
        raise error
    if admission_base:
        if not checkout_is_owned_by(project.repo_path, worktree):
            raise WorktreeUnavailable(
                "the recreated task checkout is not owned by the currently registered repository: "
                f"{worktree}"
            )
        require_base_lineage(worktree, admission_base, "HEAD")
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


@dataclass(frozen=True)
class WorktreeRegistration:
    path: Path
    branch: str | None


def _registered_worktrees(repo: Path) -> list[WorktreeRegistration]:
    """Return Git's path and branch identity for every registered checkout."""
    result = _git(repo, "worktree", "list", "--porcelain", check=False)
    if result.returncode != 0:
        raise GitError(f"git worktree list failed:\n{result.stderr or result.stdout}")
    records: list[WorktreeRegistration] = []
    path: Path | None = None
    branch: str | None = None
    for line in [*result.stdout.splitlines(), ""]:
        if line.startswith("worktree "):
            if path is not None:
                records.append(WorktreeRegistration(path, branch))
            path = Path(line[len("worktree ") :])
            branch = None
        elif line.startswith("branch "):
            branch = line[len("branch ") :]
        elif not line and path is not None:
            records.append(WorktreeRegistration(path, branch))
            path = None
            branch = None
    return records


def _registered_worktree_paths(repo: Path) -> list[Path]:
    """List every checkout Git knows about.

    A listing failure raises: registration state that cannot be read must never be treated as
    "nothing registered", because that is exactly how a removal gets misreported as complete.
    """
    return [registration.path for registration in _registered_worktrees(repo)]


def _remove_target_worktree_registration(
    repo: Path,
    worktree: Path,
    containment_root: Path,
    protected: Iterable[Path],
    *,
    expected_branch: str,
) -> bool:
    """Remove only the exact stale registration Forge is about to recreate.

    No repository-wide pruning occurs. The exact path and expected task branch prove which
    registration belongs to this task; a mismatch or duplicate match fails closed. When the
    directory remains on disk, the normal disposability proof runs before Git may remove it.
    """
    target = _canonical_lookup(worktree)
    matches = [
        registration
        for registration in _registered_worktrees(repo)
        if _canonical_lookup(registration.path) == target
    ]
    if not matches:
        return False
    if len(matches) != 1:
        raise GitError(f"multiple worktree registrations matched the task checkout: {worktree}")
    registration = matches[0]
    expected_ref = f"refs/heads/{expected_branch}"
    if registration.branch != expected_ref:
        raise GitError(
            "the registered checkout is not on the task's expected branch and was refused: "
            f"{worktree}"
        )
    if os.path.lexists(worktree):
        ensure_disposable_checkout(worktree, containment_root, protected)
        # Git for Windows refuses `worktree remove` while the checkout's `.git` file points at
        # the repository's pre-move path. The exact registration and disposability proofs above
        # make removal safe; once the directory is absent Git can remove that one stale registry
        # entry without touching any other worktree.
        shutil.rmtree(worktree)
    _git(repo, "worktree", "remove", "--force", str(registration.path))
    if os.path.lexists(worktree):
        raise GitError(f"worktree checkout still present after targeted removal: {worktree}")
    if any(
        _canonical_lookup(item.path) == target for item in _registered_worktrees(repo)
    ):
        raise GitError(f"worktree registration still present after targeted removal: {worktree}")
    return True


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


def explicit_candidate_commit(repository: Path, worktree: Path, base: str) -> str:
    """Return the one commit that completely represents an explicit-base terminal state.

    Candidate identity is always the current checkout HEAD, regardless of whether this attempt
    created it. Before it can become terminal authority, Git must prove that the checkout belongs
    to the currently registered repository, the immutable base still exists, HEAD descends from
    that base, and no tracked or untracked implementation state remains outside HEAD.
    """
    if not checkout_is_owned_by(repository, worktree):
        raise CandidateStateUnavailable(
            "the terminal checkout is not owned by the currently registered repository."
        )
    require_admission_base_commit(repository, base)
    try:
        candidate = current_commit(worktree)
    except GitError as exc:
        raise CandidateStateUnavailable(
            "the terminal checkout has no canonical candidate commit."
        ) from exc
    try:
        require_base_lineage(worktree, base, candidate)
    except BaseCommitUnavailable as exc:
        raise CandidateStateUnavailable(
            f"the candidate commit {candidate} does not descend from admission base {base}."
        ) from exc
    status = _git(
        worktree,
        "status",
        "--porcelain",
        "--untracked-files=all",
        check=False,
    )
    if status.returncode != 0:
        raise CandidateStateUnavailable(
            "the terminal checkout state could not be compared with its candidate commit."
        )
    if status.stdout:
        raise CandidateStateUnavailable(
            "the terminal checkout contains changes not represented by its candidate commit."
        )
    return candidate


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


def push_candidate(worktree: Path, branch: str, candidate: str) -> None:
    """Publish without force and confirm the exact reviewed tip on the remote."""
    # The checked object, never a mutable local ref, is the publication authority.
    _git(worktree, "push", "origin", f"{candidate}:refs/heads/{branch}", timeout=1200)
    ref = f"refs/heads/{branch}"
    remote = _git(worktree, "ls-remote", "--exit-code", "origin", ref, timeout=300)
    records = [line.split() for line in remote.stdout.splitlines() if line.strip()]
    if records != [[candidate, ref]]:
        raise GitError("Remote task branch did not confirm the reviewed candidate.")
