"""Support code for Gate 3 of the shared agent telemetry contract.

Gate 3 requires proving, with **synthetic** payloads, that the local Collector strips identity and
content from every telemetry surface before anything is batched or exported. Nothing in this module
calls Claude, Codex, a Forge task, or any network service outside the loopback interface, and
nothing here downloads a Collector.

Two independent kinds of evidence live here:

* :func:`certify_configuration` reads ``ops/otel/collector-config.yaml`` and reports every way in
  which it fails the contract. This is what the fail-closed tests mutate.
* :func:`run_collector` starts a **real** installed Collector on temporary loopback ports, feeds it
  the hostile payloads below over OTLP/HTTP, and returns what the debug exporter printed *after*
  processing. A static assertion about YAML cannot prove redaction; this can.
"""

from __future__ import annotations

import copy
import json
import os
import re
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "ops" / "otel" / "collector-config.yaml"

# The Collector this slice was certified against. Contract section 10 makes a Collector upgrade a
# drift event, so the version is recorded rather than assumed.
CERTIFIED_COLLECTOR_VERSION = "0.158.0"

PRIVACY_PROCESSOR = "transform/privacy"
UNREDACTABLE_FILTER = "filter/unredactable"
BATCH_PROCESSOR = "batch"
MEMORY_LIMITER = "memory_limiter"

# Span links and metric exemplars carry their own attribute maps and no Collector 0.158.0 processor
# can reach them, so their carriers are dropped instead. Both conditions must survive editing.
UNREDACTABLE_CONDITIONS = {
    ("traces", "span"): "Len(span.links) > 0",
    ("metrics", "datapoint"): "Len(datapoint.exemplars) > 0",
}

# Contract section 6, "minimum deletion set", verbatim and in order.
FORBIDDEN_ATTRIBUTE_KEYS = (
    "user.email",
    "organization.id",
    "user.id",
    "user.account_uuid",
    "user.account_id",
    "session.id",
    "ccr.session.id",
    "workspace.host_paths",
    "user_prompt",
    "system_prompt",
    "system_prompt_preview",
    "user_system_prompt",
    "assistant_response",
    "response",
    "tool_input",
    "tool_parameters",
    "tool_output",
    "payload",
    "content",
    "error",
    "error.message",
    "url.full",
    "url.query",
)

# Bounded operational fields the configuration must keep carrying. The first three are the Forge
# resource; the rest are the certified Forge span contract plus the two correlation attributes
# contract sections 4 and 10 require of any future agent slice.
REQUIRED_ALLOWED_ATTRIBUTE_KEYS = (
    "service.name",
    "service.version",
    "deployment.environment",
    "forge.telemetry.contract.version",
    "forge.invocation.id",
    "forge.task.id",
    "forge.project.id",
    "forge.task.risk",
    "forge.task.max_attempts",
    "forge.task.outcome",
    "forge.attempt.number",
    "forge.attempt.kind",
    "forge.attempt.outcome",
    "forge.attempt.terminated_by",
    "forge.retry.bucket",
    "forge.phase.name",
    "forge.phase.cycle",
    "forge.phase.outcome",
    "forge.phase.duration_ms",
    "forge.agent.role",
    "gen_ai.request.model",
    "gen_ai.request.reasoning_effort",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.cached_input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.reasoning_tokens",
    "gen_ai.token.type",
    "forge.usage.fresh_input_tokens",
    "forge.usage.raw_total_tokens",
    "forge.usage.cache_hit_ratio",
    "forge.usage.accounting_basis",
    "forge.usage.budget_basis",
    "forge.usage.token_type",
    "error.type",
    "error.code",
)

# The surfaces the privacy processor must cover, per signal. Contract section 6 names span, span
# event, metric, and resource attributes; `scope` is carried here as well because scope attributes
# are a fifth carrier none of those four names covers.
REQUIRED_TRACE_SURFACES = ("resource", "scope", "span", "spanevent")
REQUIRED_METRIC_SURFACES = ("resource", "scope", "datapoint")
REQUIRED_PIPELINES = ("traces", "metrics")
FORBIDDEN_PIPELINES = ("logs",)
ALLOWED_EXPORTERS = ("debug",)

# ---------------------------------------------------------------------------
# Bounded span-name diagnostic
# ---------------------------------------------------------------------------
#
# `otelcol_receiver_accepted_spans` and `otelcol_exporter_sent_spans` answer *how many* spans a run
# lost between the receiver and the exporter. They cannot answer *which*, and the obvious way to find
# out -- printing the spans that did not make it -- is exactly what the privacy policy forbids, since
# the spans in question are the ones carrying a carrier no processor can redact.
#
# The diagnostic below answers "which" without exporting anything from the spans. Two `count`
# connectors convert traces to metrics: one fed straight off the receiver, one attached to the end of
# the certified traces pipeline. A `count` connector configured without an `attributes` block emits a
# bare counter -- no trace id, no span id, no link, no attribute of any kind survives the conversion,
# because a counter has nowhere to put one. The bucket is carried by the *metric name*, drawn from
# the closed vocabulary below and written by this file rather than by the span.
#
# Everything else is stripped again downstream. See `diagnostic_configuration`.

DIAGNOSTIC_SPAN_NAMES = (
    "forge.task",
    "forge.attempt",
    "forge.phase",
    "claude_code.interaction",
    "claude_code.llm_request",
    "claude_code.tool",
    "claude_code.tool.execution",
    "claude_code.subagent.spawn",
    "claude_code.hook",
    "claude_code.compaction",
)

# Every name outside the vocabulary lands here. A span name is agent-controlled and therefore
# unbounded; without this bucket an unrecognized name would either be published verbatim or silently
# vanish from the accounting, and both are worse than "one span was something we do not have a name
# for".
DIAGNOSTIC_OTHER_BUCKET = "other"
DIAGNOSTIC_BUCKETS = DIAGNOSTIC_SPAN_NAMES + (DIAGNOSTIC_OTHER_BUCKET,)

# `accepted` is counted before the privacy pipeline, `exported` after it. The difference, by bucket,
# is the answer the accounting exists to produce.
DIAGNOSTIC_STAGES = ("accepted", "exported")
DIAGNOSTIC_METRIC_PREFIX = "forge.diagnostic.span"
DIAGNOSTIC_CONNECTORS = {stage: f"count/diagnostic_{stage}" for stage in DIAGNOSTIC_STAGES}
DIAGNOSTIC_ACCEPTED_PIPELINE = "traces/diagnostic_accepted"
DIAGNOSTIC_METRICS_PIPELINE = "metrics/diagnostic"
DIAGNOSTIC_VOCABULARY_FILTER = "filter/diagnostic_vocabulary"
DIAGNOSTIC_SCRUB_PROCESSOR = "transform/diagnostic_scrub"

