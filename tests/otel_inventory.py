"""Gate 6 field inventory: what a Claude canary actually exports, after the certified privacy pipeline.

Gate 5 answered *how many* spans survived. Gate 6 asks a different question, and a harder one: for
**every** field on **every** resource, scope, span, event, and metric that survived, what is it, what
type is it, what class of value does it carry, how often does it appear, and has a human approved it?

Three properties make that question answerable without recreating the risk it exists to manage:

**1. The inventory has no input but the debug exporter's own output.** It opens no socket, declares no
receiver, adds no exporter, and creates no pipeline. :func:`inventory_configuration` returns the
shipped Collector configuration plus the bounded span-name diagnostic and *nothing else* — the same
configuration a Gate 5 canary runs. The only thing this module reads is the text the `debug` exporter
printed at the end of the `traces` and `metrics` pipelines, whose processor chain is
`[memory_limiter, filter/unredactable, transform/privacy, batch]`. There is no receiver-to-inventory
route that bypasses those processors because there is no receiver-to-inventory route at all.

**2. Values are classified where they are read and then discarded.** :func:`parse_export` never stores
an attribute value, a unit, or a description. It hands each one to :func:`classify_value`, keeps the
resulting bounded class, and keeps the value itself only when the approval manifest lists that exact
value as safe to print. Nothing downstream — not the JSON report, not the Markdown report, not the
candidate manifest, not an exception message — has the original to leak.

**3. Schema identifiers are reviewable, one at a time.** A *schema identifier* is a name in the
telemetry's shape rather than in its payload: an attribute key, a scope name, a span name, an event
name, or a metric name. Nine categories, listed in :data:`IDENTIFIER_CATEGORIES`, and no others. An
unknown identifier gets **its own inventory entry**, never a shared bucket, and is printed exactly
when it passes the safe-identifier policy in :func:`assess_identifier`. When it does not, the entry
carries a stable SHA-256 fingerprint and its category instead, is classed `UNPRINTABLE_UNKNOWN`, and
fails Gate 6.

That is what makes the reports self-sufficient: a reviewer approves new fields from
`candidate-manifest.yaml`, which this harness writes, without reading the raw Collector log and
without editing Python. Descriptions and units are **values**, not identifiers, and stay normalized
or allowlisted however safe the metric name carrying them turns out to be.

**Observed is not approved.** This module produces the observed inventory. The manifest records the
approvals. They are separate files with separate lifecycles, and a Gate 6 pass requires the manifest
to carry a human's name — which the shipped candidate manifest deliberately does not.

**This module starts no agent by itself.** Like :mod:`otel_canary` it refuses to launch one without
`--approved-by`, and the argument vector it would launch is a constant in this file
(:data:`DEFAULT_CLAUDE_ARGV`) rather than a string assembled on a command line.

**Everything above the verdict is agent-independent, and Gate 8 reuses it.**
:mod:`otel_codex_inventory` inventories Codex through this parser, this classifier, this identifier
policy, and this comparison, supplying only its own manifest, its own run facts, and its own
conditions. Three parameters exist for that and no other reason: :func:`load_manifest` takes the
schema a caller expects, and :func:`build_document` takes the report schema, the gate key, and the
verdict function. Their defaults are Claude's, so Gate 6 reads exactly as it did.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

import otel_canary as canary
import otel_certification as certification

# The report format. A consumer that does not understand this string must not read the file, and a
# change to the shape of the document below is a change to this literal.
SCHEMA_VERSION = "forge.claude.field-inventory/2"
MANIFEST_SCHEMA = "forge.claude.field-approvals/1"

MANIFEST_PATH = certification.REPO_ROOT / "ops" / "otel" / "claude-field-approvals.yaml"

REPORT_JSON_NAME = "inventory-report.json"
REPORT_MARKDOWN_NAME = "inventory-report.md"
CANDIDATE_MANIFEST_NAME = "candidate-manifest.yaml"
# The only three names this harness will ever delete, and only with --replace-existing. An output
# directory holding anything else is somebody's data, not a stale run of this tool.
OWNED_REPORT_NAMES = (REPORT_JSON_NAME, REPORT_MARKDOWN_NAME, CANDIDATE_MANIFEST_NAME)


# ---------------------------------------------------------------------------
# Approval statuses and observed dispositions
# ---------------------------------------------------------------------------

# What a human may say about a field in the manifest.
STATUS_APPROVED = "APPROVED"
STATUS_BLOCKED = "BLOCKED"
STATUS_REDACTED = "REDACTED"
STATUS_UNAPPROVED = "UNAPPROVED"
MANIFEST_STATUSES = (STATUS_APPROVED, STATUS_BLOCKED, STATUS_REDACTED, STATUS_UNAPPROVED)

# Two further dispositions the *observation* can produce that no human can write down in advance.
# `UNKNOWN` is a field with no manifest entry at all; `UNRESOLVED` is output this parser could not
# interpret. Both fail closed, and both are visible rather than dropped.
DISPOSITION_UNKNOWN = "UNKNOWN"
DISPOSITION_UNRESOLVED = "UNRESOLVED"
DISPOSITIONS = MANIFEST_STATUSES + (DISPOSITION_UNKNOWN, DISPOSITION_UNRESOLVED)


# ---------------------------------------------------------------------------
# Normalization policy for values
# ---------------------------------------------------------------------------
#
# A class describes a value without reproducing it. Every class below except the two allowlisted ones
# emits *nothing* from the original: `lowercase-hex-32` says an invocation id was there, not which.
#
# `approved-static-string` and `approved-enum` are the only classes that may carry a value, and they
# reach it only through an explicit per-key allowlist in the manifest. That is the whole of the
# "static low-risk values may be emitted only through an explicit allowlist" rule.
#
# There is deliberately no `bounded-string` or `short-string` class. A free-form string that matches
# none of these is `unclassifiable`, which no approval may cover — because "it looked short and
# harmless" is how a prompt preview gets published.

CLASS_ABSENT = "absent"
CLASS_BOOLEAN = "boolean"
CLASS_INTEGER = "integer"
CLASS_NONNEGATIVE_INTEGER = "nonnegative-integer"
CLASS_DOUBLE = "double"
CLASS_NONNEGATIVE_DOUBLE = "nonnegative-double"
CLASS_HEX32 = "lowercase-hex-32"
CLASS_HEX16 = "lowercase-hex-16"
CLASS_SEMVER = "semantic-version"
CLASS_DURATION = "duration"
CLASS_TIMESTAMP_PRESENT = "timestamp-present"
CLASS_LOCALHOST_ENDPOINT = "localhost-endpoint"
CLASS_ENUM = "approved-enum"
CLASS_STATIC = "approved-static-string"
CLASS_UNCLASSIFIABLE = "unclassifiable"

VALUE_CLASSES = (
    CLASS_ABSENT,
    CLASS_BOOLEAN,
    CLASS_INTEGER,
    CLASS_NONNEGATIVE_INTEGER,
    CLASS_DOUBLE,
    CLASS_NONNEGATIVE_DOUBLE,
    CLASS_HEX32,
    CLASS_HEX16,
    CLASS_SEMVER,
    CLASS_DURATION,
    CLASS_TIMESTAMP_PRESENT,
    CLASS_LOCALHOST_ENDPOINT,
    CLASS_ENUM,
    CLASS_STATIC,
    CLASS_UNCLASSIFIABLE,
)

# The two classes, and the only two, whose entries may carry the original value into the report.
VALUE_BEARING_CLASSES = frozenset({CLASS_STATIC, CLASS_ENUM})

# Pseudo-keys under which a metric's unit and description are allowlisted. They are values, not
# identifiers: a safe metric *name* buys its unit and description nothing.
UNIT_KEY = "metric.unit"
DESCRIPTION_KEY = "metric.description"

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX16 = re.compile(r"[0-9a-f]{16}")
_SEMVER = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")
_INTEGER = re.compile(r"[-+]?\d+")
_DOUBLE = re.compile(r"[-+]?\d+\.\d+(?:[eE][-+]?\d+)?")
_DURATION = re.compile(r"\d+(?:\.\d+)?(?:ns|us|ms|s|m|h)")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[ T][0-9:.+\- ]+(?:UTC|Z)?")
_LOCALHOST = re.compile(r"http://(?:127\.0\.0\.1|localhost):\d{1,5}")

# The OTLP attribute value types the 0.158.0 debug exporter prints. A token outside this set is a
# type this policy has never classified, so it is neither printed nor accepted.
OTLP_VALUE_TYPES = ("Bool", "Bytes", "Double", "Empty", "Int", "Map", "Slice", "Str")

# Bounded OTLP enumerations. A value outside one of these is not printed either: it would be a token
# from a specification revision nobody here has read.
SPAN_KINDS = ("Unspecified", "Internal", "Server", "Client", "Producer", "Consumer")
SPAN_STATUS_CODES = ("Unset", "Ok", "Error")
METRIC_DATA_TYPES = ("Empty", "Gauge", "Sum", "Histogram", "ExponentialHistogram", "Summary")
AGGREGATION_TEMPORALITIES = ("Unspecified", "Delta", "Cumulative")
MONOTONICITY = ("true", "false")


def classify_value(key: str, otlp_type: str, raw: str, manifest: Manifest) -> tuple[str, str | None]:
    """Return ``(value_class, emitted_value)`` for one value.

    ``emitted_value`` is None for every class but the two allowlisted ones. The allowlist is
    consulted *first*, so a value a human has explicitly cleared is reported exactly, and everything
    else is reduced to a shape before this function returns.
    """
    if otlp_type not in OTLP_VALUE_TYPES:
        return CLASS_UNCLASSIFIABLE, None
    if otlp_type == "Empty" or raw == "":
        return CLASS_ABSENT, None
    if raw in manifest.static_values.get(key, ()):
        return CLASS_STATIC, raw
    if raw in manifest.enum_values.get(key, ()):
        return CLASS_ENUM, raw
    if otlp_type == "Bool":
        return CLASS_BOOLEAN, None
    if otlp_type == "Int":
        try:
            return (CLASS_NONNEGATIVE_INTEGER if int(raw) >= 0 else CLASS_INTEGER), None
        except ValueError:
            return CLASS_UNCLASSIFIABLE, None
    if otlp_type == "Double":
        try:
            return (CLASS_NONNEGATIVE_DOUBLE if float(raw) >= 0 else CLASS_DOUBLE), None
        except ValueError:
            return CLASS_UNCLASSIFIABLE, None
    if otlp_type != "Str":
        # Map, Slice, and Bytes carry structure this policy has no bounded description for.
        return CLASS_UNCLASSIFIABLE, None
    for pattern, name in (
        (_HEX32, CLASS_HEX32),
        (_HEX16, CLASS_HEX16),
        (_SEMVER, CLASS_SEMVER),
        (_INTEGER, CLASS_NONNEGATIVE_INTEGER),
        (_DOUBLE, CLASS_NONNEGATIVE_DOUBLE),
        (_DURATION, CLASS_DURATION),
        (_TIMESTAMP, CLASS_TIMESTAMP_PRESENT),
        (_LOCALHOST, CLASS_LOCALHOST_ENDPOINT),
    ):
        if pattern.fullmatch(raw):
            if name is CLASS_NONNEGATIVE_INTEGER and raw.startswith("-"):
                return CLASS_INTEGER, None
            if name is CLASS_NONNEGATIVE_DOUBLE and raw.startswith("-"):
                return CLASS_DOUBLE, None
            return name, None
    if raw.lower() in ("true", "false"):
        return CLASS_BOOLEAN, None
    return CLASS_UNCLASSIFIABLE, None


# ---------------------------------------------------------------------------
# The safe-identifier policy
# ---------------------------------------------------------------------------
#
# A *schema identifier* names the shape of the telemetry rather than its payload. There are exactly
# nine kinds, and this list is closed: a unit, a description, a status message, a span name's
# *value*, or anything else is not an identifier and does not get this treatment.
#
# Why identifiers are treated differently from values at all: a reviewer cannot approve a field they
# cannot name. Collapsing unknown names into one bucket and telling the reviewer to go read the
# Collector log makes the report a pointer to evidence instead of the evidence, and moves the review
# out of the artifact that gets checked in. So an unknown identifier is printed *exactly* -- provided
# it passes every check below, each of which exists because an identifier is still vendor-controlled
# text and a hostile one could carry a path, an address, or a credential in the key position.
#
# When any check fails the identifier is replaced by an unsalted SHA-256 of its UTF-8 bytes. Unsalted
# so that two runs, and two machines, produce the same fingerprint for the same identifier -- which
# is what makes a fingerprint something a reviewer can track across runs. The tradeoff is accepted
# deliberately: a fingerprint of a low-entropy name is guessable, and the alternative is publishing
# the name itself, which is exactly what the policy declined to do.

IDENTIFIER_CATEGORIES = (
    "resource.attribute.key",
    "scope.name",
    "scope.attribute.key",
    "span.name",
    "span.attribute.key",
    "event.name",
    "event.attribute.key",
    "metric.name",
    "metric.datapoint.attribute.key",
)

# Which manifest vocabulary section decides whether an identifier of each category is already known.
IDENTIFIER_VOCABULARY = {
    "resource.attribute.key": "attribute_keys",
    "scope.name": "scope_names",
    "scope.attribute.key": "attribute_keys",
    "span.name": "span_names",
    "span.attribute.key": "attribute_keys",
    "event.name": "event_names",
    "event.attribute.key": "attribute_keys",
    "metric.name": "metric_names",
    "metric.datapoint.attribute.key": "attribute_keys",
}

IDENTIFIER_KNOWN = "KNOWN"
IDENTIFIER_UNKNOWN_SAFE = "UNKNOWN_SAFE"
IDENTIFIER_UNPRINTABLE = "UNPRINTABLE_UNKNOWN"

# Length ceilings. Scope names are Go import paths in practice and need more room than a key.
MAX_IDENTIFIER_CHARS = 100
MAX_SCOPE_NAME_CHARS = 200

# The conservative grammar. Dotted segments of ASCII word characters and hyphens, beginning with a
# letter or underscore. No whitespace, no colon, no slash, no backslash, no `@`, no `=`, no `?`, no
# `%`, no quote, and nothing outside ASCII -- every one of which is a component of a path, a URL, an
# address, or a credential.
_IDENTIFIER_GRAMMAR = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*")
# Scope names additionally admit `/`, because an OpenTelemetry instrumentation scope is conventionally
# a package path. A leading slash, a drive letter, and a backslash stay refused by the shape checks.
_SCOPE_GRAMMAR = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_.-]+)*")

_UUID_SHAPE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# A run of 16 or more hexadecimal characters is the shape of a trace id, a span id, a session id, or
# an invocation id. None of the four may be published, and a *key* shaped like one is not a key.
_HEX_RUN = re.compile(r"[0-9a-fA-F]{16,}")
# A separator-free run this long is an encoded blob, not a name. `_` and `-` are excluded from the
# class on purpose: they are how ordinary long identifiers break themselves up.
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_API_KEY_SHAPE = re.compile(r"\bsk[-_][A-Za-z0-9]{6,}")

# Words that make a key content-bearing. Matched against the words of each dotted segment, split on
# `_` and `-`, so `user_prompt` is caught and `gen_ai.usage.input_tokens` is not. The list is
# deliberately about *content*, not about domain nouns: `token`, `model`, `usage`, and `session` are
# absent because a key mentioning them is ordinary telemetry, while a key mentioning `prompt` or
# `stacktrace` is announcing that its value is the thing this contract exists to keep out.
CONTENT_BEARING_WORDS = frozenset(
    {
        "apikey",
        "argv",
        "authorization",
        "body",
        "bodies",
        "cmdline",
        "command",
        "completion",
        "completions",
        "content",
        "cookie",
        "credential",
        "credentials",
        "email",
        "message",
        "messages",
        "password",
        "passwd",
        "payload",
        "prompt",
        "prompts",
        "response",
        "responses",
        "secret",
        "secrets",
        "snippet",
        "stacktrace",
        "stderr",
        "stdout",
        "text",
        "traceparent",
        "tracestate",
    }
)

# The policy checks, in the order they are reported. Every one of them is named in the result whether
# or not it fired, so a reviewer can see which rule refused an identifier.
IDENTIFIER_CHECKS = (
    "category is one this policy knows",
    "identifier is non-empty",
    "identifier is within its length bound",
    "identifier is valid UTF-8",
    "identifier carries no control character",
    "identifier carries no whitespace",
    "identifier matches the conservative grammar",
    "identifier has no path shape",
    "identifier has no URL, email, or credential shape",
    "identifier has no UUID or opaque-identifier shape",
    "identifier carries no content-bearing word",
)


@dataclass(frozen=True)
class IdentifierAssessment:
    """Whether one schema identifier may be printed, and why not when it may not."""

    category: str
    safe: bool
    failures: tuple[str, ...]
    fingerprint: str

    @property
    def token(self) -> str:
        """What appears in an entry identifier. Distinct per identifier, safe or not."""
        return f"sha256-{self.fingerprint}"


def fingerprint_identifier(raw: str) -> str:
    """The stable, unsalted SHA-256 of an identifier's UTF-8 bytes, as 64 lowercase hex characters."""
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()


