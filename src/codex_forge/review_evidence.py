"""Prospective, immutable evidence of an actual trusted controller review decision."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from .gitops import CandidateStateUnavailable


CONTROLLER_REVIEW_FILENAME = "controller-review.json"


def retain_controller_review(source: Path, model: dict, effective: dict) -> None:
    """Publish once without replacing model bytes or re-running conformance policy.

    Unique temporary files and a no-clobber link make concurrent publication idempotent
    only for identical documents. A conflicting retained decision is a provenance fault.
    """
    from .blockers import _plain_bytes
    from .prerequisites import _safe_target

    target = source.parent / CONTROLLER_REVIEW_FILENAME
    temporary = source.parent / f".controller-review-{uuid.uuid4().hex}.pending"
    try:
        raw = _plain_bytes(source, maximum=32 * 1024 * 1024)
        decoded = json.loads(raw.decode("utf-8", errors="replace"))
        if json.dumps(decoded, sort_keys=True) != json.dumps(model, sort_keys=True):
            raise CandidateStateUnavailable("Review output changed after trusted decoding.")
        document = {
            "format_version": "1",
            "source_review_sha256": hashlib.sha256(raw).hexdigest(),
            "model_verdict": model["verdict"],
            "effective_verdict": effective["verdict"],
            "acceptance_criteria": model.get("acceptance_criteria", []),
            "binding_requirements": model.get("binding_requirements", []),
            "findings": effective.get("findings", []),
        }
        content = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        _safe_target(source.parent, target.name)
        if target.exists():
            if _plain_bytes(target, maximum=32 * 1024 * 1024) != content:
                raise CandidateStateUnavailable("Retained controller review decision conflicts.")
        else:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            _safe_target(source.parent, target.name)
            try:
                os.link(temporary, target)
            except FileExistsError:
                if _plain_bytes(target, maximum=32 * 1024 * 1024) != content:
                    raise CandidateStateUnavailable("Concurrent controller review decision conflicts.")
        if _plain_bytes(source, maximum=32 * 1024 * 1024) != raw:
            raise CandidateStateUnavailable("Review output changed during decision retention.")
    except (OSError, ValueError, KeyError) as exc:
        raise CandidateStateUnavailable("Controller review decision could not be retained safely.") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise CandidateStateUnavailable("Controller review temporary evidence could not be removed safely.") from exc
