"""Read-only, versioned access to task evidence for external control-plane consumers.

This module is the single place that knows how Forge lays evidence out on disk, which artifacts are
publishable, how an artifact is named by an opaque identifier, and how its bytes are bounded,
redacted, and sanitized before they leave the process. The CLI only renders what this module
returns; it never touches the evidence tree itself.

The contract is an explicit allowlist. An artifact that this module does not positively recognize as
publishable is excluded, so a new controller output cannot become public by accident.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .config import ForgeConfig
from .control_plane import SCHEMA_VERSION, redact_secret_arguments
from .db import ForgeDB
from .models import Task


EVIDENCE_LIST_COMMAND = "task.evidence.list"
EVIDENCE_SHOW_COMMAND = "task.evidence.show"

# Documented content bounds. `MAX_ARTIFACT_BYTES` is the most content a single response returns;
# anything longer is truncated and reported as such. `MAX_READABLE_BYTES` is the point at which Forge
# refuses to read at all, so an unexpectedly large file cannot be turned into work for the caller.
MAX_ARTIFACT_BYTES = 262_144
MAX_READABLE_BYTES = 8_388_608
_READ_CHUNK_BYTES = 65_536
_BINARY_SNIFF_BYTES = 8_192
_REDACTED = "[REDACTED]"

_ARTIFACT_ID_PREFIX = "ev_"
_ARTIFACT_ID_HEX = 32
_ARTIFACT_ID_PATTERN = re.compile(rf"^{_ARTIFACT_ID_PREFIX}[0-9a-f]{{{_ARTIFACT_ID_HEX}}}$")

_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_UNSAFE_NAME_CHARACTERS = frozenset('\x00:/\\')

# Evidence layout, as written by orchestrator.py, runner.py, and validation.py:
#
#   <evidence_root>/<project>/<task-id>/attempt-NN/
#       task-result.json                          attempt-level final result
#       changes.patch                             attempt-level patch
#       <group>/<group>.final.json                schema-constrained phase result
#       <group>/<group>.events.jsonl              model event stream        (excluded)
#       <group>/<group>.stderr.log                model process stderr      (excluded)
#       <group>/<group>.prompt.txt                raw model prompt          (excluded)
#       <group>/validation-NN.stdout.log          declared validation command output
#       <group>/validation-NN.stderr.log          declared validation command output
#
# A repair phase and the validation run that follows it deliberately share one group directory, so
# classification is driven by the file name in its group context rather than by the directory alone.
# The digit run is bounded so an absurd directory name cannot raise on integer conversion.
_ATTEMPT_DIRECTORY = re.compile(r"^attempt-(?P<number>\d{1,9})$")
_VALIDATION_LOG = re.compile(r"^validation-(?P<index>\d+)\.(?P<stream>stdout|stderr)\.log$")
_IMPLEMENTATION_GROUP = re.compile(r"^implementation$")
_REVIEW_GROUP = re.compile(r"^review(?:-\d+)?$")
_REPAIR_GROUP = re.compile(r"^(?:validation|review)-repair-\d+$")
_VALIDATION_GROUP = re.compile(r"^validation-(?:initial|final|repair-\d+|review-repair-\d+)$")

_MAX_DEPTH = 3


class EvidenceError(Exception):
    """An evidence failure that maps to a stable structured error code."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


@dataclass(frozen=True, slots=True)
class ArtifactClass:
    """The public classification of one recognized evidence file."""

    kind: str
    media_type: str
    structured: bool


# The public allowlist. Nothing outside this mapping is ever returned.
PUBLIC_ARTIFACT_KINDS: dict[str, str] = {
    "final_result": "The controller's merged final task result.",
    "implementation_result": "The schema-constrained result of an implementation phase.",
    "review_result": "The result of an independent review phase.",
    "repair_summary": "The schema-constrained result of a repair phase.",
    "patch": "The unified diff the task produced.",
    "validation_log": "Captured output of a declared validation command.",
}

# Recognized-but-withheld classifications, documented so consumers know the gap is deliberate.
EXCLUDED_ARTIFACT_KINDS: dict[str, str] = {
    "model_prompt": "Raw model prompt text, which embeds controller paths and prior failure text.",
    "model_event_stream": "Unrestricted JSONL model event stream, including thread identifiers.",
    "model_stderr": "Unrestricted model process stderr, which is not classified as safe output.",
    "unclassified": "A file the public classification policy does not positively recognize.",
}

_STRUCTURED_CLASSES = {
    "final_result": ArtifactClass("final_result", "application/json", True),
    "implementation_result": ArtifactClass("implementation_result", "application/json", True),
    "review_result": ArtifactClass("review_result", "application/json", True),
    "repair_summary": ArtifactClass("repair_summary", "application/json", True),
}
_PATCH_CLASS = ArtifactClass("patch", "text/x-diff", False)
_VALIDATION_LOG_CLASS = ArtifactClass("validation_log", "text/plain", False)