def assess_identifier(category: str, raw: str) -> IdentifierAssessment:
    """Apply the safe-identifier policy. Never raises, and never echoes ``raw``."""
    failures: list[str] = []
    fingerprint = fingerprint_identifier(raw)
    if category not in IDENTIFIER_CATEGORIES:
        failures.append(IDENTIFIER_CHECKS[0])
        return IdentifierAssessment(category, False, tuple(failures), fingerprint)
    if not raw:
        failures.append(IDENTIFIER_CHECKS[1])
        return IdentifierAssessment(category, False, tuple(failures), fingerprint)

    limit = MAX_SCOPE_NAME_CHARS if category == "scope.name" else MAX_IDENTIFIER_CHARS
    if len(raw) > limit:
        failures.append(IDENTIFIER_CHECKS[2])
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate. It cannot be written to a UTF-8 report at all, let alone safely.
        failures.append(IDENTIFIER_CHECKS[3])
    if any(unicodedata.category(character) in ("Cc", "Cf", "Cs", "Co", "Cn") for character in raw):
        failures.append(IDENTIFIER_CHECKS[4])
    # No identifier category requires whitespace, so none of them may contain any.
    if any(character.isspace() for character in raw):
        failures.append(IDENTIFIER_CHECKS[5])

    grammar = _SCOPE_GRAMMAR if category == "scope.name" else _IDENTIFIER_GRAMMAR
    if not grammar.fullmatch(raw):
        failures.append(IDENTIFIER_CHECKS[6])
    if "\\" in raw or raw.startswith("/") or re.match(r"[A-Za-z]:", raw) or raw.startswith("~"):
        failures.append(IDENTIFIER_CHECKS[7])
    if (
        ":" in raw
        or "@" in raw
        or "?" in raw
        or "=" in raw
        or _BASE64_RUN.search(raw)
        or _API_KEY_SHAPE.search(raw)
    ):
        failures.append(IDENTIFIER_CHECKS[8])
    if _UUID_SHAPE.search(raw) or _HEX_RUN.search(raw):
        failures.append(IDENTIFIER_CHECKS[9])
    words = {word for segment in re.split(r"[./]", raw) for word in re.split(r"[_-]", segment)}
    if words & CONTENT_BEARING_WORDS:
        failures.append(IDENTIFIER_CHECKS[10])

    ordered = tuple(check for check in IDENTIFIER_CHECKS if check in set(failures))
    return IdentifierAssessment(category, not ordered, ordered, fingerprint)


# ---------------------------------------------------------------------------
# Forbidden output
# ---------------------------------------------------------------------------
#
# A backstop, not the mechanism. The mechanism is that a raw value is never carried past
# `classify_value` and an unsafe identifier is never carried past `assess_identifier`; this scan is
# what refuses to *write* a report if either mechanism ever fails.
#
# Each entry is (name, pattern). Only the name is ever reported -- printing the match would publish
# the very thing the scan caught.
#
# The two identifier patterns are anchored with hexadecimal lookaround on both sides so that a 64
# character SHA-256 fingerprint is not mistaken for a 32-character trace id embedded in it.

FORBIDDEN_OUTPUT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email-address", re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")),
    ("url", re.compile(r"[a-zA-Z][\w+.-]*://")),
    ("windows-path", re.compile(r"[A-Za-z]:\\\\?[\w.$-]")),
    ("unix-path", re.compile(r"(?:^|[\s\"'(])/(?:home|Users|usr|etc|var|root|tmp|mnt)/")),
    ("trace-or-invocation-identifier", re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{32}(?![0-9a-fA-F])")),
    ("span-identifier", re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{16}(?![0-9a-fA-F])")),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{4,}")),
    ("authorization-material", re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9+/=_-]{8,}")),
    ("environment-assignment", re.compile(r"\b[A-Z][A-Z0-9_]{3,}=[^\s\"']")),
    ("query-string", re.compile(r"[?&][A-Za-z_][\w.-]*=[^\s\"']")),
)


def scan_forbidden_output(text: str) -> tuple[str, ...]:
    """Return the names of every forbidden pattern present in ``text``. Never returns a match."""
    return tuple(name for name, pattern in FORBIDDEN_OUTPUT_PATTERNS if pattern.search(text))


# ---------------------------------------------------------------------------
# The approval manifest
# ---------------------------------------------------------------------------


class ManifestError(Exception):
    """The approval manifest is missing, malformed, or describes a different subject."""