# The `count` connector ships defaults for the surfaces the configuration does not mention, so a
# configuration that names only `spans` still emits `trace.span.event.count`. That metric is outside
# the vocabulary, so it is suppressed at the source with a condition no span event can satisfy, and
# denied again by the vocabulary filter. Verified against 0.158.0: `spanevents: {}` and
# `spanevents: null` both leave the default in place; only a never-true condition removes it.
DIAGNOSTIC_NEVER = "1 == 2"


def diagnostic_metric_name(stage: str, bucket: str) -> str:
    """The one metric name a (stage, bucket) pair is allowed to produce."""
    if stage not in DIAGNOSTIC_STAGES:
        raise ValueError(f"unknown diagnostic stage: {stage}")
    if bucket not in DIAGNOSTIC_BUCKETS:
        raise ValueError(f"unknown diagnostic bucket: {bucket}")
    return f"{DIAGNOSTIC_METRIC_PREFIX}.{stage}.{bucket.replace('.', '_')}"


def diagnostic_metric_names() -> tuple[str, ...]:
    """Every metric name the diagnostic may emit. Anything else is a defect, not a new bucket."""
    return tuple(
        diagnostic_metric_name(stage, bucket)
        for stage in DIAGNOSTIC_STAGES
        for bucket in DIAGNOSTIC_BUCKETS
    )


def _count_connector(stage: str) -> dict:
    """One `count` connector: a counter per bucket, and no `attributes` block anywhere.

    The absent `attributes` block is the whole privacy argument. With it, the connector would copy
    the named span attributes onto the counter's data points; without it, the counter carries no
    dimension at all and there is no path from a span attribute, link, or event to the output.
    """
    spans = {
        diagnostic_metric_name(stage, name): {
            "description": f"spans named {name}, counted {stage}",
            "conditions": [f'span.name == "{name}"'],
        }
        for name in DIAGNOSTIC_SPAN_NAMES
    }
    spans[diagnostic_metric_name(stage, DIAGNOSTIC_OTHER_BUCKET)] = {
        "description": f"spans named outside the diagnostic vocabulary, counted {stage}",
        "conditions": [" and ".join(f'span.name != "{name}"' for name in DIAGNOSTIC_SPAN_NAMES)],
    }
    return {
        "spans": spans,
        "spanevents": {
            f"{DIAGNOSTIC_METRIC_PREFIX}.spanevent.suppressed": {
                "description": "never emitted; displaces the connector's default span-event metric",
                "conditions": [DIAGNOSTIC_NEVER],
            }
        },
    }


def diagnostic_configuration(config: dict) -> dict:
    """Return ``config`` plus the bounded span-name diagnostic. The original is not modified.

    This is a **temporary certification** configuration. It is built here, in test code, from the
    shipped file; it is never written back to `ops/otel/collector-config.yaml`, because the pipeline
    it adds deliberately does not satisfy `certify_configuration` -- `traces/diagnostic_accepted`
    bypasses `filter/unredactable` on purpose, which is the only way to see what that filter dropped.
    That bypass is safe *only* because the pipeline terminates in a counter. It would not be safe
    with any exporter that can print a span, which is why it does not live in the shipped file.

    Three layers keep the diagnostic output inside the vocabulary:

    1. the connectors emit only the names in `diagnostic_metric_names`, with no attribute block;
    2. `filter/diagnostic_vocabulary` denies any metric name outside that list, so a future
       Collector default cannot appear merely because nobody predicted it;
    3. `transform/diagnostic_scrub` empties the resource, scope, and data-point attribute maps the
       connector inherits from the spans it counted.

    Layer 3 is load-bearing rather than belt-and-braces: a `count` connector propagates the incoming
    resource onto the metrics it produces.
    """
    diagnostic = copy.deepcopy(config)
    connectors = diagnostic.setdefault("connectors", {})
    for stage, name in DIAGNOSTIC_CONNECTORS.items():
        connectors[name] = _count_connector(stage)

    allowed = list(diagnostic_metric_names())
    diagnostic["processors"][DIAGNOSTIC_VOCABULARY_FILTER] = {
        "error_mode": "propagate",
        "metrics": {
            # The filter processor drops what its conditions match, so the allowlist is expressed as
            # "drop everything that is not one of these".
            "metric": [" and ".join(f'metric.name != "{name}"' for name in allowed)]
        },
    }
    diagnostic["processors"][DIAGNOSTIC_SCRUB_PROCESSOR] = {
        "error_mode": "propagate",
        "metric_statements": [
            {"context": "resource", "statements": ["keep_keys(resource.attributes, [])"]},
            {"context": "scope", "statements": ["keep_keys(scope.attributes, [])"]},
            {"context": "datapoint", "statements": ["keep_keys(datapoint.attributes, [])"]},
        ],
    }

    pipelines = diagnostic["service"]["pipelines"]
    # Counted at the end of the certified pipeline, so `exported` is what the debug exporter saw and
    # not an approximation of it.
    pipelines["traces"]["exporters"] = ["debug", DIAGNOSTIC_CONNECTORS["exported"]]
    pipelines[DIAGNOSTIC_ACCEPTED_PIPELINE] = {
        "receivers": ["otlp"],
        "processors": [MEMORY_LIMITER],
        "exporters": [DIAGNOSTIC_CONNECTORS["accepted"]],
    }
    pipelines[DIAGNOSTIC_METRICS_PIPELINE] = {
        "receivers": list(DIAGNOSTIC_CONNECTORS.values()),
        "processors": [
            MEMORY_LIMITER,
            UNREDACTABLE_FILTER,
            DIAGNOSTIC_VOCABULARY_FILTER,
            PRIVACY_PROCESSOR,
            DIAGNOSTIC_SCRUB_PROCESSOR,
            BATCH_PROCESSOR,
        ],
        "exporters": ["debug"],
    }
    return diagnostic


# The complete list of ways the diagnostic configuration is allowed to differ from a configuration
# `certify_configuration` passes. Anything else appearing here is a real regression: the diagnostic
# must not become a way to relax the shipped pipeline. The two "used but not declared" entries are
# the connectors, which live under `connectors:` rather than `exporters:`/`receivers:`.
DIAGNOSTIC_EXPECTED_DEVIATIONS = frozenset(
    {
        "traces: count/diagnostic_exported is used but not declared",
        "traces: exporter other than debug: count/diagnostic_exported",
        f"{DIAGNOSTIC_ACCEPTED_PIPELINE}: count/diagnostic_accepted is used but not declared",
        f"{DIAGNOSTIC_ACCEPTED_PIPELINE}: exporter other than debug: count/diagnostic_accepted",
        f"{DIAGNOSTIC_ACCEPTED_PIPELINE}: {UNREDACTABLE_FILTER} is missing",
        f"{DIAGNOSTIC_ACCEPTED_PIPELINE}: privacy processor {PRIVACY_PROCESSOR} is missing",
        f"{DIAGNOSTIC_METRICS_PIPELINE}: count/diagnostic_accepted is used but not declared",
        f"{DIAGNOSTIC_METRICS_PIPELINE}: count/diagnostic_exported is used but not declared",
    }
)