@dataclass(frozen=True, slots=True)
class EvidenceArtifact:
    """One discovered, publishable artifact and the metadata the manifest reports."""

    artifact_id: str
    relative_path: str
    absolute_path: Path
    attempt: int
    phase: str | None
    classification: ArtifactClass
    size_bytes: int
    modified_at: str | None

    @property
    def display_name(self) -> str:
        return self.relative_path


# ---------------------------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------------------------

# Assignments are only redacted when the *name* looks credential-bearing. A blanket `name=value`
# rule would destroy ordinary validation output such as `exit_code=0` and violate the promise that
# line structure stays useful.
_CREDENTIAL_NAME = (
    r"(?:api[_-]?keys?|secrets?|tokens?|passwords?|passwd|pwd|credentials?|"
    r"authorization|auth[_-]?token|access[_-]?key|private[_-]?key|client[_-]?secret|"
    r"refresh[_-]?token|session[_-]?key|passphrase)"
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    rf"(?P<prefix>(?:^|[\s;&,{{\[])[\"']?[A-Za-z0-9_.-]*{_CREDENTIAL_NAME}[A-Za-z0-9_.-]*[\"']?"
    r"\s*(?:[:=]|=>)\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s;&,}\]\r\n]+)",
    re.IGNORECASE,
)
# A quoted value is matched as the quoted string so JSON and TOML structure survives; an unquoted
# value runs to end of line so `Authorization: Bearer <token>` is redacted whole.
_AUTHORIZATION_HEADER = re.compile(
    r"(?P<prefix>[\"']?\bauthorization\b[\"']?\s*[:=]\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n]+)",
    re.IGNORECASE,
)
_BEARER_TOKEN = re.compile(
    r"(?P<prefix>\b(?:bearer|basic|token)\s+)(?P<value>[A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)
# These formats are distinctive enough to match anywhere, deliberately without a leading `\b`.
# Requiring a word boundary would miss a secret that is concatenated directly onto preceding text,
# which is exactly what happens when a log line runs into a token with no separator.
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}")
_KNOWN_SECRET_FORMATS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"ya29\.[A-Za-z0-9._-]{10,}"),
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    re.compile(r"AIza[A-Za-z0-9_-]{20,}"),
)
_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PRIVATE_KEY_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY-----")


def _replace_value(match: re.Match[str]) -> str:
    """Replace a matched value, leaving an already-redacted one alone.

    Several patterns overlap by design. Without this guard a second pattern would match the marker
    left by the first and emit a mangled value such as `[REDACTED]]`.
    """
    if match.group("value").startswith(_REDACTED[:-1]):
        return match.group(0)
    return f"{match.group('prefix')}{_REDACTED}"


def _redact_line(line: str) -> str:
    redacted = _AUTHORIZATION_HEADER.sub(_replace_value, line)
    redacted = _CREDENTIAL_ASSIGNMENT.sub(_replace_value, redacted)
    redacted = redact_secret_arguments(redacted)
    redacted = _BEARER_TOKEN.sub(_replace_value, redacted)
    redacted = _JWT.sub(_REDACTED, redacted)
    for pattern in _KNOWN_SECRET_FORMATS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


def redact_text(text: str) -> tuple[str, bool]:
    """Redact secret-shaped values while preserving line structure.

    Returns the redacted text and whether anything changed. Substitution happens line by line so a
    pattern can never consume a newline and collapse two lines into one.
    """
    lines = text.split("\n")
    output: list[str] = []
    inside_private_key = False
    for line in lines:
        if inside_private_key:
            if _PRIVATE_KEY_END.search(line):
                inside_private_key = False
                output.append(_PRIVATE_KEY_END.sub("-----END PRIVATE KEY-----", line))
            else:
                output.append(_REDACTED if line.strip() else line)
            continue
        if _PRIVATE_KEY_BEGIN.search(line):
            inside_private_key = True
            output.append(_PRIVATE_KEY_BEGIN.sub("-----BEGIN PRIVATE KEY-----", line))
            continue
        output.append(_redact_line(line))
    redacted = "\n".join(output)
    return redacted, redacted != text


def _path_scrubbers(config: ForgeConfig) -> list[tuple[re.Pattern[str], str]]:
    """Build replacements that hide production absolute roots without touching relative paths."""
    roots: list[tuple[Path, str]] = [
        (config.evidence_root, "<evidence-root>"),
        (config.workspace_root, "<workspace-root>"),
        (config.repository_root, "<repository-root>"),
        (config.knowledge_root, "<knowledge-root>"),
        (config.inbox_root, "<inbox-root>"),
        (config.data_root, "<data-root>"),
        (config.config_path.parent, "<forge-root>"),
    ]
    scrubbers: list[tuple[re.Pattern[str], str]] = []
    # Longest first so a nested root is replaced before the parent that contains it.
    for root, placeholder in sorted(roots, key=lambda item: len(str(item[0])), reverse=True):
        text = str(root).rstrip("\\/")
        if not text:
            continue
        pattern = re.escape(text).replace("/", "[\\\\/]").replace("\\\\", "[\\\\/]")
        flags = re.IGNORECASE if os.name == "nt" else 0
        scrubbers.append((re.compile(pattern, flags), placeholder))
    return scrubbers