@dataclass(frozen=True)
class ManifestField:
    """One reviewable field, and what a human has said about it.

    ``constraints`` holds the per-kind expectations — declared types and value classes for an
    attribute, kinds and status codes for a span, data types and unit classes for a metric. An
    APPROVED field whose observation falls outside its constraints is reported UNAPPROVED: the
    approval was for the field as described, not for whatever it turns out to carry.
    """

    id: str
    status: str
    expected: bool
    note: str
    constraints: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class Manifest:
    path: Path
    version: str
    subject: Mapping[str, str]
    review: Mapping[str, str]
    vocabulary: Mapping[str, frozenset[str]]
    static_values: Mapping[str, tuple[str, ...]]
    enum_values: Mapping[str, tuple[str, ...]]
    redacted_keys: frozenset[str]
    fields: Mapping[str, ManifestField]

    @property
    def is_human_approved(self) -> bool:
        """True only when a human recorded a name and a review state of `approved`."""
        return bool(self.review.get("approved_by", "").strip()) and (
            self.review.get("state", "") == "approved"
        )

    def knows(self, category: str, name: str) -> bool:
        section = IDENTIFIER_VOCABULARY.get(category, "")
        return name in self.vocabulary.get(section, frozenset())


_VOCABULARIES = ("attribute_keys", "span_names", "event_names", "metric_names", "scope_names")

_CONSTRAINT_KEYS = (
    "types",
    "value_classes",
    "kinds",
    "status_codes",
    "units",
    "unit_classes",
    "descriptions",
    "description_classes",
    "data_types",
    "aggregation_temporalities",
    "monotonicity",
    "version_classes",
)


def load_manifest(path: Path = MANIFEST_PATH, *, schema: str = MANIFEST_SCHEMA) -> Manifest:
    """Read and validate the approval manifest. A malformed manifest is an error, not a default.

    ``schema`` is the literal a manifest must declare. It is a parameter rather than a constant so a
    second agent's manifest -- Gate 8's `ops/otel/codex-field-approvals.yaml` -- can reuse this
    reader without being able to masquerade as the Claude one: each caller names the schema it
    expects, and a manifest declaring the other is rejected here rather than merged by accident.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ManifestError(f"no approval manifest at {path}") from error
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: the manifest must be a mapping")
    if raw.get("schema") != schema:
        raise ManifestError(f"{path}: schema must be {schema!r}")

    subject = raw.get("subject") or {}
    if not isinstance(subject, dict):
        raise ManifestError(f"{path}: subject must be a mapping")
    review = raw.get("review") or {}
    if not isinstance(review, dict):
        raise ManifestError(f"{path}: review must be a mapping")

    vocabulary = {}
    declared = raw.get("vocabulary") or {}
    if not isinstance(declared, dict):
        raise ManifestError(f"{path}: vocabulary must be a mapping")
    for name in _VOCABULARIES:
        entries = declared.get(name) or []
        if not isinstance(entries, list):
            raise ManifestError(f"{path}: vocabulary.{name} must be a list")
        vocabulary[name] = frozenset(str(item) for item in entries)
    unexpected = set(declared) - set(_VOCABULARIES)
    if unexpected:
        raise ManifestError(f"{path}: unknown vocabulary section(s): {sorted(unexpected)}")

    static_values = _string_lists(path, raw.get("static_values") or {}, "static_values")
    enum_values = _string_lists(path, raw.get("enum_values") or {}, "enum_values")

    # Redaction is a property of the attribute key, not of the surface it sits on, so these are
    # declared once rather than once per surface. Observing any of them after `transform/privacy`
    # means the privacy processor did not run or no longer covers that surface.
    redacted = raw.get("redacted_keys") or []
    if not isinstance(redacted, list):
        raise ManifestError(f"{path}: redacted_keys must be a list")

    fields: dict[str, ManifestField] = {}
    for entry in raw.get("fields") or []:
        if not isinstance(entry, dict):
            raise ManifestError(f"{path}: every field entry must be a mapping")
        identifier = str(entry.get("id", "")).strip()
        if not identifier:
            raise ManifestError(f"{path}: every field entry needs an id")
        if identifier in fields:
            raise ManifestError(f"{path}: duplicate field id {identifier!r}")
        status = str(entry.get("status", "")).strip()
        if status not in MANIFEST_STATUSES:
            raise ManifestError(f"{path}: {identifier}: status must be one of {MANIFEST_STATUSES}")
        constraints = {}
        for key in _CONSTRAINT_KEYS:
            if key in entry:
                values = entry[key]
                if not isinstance(values, list):
                    raise ManifestError(f"{path}: {identifier}: {key} must be a list")
                constraints[key] = tuple(_scalar(value) for value in values)
        unknown_keys = set(entry) - {"id", "status", "expected", "note"} - set(_CONSTRAINT_KEYS)
        if unknown_keys:
            raise ManifestError(f"{path}: {identifier}: unknown key(s) {sorted(unknown_keys)}")
        fields[identifier] = ManifestField(
            id=identifier,
            status=status,
            expected=bool(entry.get("expected", False)),
            note=str(entry.get("note", "")),
            constraints=constraints,
        )

    return Manifest(
        path=path,
        version=str(raw.get("version", "")),
        subject={str(k): str(v) for k, v in subject.items()},
        review={str(k): str(v) for k, v in review.items()},
        vocabulary=vocabulary,
        static_values=static_values,
        enum_values=enum_values,
        redacted_keys=frozenset(str(item) for item in redacted),
        fields=fields,
    )


def _scalar(value: object) -> str:
    """Render a YAML scalar the way the parser renders an observed one, so the two compare."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _string_lists(path: Path, raw: object, section: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: {section} must be a mapping")
    result: dict[str, tuple[str, ...]] = {}
    for key, values in raw.items():
        if not isinstance(values, list):
            raise ManifestError(f"{path}: {section}.{key} must be a list")
        result[str(key)] = tuple(_scalar(value) for value in values)
    return result


# ---------------------------------------------------------------------------
# Parsing the debug exporter's output
# ---------------------------------------------------------------------------
#
# The grammar below is the 0.158.0 `debug` exporter at `verbosity: detailed`, read off a real run
# rather than assumed. Two rules govern every branch:
#
#   * a line this parser does not recognize is counted as an unresolved remainder, never skipped;
#   * a value is classified where it is read and the original is not retained.

_RESOURCE_BLOCK = re.compile(r"Resource(?P<signal>Spans|Metrics) #\d+")
_SCOPE_BLOCK = re.compile(r"Scope(?:Spans|Metrics) #\d+")
_SCHEMA_URL = re.compile(r"(?:Resource|ScopeSpans|ScopeMetrics) SchemaURL:.*")
_SCOPE_IDENTITY = re.compile(r"InstrumentationScope(?P<rest>.*)")
_SPAN_BLOCK = re.compile(r"Span #\d+")
_SPAN_EVENT_BLOCK = re.compile(r"SpanEvent #\d+")
_SPAN_LINK_BLOCK = re.compile(r"SpanLink #\d+")
_METRIC_BLOCK = re.compile(r"Metric #\d+")
_DATAPOINT_BLOCK = re.compile(r"\w*DataPoints #\d+")
_EXEMPLAR_BLOCK = re.compile(r"Exemplar #\d+")

_ATTRIBUTE_LINE = re.compile(r"\s*-> (?P<key>[^:]+): (?P<rest>.*)")
_TYPED_VALUE = re.compile(r"(?P<type>[A-Za-z]+)\((?P<value>.*)\)")
_DESCRIPTOR_LINE = re.compile(r"\s*-> (?P<field>[A-Za-z]+):\s?(?P<value>.*)")
_SPAN_FIELD_LINE = re.compile(r" {4}(?P<field>[A-Za-z ]+?)\s*:\s?(?P<value>.*)")
_DATAPOINT_VALUE_LINE = re.compile(
    r"(?:StartTimestamp|Timestamp|Value|Count|Sum|Min|Max|Scale|ZeroCount|ZeroThreshold)"
    r":.*|(?:ExplicitBounds|Buckets|QuantileValue|Positive|Negative)\b.*"
)

# Span descriptor fields the inventory reads. Everything else the exporter prints on a span --
# `Trace ID`, `Parent ID`, `ID`, `TraceState`, `Start time`, `End time` -- is recognized so it is not
# counted as unresolved, and then deliberately dropped on the floor.
_SPAN_FIELDS_READ = ("Name", "Kind", "Status code")
_SPAN_FIELDS_IGNORED = (
    "Trace ID",
    "Parent ID",
    "ID",
    "TraceState",
    "Start time",
    "End time",
    "Status message",
    "DroppedAttributesCount",
    "DroppedEventsCount",
    "DroppedLinksCount",
    "Flags",
)

_EVENT_FIELDS_IGNORED = ("Timestamp", "DroppedAttributesCount")
_DESCRIPTOR_FIELDS = (
    "Name",
    "Description",
    "Unit",
    "DataType",
    "IsMonotonic",
    "AggregationTemporality",
    "StartTimestamp",
)


class _Mode:
    NONE = "none"
    ATTRIBUTES = "attributes"
    SPAN_HEADER = "span-header"
    EVENT_HEADER = "event-header"
    EVENT_ATTRIBUTES = "event-attributes"
    DESCRIPTOR = "descriptor"
    DATAPOINT = "datapoint"
    LINK = "link"
    EXEMPLAR = "exemplar"


class Inventory:
    """Everything one run exported, reduced to bounded descriptions and occurrence counts.

    Entries are keyed by a deterministic identifier so the same export always produces the same
    document, and so the approval manifest can name a field without repeating its description.
    """

    def __init__(self, manifest: Manifest) -> None:
        self._manifest = manifest
        self._entries: dict[str, dict] = {}
        self.link_count = 0
        self.exemplar_count = 0
        self.diagnostic_records = 0

    # -- entry construction ------------------------------------------------

    def entry(self, identifier: str, kind: str, **initial) -> dict:
        entry = self._entries.get(identifier)
        if entry is None:
            entry = {"id": identifier, "kind": kind, "occurrences": 0}
            entry.update(initial)
            self._entries[identifier] = entry
        return entry

    def note(self, entry: dict, reason: str) -> None:
        entry.setdefault("reasons", set()).add(reason)

    def identifier(self, category: str, raw: str) -> tuple[str, dict]:
        """Resolve one schema identifier into ``(entry token, entry fields)``.

        The token is what appears in an entry id. It is the identifier itself when the manifest
        already knows it or when it passes the safe-identifier policy, and a per-identifier
        fingerprint otherwise — so two distinct unknown identifiers are never merged, whether or not
        either can be printed.
        """
        if self._manifest.knows(category, raw):
            return raw, {"identifier_status": IDENTIFIER_KNOWN, "identifier_category": category}
        assessment = assess_identifier(category, raw)
        if assessment.safe:
            return raw, {
                "identifier_status": IDENTIFIER_UNKNOWN_SAFE,
                "identifier_category": category,
            }
        return assessment.token, {
            "identifier_status": IDENTIFIER_UNPRINTABLE,
            "identifier_category": category,
            "fingerprint": assessment.fingerprint,
            "fingerprint_algorithm": "sha256",
            "identifier_findings": sorted(assessment.failures),
        }

    def add_attribute(self, kind: str, category: str, prefix: str, key: str, otlp_type: str, raw: str) -> None:
        token, identity = self.identifier(category, key)
        entry = self.entry(
            f"{kind}/{prefix}/{token}" if prefix else f"{kind}/{token}", kind, key=token, **identity
        )
        entry["occurrences"] += 1
        if otlp_type in OTLP_VALUE_TYPES:
            entry.setdefault("types", set()).add(otlp_type)
        else:
            self.note(entry, "value type outside the OTLP vocabulary")
        value_class, emitted = classify_value(key, otlp_type, raw, self._manifest)
        entry.setdefault("value_classes", set()).add(value_class)
        if emitted is not None:
            entry.setdefault("values", set()).add(emitted)

    def add_value(self, entry: dict, key: str, field: str, raw: str) -> None:
        """Record a *value* carried in a descriptor slot — a metric's unit or description.

        Not an identifier, and deliberately not treated as one: a safe metric name says nothing
        about the prose beneath it, so this goes through the same allowlist every other value does.
        """
        value_class, emitted = classify_value(key, "Str", raw, self._manifest)
        entry.setdefault(f"{field}_classes", set()).add(value_class)
        if emitted is not None:
            entry.setdefault(f"{field}s", set()).add(emitted)

    def unresolved(self, context: str) -> None:
        entry = self.entry(f"unresolved/{context}", "unresolved", context=context)
        entry["occurrences"] += 1
        self.note(entry, "the exporter printed a line this parser does not recognize")

    # -- finishing ---------------------------------------------------------

    def documents(self) -> list[dict]:
        """Every entry, sets flattened to sorted lists, sorted by identifier. Deterministic."""
        rendered = []
        for entry in self._entries.values():
            item = {}
            for key, value in entry.items():
                item[key] = sorted(value) if isinstance(value, set) else value
            rendered.append(item)
        return sorted(rendered, key=lambda item: (item["kind"], item["id"]))