def certify_diagnostic_configuration(config: dict) -> list[str]:
    """Return every way ``config`` fails the diagnostic's own privacy invariants.

    `certify_configuration` certifies the shipped pipeline. This certifies the additions: that the
    diagnostic cannot carry anything out of a span, and that adding it did not quietly weaken the
    pipeline it is measuring.
    """
    problems: list[str] = []

    connectors = config.get("connectors") or {}
    unexpected = set(connectors) - set(DIAGNOSTIC_CONNECTORS.values())
    if unexpected:
        problems.append(f"unexpected connector: {sorted(unexpected)}")
    allowed_names = set(diagnostic_metric_names())
    for name, connector in connectors.items():
        if not isinstance(connector, dict):
            problems.append(f"{name}: connector is not a mapping")
            continue
        for surface, metrics in connector.items():
            for metric, definition in (metrics or {}).items():
                if not isinstance(definition, dict):
                    problems.append(f"{name}/{surface}/{metric}: definition is not a mapping")
                    continue
                # The one field that would let a span attribute become a metric dimension.
                if "attributes" in definition:
                    problems.append(f"{name}/{surface}/{metric}: declares span attributes")
                if surface == "spans" and metric not in allowed_names:
                    problems.append(f"{name}: metric outside the vocabulary: {metric}")
                if surface != "spans" and definition.get("conditions") != [DIAGNOSTIC_NEVER]:
                    problems.append(f"{name}/{surface}/{metric}: not suppressed")

    vocabulary = (config.get("processors") or {}).get(DIAGNOSTIC_VOCABULARY_FILTER)
    if not isinstance(vocabulary, dict):
        problems.append(f"{DIAGNOSTIC_VOCABULARY_FILTER} is not declared")
    else:
        if vocabulary.get("error_mode") != "propagate":
            problems.append(f"{DIAGNOSTIC_VOCABULARY_FILTER}: error_mode must be 'propagate'")
        conditions = " ".join(str(item) for item in (vocabulary.get("metrics") or {}).get("metric") or [])
        for name in sorted(allowed_names):
            if f'metric.name != "{name}"' not in conditions:
                problems.append(f"{DIAGNOSTIC_VOCABULARY_FILTER}: does not admit {name}")

    scrub = (config.get("processors") or {}).get(DIAGNOSTIC_SCRUB_PROCESSOR)
    if not isinstance(scrub, dict):
        problems.append(f"{DIAGNOSTIC_SCRUB_PROCESSOR} is not declared")
    else:
        if scrub.get("error_mode") != "propagate":
            problems.append(f"{DIAGNOSTIC_SCRUB_PROCESSOR}: error_mode must be 'propagate'")
        emptied = {
            group.get("context")
            for group in scrub.get("metric_statements") or []
            if isinstance(group, dict)
            and any(re.fullmatch(r"keep_keys\(\s*\w+\.attributes\s*,\s*\[\s*\]\s*\)", str(s)) for s in group.get("statements") or [])
        }
        for surface in REQUIRED_METRIC_SURFACES:
            if surface not in emptied:
                problems.append(f"{DIAGNOSTIC_SCRUB_PROCESSOR}: {surface} attributes are not emptied")

    pipelines = (config.get("service") or {}).get("pipelines") or {}
    # The measured pipeline must still be the certified one, whatever the diagnostic added to it.
    certified_processors = [MEMORY_LIMITER, UNREDACTABLE_FILTER, PRIVACY_PROCESSOR, BATCH_PROCESSOR]
    for name in REQUIRED_PIPELINES:
        pipeline = pipelines.get(name) or {}
        if list(pipeline.get("processors") or []) != certified_processors:
            problems.append(f"{name}: the certified processor chain was modified")
        if "debug" not in (pipeline.get("exporters") or []):
            problems.append(f"{name}: no longer exports to debug")
    diagnostic_metrics = pipelines.get(DIAGNOSTIC_METRICS_PIPELINE) or {}
    processors = list(diagnostic_metrics.get("processors") or [])
    for required in (DIAGNOSTIC_VOCABULARY_FILTER, DIAGNOSTIC_SCRUB_PROCESSOR, PRIVACY_PROCESSOR):
        if required not in processors:
            problems.append(f"{DIAGNOSTIC_METRICS_PIPELINE}: {required} is missing")
    if DIAGNOSTIC_SCRUB_PROCESSOR in processors and BATCH_PROCESSOR in processors:
        if processors.index(DIAGNOSTIC_SCRUB_PROCESSOR) > processors.index(BATCH_PROCESSOR):
            problems.append(f"{DIAGNOSTIC_METRICS_PIPELINE}: the scrub runs after {BATCH_PROCESSOR}")
    for pipeline_name, pipeline in pipelines.items():
        for exporter in pipeline.get("exporters") or []:
            if exporter not in ALLOWED_EXPORTERS and exporter not in DIAGNOSTIC_CONNECTORS.values():
                problems.append(f"{pipeline_name}: exporter other than debug: {exporter}")
    for forbidden in FORBIDDEN_PIPELINES:
        if forbidden in pipelines:
            problems.append(f"forbidden pipeline present: {forbidden}")
    extra_exporters = set(config.get("exporters") or {}) - set(ALLOWED_EXPORTERS)
    if extra_exporters:
        problems.append(f"exporter other than debug: {sorted(extra_exporters)}")

    return problems


# ---------------------------------------------------------------------------
# Structural certification
# ---------------------------------------------------------------------------


def load_configuration() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def _is_loopback(endpoint: str) -> bool:
    import ipaddress

    host = endpoint.rsplit(":", 1)[0].strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def listening_endpoints(config: dict) -> list[str]:
    """Every address the Collector would bind, including internal telemetry."""
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "endpoint" and isinstance(value, str):
                    found.append(value)
                elif key == "host" and isinstance(value, str):
                    found.append(f"{value}:0")
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(config)
    return found


_DELETE_MATCHING = re.compile(r'^delete_matching_keys\(\s*([\w.]+)\.attributes\s*,\s*"(.*)"\s*\)$')
_DELETE_KEY = re.compile(r'^delete_key\(\s*([\w.]+)\.attributes\s*,\s*"(.*?)"\s*\)$')
_KEEP_KEYS = re.compile(r"^keep_keys\(\s*([\w.]+)\.attributes\s*,\s*\[(.*)\]\s*\)$")


@dataclass
class SurfacePolicy:
    """What the privacy processor does to one context of one signal."""

    context: str
    deleted_keys: set[str] = field(default_factory=set)
    delete_patterns: list[str] = field(default_factory=list)
    allowed_keys: set[str] | None = None
    unqualified_paths: int = 0

    def deletes(self, key: str) -> bool:
        if key in self.deleted_keys:
            return True
        # `delete_matching_keys` uses Go's unanchored regexp.MatchString; re.search matches it for
        # the literal alternations this configuration uses.
        return any(re.search(pattern, key) for pattern in self.delete_patterns)