def scrub_paths(text: str, scrubbers: Sequence[tuple[re.Pattern[str], str]]) -> str:
    for pattern, placeholder in scrubbers:
        text = pattern.sub(placeholder, text)
    return text


# ---------------------------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------------------------


def classify_attempt_file(name: str) -> ArtifactClass | None:
    """Classify a file that sits directly in an attempt directory."""
    if name == "task-result.json":
        return _STRUCTURED_CLASSES["final_result"]
    if name == "changes.patch":
        return _PATCH_CLASS
    return None


def classify_group_file(group: str, name: str) -> ArtifactClass | None:
    """Classify a file inside a phase or validation group directory."""
    if _VALIDATION_LOG.match(name):
        # A declared validation command's captured output. Both streams are classified evidence:
        # they exist only because an operator registered the command, and the failing stream is
        # usually the one that explains a validation failure. This is distinct from the model
        # process's own `<group>.stderr.log`, which stays excluded.
        if _VALIDATION_GROUP.match(group) or _REPAIR_GROUP.match(group):
            return _VALIDATION_LOG_CLASS
        return None
    if name == f"{group}.final.json":
        if _IMPLEMENTATION_GROUP.match(group):
            return _STRUCTURED_CLASSES["implementation_result"]
        if _REPAIR_GROUP.match(group):
            return _STRUCTURED_CLASSES["repair_summary"]
        if _REVIEW_GROUP.match(group):
            return _STRUCTURED_CLASSES["review_result"]
        return None
    return None


def excluded_kind(group: str | None, name: str) -> str:
    """Name the exclusion reason for a file the allowlist refuses. Diagnostic only."""
    if group is not None:
        if name == f"{group}.prompt.txt":
            return "model_prompt"
        if name == f"{group}.events.jsonl":
            return "model_event_stream"
        if name == f"{group}.stderr.log":
            return "model_stderr"
    return "unclassified"


# ---------------------------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------------------------


def _safe_component(value: str) -> bool:
    """Reject a path component that could traverse, escape, or name an alternate data stream."""
    if not value or value in {".", ".."}:
        return False
    return not any(character in _UNSAFE_NAME_CHARACTERS for character in value)