def parse_export(output: str, manifest: Manifest) -> Inventory:
    """Build an :class:`Inventory` from the Collector's own log.

    The only input is text the `debug` exporter printed, which is post-privacy by construction: the
    exporter is the terminal component of pipelines whose processors are `memory_limiter`,
    `filter/unredactable`, `transform/privacy`, and `batch`.
    """
    inventory = Inventory(manifest)
    state = _State(inventory, manifest)
    in_dump = False
    for line in output.splitlines():
        parts = line.split("\t")
        if line[:4].isdigit() and line[4:5] == "-":
            # A Collector log line. It begins a detailed dump only when its message is a resource
            # block header; anything else -- startup notices, the exporter's own summary -- is not
            # inventory input and is skipped without being counted as unresolved.
            start = next((part for part in parts if _RESOURCE_BLOCK.fullmatch(part.strip())), None)
            in_dump = start is not None
            if in_dump:
                state.handle(start.strip())
            continue
        if line.startswith("\t{") or line.startswith("{"):
            # The structured trailer the Collector appends after a dump. It carries the Collector's
            # own resource, never the payload's, and it ends the dump.
            in_dump = False
            continue
        if in_dump:
            state.handle(line)
    return inventory


class _State:
    """The line-by-line state machine. Split out so `parse_export` stays readable."""

    def __init__(self, inventory: Inventory, manifest: Manifest) -> None:
        self.inventory = inventory
        self.manifest = manifest
        self.signal = "traces"
        self.scope = "unnamed"
        self.span = "unnamed"
        self.event = "unnamed"
        self.metric = "unnamed"
        self.mode = _Mode.NONE
        self.attribute_kind = ""
        self.attribute_category = ""
        self.attribute_prefix = ""
        self.span_entry: dict | None = None
        self.metric_entry: dict | None = None

    # -- helpers -----------------------------------------------------------

    def _attributes(self, kind: str, category: str, prefix: str) -> None:
        self.mode = _Mode.ATTRIBUTES
        self.attribute_kind = kind
        self.attribute_category = category
        self.attribute_prefix = prefix

    def _context(self) -> str:
        return {
            _Mode.ATTRIBUTES: self.attribute_kind or "attributes",
            _Mode.SPAN_HEADER: "span",
            _Mode.EVENT_HEADER: "span.event",
            _Mode.EVENT_ATTRIBUTES: "span.event.attribute",
            _Mode.DESCRIPTOR: "metric",
            _Mode.DATAPOINT: "metric.datapoint",
        }.get(self.mode, "record")

    # -- the machine -------------------------------------------------------

    def handle(self, line: str) -> None:
        stripped = line.rstrip()
        if not stripped.strip():
            return
        if self._block(stripped):
            return
        if self.mode == _Mode.ATTRIBUTES:
            self._attribute(stripped, self.attribute_kind, self.attribute_category, self.attribute_prefix)
            return
        if self.mode == _Mode.EVENT_ATTRIBUTES:
            self._attribute(
                stripped, "span.event.attribute", "event.attribute.key", f"{self.span}/{self.event}"
            )
            return
        if self.mode == _Mode.SPAN_HEADER and self._span_field(stripped):
            return
        if self.mode == _Mode.EVENT_HEADER and self._event_field(stripped):
            return
        if self.mode == _Mode.DESCRIPTOR and self._descriptor_field(stripped):
            return
        if self.mode == _Mode.DATAPOINT and _DATAPOINT_VALUE_LINE.fullmatch(stripped.strip()):
            return
        if self.mode in (_Mode.LINK, _Mode.EXEMPLAR):
            # The carrier itself is already counted and already a finding. Its contents are not
            # read, not classified, and not stored -- a dropped carrier is not an inventory input.
            return
        self.inventory.unresolved(self._context())

    def _block(self, line: str) -> bool:
        """Handle the column-zero structural markers. Returns True when ``line`` was one."""
        text = line.strip()
        matched = _RESOURCE_BLOCK.fullmatch(text)
        if matched:
            self.signal = "traces" if matched.group("signal") == "Spans" else "metrics"
            self.scope = self.span = self.metric = "unnamed"
            self.mode = _Mode.NONE
            return True
        if text == "Resource attributes:":
            self._attributes("resource.attribute", "resource.attribute.key", self.signal)
            return True
        if _SCOPE_BLOCK.fullmatch(text) or _SCHEMA_URL.fullmatch(text):
            self.mode = _Mode.NONE
            return True
        if text == "InstrumentationScope attributes:":
            self._attributes(
                "scope.attribute", "scope.attribute.key", f"{self.signal}/{self.scope}"
            )
            return True
        matched = _SCOPE_IDENTITY.fullmatch(text)
        if matched:
            self._scope(matched.group("rest").strip())
            return True
        if _SPAN_BLOCK.fullmatch(text):
            self.span = "unnamed"
            self.span_entry = None
            self.mode = _Mode.SPAN_HEADER
            return True
        if text == "Attributes:":
            self._attributes("span.attribute", "span.attribute.key", self.span)
            return True
        if text == "Events:":
            self.mode = _Mode.NONE
            return True
        if _SPAN_EVENT_BLOCK.fullmatch(text):
            self.event = "unnamed"
            self.mode = _Mode.EVENT_HEADER
            return True
        if text == "Links:":
            self.mode = _Mode.LINK
            return True
        if _SPAN_LINK_BLOCK.fullmatch(text):
            self.inventory.link_count += 1
            if self.span_entry is not None:
                self.span_entry["link_count"] = self.span_entry.get("link_count", 0) + 1
                self.inventory.note(self.span_entry, "a span link reached the exporter")
            self.mode = _Mode.LINK
            return True
        if _METRIC_BLOCK.fullmatch(text):
            self.metric = "unnamed"
            self.metric_entry = None
            self.mode = _Mode.NONE
            return True
        if text == "Descriptor:":
            self.mode = _Mode.DESCRIPTOR
            return True
        if _DATAPOINT_BLOCK.fullmatch(text):
            self.mode = _Mode.DATAPOINT
            return True
        if text == "Data point attributes:":
            self._attributes(
                "metric.datapoint.attribute", "metric.datapoint.attribute.key", self.metric
            )
            return True
        if _DATAPOINT_VALUE_LINE.fullmatch(text) and self.mode in (_Mode.DATAPOINT, _Mode.ATTRIBUTES):
            # The measurement itself -- timestamps, sums, bucket counts. Recognized so it is not
            # counted as unresolved, and then dropped: a raw timestamp is not inventory material,
            # and a data point's value says nothing about which *fields* exist.
            self.mode = _Mode.DATAPOINT
            return True
        if text == "Exemplars:":
            self.mode = _Mode.EXEMPLAR
            return True
        if _EXEMPLAR_BLOCK.fullmatch(text):
            self.inventory.exemplar_count += 1
            if self.metric_entry is not None:
                self.metric_entry["exemplar_count"] = self.metric_entry.get("exemplar_count", 0) + 1
                self.inventory.note(self.metric_entry, "a metric exemplar reached the exporter")
            self.mode = _Mode.EXEMPLAR
            return True
        return False

    def _scope(self, rest: str) -> None:
        # `InstrumentationScope <name> <version>`, with the version omitted when the scope has none.
        pieces = rest.split()
        name = pieces[0] if pieces else ""
        version = pieces[1] if len(pieces) > 1 else ""
        token, identity = self.inventory.identifier("scope.name", name)
        self.scope = token
        if canary.COUNT_CONNECTOR_SCOPE in name:
            self.inventory.diagnostic_records += 1
        entry = self.inventory.entry(
            f"scope/{self.signal}/{token}", "scope", name=token, signal=self.signal, **identity
        )
        entry["occurrences"] += 1
        version_class, _ = classify_value("", "Str", version, self.manifest)
        entry.setdefault("version_classes", set()).add(version_class)
        self.mode = _Mode.NONE

    def _span_field(self, line: str) -> bool:
        matched = _SPAN_FIELD_LINE.fullmatch(line)
        if not matched:
            return False
        name, value = matched.group("field").strip(), matched.group("value").strip()
        if name in _SPAN_FIELDS_IGNORED:
            return True
        if name not in _SPAN_FIELDS_READ:
            return False
        if name == "Name":
            token, identity = self.inventory.identifier("span.name", value)
            self.span = token
            self.span_entry = self.inventory.entry(f"span/{token}", "span", name=token, **identity)
            self.span_entry["occurrences"] += 1
            self.span_entry.setdefault("link_count", 0)
            return True
        if self.span_entry is None:
            return False
        if name == "Kind":
            self._enumerated(self.span_entry, "kinds", value, SPAN_KINDS, "span kind")
        else:
            self._enumerated(
                self.span_entry, "status_codes", value, SPAN_STATUS_CODES, "span status code"
            )
        return True

    def _event_field(self, line: str) -> bool:
        if line.strip() == "-> Attributes::":
            self.mode = _Mode.EVENT_ATTRIBUTES
            return True
        matched = _DESCRIPTOR_LINE.fullmatch(line)
        if not matched:
            return False
        name, value = matched.group("field"), matched.group("value").strip()
        if name in _EVENT_FIELDS_IGNORED:
            return True
        if name != "Name":
            return False
        token, identity = self.inventory.identifier("event.name", value)
        self.event = token
        entry = self.inventory.entry(
            f"span.event/{self.span}/{token}", "span.event", name=token, span=self.span, **identity
        )
        entry["occurrences"] += 1
        return True

    def _descriptor_field(self, line: str) -> bool:
        matched = _DESCRIPTOR_LINE.fullmatch(line)
        if not matched:
            return False
        name, value = matched.group("field"), matched.group("value").strip()
        if name not in _DESCRIPTOR_FIELDS:
            return False
        if name == "Name":
            token, identity = self.inventory.identifier("metric.name", value)
            self.metric = token
            self.metric_entry = self.inventory.entry(
                f"metric/{token}", "metric", name=token, signal=self.signal, **identity
            )
            self.metric_entry["occurrences"] += 1
            self.metric_entry.setdefault("exemplar_count", 0)
            return True
        if self.metric_entry is None:
            return False
        if name == "Description":
            self.inventory.add_value(self.metric_entry, DESCRIPTION_KEY, "description", value)
        elif name == "Unit":
            self.inventory.add_value(self.metric_entry, UNIT_KEY, "unit", value)
        elif name == "DataType":
            self._enumerated(self.metric_entry, "data_types", value, METRIC_DATA_TYPES, "data type")
        elif name == "IsMonotonic":
            self._enumerated(self.metric_entry, "monotonicity", value, MONOTONICITY, "monotonicity")
        elif name == "AggregationTemporality":
            self._enumerated(
                self.metric_entry,
                "aggregation_temporalities",
                value,
                AGGREGATION_TEMPORALITIES,
                "aggregation temporality",
            )
        return True

    def _enumerated(self, entry: dict, key: str, value: str, allowed: Sequence[str], label: str) -> None:
        """Record a bounded OTLP enumeration, or refuse to print a token outside it."""
        if value in allowed:
            entry.setdefault(key, set()).add(value)
        else:
            self.inventory.note(entry, f"{label} outside the OTLP vocabulary")

    def _attribute(self, line: str, kind: str, category: str, prefix: str) -> None:
        matched = _ATTRIBUTE_LINE.fullmatch(line)
        typed = _TYPED_VALUE.fullmatch(matched.group("rest")) if matched else None
        if matched is None or typed is None:
            self.inventory.unresolved(kind)
            return
        self.inventory.add_attribute(
            kind, category, prefix, matched.group("key").strip(), typed.group("type"), typed.group("value")
        )