def _parse_statements(context: str, statements: list) -> SurfacePolicy:
    policy = SurfacePolicy(context=context)
    for raw in statements:
        statement = str(raw).strip()
        matched = _DELETE_MATCHING.match(statement)
        if matched:
            if matched.group(1) != context:
                continue
            policy.delete_patterns.append(matched.group(2))
            continue
        matched = _DELETE_KEY.match(statement)
        if matched:
            if matched.group(1) == context:
                policy.deleted_keys.add(matched.group(2))
            continue
        matched = _KEEP_KEYS.match(statement)
        if matched:
            if matched.group(1) != context:
                continue
            keys = {item.strip().strip('"') for item in matched.group(2).split(",") if item.strip()}
            policy.allowed_keys = keys if policy.allowed_keys is None else policy.allowed_keys & keys
            continue
        # An unqualified path (`attributes` rather than `span.attributes`) still works on 0.158.0
        # but the Collector logs "please rewrite them accordingly", so it is counted and reported.
        if re.search(r"\(\s*attributes\s*,", statement):
            policy.unqualified_paths += 1
    return policy


def privacy_policies(config: dict) -> dict[tuple[str, str], SurfacePolicy]:
    """Map ``(signal, context)`` to the policy the privacy processor applies there."""
    processor = config.get("processors", {}).get(PRIVACY_PROCESSOR)
    if not isinstance(processor, dict):
        return {}
    policies: dict[tuple[str, str], SurfacePolicy] = {}
    for name, block in (("traces", "trace_statements"), ("metrics", "metric_statements")):
        for group in processor.get(block) or []:
            if not isinstance(group, dict):
                continue
            context = group.get("context")
            statements = group.get("statements") or []
            if not isinstance(context, str) or not isinstance(statements, list):
                continue
            policies[(name, context)] = _parse_statements(context, statements)
    return policies


def certify_configuration(config: dict) -> list[str]:
    """Return every contract violation in ``config``. An empty list is a pass.

    This is the fail-closed gate: the tests below mutate a copy of the shipped configuration and
    assert that each mutation produces a violation here.
    """
    problems: list[str] = []

    for section in ("receivers", "processors", "exporters", "service"):
        if section not in config:
            problems.append(f"missing top-level section: {section}")
    if problems:
        return problems

    # --- egress ---------------------------------------------------------
    endpoints = listening_endpoints(config)
    if not endpoints:
        problems.append("no listening endpoint found to check")
    for endpoint in endpoints:
        if not _is_loopback(endpoint):
            problems.append(f"non-loopback endpoint: {endpoint}")
    extra_exporters = set(config["exporters"]) - set(ALLOWED_EXPORTERS)
    if extra_exporters:
        problems.append(f"exporter other than debug: {sorted(extra_exporters)}")

    pipelines = config.get("service", {}).get("pipelines") or {}

    # --- signals --------------------------------------------------------
    for forbidden in FORBIDDEN_PIPELINES:
        if forbidden in pipelines:
            problems.append(f"forbidden pipeline present: {forbidden}")
    for required in REQUIRED_PIPELINES:
        if required not in pipelines:
            problems.append(f"required pipeline missing: {required}")

    # --- pipeline shape -------------------------------------------------
    for name, pipeline in pipelines.items():
        processors = list(pipeline.get("processors") or [])
        for section in ("receivers", "processors", "exporters"):
            for component in pipeline.get(section) or []:
                if component not in config.get(section, {}):
                    problems.append(f"{name}: {component} is used but not declared")
        for exporter in pipeline.get("exporters") or []:
            if exporter not in ALLOWED_EXPORTERS:
                problems.append(f"{name}: exporter other than debug: {exporter}")
        if UNREDACTABLE_FILTER not in processors:
            problems.append(f"{name}: {UNREDACTABLE_FILTER} is missing")
        elif BATCH_PROCESSOR in processors:
            if processors.index(UNREDACTABLE_FILTER) > processors.index(BATCH_PROCESSOR):
                problems.append(f"{name}: {UNREDACTABLE_FILTER} runs after {BATCH_PROCESSOR}")
        if PRIVACY_PROCESSOR not in processors:
            problems.append(f"{name}: privacy processor {PRIVACY_PROCESSOR} is missing")
            continue
        if BATCH_PROCESSOR in processors:
            if processors.index(PRIVACY_PROCESSOR) > processors.index(BATCH_PROCESSOR):
                problems.append(f"{name}: privacy processor runs after {BATCH_PROCESSOR}")
        if MEMORY_LIMITER in processors:
            if processors.index(MEMORY_LIMITER) > processors.index(PRIVACY_PROCESSOR):
                problems.append(f"{name}: {MEMORY_LIMITER} must shed load before redaction")
        else:
            problems.append(f"{name}: {MEMORY_LIMITER} is missing")

    # --- the unredactable-carrier filter --------------------------------
    unredactable = config["processors"].get(UNREDACTABLE_FILTER)
    if not isinstance(unredactable, dict):
        problems.append(f"{UNREDACTABLE_FILTER} is not declared")
    else:
        if unredactable.get("error_mode") != "propagate":
            problems.append(f"{UNREDACTABLE_FILTER}: error_mode must be 'propagate'")
        for (name, surface), condition in UNREDACTABLE_CONDITIONS.items():
            declared = (unredactable.get(name) or {}).get(surface) or []
            if condition not in [str(item).strip() for item in declared]:
                problems.append(f"{UNREDACTABLE_FILTER}: {name}/{surface} no longer drops `{condition}`")

    # --- the privacy processor itself -----------------------------------
    processor = config["processors"].get(PRIVACY_PROCESSOR)
    if not isinstance(processor, dict):
        problems.append(f"{PRIVACY_PROCESSOR} is not declared")
        return problems
    if processor.get("error_mode") != "propagate":
        problems.append(
            f"{PRIVACY_PROCESSOR}: error_mode must be 'propagate' so a failed statement drops the "
            "batch instead of exporting it unredacted"
        )

    policies = privacy_policies(config)
    required = [("traces", surface) for surface in REQUIRED_TRACE_SURFACES]
    required += [("metrics", surface) for surface in REQUIRED_METRIC_SURFACES]
    for name, surface in required:
        if name not in pipelines:
            continue
        policy = policies.get((name, surface))
        if policy is None:
            problems.append(f"{name}: no redaction for the {surface} surface")
            continue
        if policy.unqualified_paths:
            problems.append(f"{name}/{surface}: {policy.unqualified_paths} deprecated unqualified path(s)")
        for key in FORBIDDEN_ATTRIBUTE_KEYS:
            if not policy.deletes(key):
                problems.append(f"{name}/{surface}: forbidden key not deleted: {key}")
        if policy.allowed_keys is None:
            problems.append(f"{name}/{surface}: no keep_keys allowlist")
            continue
        leaked = sorted(policy.allowed_keys & set(FORBIDDEN_ATTRIBUTE_KEYS))
        if leaked:
            problems.append(f"{name}/{surface}: allowlist admits forbidden keys: {leaked}")
        dropped = [k for k in REQUIRED_ALLOWED_ATTRIBUTE_KEYS if k not in policy.allowed_keys]
        if dropped:
            problems.append(f"{name}/{surface}: allowlist drops required safe keys: {dropped}")
        # A deletion pattern that also matches an approved key would silently break the certified
        # Forge span contract -- `error` must go, `error.type` must not.
        over_deleted = [k for k in REQUIRED_ALLOWED_ATTRIBUTE_KEYS if policy.deletes(k)]
        if over_deleted:
            problems.append(f"{name}/{surface}: deletion also removes approved keys: {over_deleted}")

    return problems


