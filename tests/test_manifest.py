import hashlib
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

# Exact record grammar: lowercase SHA-256, exactly two spaces, then a "./"-prefixed path.
# "." never matches LF, and deliberately does match U+0085 / U+2028 -- both are legal Windows
# filename characters that str.splitlines() would wrongly treat as record boundaries.
MANIFEST_RECORD = re.compile(r"([0-9a-f]{64})  (\./.+)")


def verify_manifest(repo_root: Path) -> dict[str, str]:
    """Assert MANIFEST.sha256 in ``repo_root`` is complete and correct; return its entries.

    This is the single definition of the manifest law: tracked files only, hashed from their
    working-tree bytes, one LF-terminated record each. scripts/update-manifest.ps1 reproduces it
    to generate the file, and tests/test_update_manifest.py checks the generator against this.

    Failure messages stay ASCII so a mismatch involving an exotic path cannot itself crash on a
    cp1252 console.
    """
    raw = (repo_root / "MANIFEST.sha256").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "manifest must not carry a BOM"
    text = raw.decode("utf-8")
    assert "\r" not in text, "manifest must be LF-only"

    # Split on LF alone. str.splitlines() also breaks on U+0085/U+2028 and would invent records.
    records = text.split("\n")
    assert records.pop() == "", "manifest must end with a trailing LF"

    entries: dict[str, str] = {}
    for record in records:
        matched = MANIFEST_RECORD.fullmatch(record)
        assert matched, ascii(record)
        expected, relative = matched.groups()
        assert relative not in entries, ascii(relative)
        path = repo_root / relative.removeprefix("./")
        entries[relative] = expected
        assert path.is_file(), ascii(relative)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, ascii(relative)

    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "-z"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8").split("\0")
    expected_entries = {f"./{path}" for path in tracked if path and path != "MANIFEST.sha256"}

    missing = sorted(ascii(path) for path in expected_entries - set(entries))
    extra = sorted(ascii(path) for path in set(entries) - expected_entries)
    assert not missing, f"tracked files absent from the manifest: {missing}"
    assert not extra, f"manifest entries that are not tracked files: {extra}"
    return entries


def test_manifest_hashes_match_release_files():
    entries = verify_manifest(REPO_ROOT)
    assert "./restart-service.ps1" in entries
    assert "./tests/test_orchestrator.py" in entries