# ---------------------------------------------------------------------------
# Comparison against the manifest
# ---------------------------------------------------------------------------

# Which constraint keys each entry kind is checked against. An observed value outside the declared
# set makes an APPROVED field UNAPPROVED: the approval described a field, not a wildcard.
_ATTRIBUTE_CONSTRAINTS = ("types", "value_classes")
_CHECKED_CONSTRAINTS = {
    "resource.attribute": _ATTRIBUTE_CONSTRAINTS,
    "scope.attribute": _ATTRIBUTE_CONSTRAINTS,
    "span.attribute": _ATTRIBUTE_CONSTRAINTS,
    "span.event.attribute": _ATTRIBUTE_CONSTRAINTS,
    "metric.datapoint.attribute": _ATTRIBUTE_CONSTRAINTS,
    "scope": ("version_classes",),
    "span": ("kinds", "status_codes"),
    "span.event": (),
    "metric": (
        "unit_classes",
        "description_classes",
        "data_types",
        "aggregation_temporalities",
        "monotonicity",
    ),
}

# Kinds whose entries name an attribute key, and are therefore subject to `redacted_keys`.
_ATTRIBUTE_KINDS = frozenset(
    {
        "resource.attribute",
        "scope.attribute",
        "span.attribute",
        "span.event.attribute",
        "metric.datapoint.attribute",
    }
)


def compare(entries: Sequence[dict], manifest: Manifest) -> list[dict]:
    """Attach a disposition and its reasons to every observed entry. Pure, and order-preserving."""
    judged = []
    for entry in entries:
        item = dict(entry)
        # A reason recorded by the parser -- an OTLP token outside its vocabulary, an unredactable
        # carrier -- survives into the judgement, so it can never be approved away.
        reasons = list(item.pop("reasons", []))
        if item["kind"] == "unresolved":
            item["status"] = DISPOSITION_UNRESOLVED
            item["reasons"] = sorted(reasons)
            judged.append(item)
            continue
        # Redaction is judged before the manifest lookup, and by key rather than by identifier: a key
        # the privacy processor is supposed to have deleted is a failure on whatever surface it turns
        # up on, and there is no per-surface entry a reviewer could have forgotten to write.
        if item["kind"] in _ATTRIBUTE_KINDS and item.get("key") in manifest.redacted_keys:
            item["status"] = STATUS_REDACTED
            reasons.append("transform/privacy is required to delete this key; it survived")
            item["reasons"] = sorted(set(reasons))
            judged.append(item)
            continue
        declared = manifest.fields.get(item["id"])
        if declared is None:
            item["status"] = DISPOSITION_UNKNOWN
            reasons.append("no entry in the approval manifest")
        elif declared.status in (STATUS_BLOCKED, STATUS_REDACTED):
            item["status"] = declared.status
            reasons.append(
                f"the manifest marks this field {declared.status}; it must not survive the pipeline"
            )
        elif declared.status == STATUS_UNAPPROVED:
            item["status"] = STATUS_UNAPPROVED
            reasons.append("the manifest has not approved this field")
        else:
            reasons.extend(_constraint_mismatches(item, declared, manifest))
            item["status"] = STATUS_UNAPPROVED if reasons else STATUS_APPROVED
        if declared is not None and declared.note:
            item["note"] = declared.note
        # Four things no manifest edit can approve away. A carrier that cannot be redacted must not
        # be on the export path at all; an identifier nobody could print was never reviewed; and a
        # value nothing can describe is not a value anybody can consent to.
        if item.get("link_count"):
            item["status"] = STATUS_UNAPPROVED
            reasons.append("a span link reached the exporter and cannot be approved")
        if item.get("exemplar_count"):
            item["status"] = STATUS_UNAPPROVED
            reasons.append("a metric exemplar reached the exporter and cannot be approved")
        if item.get("identifier_status") == IDENTIFIER_UNPRINTABLE:
            # Refusing an identifier nobody can print is always available; *approving* one never is.
            # A manifest that already says BLOCKED or REDACTED has refused it, so that disposition
            # stands. Anything else -- APPROVED, UNAPPROVED, or no entry at all -- is forced down.
            if declared is None:
                item["status"] = DISPOSITION_UNKNOWN
            elif declared.status not in (STATUS_BLOCKED, STATUS_REDACTED):
                item["status"] = STATUS_UNAPPROVED
            reasons.append("the identifier failed the safe-identifier policy and cannot be approved")
        if CLASS_UNCLASSIFIABLE in _observed_classes(item) and item["status"] == STATUS_APPROVED:
            item["status"] = STATUS_UNAPPROVED
            reasons.append("an unclassifiable value cannot be approved; allowlist it or reject it")
        item["reasons"] = sorted(set(reasons))
        judged.append(item)
    return judged


def _observed_classes(item: Mapping) -> set[str]:
    return set(item.get("value_classes") or ()) | set(item.get("unit_classes") or ()) | set(
        item.get("description_classes") or ()
    ) | set(item.get("version_classes") or ())


def _constraint_mismatches(item: dict, declared: ManifestField, manifest: Manifest) -> list[str]:
    problems: list[str] = []
    for key in _CHECKED_CONSTRAINTS.get(item["kind"], ()):
        observed = set(item.get(key) or [])
        if not observed:
            continue
        permitted = set(declared.constraints.get(key, ()))
        outside = sorted(observed - permitted)
        if outside:
            problems.append(f"{key}: observed {outside} outside the approved set")
    # Defence in depth beneath `classify_value`, which is what puts a value into an entry in the
    # first place and only does so from these two lists.
    for field_name, key in (("values", item.get("key", "")), ("units", UNIT_KEY),
                            ("descriptions", DESCRIPTION_KEY)):
        allowlisted = set(manifest.static_values.get(key, ())) | set(manifest.enum_values.get(key, ()))
        if set(item.get(field_name) or ()) - allowlisted:
            problems.append(f"{field_name}: a value was emitted that the manifest does not allowlist")
    return problems


# ---------------------------------------------------------------------------
# The report document
# ---------------------------------------------------------------------------


def build_document(
    report: canary.CanaryReport,
    manifest: Manifest,
    *,
    schema: str = SCHEMA_VERSION,
    gate: str = "gate6",
    verdict: Callable[[dict, Manifest], dict] | None = None,
) -> dict:
    """Turn one canary run into the canonical inventory document. Deterministic and sorted.

    ``schema``, ``gate``, and ``verdict`` exist so a second agent can be inventoried by the same
    parser, the same classifier, and the same comparison, and still be judged by its own conditions
    under its own report format. Everything above the verdict is agent-independent; the verdict is
    not, and pretending otherwise would let one agent's gate be reported under another agent's name.
    """
    inventory = parse_export(report.output, manifest)
    fields = compare(inventory.documents(), manifest)

    observed_ids = {item["id"] for item in fields}
    counts = {status: sum(1 for item in fields if item["status"] == status) for status in DISPOSITIONS}
    expected_missing = sorted(
        declared.id
        for declared in manifest.fields.values()
        if declared.expected and declared.id not in observed_ids
    )

    observed = len(fields)
    resolved = sum(counts[status] for status in MANIFEST_STATUSES) + counts[DISPOSITION_UNKNOWN]
    totals = {
        "observed": observed,
        "approved": counts[STATUS_APPROVED],
        "blocked": counts[STATUS_BLOCKED],
        "redacted": counts[STATUS_REDACTED],
        "unapproved": counts[STATUS_UNAPPROVED],
        "unknown": counts[DISPOSITION_UNKNOWN],
        "expected_not_observed": len(expected_missing),
        "unresolved_remainder": observed - resolved,
    }
    identifiers = {
        status: sum(1 for item in fields if item.get("identifier_status") == status)
        for status in (IDENTIFIER_KNOWN, IDENTIFIER_UNKNOWN_SAFE, IDENTIFIER_UNPRINTABLE)
    }

    accounting = report.accounting
    body = {
        "schema": schema,
        "subject": {
            "agent": manifest.subject.get("agent", ""),
            "agent_version": manifest.subject.get("agent_version", ""),
            "collector_version": manifest.subject.get("collector_version", ""),
            "contract_version": manifest.subject.get("contract_version", ""),
        },
        "manifest": {
            "path": manifest.path.name,
            "version": manifest.version,
            "review_state": manifest.review.get("state", ""),
            "human_approved": manifest.is_human_approved,
        },
        "totals": totals,
        "identifiers": identifiers,
        "carriers": {
            "span_links": inventory.link_count,
            "metric_exemplars": inventory.exemplar_count,
            "diagnostic_records": inventory.diagnostic_records,
        },
        "span_accounting": {
            "accepted": getattr(accounting, "accepted", None),
            "exported": getattr(accounting, "exported", None),
            "filtered": getattr(accounting, "filtered", None),
            "refused": getattr(accounting, "refused", None),
            "send_failed": getattr(accounting, "send_failed", None),
            "unresolved": getattr(accounting, "unresolved", None),
            "settled": getattr(accounting, "settled", None),
            "balanced": bool(accounting is not None and accounting.balanced),
        },
        "run": {
            "canary_passed": report.passed,
            "privacy_checks_passed": report.privacy.passed,
            "privacy_findings": list(report.privacy.findings),
            "collector_returncode": report.collector_returncode,
            "agent_returncode": report.agent_returncode,
            "errors": list(report.errors),
        },
        "fields": fields,
        "expected_not_observed": expected_missing,
    }

    # The scan runs on the document as serialized, so anything a future field could carry into the
    # report is caught before the report exists rather than after somebody reads it.
    leaked = scan_forbidden_output(json.dumps(body, sort_keys=True, ensure_ascii=False))
    body["privacy_scan"] = {
        "patterns_checked": [name for name, _ in FORBIDDEN_OUTPUT_PATTERNS],
        "patterns_found": list(leaked),
        "passed": not leaked,
    }
    body[gate] = (verdict or _verdict)(body, manifest)
    return body