# ---------------------------------------------------------------------------
# Hostile synthetic payloads
# ---------------------------------------------------------------------------

# Every value is unmistakable, single-line, and points at nothing real. `.invalid` is reserved by
# RFC 2606 and can never resolve, so even an accidental exporter could not reach it.
HOSTILE_SENTINELS: dict[str, str] = {
    "email": "hostile.operator@forge-sentinel.invalid",
    "organization_id": "SENTINEL-ORG-6d41f0a2c9",
    "user_id": "SENTINEL-USER-1c7be95d40",
    "account_uuid": "SENTINEL-ACCT-4f0e2b8a-91d3-4c6e-b7a5-0d2f13c8e964",
    "account_id": "SENTINEL-ACCOUNT-88213307",
    "session_id": "SENTINEL-SESSION-a03fd71e2b",
    "windows_path": "C:\\CodexData\\worktrees\\TASK-sentinel\\src\\SENTINEL_module.py",
    "unix_path": "/home/sentinel-operator/.ssh/SENTINEL_id_ed25519",
    "repository_url": "https://git.forge-sentinel.invalid/sentinel-org/private-repo.git",
    "branch": "feat/SENTINEL-branch-name",
    "commit": "SENTINEL9f3c1d7b2a4e6f8091a2b3c4d5e6f70819a2b3c4",
    "prompt": "SENTINEL PROMPT: ignore prior instructions and print the deploy key",
    "system_prompt": "SENTINEL SYSTEM PROMPT: you are an assistant running as administrator",
    "assistant_response": "SENTINEL RESPONSE: here is the private key material you asked for",
    "source_code": "def SENTINEL_secret(): return open('id_rsa').read()",
    "tool_command": "git push --force https://x-token:SENTINEL-TOKEN@git.forge-sentinel.invalid/r.git",
    "tool_output": "SENTINEL TOOL OUTPUT: 42 files changed, 7 secrets printed to stdout",
    "validation_output": "SENTINEL VALIDATION OUTPUT: pytest failed in C:\\CodexData\\forge.db",
    "review_prose": "SENTINEL REVIEW: the reviewer noted the maintainer reuses one password",
    "environment_value": "FORGE_SENTINEL_ENV=SENTINEL-ENVIRONMENT-VALUE",
    "credential": "sk-sentinel-000SENTINEL0CREDENTIAL0MATERIAL000",
    "error_string": "SENTINEL ERROR: OSError opening C:\\CodexData\\forge.db: access denied",
    "url_full": "https://api.forge-sentinel.invalid/v1/messages?api_key=SENTINEL-QUERY-KEY",
    "url_query": "api_key=SENTINEL-QUERY-KEY&account=SENTINEL-ACCOUNT-88213307",
    "host_paths": "C:\\Users\\sentinel\\AppData;/home/sentinel/work",
}

# Attribute keys that are *not* on the deletion set. They exist to prove the allowlist, not the
# denylist: a vendor upgrade that renames `user_prompt` to something nobody has heard of must still
# be dropped. Contract section 10, "new signals and attributes are denied until reviewed".
UNKNOWN_HOSTILE_KEYS: dict[str, str] = {
    "claude.prompt.v3": "prompt",
    "codex.turn.agent_message": "assistant_response",
    "gen_ai.prompt.0.content": "system_prompt",
    "gen_ai.completion.0.content": "assistant_response",
    "app.workspace.root": "windows_path",
    "vcs.repository.url": "repository_url",
    "vcs.ref.head.name": "branch",
    "vcs.ref.head.revision": "commit",
    "process.command_line": "tool_command",
    "process.environment": "environment_value",
    "http.request.header.authorization": "credential",
    "exception.message": "error_string",
    "exception.stacktrace": "unix_path",
    "code.snippet": "source_code",
    "forge.review.findings": "review_prose",
    "forge.validation.stdout": "validation_output",
}

# Bounded values that must survive, keyed by an approved attribute name. These are what a reader of
# the debug output is allowed to see.
SAFE_ATTRIBUTE_VALUES: dict[str, object] = {
    "service.name": "codex-forge",
    "service.version": "0.2.0",
    "deployment.environment": "local",
    "forge.telemetry.contract.version": "1.0",
    "forge.invocation.id": "b7c4e21f9a08d5364e1fb0972c8ad653",
    "forge.task.id": "TASK-0a1b2c3d",
    "forge.project.id": "codex-forge",
    "forge.task.risk": "medium",
    "forge.task.max_attempts": 3,
    "forge.task.outcome": "completed",
    "forge.attempt.number": 1,
    "forge.attempt.kind": "full",
    "forge.attempt.outcome": "completed",
    "forge.attempt.terminated_by": "completion",
    "forge.retry.bucket": "0",
    "forge.phase.name": "implementation",
    "forge.phase.cycle": 0,
    "forge.phase.outcome": "ok",
    "forge.phase.duration_ms": 12345,
    "forge.agent.role": "implementer",
    "gen_ai.request.model": "gpt-5-codex",
    "gen_ai.request.reasoning_effort": "high",
    "gen_ai.usage.input_tokens": 1200,
    "gen_ai.usage.cached_input_tokens": 800,
    "gen_ai.usage.output_tokens": 340,
    "gen_ai.usage.reasoning_tokens": 120,
    "gen_ai.token.type": "output",
    "forge.usage.fresh_input_tokens": 400,
    "forge.usage.raw_total_tokens": 1540,
    "forge.usage.cache_hit_ratio": 0.667,
    "forge.usage.accounting_basis": "raw_input_output",
    "forge.usage.budget_basis": "raw_input_output",
    "forge.usage.token_type": "output",
    "error.type": "TimeoutError",
    "error.code": "codex_exit_nonzero",
}

# Every approved key is exercised end to end, so the certification proves the whole allowlist
# rather than the part of it somebody remembered to inject.
assert set(SAFE_ATTRIBUTE_VALUES) == set(REQUIRED_ALLOWED_ATTRIBUTE_KEYS)

# The single invocation id the join in contract section 4 depends on. It must survive everywhere.
INVOCATION_ID = SAFE_ATTRIBUTE_VALUES["forge.invocation.id"]

