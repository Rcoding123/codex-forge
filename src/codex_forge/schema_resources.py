"""One packaged schema source for transport and controller-side decoding."""
from __future__ import annotations

import json
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path


def _resource(name: str):
    if name not in {"review-result.schema.json", "task-result.schema.json"}:
        raise ValueError(f"Unknown Forge schema: {name}")
    return files("codex_forge").joinpath("schemas", name)


def load_schema(name: str) -> dict:
    return json.loads(_resource(name).read_text(encoding="utf-8"))


@contextmanager
def schema_file(name: str, override_root: Path | None = None):
    # Overrides are controller-owned test/custom transport roots, never client paths.
    if override_root is not None:
        yield override_root / name
    else:
        with as_file(_resource(name)) as path:
            yield path