def _verdict(body: dict, manifest: Manifest) -> dict:
    """Every condition Gate 6 requires, each reported by name so a FAIL says which one failed."""
    totals = body["totals"]
    run = body["run"]
    conditions = {
        "manifest subject is Claude Code 2.1.223": manifest.subject.get("agent_version") == "2.1.223",
        "manifest subject is Collector 0.158.0": (
            manifest.subject.get("collector_version") == certification.CERTIFIED_COLLECTOR_VERSION
        ),
        "a human recorded an approval in the manifest": manifest.is_human_approved,
        "at least one field was observed": totals["observed"] > 0,
        "every observed field is in the manifest": totals["unknown"] == 0,
        "no observed field is UNAPPROVED": totals["unapproved"] == 0,
        "no BLOCKED field was observed": totals["blocked"] == 0,
        "no REDACTED field was observed": totals["redacted"] == 0,
        "every expected field was observed": totals["expected_not_observed"] == 0,
        "the inventory remainder is zero": totals["unresolved_remainder"] == 0,
        "every observed identifier is printable": body["identifiers"][IDENTIFIER_UNPRINTABLE] == 0,
        "no span link reached the inventory": body["carriers"]["span_links"] == 0,
        "no metric exemplar reached the inventory": body["carriers"]["metric_exemplars"] == 0,
        "no forbidden value appears in this report": body["privacy_scan"]["passed"],
        "the span accounting balanced": body["span_accounting"]["balanced"],
        "the canary run passed": run["canary_passed"],
        "the Collector exited cleanly": run["collector_returncode"] == 0,
        "the agent exited cleanly": run["agent_returncode"] == 0,
    }
    return {
        "verdict": "PASS" if all(conditions.values()) else "FAIL",
        "conditions": [
            {"condition": name, "met": met} for name, met in sorted(conditions.items())
        ],
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_json(document: Mapping) -> str:
    """The canonical machine-readable report. Sorted keys, fixed indent, trailing newline."""
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def iter_field_ids(document: Mapping) -> Iterable[str]:
    """Every field identifier in a document, in document order — which is sorted order."""
    return (item["id"] for item in document.get("fields", []))


_MARKDOWN_SECTIONS = (
    ("resource.attribute", "Resource attributes"),
    ("scope", "Instrumentation scopes"),
    ("scope.attribute", "Scope attributes"),
    ("span", "Spans"),
    ("span.attribute", "Span attributes"),
    ("span.event", "Span events"),
    ("span.event.attribute", "Span event attributes"),
    ("metric", "Metrics"),
    ("metric.datapoint.attribute", "Metric data-point attributes"),
    ("unresolved", "Unresolved remainder"),
)

# Which detail columns each section shows, in order. Every column is a bounded description, a
# fingerprint, or a count; none of them can hold a value that did not come through the allowlist.
_ATTRIBUTE_COLUMNS = ("key", "identifier_status", "fingerprint", "types", "value_classes", "values")
_MARKDOWN_COLUMNS = {
    "resource.attribute": ("signal", *_ATTRIBUTE_COLUMNS),
    "scope": ("signal", "name", "identifier_status", "fingerprint", "version_classes"),
    "scope.attribute": _ATTRIBUTE_COLUMNS,
    "span": ("name", "identifier_status", "fingerprint", "kinds", "status_codes", "link_count"),
    "span.attribute": _ATTRIBUTE_COLUMNS,
    "span.event": ("span", "name", "identifier_status", "fingerprint"),
    "span.event.attribute": _ATTRIBUTE_COLUMNS,
    "metric": (
        "name",
        "identifier_status",
        "fingerprint",
        "descriptions",
        "description_classes",
        "units",
        "unit_classes",
        "data_types",
        "aggregation_temporalities",
        "monotonicity",
        "exemplar_count",
    ),
    "metric.datapoint.attribute": _ATTRIBUTE_COLUMNS,
    "unresolved": ("context",),
}


def render_markdown(document: Mapping) -> str:
    """The human-readable report. Rendered only from ``document``, so the two always agree."""
    totals = document["totals"]
    gate = document["gate6"]
    subject = document["subject"]
    lines = [
        "# Claude telemetry field inventory (Gate 6)",
        "",
        f"- schema: `{document['schema']}`",
        f"- agent: `{subject['agent']}` `{subject['agent_version']}`",
        f"- collector: `{subject['collector_version']}`",
        f"- contract: `{subject['contract_version']}`",
        f"- approval manifest: `{document['manifest']['path']}` version "
        f"`{document['manifest']['version']}` "
        f"(review state `{document['manifest']['review_state']}`)",
        "",
        f"## Gate 6 verdict: {gate['verdict']}",
        "",
        "| Condition | Met |",
        "| --- | --- |",
    ]
    for condition in gate["conditions"]:
        lines.append(f"| {condition['condition']} | {'yes' if condition['met'] else '**NO**'} |")

    lines += [
        "",
        "## Field accounting",
        "",
        "| Bucket | Fields |",
        "| --- | --- |",
    ]
    for label in (
        "observed",
        "approved",
        "blocked",
        "redacted",
        "unapproved",
        "unknown",
        "expected_not_observed",
        "unresolved_remainder",
    ):
        lines.append(f"| {label.replace('_', ' ')} | {totals[label]} |")

    lines += [
        "",
        "## Schema identifiers",
        "",
        "| Disposition | Entries |",
        "| --- | --- |",
        f"| known to the manifest | {document['identifiers'][IDENTIFIER_KNOWN]} |",
        f"| unknown, safe to print, reviewable below | {document['identifiers'][IDENTIFIER_UNKNOWN_SAFE]} |",
        f"| unknown, unprintable, fingerprinted | {document['identifiers'][IDENTIFIER_UNPRINTABLE]} |",
    ]

    carriers = document["carriers"]
    accounting = document["span_accounting"]
    run = document["run"]
    lines += [
        "",
        "## Unredactable carriers and span accounting",
        "",
        "| Reading | Value |",
        "| --- | --- |",
        f"| span links reaching the inventory | {carriers['span_links']} |",
        f"| metric exemplars reaching the inventory | {carriers['metric_exemplars']} |",
        f"| bounded diagnostic records (counted, not inventoried) | {carriers['diagnostic_records']} |",
        f"| receiver accepted | {accounting['accepted']} |",
        f"| exporter sent | {accounting['exported']} |",
        f"| deliberately filtered | {accounting['filtered']} |",
        f"| receiver refused | {accounting['refused']} |",
        f"| exporter send-failed | {accounting['send_failed']} |",
        f"| unresolved span remainder | {accounting['unresolved']} |",
        f"| counters settled | {accounting['settled']} |",
        f"| privacy checks passed | {run['privacy_checks_passed']} |",
        f"| collector exit | {run['collector_returncode']} |",
        f"| agent exit | {run['agent_returncode']} |",
    ]

    fields = list(document["fields"])
    for kind, title in _MARKDOWN_SECTIONS:
        section = [item for item in fields if item["kind"] == kind]
        columns = _MARKDOWN_COLUMNS[kind]
        lines += ["", f"## {title}", ""]
        if not section:
            lines.append("_none observed_")
            continue
        header = ["id", *columns, "occurrences", "status", "reasons"]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join("---" for _ in header) + " |")
        for item in section:
            cells = [f"`{item['id']}`"]
            cells += [_cell(item.get(column)) for column in columns]
            cells += [str(item["occurrences"]), item["status"], _cell(item.get("reasons"))]
            lines.append("| " + " | ".join(cells) + " |")

    lines += ["", "## Expected by the manifest but not observed", ""]
    if document["expected_not_observed"]:
        for identifier in document["expected_not_observed"]:
            lines.append(f"- `{identifier}`")
    else:
        lines.append("_none_")

    scan = document["privacy_scan"]
    lines += [
        "",
        "## Forbidden-output scan",
        "",
        f"- patterns checked: {len(scan['patterns_checked'])}",
        f"- patterns found: {', '.join(scan['patterns_found']) or 'none'}",
        f"- result: {'pass' if scan['passed'] else 'FAIL'}",
    ]
    if run["errors"]:
        lines += ["", "## Run errors", ""]
        lines += [f"- {error}" for error in run["errors"]]
    if run["privacy_findings"]:
        lines += ["", "## Canary privacy findings", ""]
        lines += [f"- {finding}" for finding in run["privacy_findings"]]
    return "\n".join(lines) + "\n"


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(f"`{item}`" for item in value) or "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    return f"`{value}`" if isinstance(value, str) else str(value)


# ---------------------------------------------------------------------------
# The candidate manifest
# ---------------------------------------------------------------------------
#
# The artifact that makes the review self-contained. It proposes, in copy-and-paste form, exactly the
# manifest additions the observed run needs in order for nothing to be UNKNOWN -- and it proposes
# nothing else. It carries:
#
#   * vocabulary additions, drawn only from identifiers that passed the safe-identifier policy;
#   * one `fields:` entry per unknown field, with `status: UNAPPROVED` and the observed *classes*.
#
# It carries no telemetry value: no attribute value, no unit, no description, not even an allowlisted
# one. Widening `static_values` is a decision a human takes deliberately, and a machine-written
# proposal is not the place for it. Where a field carries a value nothing can describe, the proposal
# says so in a comment instead of proposing `unclassifiable`.

CANDIDATE_ENUM_CONSTRAINTS = (
    "types",
    "value_classes",
    "kinds",
    "status_codes",
    "data_types",
    "aggregation_temporalities",
    "monotonicity",
    "unit_classes",
    "description_classes",
    "version_classes",
)