# The two carriers that cannot be redacted are dropped whole, so they are given their own span name
# and data-point value to make their absence from the processed output checkable.
LINKED_SPAN_NAME = "forge.certification.linked"
EXEMPLAR_DATAPOINT_VALUE = "987654"


def _otlp_value(value: object) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def hostile_attributes(surface: str) -> list[dict]:
    """Every forbidden key, every unknown key, and every approved key, on one surface.

    ``surface`` is woven into the sentinel values so a leak can be traced to the exact surface that
    leaked it rather than merely to "somewhere in the payload".
    """
    attributes: list[dict] = []

    def add(key: str, value: object) -> None:
        attributes.append({"key": key, "value": _otlp_value(value)})

    # 1. Every key on the contract's minimum deletion set, each carrying a distinct sentinel.
    forbidden_values = {
        "user.email": "email",
        "organization.id": "organization_id",
        "user.id": "user_id",
        "user.account_uuid": "account_uuid",
        "user.account_id": "account_id",
        "session.id": "session_id",
        "ccr.session.id": "session_id",
        "workspace.host_paths": "host_paths",
        "user_prompt": "prompt",
        "system_prompt": "system_prompt",
        "system_prompt_preview": "system_prompt",
        "user_system_prompt": "system_prompt",
        "assistant_response": "assistant_response",
        "response": "assistant_response",
        "tool_input": "tool_command",
        "tool_parameters": "tool_command",
        "tool_output": "tool_output",
        "payload": "source_code",
        "content": "source_code",
        "error": "error_string",
        "error.message": "error_string",
        "url.full": "url_full",
        "url.query": "url_query",
    }
    assert set(forbidden_values) == set(FORBIDDEN_ATTRIBUTE_KEYS)
    for key, sentinel in forbidden_values.items():
        add(key, f"{HOSTILE_SENTINELS[sentinel]} [{surface}]")

    # 2. Keys no deletion set has ever heard of, carrying the same class of content.
    for key, sentinel in UNKNOWN_HOSTILE_KEYS.items():
        add(key, f"{HOSTILE_SENTINELS[sentinel]} [{surface}]")

    # 3. The bounded fields that must survive.
    for key, value in SAFE_ATTRIBUTE_VALUES.items():
        add(key, value)
    return attributes


def hostile_trace_payload() -> dict:
    """An OTLP/HTTP JSON trace export with hostile values on resource, scope, span, and event."""
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": hostile_attributes("trace-resource")},
                "scopeSpans": [
                    {
                        "scope": {
                            "name": "forge.certification",
                            "version": "1.0",
                            "attributes": hostile_attributes("trace-scope"),
                        },
                        "spans": [
                            {
                                "traceId": "5b8efff798038103d269b633813fc60c",
                                "spanId": "eee19b7ec3c1b174",
                                "name": "forge.phase",
                                "kind": 1,
                                "startTimeUnixNano": "1700000000000000000",
                                "endTimeUnixNano": "1700000012345000000",
                                "attributes": hostile_attributes("span"),
                                "events": [
                                    {
                                        "timeUnixNano": "1700000005000000000",
                                        "name": "forge.certification.event",
                                        "attributes": hostile_attributes("span-event"),
                                    }
                                ],
                                "status": {"code": 2},
                            },
                            # A separate span, because a span carrying a link is dropped whole and
                            # would take the surfaces above with it.
                            {
                                "traceId": "5b8efff798038103d269b633813fc60c",
                                "spanId": "aaaaaaaaaaaaaaaa",
                                "name": LINKED_SPAN_NAME,
                                "kind": 1,
                                "startTimeUnixNano": "1700000000000000000",
                                "endTimeUnixNano": "1700000012345000000",
                                "attributes": hostile_attributes("linked-span"),
                                "links": [
                                    {
                                        "traceId": "5b8efff798038103d269b633813fc60c",
                                        "spanId": "eee19b7ec3c1b174",
                                        "attributes": hostile_attributes("span-link"),
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        ]
    }


# The span names the diagnostic payload below emits, and whether each carries a link. This is
# deliberately shaped like the reported canary -- six spans in, two of them carrying a link -- so the
# accounting is exercised against the arithmetic it has to explain, and so a bucket the vocabulary
# does not know about (`some.unknown.vendor.span`) is present to land in `other`.
DIAGNOSTIC_PAYLOAD_SPANS = (
    ("forge.phase", False),
    ("claude_code.interaction", False),
    ("claude_code.llm_request", True),
    ("claude_code.llm_request", False),
    ("claude_code.tool", True),
    ("some.unknown.vendor.span", False),
)


def diagnostic_trace_payload() -> dict:
    """Vocabulary-covering spans, every surface hostile, and links on two of them.

    Every span carries the full hostile attribute set on itself and on an event, the resource and
    scope are hostile, and each link carries the hostile set as well. A diagnostic that emits
    anything but a name from its own vocabulary and a count will show it against this payload.
    """
    link = [
        {
            "traceId": "5b8efff798038103d269b633813fc60c",
            "spanId": "eee19b7ec3c1b174",
            "attributes": hostile_attributes("span-link"),
        }
    ]
    spans = []
    for index, (name, linked) in enumerate(DIAGNOSTIC_PAYLOAD_SPANS):
        span = {
            "traceId": "5b8efff798038103d269b633813fc60c",
            "spanId": f"{index + 1:016x}",
            "name": name,
            "kind": 1,
            "startTimeUnixNano": "1700000000000000000",
            "endTimeUnixNano": "1700000012345000000",
            # A link-carrying span gets its own surface tag, so the certification can tell a value
            # that leaked from a dropped span apart from one that leaked from a surviving sibling.
            "attributes": hostile_attributes("linked-span" if linked else "span"),
            "events": [
                {
                    "timeUnixNano": "1700000005000000000",
                    "name": "forge.certification.event",
                    "attributes": hostile_attributes("span-event"),
                }
            ],
        }
        if linked:
            span["links"] = link
        spans.append(span)
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": hostile_attributes("trace-resource")},
                "scopeSpans": [
                    {
                        "scope": {
                            "name": "forge.certification",
                            "version": "1.0",
                            "attributes": hostile_attributes("trace-scope"),
                        },
                        "spans": spans,
                    }
                ],
            }
        ]
    }


def diagnostic_expected_counts() -> tuple[dict[str, int], dict[str, int]]:
    """What the diagnostic must report for :func:`diagnostic_trace_payload`, by bucket.

    Derived from the payload rather than written out, so the two cannot drift apart.
    """
    accepted: dict[str, int] = {}
    exported: dict[str, int] = {}
    for name, linked in DIAGNOSTIC_PAYLOAD_SPANS:
        bucket = name if name in DIAGNOSTIC_SPAN_NAMES else DIAGNOSTIC_OTHER_BUCKET
        accepted[bucket] = accepted.get(bucket, 0) + 1
        if not linked:
            exported[bucket] = exported.get(bucket, 0) + 1
    return accepted, exported


def hostile_metric_payload() -> dict:
    """An OTLP/HTTP JSON metric export with hostile values on resource, scope, and data points."""
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": hostile_attributes("metric-resource")},
                "scopeMetrics": [
                    {
                        "scope": {
                            "name": "forge.certification",
                            "version": "1.0",
                            "attributes": hostile_attributes("metric-scope"),
                        },
                        "metrics": [
                            {
                                "name": "forge.certification.token.usage",
                                "unit": "{token}",
                                "sum": {
                                    "aggregationTemporality": 2,
                                    "isMonotonic": True,
                                    "dataPoints": [
                                        {
                                            "asInt": "1200",
                                            "startTimeUnixNano": "1700000000000000000",
                                            "timeUnixNano": "1700000012345000000",
                                            "attributes": hostile_attributes("metric-datapoint"),
                                        },
                                        # A separate data point, for the same reason the linked
                                        # span above is separate: this one is dropped whole.
                                        {
                                            "asInt": EXEMPLAR_DATAPOINT_VALUE,
                                            "startTimeUnixNano": "1700000000000000000",
                                            "timeUnixNano": "1700000012345000000",
                                            "attributes": hostile_attributes("exemplar-datapoint"),
                                            "exemplars": [
                                                {
                                                    "timeUnixNano": "1700000012000000000",
                                                    "asInt": "5",
                                                    "filteredAttributes": hostile_attributes("exemplar"),
                                                }
                                            ],
                                        },
                                    ],
                                },
                            },
                            {
                                "name": "forge.certification.phase.duration",
                                "unit": "ms",
                                "histogram": {
                                    "aggregationTemporality": 2,
                                    "dataPoints": [
                                        {
                                            "count": "1",
                                            "sum": 12345.0,
                                            "bucketCounts": ["0", "1"],
                                            "explicitBounds": [1000.0],
                                            "startTimeUnixNano": "1700000000000000000",
                                            "timeUnixNano": "1700000012345000000",
                                            "attributes": hostile_attributes("histogram-datapoint"),
                                        }
                                    ],
                                },
                            },
                        ],
                    }
                ],
            }
        ]
    }


