import hashlib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_manifest_hashes_match_release_files():
    entries = {}
    for line in (REPO_ROOT / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines():
        expected, relative = line.split(maxsplit=1)
        path = REPO_ROOT / relative.removeprefix("./")
        entries[relative] = expected
        assert path.is_file(), relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, relative
    assert "./restart-service.ps1" in entries
    assert "./tests/test_orchestrator.py" in entries