def build_candidate_manifest(document: Mapping) -> str:
    """Render the deterministic candidate manifest for one inventory document."""
    unknown = [item for item in document["fields"] if item["status"] == DISPOSITION_UNKNOWN]
    printable = [item for item in unknown if item.get("identifier_status") != IDENTIFIER_UNPRINTABLE]
    withheld = [item for item in unknown if item.get("identifier_status") == IDENTIFIER_UNPRINTABLE]

    additions: dict[str, set[str]] = {section: set() for section in _VOCABULARIES}
    for item in document["fields"]:
        if item.get("identifier_status") != IDENTIFIER_UNKNOWN_SAFE:
            continue
        section = IDENTIFIER_VOCABULARY.get(item.get("identifier_category", ""), "")
        name = item.get("key") or item.get("name") or ""
        if section and name:
            additions[section].add(name)

    lines = [
        "# Candidate manifest additions -- A PROPOSAL, NOT AN APPROVAL.",
        "#",
        "# Written by tests/otel_inventory.py from one observed run. Nothing here is approved, and",
        "# copying it in approves nothing: every proposed field arrives as UNAPPROVED, and the Gate 6",
        "# verdict still requires a named reviewer in ops/otel/claude-field-approvals.yaml.",
        "#",
        "# How to use it: merge `vocabulary` into the manifest's `vocabulary`, append `fields` to the",
        "# manifest's `fields`, then decide each entry. Re-run the inventory afterwards -- entries you",
        "# left UNAPPROVED will still fail, which is the point.",
        "#",
        "# What is deliberately absent: every telemetry value. No attribute value, unit, or",
        "# description appears here, because widening `static_values` is a decision to take with the",
        f"# value in front of you. Identifiers that failed the safe-identifier policy ({len(withheld)}",
        "# of them) are also absent -- they are fingerprinted in the inventory report and need",
        "# investigation, not approval.",
        "",
        f"# proposed vocabulary additions: {sum(len(v) for v in additions.values())}",
        f"# proposed field entries:        {len(printable)}",
        f"# withheld, unprintable:         {len(withheld)}",
        "",
        "vocabulary:",
    ]
    for section in _VOCABULARIES:
        names = sorted(additions[section])
        if not names:
            lines.append(f"  {section}: []")
            continue
        lines.append(f"  {section}:")
        lines += [f"    - {name}" for name in names]

    lines += ["", "fields:"]
    if not printable:
        lines.append("  []")
    for item in printable:
        lines.append(f"  - id: {item['id']}")
        lines.append("    status: UNAPPROVED")
        needs: list[str] = []
        for key in CANDIDATE_ENUM_CONSTRAINTS:
            observed = item.get(key)
            if not observed:
                continue
            if CLASS_UNCLASSIFIABLE in observed:
                needs.append(key)
                continue
            lines.append(f"    {key}: [{', '.join(_yaml_scalar(v) for v in observed)}]")
        if needs:
            lines.append(
                f"    # review: {', '.join(needs)} carried an unclassifiable value. Read it in the"
            )
            lines.append(
                "    #         run's Collector log, then either allowlist it under static_values or"
            )
            lines.append("    #         reject this field. It cannot be approved as it stands.")
    return "\n".join(lines) + "\n"


def _yaml_scalar(value: str) -> str:
    """Quote a scalar only when it needs it, so the proposal reads like hand-written YAML."""
    return value if re.fullmatch(r"[A-Za-z0-9_.-]+", value) else json.dumps(value)


# ---------------------------------------------------------------------------
# Writing the reports
# ---------------------------------------------------------------------------


class OutputDirectoryRefused(Exception):
    """The output directory already holds something this run will not overwrite."""


def prepare_output_directory(path: Path, *, replace_existing: bool = False) -> Path:
    """Create ``path``, refusing to disturb anything already there.

    With ``replace_existing`` the report files this harness owns are removed and rewritten. Anything
    else in the directory still stops the run: `--replace-existing` is a re-run switch, not a licence
    to delete an operator's evidence.
    """
    if path.exists() and not path.is_dir():
        raise OutputDirectoryRefused(f"{path} exists and is not a directory")
    if path.is_dir():
        present = sorted(item.name for item in path.iterdir())
        if present and not replace_existing:
            raise OutputDirectoryRefused(
                f"{path} is not empty; refusing to overwrite an existing inventory. "
                "Re-run with --replace-existing to replace this harness's own report files."
            )
        foreign = [name for name in present if name not in OWNED_REPORT_NAMES]
        if foreign:
            raise OutputDirectoryRefused(
                f"{path} holds files this harness did not write ({', '.join(foreign)}); "
                "refusing to replace them. Choose an empty output directory."
            )
        for name in present:
            (path / name).unlink()
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_reports(document: Mapping, output_dir: Path) -> tuple[Path, Path, Path]:
    """Write the JSON, Markdown, and candidate-manifest files — or stubs instead of leaking ones.

    The final bytes of all three are scanned again here. A report that would publish something
    forbidden is not written: a stub naming the patterns that matched takes its place, so the failure
    is visible without the finding being the leak.
    """
    json_text = render_json(document)
    markdown_text = render_markdown(document)
    candidate_text = build_candidate_manifest(document)
    leaked = tuple(
        sorted(
            set(scan_forbidden_output(json_text))
            | set(scan_forbidden_output(markdown_text))
            | set(scan_forbidden_output(candidate_text))
        )
    )
    json_path = output_dir / REPORT_JSON_NAME
    markdown_path = output_dir / REPORT_MARKDOWN_NAME
    candidate_path = output_dir / CANDIDATE_MANIFEST_NAME
    if leaked:
        stub = {
            "schema": SCHEMA_VERSION,
            "gate6": {"verdict": "FAIL", "conditions": []},
            "withheld": {
                "reason": "the rendered report matched a forbidden-output pattern and was withheld",
                "patterns_found": list(leaked),
            },
        }
        _write(json_path, render_json(stub))
        _write(
            markdown_path,
            "# Claude telemetry field inventory (Gate 6)\n\n"
            "## Gate 6 verdict: FAIL\n\n"
            "The rendered report matched a forbidden-output pattern and was withheld.\n\n"
            + "".join(f"- `{name}`\n" for name in leaked),
        )
        _write(
            candidate_path,
            "# Withheld: the inventory matched a forbidden-output pattern, so no candidate\n"
            "# manifest was produced. Nothing here may be approved.\n"
            + "".join(f"# - {name}\n" for name in leaked),
        )
        return json_path, markdown_path, candidate_path
    _write(json_path, json_text)
    _write(markdown_path, markdown_text)
    _write(candidate_path, candidate_text)
    return json_path, markdown_path, candidate_path


def _write(path: Path, text: str) -> None:
    """Write UTF-8 with LF endings on every platform, so a report hashes the same everywhere."""
    path.write_text(text, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def inventory_configuration() -> dict:
    """The Collector configuration an inventory run executes.

    The shipped file plus the bounded span-name diagnostic, and **nothing else**. The inventory adds
    no receiver, no exporter, no connector, and no pipeline, so there is no route by which telemetry
    could reach it without first passing `memory_limiter`, `filter/unredactable`, and
    `transform/privacy` on the `traces` and `metrics` pipelines.
    """
    return certification.diagnostic_configuration(certification.load_configuration())


def _emptied(report: canary.CanaryReport, reason: str) -> canary.CanaryReport:
    """The same run with no exporter output, so a failed inventory still renders a whole report."""
    return canary.CanaryReport(
        accounting=report.accounting,
        privacy=report.privacy,
        collector_returncode=report.collector_returncode,
        agent_returncode=report.agent_returncode,
        errors=report.errors + (reason,),
        output="",
    )


@dataclass(frozen=True)
class InventoryRun:
    canary: canary.CanaryReport
    document: dict
    json_path: Path | None
    markdown_path: Path | None
    candidate_path: Path | None
    errors: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.errors and self.document.get("gate6", {}).get("verdict") == "PASS"

    def render(self) -> str:
        gate = self.document.get("gate6", {})
        totals = self.document.get("totals", {})
        identifiers = self.document.get("identifiers", {})
        lines = [
            "FIELD INVENTORY",
            f"  fields observed        {totals.get('observed')}",
            f"  approved               {totals.get('approved')}",
            f"  blocked                {totals.get('blocked')}",
            f"  redacted               {totals.get('redacted')}",
            f"  unapproved             {totals.get('unapproved')}",
            f"  unknown                {totals.get('unknown')}",
            f"  expected not observed  {totals.get('expected_not_observed')}",
            f"  unresolved remainder   {totals.get('unresolved_remainder')}",
            "",
            "SCHEMA IDENTIFIERS",
            f"  known                  {identifiers.get(IDENTIFIER_KNOWN)}",
            f"  unknown, printable     {identifiers.get(IDENTIFIER_UNKNOWN_SAFE)}",
            f"  unknown, fingerprinted {identifiers.get(IDENTIFIER_UNPRINTABLE)}",
            "",
        ]
        for condition in gate.get("conditions", []):
            if not condition["met"]:
                lines.append(f"  UNMET {condition['condition']}")
        for error in self.errors:
            lines.append(f"  ERROR {error}")
        lines.append("")
        lines.append(self.canary.render())
        lines.append("")
        lines.append(f"GATE 6 {gate.get('verdict', 'FAIL')}")
        return "\n".join(lines)


def run_inventory(
    *,
    binary: Path,
    workdir: Path,
    output_dir: Path,
    agent: Callable[[dict, Path], int | None],
    manifest: Manifest,
    replace_existing: bool = False,
    startup_timeout: float = 60.0,
    settle_timeout: float = 180.0,
) -> InventoryRun:
    """Run one canary and inventory what it exported.

    The output directory is prepared **before** the Collector starts, so a refusal starts nothing.
    The Collector and the child are reaped inside :func:`otel_canary.run_canary`, which returns
    before any report is built — so a failure in report generation cannot leave a process behind,
    and is recorded rather than raised.
    """
    prepare_output_directory(output_dir, replace_existing=replace_existing)
    report = canary.run_canary(
        binary=binary,
        workdir=workdir,
        agent=agent,
        config=inventory_configuration(),
        startup_timeout=startup_timeout,
        settle_timeout=settle_timeout,
    )
    errors: list[str] = []
    try:
        document = build_document(report, manifest)
    except Exception as error:  # noqa: BLE001 - the report is the deliverable, not a traceback
        reason = f"the inventory could not be built: {type(error).__name__}: {error}"
        errors.append(reason)
        # Rebuilt from an empty export rather than hand-assembled, so the failing report has the
        # same shape as a passing one and every accounting line is present and zero.
        document = build_document(_emptied(report, reason), manifest)
    paths: tuple[Path | None, Path | None, Path | None] = (None, None, None)
    try:
        paths = write_reports(document, output_dir)
    except Exception as error:  # noqa: BLE001 - a report that cannot be written is a result
        errors.append(f"the report could not be written: {type(error).__name__}: {error}")
    return InventoryRun(
        canary=report,
        document=document,
        json_path=paths[0],
        markdown_path=paths[1],
        candidate_path=paths[2],
        errors=tuple(errors),
    )


# ---------------------------------------------------------------------------
# The Claude agent, and what it is allowed to be
# ---------------------------------------------------------------------------
#
# The canonical form of the canary invocation is an *argument vector*, not a command string. A string
# has to survive PowerShell quoting and then `shlex` quoting before it becomes argv, and every one of
# those hops is a place for an argument to be joined to its neighbour or lost. The vector below is
# what `subprocess` receives; the command line an operator types does not contain it at all.

CANARY_PROMPT = "Reply with exactly CANARY_OK and nothing else."

DEFAULT_CLAUDE_ARGV: tuple[str, ...] = (
    "claude",
    # One shot. No interactive session, no follow-up turn.
    "-p",
    CANARY_PROMPT,
    # Pinned, so a default-model change is not a silent change to what the canary exercised.
    "--model",
    "sonnet",
    # Non-bypass. `dontAsk` suppresses the prompt for a non-interactive run without granting
    # anything; `bypassPermissions`, `acceptEdits`, `auto`, `manual`, and `plan` are the other
    # choices the CLI offers and none of them is this one.
    "--permission-mode",
    "dontAsk",
    # No tools at all. The CLI documents the empty string as the way to say so: "Use "" to disable
    # all tools". This is `--tools`, not `--allowedTools`: the latter pre-approves a list rather
    # than restricting the set, and is refused below.
    "--tools",
    "",
    # No slash command or skill can run, so nothing in a user or project configuration alters it.
    "--disable-slash-commands",
    # One turn.
    "--max-turns",
    "1",
)

# The permission mode this canary runs under, and the only one it accepts. The CLI's full choice set
# is `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan`; the other five are
# refused by `resolve_agent_argv`.
REQUIRED_PERMISSION_MODE = "dontAsk"

# Every flag in `DEFAULT_CLAUDE_ARGV`, and how it was verified against the CLI this canary targets.
# Recorded here rather than in a commit message because a vendor upgrade that renames or drops one
# of these is a drift event under contract section 10, and this is what a reviewer diffs against.
#
# Verified against Claude Code 2.1.223 on 2026-08-06 by reading `claude --help` and by parsing the
# complete vector through `subprocess` with a deliberate unknown trailing flag, which makes the CLI
# stop at its unknown-option check *before* the action handler -- so the whole vector was proven to
# parse without a model request being made.
VERIFIED_CLAUDE_FLAGS = {
    "-p": "in --help: '-p, --print  Print response and exit'",
    "--model": "in --help: '--model <model>', with 'sonnet' named as a valid alias",
    "--permission-mode": "in --help, choices acceptEdits|auto|bypassPermissions|manual|dontAsk|plan",
    "--tools": "in --help: '--tools <tools...>  Use \"\" to disable all tools'",
    "--disable-slash-commands": "in --help: 'Disable all skills'",
    # The one flag the vector uses that `--help` does not list. It is accepted: with unknown options
    # the CLI reports the *first* unrecognized flag, and a run passing `--max-turns 1` followed by a
    # bogus flag reports the bogus one. Undocumented, so a version bump must re-check it.
    "--max-turns": "accepted by 2.1.223 but absent from --help; re-verify on any version change",
}

# Flags that would make the run something other than the canary that was approved. All four exist in
# Claude Code 2.1.223: the first two grant permission authority the approval did not, and the last
# two set a tool policy that would displace `--tools ""`.
#
# `--bypassPermissions` and `--acceptEdits` are deliberately *not* here: they are values of
# `--permission-mode`, not flags, and are refused by the permission-mode check instead.
BYPASS_FLAGS = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--allowedtools",
    "--allowed-tools",
    "--disallowedtools",
    "--disallowed-tools",
)