def hostile_log_payload() -> dict:
    """A log export. There is no logs pipeline, so this must be refused, not redacted."""
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": hostile_attributes("log-resource")},
                "scopeLogs": [
                    {
                        "scope": {"name": "forge.certification"},
                        "logRecords": [
                            {
                                "timeUnixNano": "1700000005000000000",
                                "severityNumber": 9,
                                "severityText": "INFO",
                                "body": {"stringValue": HOSTILE_SENTINELS["prompt"] + " [log-body]"},
                                "attributes": hostile_attributes("log-record"),
                            }
                        ],
                    }
                ],
            }
        ]
    }


def sentinel_values(surfaces: tuple[str, ...]) -> list[str]:
    """Every hostile value that should have been injected on ``surfaces``."""
    values = []
    for surface in surfaces:
        for sentinel in HOSTILE_SENTINELS.values():
            values.append(f"{sentinel} [{surface}]")
    return values


TRACE_SURFACES = ("trace-resource", "trace-scope", "span", "span-event", "linked-span", "span-link")
METRIC_SURFACES = (
    "metric-resource",
    "metric-scope",
    "metric-datapoint",
    "histogram-datapoint",
    "exemplar-datapoint",
    "exemplar",
)


# ---------------------------------------------------------------------------
# Real Collector harness
# ---------------------------------------------------------------------------


class CollectorUnavailable(Exception):
    """No Collector binary was named by the test configuration."""


class CollectorFailed(Exception):
    """The Collector exited on its own, or refused to start."""


def collector_binary() -> Path:
    """Locate the Collector through explicit test configuration only.

    ``PATH`` is deliberately not searched: a certification run must name the binary it certifies.
    Nothing here downloads anything.
    """
    for variable in ("FORGE_OTELCOL_PATH", "OTELCOL_PATH"):
        raw = os.environ.get(variable, "").strip().strip('"')
        if not raw:
            continue
        candidate = Path(raw)
        if not candidate.is_file():
            raise CollectorUnavailable(f"{variable} is set but does not name a file: {candidate}")
        return candidate
    raise CollectorUnavailable(
        "set OTELCOL_PATH (or FORGE_OTELCOL_PATH) to an installed otelcol-contrib executable to run "
        "the end-to-end certification; this suite never downloads one"
    )