def _is_reparse_point(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _is_plain_directory(info: os.stat_result) -> bool:
    return stat.S_ISDIR(info.st_mode) and not _is_reparse_point(info)


def _is_plain_file(info: os.stat_result) -> bool:
    """A regular file only. Directories, devices, FIFOs, sockets, and reparse points are refused."""
    return stat.S_ISREG(info.st_mode) and not _is_reparse_point(info)


def _within(candidate: Path, root: Path) -> bool:
    """Confirm a fully resolved candidate still lives under a fully resolved root."""
    try:
        return candidate == root or candidate.is_relative_to(root)
    except (OSError, ValueError):
        return False


def _resolve_within(candidate: Path, root: Path) -> Path:
    """Canonicalize a candidate and prove it did not escape the evidence root."""
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise EvidenceError(
            "FILESYSTEM_ERROR",
            "The evidence path could not be resolved.",
            {"scope": "artifact"},
        ) from exc
    if not _within(resolved, resolved_root):
        raise EvidenceError(
            "ARTIFACT_OUTSIDE_EVIDENCE_ROOT",
            "The artifact resolved outside the task evidence root and was refused.",
            {"scope": "artifact"},
        )
    return resolved


def task_evidence_root(config: ForgeConfig, project_name: str, task_id: str) -> Path:
    """Derive the task's evidence root. Callers never supply any part of this path."""
    if not _safe_component(project_name) or not _safe_component(task_id):
        raise EvidenceError(
            "ARTIFACT_OUTSIDE_EVIDENCE_ROOT",
            "The stored project or task identifier is not a safe path component.",
            {"task_id": task_id},
        )
    root = config.evidence_root / project_name / task_id
    # The join is derived entirely from controller state, but containment is still proven rather
    # than assumed, so an unexpected stored name cannot reach outside the evidence tree.
    if not _within(Path(os.path.normpath(root)), Path(os.path.normpath(config.evidence_root))):
        raise EvidenceError(
            "ARTIFACT_OUTSIDE_EVIDENCE_ROOT",
            "The derived task evidence root fell outside the configured evidence root.",
            {"task_id": task_id},
        )
    return root


def compute_artifact_id(task_id: str, relative_path: str) -> str:
    """Return a deterministic, opaque identifier for one artifact of one task.

    The identifier is a truncated SHA-256 over the task id and the artifact's relative path. It is
    stable across runs and across processes, and it discloses no path information. It is never
    decoded: `show` recomputes the manifest and matches the identifier against it.
    """
    digest = hashlib.sha256(f"{task_id}\x00{relative_path}".encode("utf-8")).hexdigest()
    return f"{_ARTIFACT_ID_PREFIX}{digest[:_ARTIFACT_ID_HEX]}"


# ---------------------------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------------------------


def _scandir(directory: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as entries:
            return sorted(entries, key=lambda entry: entry.name)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise EvidenceError(
            "FILESYSTEM_ERROR",
            "The evidence directory could not be read.",
            {"scope": "directory"},
        ) from exc


def _entry_stat(entry: os.DirEntry[str]) -> os.stat_result | None:
    try:
        # follow_symlinks=False: a symlink or reparse point is inspected as itself, never followed.
        return entry.stat(follow_symlinks=False)
    except OSError:
        return None


def _iso_mtime(info: os.stat_result) -> str | None:
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(info.st_mtime, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _discover(task_root: Path, task_id: str) -> Iterator[EvidenceArtifact]:
    """Walk the task evidence tree at bounded depth, yielding only publishable artifacts."""
    resolved_root = task_root.resolve()
    for attempt_entry in _scandir(task_root):
        match = _ATTEMPT_DIRECTORY.match(attempt_entry.name)
        if not match or not _safe_component(attempt_entry.name):
            continue
        attempt_info = _entry_stat(attempt_entry)
        if attempt_info is None or not _is_plain_directory(attempt_info):
            continue
        attempt = int(match.group("number"))
        attempt_path = Path(attempt_entry.path)

        for entry in _scandir(attempt_path):
            if not _safe_component(entry.name):
                continue
            info = _entry_stat(entry)
            if info is None:
                continue
            if _is_plain_file(info):
                classification = classify_attempt_file(entry.name)
                if classification is None:
                    continue
                artifact = _build_artifact(
                    Path(entry.path),
                    resolved_root,
                    task_id,
                    attempt,
                    None,
                    classification,
                    info,
                )
                if artifact is not None:
                    yield artifact
                continue
            if not _is_plain_directory(info):
                continue

            group = entry.name
            for leaf in _scandir(Path(entry.path)):
                if not _safe_component(leaf.name):
                    continue
                leaf_info = _entry_stat(leaf)
                if leaf_info is None or not _is_plain_file(leaf_info):
                    continue
                classification = classify_group_file(group, leaf.name)
                if classification is None:
                    continue
                artifact = _build_artifact(
                    Path(leaf.path),
                    resolved_root,
                    task_id,
                    attempt,
                    group,
                    classification,
                    leaf_info,
                )
                if artifact is not None:
                    yield artifact


def _build_artifact(
    path: Path,
    resolved_root: Path,
    task_id: str,
    attempt: int,
    group: str | None,
    classification: ArtifactClass,
    info: os.stat_result,
) -> EvidenceArtifact | None:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    if not _within(resolved, resolved_root):
        # A discovered entry that canonicalizes outside the root is dropped silently rather than
        # reported: it is not part of this task's evidence and must not appear in the manifest.
        return None
    try:
        relative = resolved.relative_to(resolved_root).as_posix()
    except ValueError:
        return None
    return EvidenceArtifact(
        artifact_id=compute_artifact_id(task_id, relative),
        relative_path=relative,
        absolute_path=resolved,
        attempt=attempt,
        phase=group,
        classification=classification,
        size_bytes=info.st_size,
        modified_at=_iso_mtime(info),
    )


def discover_artifacts(task_root: Path, task_id: str) -> list[EvidenceArtifact]:
    """Return every publishable artifact for a task in deterministic order."""
    artifacts = list(_discover(task_root, task_id))
    artifacts.sort(key=lambda item: (item.attempt, item.relative_path))
    return artifacts


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArtifactBytes:
    """The artifact's complete bytes, bounded by `MAX_READABLE_BYTES`.

    The whole artifact is retained rather than only the returnable prefix, because redaction must
    see complete secrets. Truncating first would let a secret that straddles the return limit
    survive as an unmatched — and therefore unredacted — prefix.
    """

    data: bytes
    original_size_bytes: int
    sha256: str


def _read_bounded(path: Path) -> ArtifactBytes:
    """Read an artifact with a hard bound, proving it did not change underneath us.

    The file is opened for shared reading only and is never locked, so the controller stays free to
    keep writing evidence. Metadata is captured from the same descriptor before and after the read;
    any difference is reported as a retryable error instead of a confident but stale answer.
    """
    try:
        link_info = path.lstat()
    except FileNotFoundError as exc:
        raise EvidenceError(
            "ARTIFACT_NOT_FOUND",
            "The artifact no longer exists.",
            {"scope": "artifact"},
        ) from exc
    except OSError as exc:
        raise EvidenceError(
            "FILESYSTEM_ERROR",
            "The artifact could not be inspected.",
            {"scope": "artifact"},
        ) from exc
    if not _is_plain_file(link_info):
        raise EvidenceError(
            "ARTIFACT_UNSAFE_TYPE",
            "The artifact is not a regular file.",
            {"scope": "artifact"},
        )

    try:
        handle = path.open("rb")
    except FileNotFoundError as exc:
        raise EvidenceError(
            "ARTIFACT_NOT_FOUND",
            "The artifact no longer exists.",
            {"scope": "artifact"},
        ) from exc
    except OSError as exc:
        raise EvidenceError(
            "FILESYSTEM_ERROR",
            "The artifact could not be opened.",
            {"scope": "artifact"},
        ) from exc

    with handle:
        try:
            before = os.fstat(handle.fileno())
            if not _is_plain_file(before):
                raise EvidenceError(
                    "ARTIFACT_UNSAFE_TYPE",
                    "The artifact is not a regular file.",
                    {"scope": "artifact"},
                )
            if not _same_file(link_info, before):
                raise EvidenceError(
                    "ARTIFACT_CHANGED_DURING_READ",
                    "The artifact could not be proven unchanged while it was opened. Retry the request.",
                    {"scope": "artifact", "retryable": True},
                )
            if before.st_size > MAX_READABLE_BYTES:
                raise EvidenceError(
                    "ARTIFACT_TOO_LARGE",
                    "The artifact exceeds the maximum readable size.",
                    {
                        "original_size_bytes": before.st_size,
                        "max_readable_bytes": MAX_READABLE_BYTES,
                    },
                )

            digest = hashlib.sha256()
            kept = bytearray()
            total = 0
            while True:
                chunk = handle.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_READABLE_BYTES:
                    raise EvidenceError(
                        "ARTIFACT_TOO_LARGE",
                        "The artifact grew past the maximum readable size while being read.",
                        {"max_readable_bytes": MAX_READABLE_BYTES},
                    )
                digest.update(chunk)
                kept.extend(chunk)
            after = os.fstat(handle.fileno())
        except OSError as exc:
            raise EvidenceError(
                "FILESYSTEM_ERROR",
                "The artifact could not be read.",
                {"scope": "artifact"},
            ) from exc

    if _changed(before, after) or total != before.st_size:
        raise EvidenceError(
            "ARTIFACT_CHANGED_DURING_READ",
            "The artifact changed while it was being read. Retry the request.",
            {"scope": "artifact", "retryable": True},
        )
    return ArtifactBytes(data=bytes(kept), original_size_bytes=total, sha256=digest.hexdigest())


def _identity(info: os.stat_result) -> tuple[int, int] | None:
    """Return the file's (device, inode) identity, or None if the filesystem does not supply one."""
    if info.st_dev and info.st_ino:
        return (info.st_dev, info.st_ino)
    return None


def _same_file(link_info: os.stat_result, opened: os.stat_result) -> bool:
    """Prove an open descriptor refers to the file that was inspected before it was opened.

    Inspecting and opening cannot be made atomic, so the check is retroactive: a regular file
    swapped for a symlink or reparse point between the two calls is detected because `open` follows
    the link and lands on a different (device, inode).

    Identity is unavailable on a filesystem that reports zeroed device and inode numbers. The swap
    then cannot be disproved, so this reports failure rather than silently skipping the check —
    a security control must not disable itself based on where the evidence root happens to live.
    """
    inspected = _identity(link_info)
    return inspected is not None and inspected == _identity(opened)


def _changed(before: os.stat_result, after: os.stat_result) -> bool:
    return (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)


def _decode_utf8(data: bytes) -> str:
    """Decode the artifact's complete bytes as UTF-8."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError(
            "ARTIFACT_INVALID_UTF8",
            "The artifact is not valid UTF-8 text.",
            {"scope": "artifact"},
        ) from exc


def _truncate_utf8(text: str, limit: int) -> tuple[str, bool]:
    """Bound text to `limit` UTF-8 bytes, backing off so a multi-byte character is never split.

    Truncation is applied last, after redaction and sanitization, so a secret spanning the limit is
    already replaced before the boundary is applied.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    clipped = encoded[:limit]
    for trim in range(0, 4):
        try:
            return clipped[: len(clipped) - trim].decode("utf-8"), True
        except UnicodeDecodeError:
            continue
    return "", True


def _reject_binary(data: bytes) -> None:
    if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
        raise EvidenceError(
            "ARTIFACT_UNSAFE_TYPE",
            "The artifact contains binary content and is not served by this contract.",
            {"scope": "artifact"},
        )


# ---------------------------------------------------------------------------------------------
# Structured sanitization
# ---------------------------------------------------------------------------------------------

_DECISION_FIELDS = ("question", "options", "recommendation")
_FINDING_FIELDS = ("severity", "file", "line", "issue", "required_fix")
_PATTERN_FIELDS = ("trigger", "lesson")
_VALIDATION_FIELDS = ("command", "exit_code", "passed", "duration_seconds")


_MAX_SANITIZE_DEPTH = 8
# The sanitized projection is re-serialized into the response, so its cardinality is bounded too.
# Without this a legitimate document with thousands of findings could serialize past the return
# limit even though the file on disk was within it.
_MAX_COLLECTION_ITEMS = 500


def _clean(value: Any, sanitize: Any, depth: int = 0) -> Any:
    """Sanitize a JSON value of any shape.

    An allowlisted field is documented as a string, but the document is model-produced and its
    schema is not re-validated here. A nested object or array in a string field would otherwise
    reach the caller with neither redaction nor path scrubbing applied, so every string is
    sanitized at every depth and anything deeper than `_MAX_SANITIZE_DEPTH` is dropped.
    """
    if isinstance(value, str):
        return sanitize(value)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if depth >= _MAX_SANITIZE_DEPTH:
        return None
    if isinstance(value, list):
        return [_clean(item, sanitize, depth + 1) for item in value]
    if isinstance(value, dict):
        return {
            sanitize(str(key)): _clean(item, sanitize, depth + 1) for key, item in value.items()
        }
    return None


def _string_list(value: Any, sanitize: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [sanitize(item) for item in value if isinstance(item, str)]


def _object_list(value: Any, fields: Sequence[str], sanitize: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {}
        for field_name in fields:
            raw = item.get(field_name)
            if isinstance(raw, list):
                entry[field_name] = _string_list(raw, sanitize)
            else:
                entry[field_name] = _clean(raw, sanitize)
        cleaned.append(entry)
        if len(cleaned) >= _MAX_COLLECTION_ITEMS:
            break
    return cleaned


def _sanitize_task_result(document: Mapping[str, Any], sanitize: Any) -> dict[str, Any]:
    """Allowlist the task-result shape. `worktree`, `patch`, and per-run log paths are dropped."""
    return {
        "status": _clean(document.get("status"), sanitize),
        "summary": _clean(document.get("summary"), sanitize),
        "changed_files": _string_list(document.get("changed_files"), sanitize),
        "validation_performed": _string_list(document.get("validation_performed"), sanitize),
        "assumptions": _string_list(document.get("assumptions"), sanitize),
        "blocking_reason": _clean(document.get("blocking_reason"), sanitize),
        "decisions_needed": _object_list(document.get("decisions_needed"), _DECISION_FIELDS, sanitize),
        "reusable_patterns": _object_list(document.get("reusable_patterns"), _PATTERN_FIELDS, sanitize),
    }


def _sanitize_review_result(document: Mapping[str, Any], sanitize: Any) -> dict[str, Any]:
    return {
        "verdict": _clean(document.get("verdict"), sanitize),
        "summary": _clean(document.get("summary"), sanitize),
        "findings": _object_list(document.get("findings"), _FINDING_FIELDS, sanitize),
    }


def _sanitize_final_result(document: Mapping[str, Any], sanitize: Any) -> dict[str, Any]:
    payload = _sanitize_task_result(document, sanitize)
    review = document.get("review")
    payload["review"] = (
        _sanitize_review_result(review, sanitize) if isinstance(review, dict) else None
    )
    payload["controller_validation"] = _object_list(
        document.get("controller_validation"), _VALIDATION_FIELDS, sanitize
    )
    payload["branch"] = _clean(document.get("branch"), sanitize)
    payload["commit"] = _clean(document.get("commit"), sanitize)
    payload["completed_at"] = _clean(document.get("completed_at"), sanitize)
    return payload


def sanitize_structured(kind: str, document: Any, sanitize: Any) -> dict[str, Any] | None:
    """Return an allowlisted view of a recognized controller document, or None if unrecognized."""
    if not isinstance(document, dict):
        return None
    if kind == "final_result":
        return _sanitize_final_result(document, sanitize)
    if kind == "review_result":
        return _sanitize_review_result(document, sanitize)
    if kind in {"implementation_result", "repair_summary"}:
        return _sanitize_task_result(document, sanitize)
    return None


def _format_structured(kind: str, payload: Mapping[str, Any]) -> str:
    lines: list[str] = []
    if kind == "review_result":
        lines.append(f"Verdict: {payload.get('verdict')}")
        lines.append(f"Summary: {payload.get('summary')}")
        findings = payload.get("findings") or []
        lines.append(f"Findings: {len(findings)}")
        for finding in findings:
            location = finding.get("file") or "(unspecified)"
            line_number = finding.get("line")
            suffix = f":{line_number}" if line_number is not None else ""
            lines.append(f"- [{finding.get('severity')}] {location}{suffix} {finding.get('issue')}")
            lines.append(f"  Required fix: {finding.get('required_fix')}")
        return "\n".join(lines)

    lines.append(f"Status: {payload.get('status')}")
    lines.append(f"Summary: {payload.get('summary')}")
    changed = payload.get("changed_files") or []
    lines.append(f"Changed files: {len(changed)}")
    lines.extend(f"- {item}" for item in changed)
    if payload.get("blocking_reason"):
        lines.append(f"Blocking reason: {payload['blocking_reason']}")
    validation = payload.get("controller_validation") or []
    if validation:
        lines.append(f"Controller validation: {len(validation)}")
        for item in validation:
            state = "passed" if item.get("passed") else "failed"
            lines.append(f"- {state} (exit {item.get('exit_code')}) {item.get('command')}")
    review = payload.get("review")
    if isinstance(review, dict):
        lines.append(f"Review verdict: {review.get('verdict')}")
    if payload.get("branch"):
        lines.append(f"Branch: {payload['branch']}")
    if payload.get("commit"):
        lines.append(f"Commit: {payload['commit']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------------------------


def _require_schema(schema_version: str | None) -> None:
    if schema_version is not None and schema_version != SCHEMA_VERSION:
        raise EvidenceError(
            "UNSUPPORTED_SCHEMA",
            "The requested contract schema version is not supported.",
            {"requested": schema_version, "supported": [SCHEMA_VERSION]},
        )


def _load_task(config: ForgeConfig, task_id: str) -> tuple[Task, str | None]:
    if not config.database_path.exists():
        raise EvidenceError(
            "CONFIGURATION_ERROR",
            "Forge is configured but not initialized. Run `forge init`.",
            {"task_id": task_id},
        )
    try:
        # ForgeDB's constructor creates the parent directory, so it is guarded too.
        db = ForgeDB(config.database_path)
        task = db.get_task(task_id)
        project_name = None
        if task is not None:
            project = db.get_project(task.project_id)
            project_name = project.name if project else None
    except OSError as exc:
        raise EvidenceError(
            "FILESYSTEM_ERROR",
            "The Forge database location could not be accessed.",
            {"task_id": task_id},
        ) from exc
    except (sqlite3.Error, ValueError) as exc:
        # ValueError covers a persisted row that no longer maps onto a known enum value, which is a
        # database-content problem rather than a bug to surface as a traceback.
        raise EvidenceError(
            "DATABASE_ERROR",
            "The Forge database could not be read.",
            {"task_id": task_id},
        ) from exc
    if task is None:
        raise EvidenceError(
            "TASK_NOT_FOUND",
            "Task was not found.",
            {"task_id": task_id},
        )
    return task, project_name


def _limits() -> dict[str, int]:
    return {
        "max_returned_bytes": MAX_ARTIFACT_BYTES,
        "max_readable_bytes": MAX_READABLE_BYTES,
    }


@dataclass(frozen=True, slots=True)
class _Probe:
    """The result of one bounded, non-blocking metadata pass over an artifact."""

    sha256: str | None
    readable: bool


def _probe(artifact: EvidenceArtifact) -> _Probe:
    """Hash an artifact and confirm it is servable, without ever claiming an unstable result.

    The manifest is built while the controller may still be writing. If the file changes underneath
    this pass, `sha256` is reported as `null` rather than as a hash that no longer describes any
    version of the file.

    Discovery already refused links, but discovery and probing are separate passes. The link and
    identity checks are repeated here so an artifact swapped for a symlink in between is not opened
    and hashed — otherwise the manifest could publish the hash of a file outside the evidence root.
    """
    if artifact.size_bytes > MAX_READABLE_BYTES:
        return _Probe(None, False)
    digest = hashlib.sha256()
    text = True
    total = 0
    try:
        link_info = artifact.absolute_path.lstat()
        if not _is_plain_file(link_info):
            return _Probe(None, False)
        with artifact.absolute_path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not _is_plain_file(before) or not _same_file(link_info, before):
                return _Probe(None, False)
            first = True
            while True:
                chunk = handle.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_READABLE_BYTES:
                    return _Probe(None, False)
                if first:
                    text = b"\x00" not in chunk[:_BINARY_SNIFF_BYTES]
                    first = False
                digest.update(chunk)
            after = os.fstat(handle.fileno())
    except OSError:
        return _Probe(None, False)
    if _changed(before, after) or total != before.st_size:
        return _Probe(None, text)
    return _Probe(digest.hexdigest(), text)


def _artifact_to_public(artifact: EvidenceArtifact, probe: _Probe | None = None) -> dict[str, Any]:
    readable = artifact.size_bytes <= MAX_READABLE_BYTES
    return {
        "artifact_id": artifact.artifact_id,
        "artifact_kind": artifact.classification.kind,
        "display_name": artifact.display_name,
        "media_type": artifact.classification.media_type,
        "attempt": artifact.attempt,
        "phase": artifact.phase,
        "original_size_bytes": artifact.size_bytes,
        "sha256": probe.sha256 if probe else None,
        "modified_at": artifact.modified_at,
        "content_available": (probe.readable if probe else readable),
        "truncated_when_read": readable and artifact.size_bytes > MAX_ARTIFACT_BYTES,
        "redaction_required": True,
        "structured": artifact.classification.structured,
    }


def evidence_list(
    config: ForgeConfig,
    task_id: str,
    *,
    schema_version: str | None = None,
) -> dict[str, Any]:
    """Build the safe evidence manifest for one task."""
    _require_schema(schema_version)
    task, project_name = _load_task(config, task_id)

    artifacts: list[EvidenceArtifact] = []
    evidence_available = False
    if project_name is not None:
        root = task_evidence_root(config, project_name, task_id)
        if root.is_dir():
            artifacts = discover_artifacts(root, task_id)
            evidence_available = True

    public = [_artifact_to_public(artifact, _probe(artifact)) for artifact in artifacts]
    kinds = {artifact.classification.kind for artifact in artifacts}
    attempts = sorted({artifact.attempt for artifact in artifacts})
    return {
        "task_id": task.id,
        "project": project_name,
        "task_state": task.state.value,
        "evidence_available": evidence_available,
        "attempt_count": task.attempts,
        "attempts_with_evidence": attempts,
        "artifact_count": len(public),
        "availability": {
            "final_result": "final_result" in kinds,
            "validation_result": "validation_log" in kinds,
            "review_result": "review_result" in kinds,
            "patch": "patch" in kinds,
            "repair_summary": "repair_summary" in kinds,
            "implementation_result": "implementation_result" in kinds,
        },
        "artifacts": public,
        "limits": _limits(),
    }


def evidence_show(
    config: ForgeConfig,
    task_id: str,
    artifact_id: str,
    *,
    schema_version: str | None = None,
) -> dict[str, Any]:
    """Return one bounded, redacted, sanitized artifact selected by opaque identifier."""
    _require_schema(schema_version)
    if not _ARTIFACT_ID_PATTERN.match(artifact_id or ""):
        # The identifier is never interpreted as a path, so a traversal or absolute-path attempt
        # simply fails this shape check and is reported as an unknown artifact.
        raise EvidenceError(
            "ARTIFACT_NOT_FOUND",
            "The artifact identifier is not a known artifact for this task.",
            {"task_id": task_id},
        )
    task, project_name = _load_task(config, task_id)
    if project_name is None:
        raise EvidenceError(
            "EVIDENCE_UNAVAILABLE",
            "The task's project is no longer registered, so its evidence cannot be located.",
            {"task_id": task_id},
        )

    root = task_evidence_root(config, project_name, task_id)
    if not root.is_dir():
        raise EvidenceError(
            "EVIDENCE_NOT_FOUND",
            "No evidence has been recorded for this task.",
            {"task_id": task_id},
        )

    artifacts = discover_artifacts(root, task_id)
    selected = next((item for item in artifacts if item.artifact_id == artifact_id), None)
    if selected is None:
        # An excluded artifact is indistinguishable from a missing one by identifier alone, so the
        # caller learns only that the identifier is not public for this task.
        raise EvidenceError(
            "ARTIFACT_NOT_FOUND",
            "The artifact identifier is not a known artifact for this task.",
            {"task_id": task_id, "artifact_id": artifact_id},
        )

    # Re-prove containment immediately before reading, not only at discovery time.
    resolved = _resolve_within(selected.absolute_path, root)

    raw = _read_bounded(resolved)
    _reject_binary(raw.data)
    # Decode, scrub, and redact the COMPLETE artifact before any bound is applied. Truncating first
    # would leave a secret that spans the return limit as an unmatched, unredacted prefix.
    text = _decode_utf8(raw.data)

    scrubbers = _path_scrubbers(config)

    def sanitize(value: str) -> str:
        return redact_text(scrub_paths(value, scrubbers))[0]

    scrubbed = scrub_paths(text, scrubbers)
    redacted_text, redacted = redact_text(scrubbed)
    redacted = redacted or scrubbed != text

    structured_data: dict[str, Any] | None = None
    structured_status = "not_structured"
    formatted_text: str | None = None
    if selected.classification.structured:
        try:
            document = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            structured_status = "invalid_json"
        except RecursionError:
            # A legally sized document can still nest deeply enough to exhaust the parser.
            structured_status = "unsupported_shape"
        else:
            structured_data = sanitize_structured(selected.classification.kind, document, sanitize)
            if structured_data is None:
                structured_status = "unsupported_shape"
            else:
                structured_status = "sanitized"
                formatted_text = _format_structured(selected.classification.kind, structured_data)

    # A recognized controller document is never returned verbatim: its text is the serialized
    # allowlisted projection, so a field outside the allowlist cannot reach the caller through the
    # content channel after being removed from `structured_data`.
    if structured_status == "sanitized":
        display_text = json.dumps(structured_data, indent=2, ensure_ascii=False)
        if len(display_text.encode("utf-8")) > MAX_ARTIFACT_BYTES:
            # Pretty-printing can expand a document past the return limit even when the file was
            # within it. Retry compactly, and if it still does not fit, fall back rather than
            # return JSON truncated into invalid syntax.
            display_text = json.dumps(structured_data, ensure_ascii=False, separators=(",", ":"))
        if len(display_text.encode("utf-8")) > MAX_ARTIFACT_BYTES:
            structured_data = None
            formatted_text = None
            structured_status = "unavailable_truncated"
            display_text = redacted_text
            representation = "redacted_text"
        else:
            representation = "sanitized_json"
            redacted = True
    else:
        display_text = redacted_text
        representation = "redacted_text"

    display_text, truncated = _truncate_utf8(display_text, MAX_ARTIFACT_BYTES)
    if truncated and formatted_text is not None:
        formatted_text = None
    if formatted_text is not None:
        formatted_text, _ = _truncate_utf8(formatted_text, MAX_ARTIFACT_BYTES)

    manifest_entry = _artifact_to_public(selected)
    manifest_entry["sha256"] = raw.sha256
    manifest_entry["original_size_bytes"] = raw.original_size_bytes
    manifest_entry["truncated_when_read"] = truncated

    return {
        "task_id": task.id,
        "project": project_name,
        "task_state": task.state.value,
        "artifact": manifest_entry,
        "content": {
            "encoding": "utf-8",
            "text": display_text,
            "representation": representation,
            "original_size_bytes": raw.original_size_bytes,
            "returned_size_bytes": len(display_text.encode("utf-8")),
            "truncated": truncated,
            "redacted": redacted,
            "sha256": raw.sha256,
            "complete": not truncated,
        },
        "structured_status": structured_status,
        "structured_data": structured_data,
        "formatted_text": formatted_text,
        "limits": _limits(),
    }