CLAUDE_AGENT_TIMEOUT = 300.0


def resolve_agent_argv(command: str = "") -> tuple[tuple[str, ...], list[str]]:
    """Return ``(argv, problems)`` for an operator-supplied command, or the default vector.

    An empty ``command`` yields :data:`DEFAULT_CLAUDE_ARGV` unchanged — which is the ordinary path,
    because the canary invocation is not something an operator should have to retype.

    A supplied command must parse to the same vector, differing at most in the path used to reach
    the `claude` executable. Anything else is a conflict and is reported argument by argument;
    "close enough" is how a canary ends up running with tools enabled.
    """
    if not command.strip():
        return DEFAULT_CLAUDE_ARGV, []
    try:
        argv = tuple(shlex.split(command))
    except ValueError as error:
        return (), [f"the agent command could not be parsed: {error}"]
    if not argv:
        return (), ["the agent command is empty"]

    problems: list[str] = []
    lowered = [argument.lower() for argument in argv]
    for flag in BYPASS_FLAGS:
        if flag in lowered:
            problems.append(f"the agent command must not pass {flag}")
    if REQUIRED_PERMISSION_MODE not in argv and "--permission-mode" in argv:
        problems.append(f"--permission-mode must be {REQUIRED_PERMISSION_MODE}")
    if Path(argv[0]).stem.lower() != "claude":
        problems.append("the agent command must invoke `claude`")
    if argv[1:] != DEFAULT_CLAUDE_ARGV[1:]:
        missing = [item for item in DEFAULT_CLAUDE_ARGV[1:] if item not in argv[1:]]
        extra = [item for item in argv[1:] if item not in DEFAULT_CLAUDE_ARGV[1:]]
        problems.append(
            "the agent command conflicts with the approved canary vector"
            + (f"; missing {missing}" if missing else "")
            + (f"; unexpected {extra}" if extra else "")
            + ("; same arguments in a different order" if not missing and not extra else "")
        )
    return argv, problems


# Where the certified Collector lives on the supervised machine. Only used to render the command;
# the harness itself still locates the binary through OTELCOL_PATH at run time.
COLLECTOR_PATH_HINT = r"C:\Tools\OpenTelemetry\otelcol-contrib\0.158.0\otelcol-contrib.exe"


def render_powershell_command(
    output_dir: str,
    approved_by: str,
    *,
    replace_existing: bool = False,
    collector_path: str = COLLECTOR_PATH_HINT,
) -> str:
    """The exact PowerShell invocation for the later supervised run, built from the parser itself.

    The agent vector deliberately does **not** appear: it is a constant in this module, so there is
    no command string for a shell to mangle and no way for an argument to be lost in the quoting.
    """
    lines = [
        f"$env:OTELCOL_PATH = {_powershell_literal(collector_path)}",
        ".\\.venv\\Scripts\\python.exe tests\\otel_inventory.py `",
        f"    --output-dir {_powershell_literal(output_dir)} `",
        f"    --approved-by {_powershell_literal(approved_by)}" + (" `" if replace_existing else ""),
    ]
    if replace_existing:
        lines.append("    --replace-existing")
    return "\n".join(lines)


def _powershell_literal(value: str) -> str:
    """A PowerShell single-quoted literal. Nothing inside is expanded; `'` is doubled."""
    return "'" + value.replace("'", "''") + "'"


def claude_agent(
    argv: Sequence[str] = DEFAULT_CLAUDE_ARGV,
    *,
    invocation_id: str = "",
    timeout: float = CLAUDE_AGENT_TIMEOUT,
) -> Callable[[dict, Path], int | None]:
    """The real agent: a Claude child built by `build_claude_child_environment`, in a temporary cwd.

    ``argv`` is a vector, never a string: it is handed to `subprocess` as it stands, so no quoting
    layer can join or truncate an argument between here and the child.

    The working directory is a fresh temporary directory that is removed on every path, including a
    timeout and an exception. It is where the child's own stdout lands, which is why it is not the
    canary workdir: `claude -p` prints its reply, and a reply is content this slice does not keep.
    """
    sys.path.insert(0, str(certification.REPO_ROOT / "src"))
    from codex_forge import agent_telemetry

    vector = list(argv)

    def agent(ports: dict, workdir: Path) -> int | None:
        context = agent_telemetry.InvocationContext(
            invocation_id=invocation_id or agent_telemetry.new_invocation_id(),
            contract_version=agent_telemetry.AGENT_TELEMETRY_CONTRACT_VERSION,
            traceparent=None,
        )
        environment = agent_telemetry.build_claude_child_environment(
            os.environ, context, endpoint=f"http://127.0.0.1:{ports['grpc']}"
        )
        isolated = tempfile.mkdtemp(prefix="forge-claude-inventory-")
        try:
            return canary.subprocess_agent(vector, environment, timeout=timeout)(ports, Path(isolated))
        finally:
            shutil.rmtree(isolated, ignore_errors=True)

    return agent


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

APPROVAL_REQUIRED = (
    "Refusing to launch an agent. A Gate 6 inventory run is a supervised event: re-run with "
    "--approved-by naming the human who approved it. Without it this harness starts nothing."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="otel_inventory",
        description=(
            "Run one supervised Claude canary and inventory every field that survived the certified "
            "privacy pipeline. Launches nothing without --approved-by."
        ),
    )
    parser.add_argument("--output-dir", type=Path, required=True, help="where the reports are written")
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="where the Collector writes its log; defaults to <output-dir>-run",
    )
    parser.add_argument(
        "--approved-by",
        default="",
        help="the human who approved this supervised run; required before anything is launched",
    )
    parser.add_argument(
        "--agent-command",
        default="",
        help=(
            "override the Claude command line; must parse to the approved canary vector, which is "
            "used by default and needs no override"
        ),
    )
    parser.add_argument(
        "--invocation-id",
        default="",
        help="forge.invocation.id for the child; a fresh random one is used when omitted",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="replace this harness's own report files in an existing output directory",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=MANIFEST_PATH,
        help="the approval manifest to compare the observed inventory against",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved agent vector and the PowerShell invocation, and start nothing",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)

    vector, problems = resolve_agent_argv(arguments.agent_command)
    if problems:
        for problem in problems:
            print(f"Refusing this agent command: {problem}", file=sys.stderr)
        return 2

    if arguments.dry_run:
        # Prints and exits. No approval is required because nothing is started, no output directory
        # is touched, and no Collector binary is located.
        print("agent argv:")
        for index, argument in enumerate(vector):
            print(f"  [{index}] {argument!r}")
        print("")
        print("powershell:")
        print(
            render_powershell_command(
                str(arguments.output_dir),
                arguments.approved_by or "<the human approving this run>",
                replace_existing=arguments.replace_existing,
            )
        )
        return 0

    # Approval is checked before the manifest is read, before the binary is located, and before the
    # output directory is touched. Nothing at all happens without it.
    if not arguments.approved_by.strip():
        print(APPROVAL_REQUIRED, file=sys.stderr)
        return 2

    try:
        manifest = load_manifest(arguments.manifest)
    except ManifestError as error:
        print(error, file=sys.stderr)
        return 2

    try:
        binary = certification.collector_binary()
        installed = certification.collector_version(binary)
    except (certification.CollectorUnavailable, certification.CollectorFailed) as error:
        print(error, file=sys.stderr)
        return 2
    if installed != certification.CERTIFIED_COLLECTOR_VERSION:
        print(
            f"Collector {installed} is not the certified {certification.CERTIFIED_COLLECTOR_VERSION}; "
            "re-run Gate 3 before an inventory run.",
            file=sys.stderr,
        )
        return 2

    output_dir = arguments.output_dir
    workdir = arguments.workdir or output_dir.with_name(output_dir.name + "-run")
    try:
        result = run_inventory(
            binary=binary,
            workdir=workdir,
            output_dir=output_dir,
            agent=claude_agent(vector, invocation_id=arguments.invocation_id),
            manifest=manifest,
            replace_existing=arguments.replace_existing,
        )
    except OutputDirectoryRefused as error:
        print(error, file=sys.stderr)
        return 2
    print(result.render())
    return 0 if result.passed else 1


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