def collector_version(binary: Path) -> str:
    result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise CollectorFailed(f"--version exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip().split()[-1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def temporary_ports(config: dict) -> tuple[dict, dict[str, int]]:
    """Re-bind every listener onto an ephemeral loopback port.

    The production ports (4317/4318/8888) are left alone so a certification run cannot collide with,
    or be answered by, a Collector the operator is already running.
    """
    rebound = copy.deepcopy(config)
    ports = {"grpc": _free_port(), "http": _free_port(), "internal": _free_port()}
    protocols = rebound.get("receivers", {}).get("otlp", {}).get("protocols", {})
    if "grpc" in protocols:
        protocols["grpc"]["endpoint"] = f"127.0.0.1:{ports['grpc']}"
    if "http" in protocols:
        protocols["http"]["endpoint"] = f"127.0.0.1:{ports['http']}"
    for reader in rebound.get("service", {}).get("telemetry", {}).get("metrics", {}).get("readers", []):
        exporter = reader.get("pull", {}).get("exporter", {}).get("prometheus")
        if isinstance(exporter, dict):
            exporter["host"] = "127.0.0.1"
            exporter["port"] = ports["internal"]
    return rebound, ports


@dataclass
class CollectorRun:
    """What one certification run observed."""

    returncode: int
    output: str
    responses: list[tuple[str, int, str]]
    config_path: Path

    def status_for(self, path: str) -> int:
        for endpoint, status, _ in self.responses:
            if endpoint == path:
                return status
        raise KeyError(path)


def write_configuration(config: dict, path: Path) -> Path:
    """Serialise a configuration for the Collector without folding the long OTTL statements."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(config, sort_keys=False, default_flow_style=False, width=1_000_000),
        encoding="utf-8",
    )
    return path


def validate_configuration(config_path: Path, binary: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(binary), "validate", "--config", str(config_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _post(port: int, path: str, body: dict) -> tuple[str, int, str]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return path, response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return path, error.code, error.read().decode("utf-8", "replace")


def wait_for_marker(log_path: Path, needle: str, process: subprocess.Popen, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        if needle in text:
            return True
        if process.poll() is not None:
            return needle in log_path.read_text(encoding="utf-8", errors="replace")
        time.sleep(0.2)
    return False


def run_collector(
    config: dict,
    payloads: list[tuple[str, dict]],
    *,
    binary: Path,
    workdir: Path,
    expect_markers: tuple[str, ...] = (),
    settle_seconds: float = 8.0,
) -> CollectorRun:
    """Start a real Collector on temporary loopback ports, feed it ``payloads``, return its output.

    The process is always stopped and always reaped, including when an assertion or an exception
    interrupts the run. A Collector that exits on its own is an error carrying its own exit code.
    """
    rebound, ports = temporary_ports(config)
    workdir.mkdir(parents=True, exist_ok=True)
    config_path = write_configuration(rebound, workdir / "certification-config.yaml")
    log_path = workdir / "collector.log"

    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with log_path.open("w", encoding="utf-8", errors="replace") as sink:
        process = subprocess.Popen(
            [str(binary), "--config", str(config_path)],
            stdout=sink,
            stderr=subprocess.STDOUT,
            cwd=str(workdir),
            creationflags=creationflags,
        )
        responses: list[tuple[str, int, str]] = []
        try:
            if not wait_for_marker(log_path, "Everything is ready", process, timeout=60):
                raise CollectorFailed(
                    "Collector never reported readiness (exit "
                    f"{process.poll()}):\n{log_path.read_text(encoding='utf-8', errors='replace')}"
                )
            for path, body in payloads:
                if process.poll() is not None:
                    raise CollectorFailed(
                        f"Collector exited {process.returncode} before {path} was sent:\n"
                        f"{log_path.read_text(encoding='utf-8', errors='replace')}"
                    )
                responses.append(_post(ports["http"], path, body))
            for marker in expect_markers:
                # The batch processor holds for its own `timeout` before the exporter sees anything.
                if not wait_for_marker(log_path, marker, process, timeout=settle_seconds + 30):
                    raise CollectorFailed(
                        f"the exporter never printed {marker!r}, so there is nothing to certify:\n"
                        f"{log_path.read_text(encoding='utf-8', errors='replace')}"
                    )
            if not expect_markers:
                time.sleep(settle_seconds)
            if process.poll() is not None:
                raise CollectorFailed(
                    f"Collector exited {process.returncode} during the run:\n"
                    f"{log_path.read_text(encoding='utf-8', errors='replace')}"
                )
        finally:
            returncode = stop_collector(process)

    return CollectorRun(
        returncode=returncode,
        output=log_path.read_text(encoding="utf-8", errors="replace"),
        responses=responses,
        config_path=config_path,
    )


def stop_collector(process: subprocess.Popen) -> int:
    """Stop the Collector, preferring a clean shutdown, and never leave the process behind."""
    if process.poll() is not None:
        return process.returncode
    if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
        # The child owns its own process group, so this reaches it and nothing else.
        with suppress(OSError):
            os.kill(process.pid, signal.CTRL_BREAK_EVENT)
    else:  # pragma: no cover - the helpers this certifies are Windows-only
        with suppress(OSError):
            process.terminate()
    try:
        return process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass
    with suppress(OSError):
        process.terminate()
    try:
        return process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=15)


# ---------------------------------------------------------------------------
# Reading counts back out
# ---------------------------------------------------------------------------

# What the Collector's own Prometheus endpoint is read for. All five are present at the shipped
# `service.telemetry.metrics.level: basic`, verified against 0.158.0 -- the accounting needs no
# change to the shipped internal-telemetry configuration.
COUNTER_ACCEPTED = "otelcol_receiver_accepted_spans"
COUNTER_REFUSED = "otelcol_receiver_refused_spans"
COUNTER_SENT = "otelcol_exporter_sent_spans"
COUNTER_SEND_FAILED = "otelcol_exporter_send_failed_spans"
COUNTER_FILTERED = "otelcol_processor_filter_spans_filtered"

_SERIES = re.compile(r"^(?P<name>[a-zA-Z_:][\w:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)$")
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def parse_internal_metrics(body: str) -> list[tuple[str, dict[str, str], float]]:
    """Parse a Prometheus exposition body into ``(name, labels, value)`` triples."""
    series: list[tuple[str, dict[str, str], float]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        matched = _SERIES.match(line)
        if not matched:
            continue
        try:
            value = float(matched.group("value"))
        except ValueError:
            continue
        labels = dict(_LABEL.findall(matched.group("labels") or ""))
        series.append((matched.group("name"), labels, value))
    return series


def scrape_internal_metrics(port: int, *, timeout: float = 10.0) -> list[tuple[str, dict[str, str], float]]:
    """Read the Collector's loopback-bound internal telemetry endpoint. Never leaves the host."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=timeout) as response:
        return parse_internal_metrics(response.read().decode("utf-8", "replace"))


def counter_total(
    series: list[tuple[str, dict[str, str], float]], name: str, **labels: str
) -> int:
    """Sum one counter across every label set that matches ``labels``.

    A counter with no series yet reads as 0. That is the state the *timing* hypothesis produces and
    it is why the accounting waits for the counts to settle instead of reading once.
    """
    total = 0.0
    for series_name, series_labels, value in series:
        if series_name != name:
            continue
        if all(series_labels.get(key) == value_ for key, value_ in labels.items()):
            total += value
    return int(total)


_METRIC_NAME_LINE = re.compile(r"^\s*->\s*Name:\s*(\S+)\s*$")
_VALUE_LINE = re.compile(r"^\s*Value:\s*(-?\d+)")


def parse_diagnostic_counts(output: str) -> dict[str, dict[str, int]]:
    """Total the diagnostic counters printed by the debug exporter, by stage and bucket.

    The debug exporter prints each metric as a descriptor followed by its data points, and the
    diagnostic emits delta sums, so a run that flushed several batches contributes several blocks
    for the same bucket. They are summed.
    """
    totals: dict[str, dict[str, int]] = {stage: {} for stage in DIAGNOSTIC_STAGES}
    names = {
        diagnostic_metric_name(stage, bucket): (stage, bucket)
        for stage in DIAGNOSTIC_STAGES
        for bucket in DIAGNOSTIC_BUCKETS
    }
    lines = output.splitlines()
    for index, line in enumerate(lines):
        matched = _METRIC_NAME_LINE.match(line)
        if not matched or matched.group(1) not in names:
            continue
        stage, bucket = names[matched.group(1)]
        for following in lines[index + 1 : index + 16]:
            if _METRIC_NAME_LINE.match(following):
                break
            value = _VALUE_LINE.match(following)
            if value:
                totals[stage][bucket] = totals[stage].get(bucket, 0) + int(value.group(1))
                break
    return totals


def unknown_diagnostic_metric_names(output: str) -> list[str]:
    """Every ``forge.diagnostic.*`` name the exporter printed that the vocabulary does not define."""
    allowed = set(diagnostic_metric_names())
    found = []
    for line in output.splitlines():
        matched = _METRIC_NAME_LINE.match(line)
        if matched and matched.group(1).startswith(DIAGNOSTIC_METRIC_PREFIX):
            if matched.group(1) not in allowed:
                found.append(matched.group(1))
    return sorted(set(found))
