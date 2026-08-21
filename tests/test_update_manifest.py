"""scripts/update-manifest.ps1 must produce exactly what tests/test_manifest.py accepts.

The generator is PowerShell and the verifier is Python, so the law cannot simply be imported by
both. These tests close that gap: the generator runs against a throwaway Git fixture and its
output is checked with the verifier's own ``verify_manifest`` and record grammar. A developer's
working-tree manifest is never touched.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_manifest import MANIFEST_RECORD, REPO_ROOT, verify_manifest


SCRIPT = REPO_ROOT / "scripts" / "update-manifest.ps1"

# Legal Windows filenames that defeat naive parsing: a space, a doubled space colliding with the
# record separator, and the two Unicode boundaries str.splitlines() wrongly splits on.
AWKWARD_NAMES = [
    "with space.md",
    "two  spaces.md",
    "nel" + chr(0x85) + "boundary.md",
    "sep" + chr(0x2028) + "boundary.md",
]

# Every host present is exercised: CONTRIBUTING documents `powershell -File`, but the repository
# is also used under pwsh 7, and a cmdlet available in only one host is a real failure mode.
HOSTS = [name for name in ("powershell", "pwsh") if shutil.which(name)]

_NO_HOST = [pytest.param(None, marks=pytest.mark.skip("no PowerShell host on PATH"))]


@pytest.fixture(params=HOSTS or _NO_HOST)
def powershell_host(request) -> str:
    return shutil.which(request.param)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _generate(root: Path, host: str) -> bytes:
    subprocess.run(
        [
            host,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-RepositoryRoot",
            str(root),
        ],
        check=True,
        capture_output=True,
    )
    return (root / "MANIFEST.sha256").read_bytes()


def _awkward(root: Path) -> list[str]:
    """The awkward names this filesystem actually accepted."""
    return [name for name in AWKWARD_NAMES if (root / name).exists()]


def _tracked_count(root: Path) -> int:
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "-z"],
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    return len([path for path in listing.split("\0") if path and path != "MANIFEST.sha256"])


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "ignored").mkdir()
    (root / ".gitignore").write_bytes(b"ignored/\n")
    (root / "docs" / "nested.md").write_bytes(b"nested\n")
    # CRLF content proves the generator hashes raw bytes and normalizes nothing.
    (root / "tracked.ps1").write_bytes(b"Write-Host 'x'\r\n")
    (root / "untracked.txt").write_bytes(b"scratch state, never staged\n")
    (root / "ignored" / "local.txt").write_bytes(b"local only\n")
    # A stale manifest must be replaced outright, not merged into.
    (root / "MANIFEST.sha256").write_bytes(b"0" * 64 + b"  ./gone.txt\n")

    staged = [".gitignore", "docs/nested.md", "tracked.ps1"]
    for name in AWKWARD_NAMES:
        try:
            (root / name).write_bytes(b"awkward\n")
        except OSError:  # filesystem rejects the name; the remaining cases still apply
            continue
        staged.append(name)

    _git(root, "init", "-q")
    _git(root, "add", *staged)
    return root


def test_generated_manifest_satisfies_the_verifier(fixture_repo: Path, powershell_host: str):
    _generate(fixture_repo, powershell_host)
    entries = verify_manifest(fixture_repo)
    assert "./docs/nested.md" in entries
    assert "./tracked.ps1" in entries
    for name in _awkward(fixture_repo):
        assert f"./{name}" in entries, ascii(name)
    assert "./MANIFEST.sha256" not in entries
    assert "./gone.txt" not in entries


def test_untracked_and_ignored_files_are_excluded(fixture_repo: Path, powershell_host: str):
    _generate(fixture_repo, powershell_host)
    entries = verify_manifest(fixture_repo)
    assert "./untracked.txt" not in entries, "unstaged scratch state must never enter the manifest"
    assert "./ignored/local.txt" not in entries


def test_staging_is_what_adds_a_new_file(fixture_repo: Path, powershell_host: str):
    _git(fixture_repo, "add", "untracked.txt")
    _generate(fixture_repo, powershell_host)
    assert "./untracked.txt" in verify_manifest(fixture_repo)


def test_second_run_is_byte_identical(fixture_repo: Path, powershell_host: str):
    assert _generate(fixture_repo, powershell_host) == _generate(fixture_repo, powershell_host)


def test_current_manifest_is_not_rewritten(fixture_repo: Path, powershell_host: str):
    _generate(fixture_repo, powershell_host)
    manifest = fixture_repo / "MANIFEST.sha256"
    # Backdate past timestamp granularity so a rewrite cannot masquerade as a no-op.
    backdated = manifest.stat().st_mtime_ns - 10_000_000_000
    os.utime(manifest, ns=(backdated, backdated))
    stamped = manifest.stat().st_mtime_ns
    _generate(fixture_repo, powershell_host)
    assert manifest.stat().st_mtime_ns == stamped


def test_a_real_change_is_still_written(fixture_repo: Path, powershell_host: str):
    before = _generate(fixture_repo, powershell_host)
    (fixture_repo / "docs" / "nested.md").write_bytes(b"changed\n")
    assert _generate(fixture_repo, powershell_host) != before


def test_output_is_lf_only_utf8_without_bom(fixture_repo: Path, powershell_host: str):
    raw = _generate(fixture_repo, powershell_host)
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    records = raw.decode("utf-8").split("\n")
    assert records.pop() == ""
    for record in records:
        assert MANIFEST_RECORD.fullmatch(record), ascii(record)


def test_awkward_names_do_not_inflate_the_record_count(fixture_repo: Path, powershell_host: str):
    text = _generate(fixture_repo, powershell_host).decode("utf-8")
    expected = _tracked_count(fixture_repo)
    assert len(text.split("\n")) - 1 == expected
    if _awkward(fixture_repo):
        # Proves the fixture really exercises the trap the LF-only split exists to avoid.
        assert len(text.splitlines()) > expected


def test_entries_use_ordinal_path_order(fixture_repo: Path, powershell_host: str):
    text = _generate(fixture_repo, powershell_host).decode("utf-8")
    paths = [MANIFEST_RECORD.fullmatch(record).group(2) for record in text.split("\n")[:-1]]
    assert paths == sorted(paths)


@pytest.mark.skipif(len(HOSTS) < 2, reason="only one PowerShell host on PATH")
def test_all_hosts_produce_identical_bytes(fixture_repo: Path):
    outputs = {name: _generate(fixture_repo, shutil.which(name)) for name in HOSTS}
    assert len(set(outputs.values())) == 1, "hosts disagree: " + ", ".join(outputs)
