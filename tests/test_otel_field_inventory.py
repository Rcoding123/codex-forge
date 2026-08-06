"""Certification for the Gate 6 field inventory.

Three questions, and they are not the same question:

* **What did the certified pipeline export?** Answered by the inventory, which reads the debug
  exporter's own output and nothing else. Everything here that needs a real Collector proves that
  answer against a real one, fed the Gate 3 hostile payloads.
* **Can a human review it from the report alone?** Answered by the per-identifier entries and the
  candidate manifest. `test_the_candidate_manifest_closes_every_unknown_field` merges the proposal
  back in and shows nothing is left UNKNOWN — without any file but the reports.
* **What has a human approved?** Answered by `ops/otel/claude-field-approvals.yaml`, which ships with
  nothing approved. The tests below prove the harness refuses to call that a pass.

Nothing here calls Claude, Codex, a Forge task, or any service off the loopback interface, and
nothing here downloads a Collector. The agent in every test is either synthetic OTLP or a `python -c`
sleep; the one test that exercises the real child-environment builder launches `python`, never an
agent.

An unsafe identifier used as test data is never interpolated into an assertion message — the
`_leaked` helper compares first and asserts on a boolean, so a failure prints a label rather than the
string the policy exists to withhold.

    $env:OTELCOL_PATH = 'C:\\Tools\\OpenTelemetry\\otelcol-contrib\\0.158.0\\otelcol-contrib.exe'
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_otel_field_inventory.py
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

import otel_canary as canary
import otel_certification as certification
import otel_inventory as inventory

try:
    COLLECTOR = certification.collector_binary()
    UNAVAILABLE = ""
except certification.CollectorUnavailable as error:  # pragma: no cover - depends on the machine
    COLLECTOR = None
    UNAVAILABLE = str(error)

requires_collector = pytest.mark.skipif(COLLECTOR is None, reason=UNAVAILABLE or "no Collector")

MANIFEST = inventory.load_manifest()
SOURCE_ROOT = certification.REPO_ROOT / "src" / "codex_forge"


def _payload_agent():
    return canary.otlp_payload_agent(
        [
            ("/v1/traces", certification.diagnostic_trace_payload()),
            ("/v1/metrics", certification.hostile_metric_payload()),
        ]
    )


@pytest.fixture(scope="module")
def hostile_run(tmp_path_factory):
    """One inventory run over the certified pipeline, fed every hostile surface Gate 3 defines."""
    return inventory.run_inventory(
        binary=COLLECTOR,
        workdir=tmp_path_factory.mktemp("inventory-run"),
        output_dir=tmp_path_factory.mktemp("inventory-out") / "reports",
        agent=_payload_agent(),
        manifest=MANIFEST,
    )


# ---------------------------------------------------------------------------
# Synthetic exporter output, for everything that does not need a Collector
# ---------------------------------------------------------------------------


def _attributes(pairs, indent=5):
    return [f"{' ' * indent}-> {key}: {value}" for key, value in pairs]


def trace_dump(
    *,
    resource=(),
    scope="forge.certification",
    scope_version="1.0",
    scope_attributes=(),
    spans=(),
) -> str:
    """Reproduce a 0.158.0 `debug` exporter trace dump, byte-for-byte in shape."""
    lines = ["ResourceSpans #0", "Resource SchemaURL: "]
    if resource:
        lines.append("Resource attributes:")
        lines += _attributes(resource)
    lines += ["ScopeSpans #0", "ScopeSpans SchemaURL: "]
    lines.append(f"InstrumentationScope {scope} {scope_version}".rstrip() + " ")
    if scope_attributes:
        lines.append("InstrumentationScope attributes:")
        lines += _attributes(scope_attributes)
    for index, span in enumerate(spans):
        lines += [
            f"Span #{index}",
            "    Trace ID       : 5b8efff798038103d269b633813fc60c",
            "    Parent ID      : eee19b7ec3c1b174",
            f"    ID             : {index + 1:016x}",
            f"    Name           : {span['name']}",
            f"    Kind           : {span.get('kind', 'Internal')}",
            "    Start time     : 2023-11-14 22:13:20 +0000 UTC",
            "    End time       : 2023-11-14 22:13:32.345 +0000 UTC",
            f"    Status code    : {span.get('status', 'Unset')}",
            "    Status message : ",
            "    DroppedAttributesCount: 0",
        ]
        lines.append("Attributes:")
        lines += _attributes(span.get("attributes", ()))
        if span.get("events"):
            lines.append("Events:")
            for event_index, event in enumerate(span["events"]):
                lines += [
                    f"SpanEvent #{event_index}",
                    f"     -> Name: {event['name']}",
                    "     -> Timestamp: 2023-11-14 22:13:25 +0000 UTC",
                    "     -> DroppedAttributesCount: 0",
                    "     -> Attributes::",
                ]
                lines += _attributes(event.get("attributes", ()), indent=10)
        if span.get("links"):
            lines.append("Links:")
            for link_index, link in enumerate(span["links"]):
                lines += [
                    f"SpanLink #{link_index}",
                    "     -> Trace ID: 5b8efff798038103d269b633813fc60c",
                    "     -> ID: eee19b7ec3c1b174",
                    "     -> TraceState: ",
                    "     -> DroppedAttributesCount: 0",
                    "     -> Attributes::",
                ]
                lines += _attributes(link, indent=10)
    return _wrap(lines)


def metric_dump(*, resource=(), scope="forge.certification", metrics=()) -> str:
    lines = ["ResourceMetrics #0", "Resource SchemaURL: "]
    if resource:
        lines.append("Resource attributes:")
        lines += _attributes(resource)
    lines += ["ScopeMetrics #0", "ScopeMetrics SchemaURL: ", f"InstrumentationScope {scope} "]
    for index, metric in enumerate(metrics):
        lines += [
            f"Metric #{index}",
            "Descriptor:",
            f"     -> Name: {metric['name']}",
            f"     -> Description: {metric.get('description', '')}",
            f"     -> Unit: {metric.get('unit', '')}",
            f"     -> DataType: {metric.get('data_type', 'Sum')}",
            f"     -> IsMonotonic: {metric.get('monotonic', 'true')}",
            f"     -> AggregationTemporality: {metric.get('temporality', 'Delta')}",
            "NumberDataPoints #0",
        ]
        if metric.get("attributes"):
            lines.append("Data point attributes:")
            lines += _attributes(metric["attributes"])
        lines += [
            "StartTimestamp: 2023-11-14 22:13:20 +0000 UTC",
            "Timestamp: 2023-11-14 22:13:32.345 +0000 UTC",
            "Value: 1",
        ]
        if metric.get("exemplars"):
            lines += [
                "Exemplars:",
                "Exemplar #0",
                "     -> Trace ID: 5b8efff798038103d269b633813fc60c",
                "     -> Span ID: eee19b7ec3c1b174",
                "     -> Timestamp: 2023-11-14 22:13:32 +0000 UTC",
                "     -> Value: 5",
                "     -> FilteredAttributes:",
            ]
            lines += _attributes(metric["exemplars"], indent=10)
    return _wrap(lines)


def _wrap(lines: list[str]) -> str:
    """Prefix the first line the way the Collector's logger does, and append its trailer."""
    head = f"2026-01-01T00:00:00.000-0000\tinfo\t{lines[0]}"
    trailer = '\t{"resource": {}, "otelcol.component.id": "debug", "otelcol.signal": "traces"}'
    return "\n".join([head, *lines[1:], trailer]) + "\n"


def report_for(output: str, **overrides) -> canary.CanaryReport:
    values = {
        "accounting": canary.SpanAccounting(
            accepted=1, exported=1, filtered=0, refused=0, send_failed=0
        ),
        "privacy": canary.PrivacyCheck(checks=("output was captured",), findings=()),
        "collector_returncode": 0,
        "agent_returncode": 0,
        "output": output,
    }
    values.update(overrides)
    return canary.CanaryReport(**values)


def document_for(output: str, manifest=None, **overrides) -> dict:
    return inventory.build_document(report_for(output, **overrides), manifest or MANIFEST)


def rendered(document: dict) -> str:
    """Every artifact a run publishes, concatenated. What a leak check must be run against."""
    return (
        inventory.render_json(document)
        + inventory.render_markdown(document)
        + inventory.build_candidate_manifest(document)
    )


def field(document: dict, identifier: str) -> dict:
    for item in document["fields"]:
        if item["id"] == identifier:
            return item
    raise AssertionError(f"{identifier} is not in the inventory")


def _leaked(needle: str, haystack: str) -> bool:
    """Membership as a bare boolean, so a failing assertion prints False rather than ``needle``."""
    return needle in haystack


# ---------------------------------------------------------------------------
# 1. The inventory sees post-privacy telemetry, and there is no other route
# ---------------------------------------------------------------------------


def test_the_inventory_adds_no_collector_component_at_all():
    """The strongest form of "no bypass": the inventory contributes nothing to the pipeline."""
    expected = certification.diagnostic_configuration(certification.load_configuration())
    assert inventory.inventory_configuration() == expected


def test_the_inventory_configuration_keeps_the_certified_processor_chain():
    config = inventory.inventory_configuration()
    for name in certification.REQUIRED_PIPELINES:
        assert config["service"]["pipelines"][name]["processors"] == [
            "memory_limiter",
            "filter/unredactable",
            "transform/privacy",
            "batch",
        ]
        assert config["service"]["pipelines"][name]["exporters"][0] == "debug"


def test_the_inventory_configuration_passes_the_diagnostic_certification():
    assert certification.certify_diagnostic_configuration(inventory.inventory_configuration()) == []
    deviations = set(certification.certify_configuration(inventory.inventory_configuration()))
    assert deviations == set(certification.DIAGNOSTIC_EXPECTED_DEVIATIONS)


def test_the_inventory_declares_no_receiver_and_no_exporter_of_its_own():
    config = inventory.inventory_configuration()
    assert set(config["receivers"]) == {"otlp"}
    assert set(config["exporters"]) == set(certification.ALLOWED_EXPORTERS)
    for protocol in config["receivers"]["otlp"]["protocols"].values():
        assert protocol["endpoint"].startswith("127.0.0.1:")


def test_there_is_no_logs_pipeline_and_no_logs_exporter():
    """Contract section 5's two barriers, restated for the inventory run."""
    config = inventory.inventory_configuration()
    assert "logs" not in config["service"]["pipelines"]
    assert not any(name.startswith("logs") for name in config["service"]["pipelines"])
    sys.path.insert(0, str(certification.REPO_ROOT / "src"))
    from codex_forge import agent_telemetry

    context = agent_telemetry.InvocationContext(invocation_id="a" * 32)
    child = agent_telemetry.build_claude_child_environment({}, context)
    assert child["OTEL_LOGS_EXPORTER"] == "none"


def test_the_only_input_to_the_inventory_is_the_exporters_own_output():
    """`parse_export` takes text. There is no port, path, or handle it could read instead."""
    import ast
    import inspect

    signature = inspect.signature(inventory.parse_export)
    assert list(signature.parameters) == ["output", "manifest"]

    imported: set[str] = set()
    for node in ast.walk(ast.parse(Path(inventory.__file__).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    # No transport of any kind. The module cannot open a connection to a receiver even by accident.
    for network in ("socket", "urllib", "http", "asyncio", "ssl", "grpc", "opentelemetry"):
        assert network not in imported, network


@requires_collector
def test_the_run_inventories_what_the_certified_pipeline_exported(hostile_run):
    """The end-to-end shape: spans that survived are inventoried, dropped ones are not."""
    document = hostile_run.document
    assert document["totals"]["observed"] > 0
    assert field(document, "span/claude_code.interaction")["occurrences"] == 1
    assert field(document, "span/forge.phase")["occurrences"] == 1
    # `claude_code.tool` carried a link in the payload, so the filter dropped it whole and no field
    # of it can appear here.
    assert "span/claude_code.tool" not in set(inventory.iter_field_ids(document))
    assert document["span_accounting"]["balanced"] is True
    assert hostile_run.canary.passed is True


# ---------------------------------------------------------------------------
# 2. Nothing hostile reaches any of the three artifacts
# ---------------------------------------------------------------------------


def _all_artifacts(run: inventory.InventoryRun) -> str:
    return "".join(
        path.read_text(encoding="utf-8")
        for path in (run.json_path, run.markdown_path, run.candidate_path)
    )


@requires_collector
@pytest.mark.parametrize("surface", certification.TRACE_SURFACES + certification.METRIC_SURFACES)
def test_no_hostile_value_reaches_any_report(hostile_run, surface):
    text = _all_artifacts(hostile_run)
    survivors = [value for value in certification.sentinel_values((surface,)) if value in text]
    assert survivors == [], f"{len(survivors)} hostile value(s) survived the {surface} surface"


@requires_collector
def test_no_bare_sentinel_reaches_any_report(hostile_run):
    """Belt and braces: the surface-tagged check above cannot catch an untagged fragment."""
    text = _all_artifacts(hostile_run)
    assert [value for value in certification.HOSTILE_SENTINELS.values() if value in text] == []


@requires_collector
def test_no_identifier_of_any_kind_reaches_any_report(hostile_run):
    text = _all_artifacts(hostile_run)
    for forbidden in ("Trace ID", "Span ID", "Parent ID", "TraceState", "Link #", "Exemplar #"):
        assert forbidden not in text, forbidden
    assert "5b8efff798038103d269b633813fc60c" not in text
    assert "eee19b7ec3c1b174" not in text


@requires_collector
def test_the_raw_invocation_id_is_not_printed(hostile_run):
    """The one identifier the contract *allows* to be exported is still not printed by the report."""
    assert certification.INVOCATION_ID not in _all_artifacts(hostile_run)
    entry = field(hostile_run.document, "resource.attribute/traces/forge.invocation.id")
    assert entry["value_classes"] == [inventory.CLASS_HEX32]
    assert entry.get("values", []) == []


@requires_collector
def test_the_forbidden_output_scan_passes_on_every_rendered_artifact(hostile_run):
    assert hostile_run.document["privacy_scan"]["passed"] is True
    for path in (hostile_run.json_path, hostile_run.markdown_path, hostile_run.candidate_path):
        assert inventory.scan_forbidden_output(path.read_text(encoding="utf-8")) == (), path.name


def test_a_hostile_pre_redaction_export_cannot_put_a_value_in_any_report():
    """The negative control: the same hostile payload as if `transform/privacy` had not run.

    Nothing here is a claim about the Collector -- it is a claim about the inventory. Even handed an
    unredacted dump, the reports carry classes and counts, never a value.
    """
    hostile = [
        ("user.email", f"Str({certification.HOSTILE_SENTINELS['email']})"),
        ("user_prompt", f"Str({certification.HOSTILE_SENTINELS['prompt']})"),
        ("assistant_response", f"Str({certification.HOSTILE_SENTINELS['assistant_response']})"),
        ("tool_output", f"Str({certification.HOSTILE_SENTINELS['tool_output']})"),
        ("error", f"Str({certification.HOSTILE_SENTINELS['error_string']})"),
        ("url.full", f"Str({certification.HOSTILE_SENTINELS['url_full']})"),
        ("workspace.host_paths", f"Str({certification.HOSTILE_SENTINELS['windows_path']})"),
        ("process.environment", f"Str({certification.HOSTILE_SENTINELS['environment_value']})"),
        ("http.request.header.authorization", f"Str({certification.HOSTILE_SENTINELS['credential']})"),
        ("session.id", f"Str({certification.HOSTILE_SENTINELS['session_id']})"),
    ]
    output = trace_dump(
        resource=hostile,
        scope_attributes=hostile,
        spans=[
            {
                "name": "claude_code.interaction",
                "attributes": hostile,
                "events": [{"name": "hostile.event", "attributes": hostile}],
                "links": [hostile],
            }
        ],
    ) + metric_dump(metrics=[{"name": "hostile.metric", "attributes": hostile, "exemplars": hostile}])

    document = document_for(output)
    text = rendered(document)
    for sentinel in certification.HOSTILE_SENTINELS.values():
        assert sentinel not in text, sentinel
    assert document["gate6"]["verdict"] == "FAIL"


def test_a_hostile_export_is_reported_rather_than_dropped():
    """Fail-closed is not fail-silent. Every hostile surface is still counted and named."""
    hostile = [("user.email", f"Str({certification.HOSTILE_SENTINELS['email']})")]
    document = document_for(trace_dump(resource=hostile, spans=[{"name": "forge.phase"}]))
    entry = field(document, "resource.attribute/traces/user.email")
    assert entry["status"] == inventory.STATUS_REDACTED
    assert entry["occurrences"] == 1
    assert entry.get("values", []) == []
    assert document["totals"]["redacted"] == 1


# ---------------------------------------------------------------------------
# 3. Unknown schema identifiers are reviewable, one entry each
# ---------------------------------------------------------------------------


def test_two_distinct_unknown_names_produce_two_distinct_entries():
    """The heart of it. An unknown identifier is never merged into a shared bucket."""
    output = trace_dump(
        spans=[
            {"name": "claude_code.brand_new_alpha"},
            {"name": "claude_code.brand_new_beta"},
            {"name": "claude_code.brand_new_beta"},
        ]
    )
    document = document_for(output)
    alpha = field(document, "span/claude_code.brand_new_alpha")
    beta = field(document, "span/claude_code.brand_new_beta")
    assert alpha["occurrences"] == 1
    assert beta["occurrences"] == 2
    assert alpha["identifier_status"] == inventory.IDENTIFIER_UNKNOWN_SAFE
    assert beta["identifier_status"] == inventory.IDENTIFIER_UNKNOWN_SAFE
    assert document["identifiers"][inventory.IDENTIFIER_UNKNOWN_SAFE] == 2


def test_two_distinct_unsafe_names_also_produce_two_distinct_entries():
    """Withholding a name must not merge the fields that carried it."""
    output = trace_dump(spans=[{"name": "user_prompt_alpha"}, {"name": "user_prompt_beta"}])
    document = document_for(output)
    spans = [item for item in document["fields"] if item["kind"] == "span"]
    assert len(spans) == 2
    assert len({item["fingerprint"] for item in spans}) == 2
    assert all(item["identifier_status"] == inventory.IDENTIFIER_UNPRINTABLE for item in spans)


@pytest.mark.parametrize(
    "category, name",
    [
        ("resource.attribute.key", "vendor.brand.new.field"),
        ("span.name", "claude_code.brand_new_span"),
        ("event.name", "claude_code.brand_new_event"),
        ("metric.name", "claude_code.brand_new_metric"),
        ("scope.name", "com.anthropic.claude_code"),
        ("scope.name", "github.com/vendor/project/instrumentation"),
        ("metric.datapoint.attribute.key", "claude_code.decision_source"),
    ],
)
def test_an_ordinary_unknown_identifier_is_safe_and_printed_in_full(category, name):
    assessment = inventory.assess_identifier(category, name)
    assert assessment.safe is True, assessment.failures
    assert assessment.failures == ()


@pytest.mark.parametrize(
    "label, category, name",
    [
        ("windows path", "span.name", "C:\\CodexData\\worktrees\\secret.py"),
        ("unix path", "span.name", "/home/operator/.ssh/id_ed25519"),
        ("url", "span.name", "https://api.vendor.invalid/v1/messages"),
        ("email", "resource.attribute.key", "operator@vendor.invalid"),
        ("api key", "span.name", "sk-abcdefghijklmnop"),
        ("uuid", "span.name", "4f0e2b8a-91d3-4c6e-b7a5-0d2f13c8e964"),
        ("trace id", "span.name", "5b8efff798038103d269b633813fc60c"),
        ("span id", "span.name", "eee19b7ec3c1b174"),
        ("content bearing", "span.attribute.key", "claude.user_prompt.v3"),
        ("content bearing", "span.attribute.key", "gen_ai.completion.0.content"),
        ("whitespace", "span.name", "a name with spaces"),
        ("control character", "span.name", "name\x01with\x02control"),
        ("non ascii", "span.name", "na\u00efve.identifier"),
        ("too long", "span.name", "a" * 101),
        ("empty", "span.name", ""),
        ("base64 blob", "span.name", "A" * 44),
        ("leading slash", "scope.name", "/etc/passwd"),
        ("tilde", "span.name", "~operator.key"),
        ("query string", "span.name", "key=value"),
    ],
)
def test_an_unsafe_identifier_is_refused_by_the_policy(label, category, name):
    assessment = inventory.assess_identifier(category, name)
    assert assessment.safe is False, label
    assert assessment.failures, label
    # The assessment object itself must not carry the string anywhere a log or traceback could find.
    # An empty identifier is skipped here only because "" is a substring of everything.
    if name:
        assert not _leaked(name, repr(assessment)), label
        assert not _leaked(name, str(assessment.failures)), label


def test_an_unsafe_identifier_produces_a_fingerprint_and_nothing_else():
    hostile = "C:\\CodexData\\worktrees\\TASK-sentinel\\secret.py"
    document = document_for(trace_dump(spans=[{"name": hostile}]))
    digest = hashlib.sha256(hostile.encode("utf-8")).hexdigest()
    entry = field(document, f"span/sha256-{digest}")
    assert entry["identifier_status"] == inventory.IDENTIFIER_UNPRINTABLE
    assert entry["fingerprint"] == digest
    assert entry["fingerprint_algorithm"] == "sha256"
    assert entry["identifier_category"] == "span.name"
    assert entry["identifier_findings"]
    assert not _leaked(hostile, rendered(document))


def test_a_fingerprint_is_deterministic_across_runs_and_matches_the_plain_digest():
    name = "user_prompt.v3"
    first = inventory.fingerprint_identifier(name)
    second = inventory.fingerprint_identifier(name)
    assert first == second == hashlib.sha256(name.encode("utf-8")).hexdigest()
    one = document_for(trace_dump(spans=[{"name": name}]))
    two = document_for(trace_dump(spans=[{"name": name}]))
    assert inventory.render_json(one) == inventory.render_json(two)
    assert field(one, f"span/sha256-{first}")["fingerprint"] == first


def test_an_unprintable_identifier_fails_the_gate_6_verdict():
    document = document_for(trace_dump(spans=[{"name": "/home/operator/.ssh/id_ed25519"}]))
    assert document["identifiers"][inventory.IDENTIFIER_UNPRINTABLE] == 1
    assert _condition(document, "every observed identifier is printable") is False
    assert document["gate6"]["verdict"] == "FAIL"


def test_an_unprintable_identifier_can_never_be_approved_by_editing_the_manifest():
    hostile = "/home/operator/.ssh/id_ed25519"
    digest = inventory.fingerprint_identifier(hostile)
    manifest = _manifest_with_field(
        inventory.ManifestField(
            id=f"span/sha256-{digest}",
            status=inventory.STATUS_APPROVED,
            expected=False,
            note="",
            constraints={"kinds": ("Internal",), "status_codes": ("Unset",)},
        )
    )
    document = document_for(trace_dump(spans=[{"name": hostile}]), manifest)
    entry = field(document, f"span/sha256-{digest}")
    assert entry["status"] == inventory.STATUS_UNAPPROVED
    assert "the identifier failed the safe-identifier policy and cannot be approved" in entry["reasons"]


def test_no_unsafe_identifier_survives_any_artifact_or_any_exception():
    """JSON, Markdown, candidate manifest, and every string this module can raise or log."""
    hostile = {
        "path": "C:\\CodexData\\secret\\id_rsa",
        "url": "https://api.vendor.invalid/v1?api_key=SENTINEL",
        "email": "operator@vendor.invalid",
        "prompt": "claude.user_prompt.raw",
    }
    output = trace_dump(
        spans=[{"name": hostile["path"], "events": [{"name": hostile["prompt"]}]}],
        scope=hostile["url"].replace(" ", ""),
    ) + metric_dump(scope=hostile["email"], metrics=[{"name": hostile["prompt"]}])
    document = document_for(output)
    text = rendered(document) + repr(document) + json.dumps(document, default=str)
    for label, value in hostile.items():
        assert not _leaked(value, text), label
    # And an exception raised while judging it carries nothing either.
    try:
        inventory.compare([{"id": "span/x", "kind": "span", "occurrences": "not a number"}], MANIFEST)
    except Exception as error:  # pragma: no cover - defensive
        for label, value in hostile.items():
            assert not _leaked(value, str(error)), label


def test_the_safe_identifier_policy_names_every_check_it_applies():
    assert len(inventory.IDENTIFIER_CHECKS) == 11
    assert len(set(inventory.IDENTIFIER_CHECKS)) == len(inventory.IDENTIFIER_CHECKS)
    findings = inventory.assess_identifier("span.name", "C:\\a b\\c").failures
    assert set(findings) <= set(inventory.IDENTIFIER_CHECKS)


def test_the_identifier_categories_are_exactly_the_nine_schema_positions():
    assert inventory.IDENTIFIER_CATEGORIES == (
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
    assert set(inventory.IDENTIFIER_VOCABULARY) == set(inventory.IDENTIFIER_CATEGORIES)


def test_a_unit_and_a_description_stay_values_however_safe_the_metric_name_is():
    """Blocker 1's explicit carve-out: a safe metric name buys the prose beneath it nothing."""
    output = metric_dump(
        metrics=[
            {
                "name": "claude_code.brand_new_metric",
                "unit": "furlongs",
                "description": "prose nobody reviewed, possibly carrying operator detail",
            }
        ]
    )
    document = document_for(output)
    entry = field(document, "metric/claude_code.brand_new_metric")
    # The name is safe and printed in full...
    assert entry["identifier_status"] == inventory.IDENTIFIER_UNKNOWN_SAFE
    assert entry["name"] == "claude_code.brand_new_metric"
    # ...and its unit and description are not.
    assert entry["unit_classes"] == [inventory.CLASS_UNCLASSIFIABLE]
    assert entry["description_classes"] == [inventory.CLASS_UNCLASSIFIABLE]
    assert entry.get("units", []) == []
    assert entry.get("descriptions", []) == []
    text = rendered(document)
    assert "furlongs" not in text
    assert "prose nobody reviewed" not in text


def test_an_allowlisted_unit_and_description_are_printed():
    output = metric_dump(
        metrics=[
            {
                "name": "forge.diagnostic.span.accepted.forge_phase",
                "unit": "ms",
                "description": "spans named forge.phase, counted accepted",
            }
        ]
    )
    entry = field(document_for(output), "metric/forge.diagnostic.span.accepted.forge_phase")
    assert entry["units"] == ["ms"]
    assert entry["unit_classes"] == [inventory.CLASS_STATIC]
    assert entry["descriptions"] == ["spans named forge.phase, counted accepted"]


# ---------------------------------------------------------------------------
# 4. The candidate manifest, and the review that needs no Collector log
# ---------------------------------------------------------------------------


def _unknown_output() -> str:
    return trace_dump(
        scope="com.anthropic.claude_code",
        resource=[("service.name", "Str(claude-code)"), ("forge.invocation.id", f"Str({'a' * 32})")],
        spans=[
            {
                "name": "claude_code.brand_new_span",
                "kind": "Client",
                "attributes": [("forge.task.id", "Str(1)")],
                "events": [{"name": "claude_code.brand_new_event"}],
            }
        ],
    ) + metric_dump(
        scope="com.anthropic.claude_code",
        metrics=[{"name": "claude_code.brand_new_metric", "unit": "", "description": ""}],
    )


def test_the_candidate_manifest_names_every_safe_unknown_identifier_in_full():
    document = document_for(_unknown_output())
    candidate = inventory.build_candidate_manifest(document)
    for name in (
        "com.anthropic.claude_code",
        "claude_code.brand_new_span",
        "claude_code.brand_new_event",
        "claude_code.brand_new_metric",
    ):
        assert name in candidate, name
    parsed = yaml.safe_load(candidate)
    assert "claude_code.brand_new_span" in parsed["vocabulary"]["span_names"]
    assert "claude_code.brand_new_event" in parsed["vocabulary"]["event_names"]
    assert "claude_code.brand_new_metric" in parsed["vocabulary"]["metric_names"]
    assert "com.anthropic.claude_code" in parsed["vocabulary"]["scope_names"]


def test_the_candidate_manifest_proposes_only_unapproved_entries():
    parsed = yaml.safe_load(inventory.build_candidate_manifest(document_for(_unknown_output())))
    assert parsed["fields"], "nothing was proposed, so this proves nothing"
    for entry in parsed["fields"]:
        assert entry["status"] == inventory.STATUS_UNAPPROVED


def test_the_candidate_manifest_carries_no_telemetry_value():
    """Classes and OTLP enumerations only. Widening `static_values` stays a human decision."""
    output = _unknown_output() + trace_dump(
        resource=[("deployment.environment", "Str(local)")], spans=[{"name": "forge.phase"}]
    ) + metric_dump(
        metrics=[{"name": "forge.diagnostic.span.accepted.forge_phase", "unit": "ms",
                  "description": "spans named forge.phase, counted accepted"}]
    )
    document = document_for(output)
    candidate = inventory.build_candidate_manifest(document)
    parsed = yaml.safe_load(candidate)
    for entry in parsed["fields"]:
        assert not {"values", "units", "descriptions"} & set(entry), entry["id"]
    # Values that the *report* is allowed to print still do not appear in the proposal.
    assert "local" not in candidate
    assert "ms" not in candidate.replace("claude_code", "").replace("_names", "")
    assert "spans named forge.phase" not in candidate
    assert inventory.scan_forbidden_output(candidate) == ()


def test_the_candidate_manifest_withholds_unprintable_identifiers_and_says_so():
    document = document_for(
        trace_dump(spans=[{"name": "claude_code.safe_span"}, {"name": "C:\\secret\\id_rsa"}])
    )
    candidate = inventory.build_candidate_manifest(document)
    assert "claude_code.safe_span" in candidate
    assert not _leaked("C:\\secret\\id_rsa", candidate)
    assert "withheld, unprintable:         1" in candidate


def test_the_candidate_manifest_is_deterministic():
    document = document_for(_unknown_output())
    assert inventory.build_candidate_manifest(document) == inventory.build_candidate_manifest(document)
    assert inventory.build_candidate_manifest(document) == inventory.build_candidate_manifest(
        document_for(_unknown_output())
    )


def test_the_candidate_manifest_closes_every_unknown_field(tmp_path):
    """The proof that the raw Collector log is unnecessary.

    Everything used here comes from the two files a run publishes. Merging the proposal into the
    manifest leaves nothing UNKNOWN — and leaves everything UNAPPROVED, because a proposal is not an
    approval.
    """
    document = document_for(_unknown_output())
    assert document["totals"]["unknown"] > 0

    merged_path = _merge_candidate(tmp_path, inventory.build_candidate_manifest(document))
    merged = inventory.load_manifest(merged_path)
    rejudged = inventory.compare(_observations(document), merged)

    assert [item for item in rejudged if item["status"] == inventory.DISPOSITION_UNKNOWN] == []
    assert all(
        item["status"] in (inventory.STATUS_UNAPPROVED, inventory.STATUS_BLOCKED)
        for item in rejudged
    )


@requires_collector
def test_a_real_run_can_be_reviewed_from_its_own_reports_alone(hostile_run, tmp_path):
    """The same closure, end to end, against a real Collector and the Gate 3 hostile payloads."""
    candidate = hostile_run.candidate_path.read_text(encoding="utf-8")
    document = json.loads(hostile_run.json_path.read_text(encoding="utf-8"))
    assert document["totals"]["unknown"] > 0

    merged = inventory.load_manifest(_merge_candidate(tmp_path, candidate))
    rejudged = inventory.compare(_observations(document), merged)
    assert [item for item in rejudged if item["status"] == inventory.DISPOSITION_UNKNOWN] == []


def _merge_candidate(tmp_path: Path, candidate: str) -> Path:
    """Merge a candidate manifest into the shipped one, exactly as a reviewer would by hand."""
    base = yaml.safe_load(inventory.MANIFEST_PATH.read_text(encoding="utf-8"))
    proposal = yaml.safe_load(candidate)
    for section, names in (proposal.get("vocabulary") or {}).items():
        base["vocabulary"][section] = sorted(
            set(base["vocabulary"].get(section) or []) | set(names or [])
        )
    base["fields"] = (base.get("fields") or []) + (proposal.get("fields") or [])
    path = tmp_path / "merged-manifest.yaml"
    path.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
    return path


def _observations(document: dict) -> list[dict]:
    """Strip the judgement back off a document, leaving what the parser observed."""
    return [
        {key: value for key, value in item.items() if key not in ("status", "reasons", "note")}
        for item in document["fields"]
    ]


# ---------------------------------------------------------------------------
# 5. Every field is represented exactly once
# ---------------------------------------------------------------------------


def test_every_field_appears_exactly_once_in_the_canonical_inventory():
    output = trace_dump(
        resource=[("service.name", "Str(claude-code)"), ("forge.invocation.id", f"Str({'a' * 32})")],
        spans=[
            {"name": "claude_code.interaction", "attributes": [("forge.task.id", "Str(x)")]},
            {"name": "claude_code.interaction", "attributes": [("forge.task.id", "Str(y)")]},
            {"name": "forge.phase", "attributes": [("forge.task.id", "Str(z)")]},
        ],
    )
    document = document_for(output)
    identifiers = list(inventory.iter_field_ids(document))
    assert len(identifiers) == len(set(identifiers))
    assert field(document, "span/claude_code.interaction")["occurrences"] == 2
    assert field(document, "span.attribute/claude_code.interaction/forge.task.id")["occurrences"] == 2
    assert field(document, "span/forge.phase")["occurrences"] == 1
    assert document["totals"]["observed"] == len(identifiers)


@requires_collector
def test_the_accounting_buckets_partition_the_observed_fields(hostile_run):
    totals = hostile_run.document["totals"]
    counted = (
        totals["approved"]
        + totals["blocked"]
        + totals["redacted"]
        + totals["unapproved"]
        + totals["unknown"]
    )
    assert counted == totals["observed"]
    assert totals["unresolved_remainder"] == 0


def test_a_line_the_parser_does_not_understand_becomes_an_unresolved_remainder():
    """Not skipped, not guessed at. An unparsed line is visible arithmetic."""
    output = trace_dump(spans=[{"name": "forge.phase", "attributes": [("forge.task.id", "Str(x)")]}])
    corrupted = output.replace("     -> forge.task.id: Str(x)", "     -> forge.task.id Str(x")
    document = document_for(corrupted)
    entry = field(document, "unresolved/span.attribute")
    assert entry["status"] == inventory.DISPOSITION_UNRESOLVED
    assert document["totals"]["unresolved_remainder"] == 1
    assert _condition(document, "the inventory remainder is zero") is False


# ---------------------------------------------------------------------------
# 6. Normalization, and the allowlist that governs printed values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "otlp_type, raw, expected",
    [
        ("Str", "b7c4e21f9a08d5364e1fb0972c8ad653", inventory.CLASS_HEX32),
        ("Str", "eee19b7ec3c1b174", inventory.CLASS_HEX16),
        ("Str", "2.1.223", inventory.CLASS_SEMVER),
        ("Str", "1200", inventory.CLASS_NONNEGATIVE_INTEGER),
        ("Str", "-4", inventory.CLASS_INTEGER),
        ("Str", "0.667", inventory.CLASS_NONNEGATIVE_DOUBLE),
        ("Str", "250ms", inventory.CLASS_DURATION),
        ("Str", "2023-11-14 22:13:20 +0000 UTC", inventory.CLASS_TIMESTAMP_PRESENT),
        ("Str", "http://127.0.0.1:4317", inventory.CLASS_LOCALHOST_ENDPOINT),
        ("Str", "", inventory.CLASS_ABSENT),
        ("Int", "3", inventory.CLASS_NONNEGATIVE_INTEGER),
        ("Int", "-3", inventory.CLASS_INTEGER),
        ("Double", "0.5", inventory.CLASS_NONNEGATIVE_DOUBLE),
        ("Bool", "true", inventory.CLASS_BOOLEAN),
        ("Empty", "anything", inventory.CLASS_ABSENT),
    ],
)
def test_a_dynamic_value_is_reduced_to_a_bounded_class(otlp_type, raw, expected):
    value_class, emitted = inventory.classify_value("forge.task.id", otlp_type, raw, MANIFEST)
    assert value_class == expected
    assert emitted is None, "a classified value must never be carried into the report"


def test_a_static_value_is_printed_only_through_the_explicit_allowlist():
    listed, emitted = inventory.classify_value("service.name", "Str", "claude-code", MANIFEST)
    assert (listed, emitted) == (inventory.CLASS_STATIC, "claude-code")
    other, emitted = inventory.classify_value("forge.task.id", "Str", "claude-code", MANIFEST)
    assert (other, emitted) == (inventory.CLASS_UNCLASSIFIABLE, None)
    unlisted, emitted = inventory.classify_value("service.name", "Str", "claude-code-fork", MANIFEST)
    assert (unlisted, emitted) == (inventory.CLASS_UNCLASSIFIABLE, None)


def test_only_the_two_allowlisted_classes_may_carry_a_value():
    output = trace_dump(
        resource=[
            ("service.name", "Str(claude-code)"),
            ("forge.invocation.id", f"Str({'d' * 32})"),
            ("forge.task.id", "Str(TASK-0a1b2c3d)"),
        ],
        spans=[{"name": "forge.phase"}],
    )
    document = document_for(output)
    for item in document["fields"]:
        for value in item.get("values", []):
            assert set(item["value_classes"]) <= inventory.VALUE_BEARING_CLASSES, item["id"]
            assert value in MANIFEST.static_values.get(item["key"], ()) + MANIFEST.enum_values.get(
                item["key"], ()
            )
    assert field(document, "resource.attribute/traces/service.name")["values"] == ["claude-code"]
    assert "values" not in field(document, "resource.attribute/traces/forge.invocation.id")


def test_an_unclassifiable_value_fails_closed_and_cannot_be_approved():
    output = trace_dump(
        resource=[("forge.task.id", "Str(TASK-0a1b2c3d)")], spans=[{"name": "forge.phase"}]
    )
    approving = _approving_manifest()
    document = document_for(output, approving)
    entry = field(document, "resource.attribute/traces/forge.task.id")
    assert entry["value_classes"] == [inventory.CLASS_UNCLASSIFIABLE]
    assert entry.get("values", []) == []
    assert entry["status"] != inventory.STATUS_APPROVED


def test_an_unknown_value_type_fails_closed():
    """A type token from a Collector revision this policy has not read is neither printed nor kept."""
    document = document_for(
        trace_dump(resource=[("forge.task.id", "Quantity(5)")], spans=[{"name": "forge.phase"}])
    )
    entry = field(document, "resource.attribute/traces/forge.task.id")
    assert entry.get("types", []) == []
    assert entry["value_classes"] == [inventory.CLASS_UNCLASSIFIABLE]
    assert "value type outside the OTLP vocabulary" in entry["reasons"]
    assert "Quantity" not in inventory.render_json(document)


def test_a_span_kind_or_status_outside_the_otlp_vocabulary_is_not_printed():
    output = trace_dump(spans=[{"name": "forge.phase", "kind": "Sideways", "status": "Perhaps"}])
    document = document_for(output)
    entry = field(document, "span/forge.phase")
    assert entry.get("kinds", []) == []
    assert entry.get("status_codes", []) == []
    assert entry["status"] == inventory.STATUS_UNAPPROVED
    text = rendered(document)
    assert "Sideways" not in text and "Perhaps" not in text


# ---------------------------------------------------------------------------
# 7. The manifest, and what a verdict requires of it
# ---------------------------------------------------------------------------


def test_an_approved_field_requires_a_named_reviewer():
    """The one invariant that survives a real review.

    This is deliberately *not* "the manifest approves nothing". A reviewer must be able to approve
    fields by editing the manifest and nothing else, so no test may assert a particular status. What
    is asserted is the consistency rule: if anything here is APPROVED, a human is named for it.
    """
    approved = [f.id for f in MANIFEST.fields.values() if f.status == inventory.STATUS_APPROVED]
    if approved:
        assert MANIFEST.is_human_approved, approved
    assert bool(MANIFEST.review["approved_by"].strip()) == (MANIFEST.review["state"] == "approved")


def test_the_manifest_is_pinned_to_the_certified_agent_and_collector():
    assert MANIFEST.subject["agent_version"] == "2.1.223"
    assert MANIFEST.subject["collector_version"] == certification.CERTIFIED_COLLECTOR_VERSION
    assert MANIFEST.subject["contract_version"] == "1.0"


def test_the_manifest_vocabulary_stays_in_step_with_the_certified_configuration():
    assert set(certification.DIAGNOSTIC_SPAN_NAMES) <= MANIFEST.vocabulary["span_names"]
    assert set(certification.REQUIRED_ALLOWED_ATTRIBUTE_KEYS) <= MANIFEST.vocabulary["attribute_keys"]
    assert set(certification.diagnostic_metric_names()) <= MANIFEST.vocabulary["metric_names"]
    assert set(certification.FORBIDDEN_ATTRIBUTE_KEYS) <= MANIFEST.redacted_keys
    assert set(certification.UNKNOWN_HOSTILE_KEYS) <= MANIFEST.redacted_keys


def test_the_manifest_never_allowlists_a_forbidden_key_for_printing():
    for key in MANIFEST.static_values:
        assert key not in certification.FORBIDDEN_ATTRIBUTE_KEYS
    for key in MANIFEST.enum_values:
        assert key not in certification.FORBIDDEN_ATTRIBUTE_KEYS


def test_an_unapproved_field_fails_the_gate_6_verdict():
    document = document_for(trace_dump(spans=[{"name": "claude_code.interaction"}]))
    assert field(document, "span/claude_code.interaction")["status"] == inventory.STATUS_UNAPPROVED
    assert document["totals"]["unapproved"] >= 1
    assert document["gate6"]["verdict"] == "FAIL"
    assert _condition(document, "no observed field is UNAPPROVED") is False


def test_an_approved_field_is_reported_approved_when_it_matches_its_declaration():
    """The positive control. Without it, "everything fails" would prove nothing about the machinery."""
    document = document_for(
        trace_dump(
            resource=[("service.name", "Str(claude-code)")],
            spans=[{"name": "claude_code.interaction"}],
        ),
        _approving_manifest(),
    )
    assert field(document, "span/claude_code.interaction")["status"] == inventory.STATUS_APPROVED
    assert field(document, "resource.attribute/traces/service.name")["status"] == (
        inventory.STATUS_APPROVED
    )


def test_an_approved_field_that_arrives_in_an_unapproved_shape_is_not_approved():
    """An approval describes a field. A span kind nobody approved is not covered by it."""
    document = document_for(
        trace_dump(spans=[{"name": "claude_code.interaction", "kind": "Producer"}]),
        _approving_manifest(),
    )
    entry = field(document, "span/claude_code.interaction")
    assert entry["status"] == inventory.STATUS_UNAPPROVED
    assert any("kinds" in reason for reason in entry["reasons"])


def test_an_unknown_field_fails_closed_rather_than_being_omitted():
    document = document_for(
        trace_dump(resource=[("forge.task.id", "Str(1)")], spans=[{"name": "forge.phase"}])
    )
    entry = field(document, "resource.attribute/traces/forge.task.id")
    assert entry["status"] == inventory.DISPOSITION_UNKNOWN
    assert "no entry in the approval manifest" in entry["reasons"]
    assert document["totals"]["unknown"] >= 1
    assert _condition(document, "every observed field is in the manifest") is False


def test_a_field_expected_by_the_manifest_but_not_observed_is_reported():
    """Absence is a finding, not a silent pass."""
    document = document_for(trace_dump(spans=[{"name": "forge.phase"}]))
    assert "span/claude_code.interaction" in document["expected_not_observed"]
    assert "resource.attribute/traces/forge.invocation.id" in document["expected_not_observed"]
    assert document["totals"]["expected_not_observed"] == len(document["expected_not_observed"])
    assert _condition(document, "every expected field was observed") is False


def test_a_blocked_field_that_survives_the_pipeline_fails_the_verdict():
    """`claude_code.llm_request` is BLOCKED under Collector 0.158.0. Seeing one is a failure."""
    document = document_for(trace_dump(spans=[{"name": "claude_code.llm_request"}]))
    entry = field(document, "span/claude_code.llm_request")
    assert entry["status"] == inventory.STATUS_BLOCKED
    assert document["totals"]["blocked"] == 1
    assert _condition(document, "no BLOCKED field was observed") is False


@requires_collector
def test_a_linked_llm_request_span_is_still_dropped_before_the_inventory_sees_it(hostile_run):
    """The block is the Collector's, not the report's. One of the two payload spans carried a link."""
    accounting = hostile_run.canary.accounting
    assert accounting.missing_by_name()["claude_code.llm_request"] == 1
    assert hostile_run.document["carriers"]["span_links"] == 0
    assert field(hostile_run.document, "span/claude_code.llm_request")["link_count"] == 0


def test_a_span_link_reaching_the_inventory_is_counted_and_can_never_be_approved():
    output = trace_dump(
        spans=[{"name": "claude_code.interaction", "links": [[("user.email", "Str(a@b.invalid)")]]}]
    )
    document = document_for(output, _approving_manifest())
    assert document["carriers"]["span_links"] == 1
    entry = field(document, "span/claude_code.interaction")
    assert entry["link_count"] == 1
    assert entry["status"] == inventory.STATUS_UNAPPROVED
    assert _condition(document, "no span link reached the inventory") is False
    assert "a@b.invalid" not in rendered(document)


def test_a_metric_exemplar_reaching_the_inventory_is_counted_and_can_never_be_approved():
    output = metric_dump(
        metrics=[
            {
                "name": "forge.diagnostic.span.accepted.other",
                "description": "spans named outside the diagnostic vocabulary, counted accepted",
                "exemplars": [("user.email", "Str(a@b.invalid)")],
            }
        ]
    )
    document = document_for(output)
    assert document["carriers"]["metric_exemplars"] == 1
    entry = field(document, "metric/forge.diagnostic.span.accepted.other")
    assert entry["exemplar_count"] == 1
    assert entry["status"] == inventory.STATUS_UNAPPROVED
    assert _condition(document, "no metric exemplar reached the inventory") is False
    assert "a@b.invalid" not in rendered(document)


def test_a_review_state_without_a_name_is_not_an_approval():
    for review in ({"approved_by": "", "state": "approved"}, {"approved_by": "A", "state": "draft"}):
        assert _replace_manifest(MANIFEST, review=review).is_human_approved is False


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ({"schema": "something-else"}, "schema must be"),
        ({"fields": [{"id": "x", "status": "MAYBE"}]}, "status must be one of"),
        ({"fields": [{"id": "x", "status": "APPROVED", "surprise": 1}]}, "unknown key"),
        ({"vocabulary": {"nonsense": []}}, "unknown vocabulary section"),
        ({"redacted_keys": "user.email"}, "redacted_keys must be a list"),
    ],
)
def test_a_malformed_manifest_is_an_error_rather_than_a_default(tmp_path, mutation, expected):
    raw = yaml.safe_load(inventory.MANIFEST_PATH.read_text(encoding="utf-8"))
    raw.update(mutation)
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(inventory.ManifestError) as error:
        inventory.load_manifest(path)
    assert expected in str(error.value)


# ---------------------------------------------------------------------------
# 8. Determinism, and JSON/Markdown agreement
# ---------------------------------------------------------------------------


def _sample_output() -> str:
    return trace_dump(
        resource=[("service.name", "Str(claude-code)"), ("forge.invocation.id", f"Str({'c' * 32})")],
        spans=[
            {"name": "claude_code.interaction", "attributes": [("forge.task.id", "Str(1)")]},
            {"name": "forge.phase", "events": [{"name": "vendor.event"}]},
        ],
    ) + metric_dump(
        metrics=[
            {
                "name": "forge.diagnostic.span.accepted.forge_phase",
                "description": "spans named forge.phase, counted accepted",
            }
        ]
    )


def test_the_json_report_is_byte_identical_across_runs():
    first = inventory.render_json(document_for(_sample_output()))
    second = inventory.render_json(document_for(_sample_output()))
    assert first == second
    assert json.loads(first) == json.loads(second)
    assert first.endswith("\n")


def test_the_markdown_report_is_byte_identical_across_runs():
    first = inventory.render_markdown(document_for(_sample_output()))
    second = inventory.render_markdown(document_for(_sample_output()))
    assert first == second
    assert first.endswith("\n")


def test_the_field_list_is_sorted_and_therefore_order_independent():
    document = document_for(_sample_output())
    keys = [(item["kind"], item["id"]) for item in document["fields"]]
    assert keys == sorted(keys)


def test_reordering_the_export_does_not_change_the_report():
    """The Collector prints metrics in map order. The report must not inherit that."""
    traces = trace_dump(spans=[{"name": "forge.phase"}, {"name": "claude_code.interaction"}])
    reversed_traces = trace_dump(spans=[{"name": "claude_code.interaction"}, {"name": "forge.phase"}])
    assert inventory.render_json(document_for(traces)) == inventory.render_json(
        document_for(reversed_traces)
    )


def test_the_json_and_the_markdown_describe_the_same_inventory():
    document = document_for(_sample_output())
    markdown = inventory.render_markdown(document)
    identifiers = list(inventory.iter_field_ids(document))
    for identifier in identifiers:
        assert markdown.count(f"`{identifier}`") == 1, identifier
    rows = {
        line.split("|")[1].strip().strip("`")
        for line in markdown.splitlines()
        if line.startswith("| `")
    }
    assert rows == set(identifiers)
    for label, value in document["totals"].items():
        assert f"| {label.replace('_', ' ')} | {value} |" in markdown
    assert f"## Gate 6 verdict: {document['gate6']['verdict']}" in markdown


def test_every_section_of_the_inventory_is_rendered_even_when_empty():
    markdown = inventory.render_markdown(document_for(""))
    for _kind, title in inventory._MARKDOWN_SECTIONS:
        assert f"## {title}" in markdown
    assert markdown.count("_none observed_") == len(inventory._MARKDOWN_SECTIONS)


def test_a_report_full_of_fingerprints_still_passes_the_forbidden_output_scan():
    """A 64-character SHA-256 must not read as a 32-character trace id embedded inside it."""
    document = document_for(
        trace_dump(spans=[{"name": f"/home/operator/secret{index}"} for index in range(4)])
    )
    assert document["identifiers"][inventory.IDENTIFIER_UNPRINTABLE] == 4
    assert inventory.scan_forbidden_output(rendered(document)) == ()
    assert document["privacy_scan"]["passed"] is True


# ---------------------------------------------------------------------------
# 9. The agent vector
# ---------------------------------------------------------------------------


def test_the_default_agent_vector_is_exactly_the_approved_canary_invocation():
    """The canonical form is a vector, so no quoting layer can join or truncate an argument."""
    assert inventory.DEFAULT_CLAUDE_ARGV == (
        "claude",
        "-p",
        "Reply with exactly CANARY_OK and nothing else.",
        "--model",
        "sonnet",
        "--permission-mode",
        "dontAsk",
        "--tools",
        "",
        "--disable-slash-commands",
        "--max-turns",
        "1",
    )


@pytest.mark.parametrize(
    "index, argument",
    [
        (0, "claude"),
        (1, "-p"),
        (2, "Reply with exactly CANARY_OK and nothing else."),
        (3, "--model"),
        (4, "sonnet"),
        (5, "--permission-mode"),
        (6, "dontAsk"),
        (7, "--tools"),
        (8, ""),
        (9, "--disable-slash-commands"),
        (10, "--max-turns"),
        (11, "1"),
    ],
)
def test_each_argument_of_the_default_vector_is_exact(index, argument):
    assert inventory.DEFAULT_CLAUDE_ARGV[index] == argument


def test_the_canary_prompt_is_intact_and_names_canary_ok():
    assert inventory.CANARY_PROMPT == "Reply with exactly CANARY_OK and nothing else."
    assert inventory.DEFAULT_CLAUDE_ARGV[2] == inventory.CANARY_PROMPT
    assert inventory.CANARY_PROMPT.endswith("nothing else.")
    assert "CANARY_OK" in inventory.CANARY_PROMPT


def test_every_flag_in_the_vector_is_recorded_as_verified_against_the_cli():
    """The vector may only use flags somebody checked against `claude --help` on 2.1.223.

    The record is the evidence a reviewer diffs when the CLI moves. `--max-turns` is in it with the
    caveat that it is accepted but undocumented, which is exactly the kind of thing a version bump
    must re-check.
    """
    flags = [item for item in inventory.DEFAULT_CLAUDE_ARGV if item.startswith("-")]
    assert flags, "the vector has no flags, so this proves nothing"
    for flag in flags:
        assert flag in inventory.VERIFIED_CLAUDE_FLAGS, flag
        assert inventory.VERIFIED_CLAUDE_FLAGS[flag].strip(), flag
    assert set(inventory.VERIFIED_CLAUDE_FLAGS) == set(flags)


def test_the_refused_flags_are_real_flags_of_the_target_cli():
    """A refusal list naming flags that do not exist protects nothing.

    `--bypassPermissions` and `--acceptEdits` are values of `--permission-mode`, not flags, so they
    belong to the permission-mode check rather than to this list.
    """
    assert set(inventory.BYPASS_FLAGS) == {
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "--allowedtools",
        "--allowed-tools",
        "--disallowedtools",
        "--disallowed-tools",
    }
    for value in ("bypassPermissions", "acceptEdits", "auto", "manual", "plan"):
        assert f"--{value.lower()}" not in inventory.BYPASS_FLAGS


@pytest.mark.parametrize("mode", ["acceptEdits", "auto", "bypassPermissions", "manual", "plan"])
def test_every_other_permission_mode_the_cli_offers_is_refused(mode):
    """The CLI's full choice set, minus the one this canary runs under."""
    command = (
        f'claude -p "{inventory.CANARY_PROMPT}" --model sonnet --permission-mode {mode} '
        '--tools "" --disable-slash-commands --max-turns 1'
    )
    _vector, problems = inventory.resolve_agent_argv(command)
    assert any("--permission-mode must be dontAsk" in problem for problem in problems), problems


def test_the_vector_disables_tools_with_tools_rather_than_allowed_tools():
    """`--allowedTools` pre-approves a list; `--tools ""` restricts the set to nothing.

    Confusing the two is how a canary ends up with every built-in tool available.
    """
    argv = inventory.DEFAULT_CLAUDE_ARGV
    assert "--tools" in argv
    assert "--allowedTools" not in argv and "--allowed-tools" not in argv
    assert argv[argv.index("--tools") + 1] == ""


def test_the_default_vector_is_tool_disabled_one_turn_and_non_bypass():
    argv = inventory.DEFAULT_CLAUDE_ARGV
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--max-turns") + 1] == "1"
    assert argv[argv.index("--permission-mode") + 1] == inventory.REQUIRED_PERMISSION_MODE == "dontAsk"
    assert "--disable-slash-commands" in argv
    lowered = [item.lower() for item in argv]
    for flag in inventory.BYPASS_FLAGS:
        assert flag not in lowered, flag


def test_an_empty_agent_command_resolves_to_the_default_vector():
    vector, problems = inventory.resolve_agent_argv("")
    assert vector == inventory.DEFAULT_CLAUDE_ARGV
    assert problems == []


def test_a_command_naming_the_claude_executable_by_path_is_accepted():
    """The one permitted difference: how the executable is reached. The vector itself is fixed."""
    command = f"'C:\\Tools\\claude\\claude.cmd' -p \"{inventory.CANARY_PROMPT}\" " + (
        '--model sonnet --permission-mode dontAsk --tools "" --disable-slash-commands --max-turns 1'
    )
    vector, problems = inventory.resolve_agent_argv(command)
    assert problems == [], problems
    assert vector[1:] == inventory.DEFAULT_CLAUDE_ARGV[1:]


@pytest.mark.parametrize(
    "command, expected",
    [
        ("codex exec 'hello'", "must invoke `claude`"),
        (
            'claude -p "hi" --model sonnet --permission-mode dontAsk --tools "" '
            "--disable-slash-commands --max-turns 1",
            "conflicts with the approved canary vector",
        ),
        (
            f'claude -p "{inventory.CANARY_PROMPT}" --model sonnet --permission-mode dontAsk '
            '--tools "" --disable-slash-commands --max-turns 5',
            "conflicts with the approved canary vector",
        ),
        (
            f'claude -p "{inventory.CANARY_PROMPT}" --model sonnet --permission-mode dontAsk '
            '--tools "Bash" --disable-slash-commands --max-turns 1',
            "conflicts with the approved canary vector",
        ),
        (
            f'claude -p "{inventory.CANARY_PROMPT}" --model sonnet --permission-mode dontAsk '
            '--tools "" --max-turns 1',
            "conflicts with the approved canary vector",
        ),
    ],
)
def test_a_conflicting_agent_command_is_refused(command, expected):
    _vector, problems = inventory.resolve_agent_argv(command)
    assert any(expected in problem for problem in problems), problems


@pytest.mark.parametrize(
    "flag",
    [
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "--allowed-tools",
        "--allowedTools",
        "--disallowed-tools",
        "--disallowedTools",
    ],
)
def test_a_bypass_or_tool_enabling_flag_is_refused_by_name(flag):
    command = (
        f'claude -p "{inventory.CANARY_PROMPT}" --model sonnet --permission-mode dontAsk '
        f'--tools "" --disable-slash-commands --max-turns 1 {flag} x'
    )
    _vector, problems = inventory.resolve_agent_argv(command)
    assert any(flag.lower() in problem for problem in problems), problems


@pytest.mark.parametrize("mode", ["bypassPermissions", "acceptEdits", "plan"])
def test_a_permission_mode_other_than_dont_ask_is_refused(mode):
    command = (
        f'claude -p "{inventory.CANARY_PROMPT}" --model sonnet --permission-mode {mode} '
        '--tools "" --disable-slash-commands --max-turns 1'
    )
    _vector, problems = inventory.resolve_agent_argv(command)
    assert any("--permission-mode must be dontAsk" in problem for problem in problems), problems


def test_the_powershell_command_is_rendered_by_the_module_and_carries_no_agent_string():
    rendered_command = inventory.render_powershell_command("C:\\CodexData\\canary\\inventory1", "A Human")
    assert "--output-dir 'C:\\CodexData\\canary\\inventory1'" in rendered_command
    assert "--approved-by 'A Human'" in rendered_command
    assert "tests\\otel_inventory.py" in rendered_command
    # The vector is a constant in the module, so it must not appear on the command line at all.
    assert "--agent-command" not in rendered_command
    assert inventory.CANARY_PROMPT not in rendered_command


def test_the_powershell_renderer_escapes_a_quote_in_a_reviewer_name():
    assert "'O''Brien'" in inventory.render_powershell_command("C:\\out", "O'Brien")


def test_a_dry_run_prints_the_vector_and_starts_nothing(tmp_path, monkeypatch, capsys):
    def refuse(*args, **kwargs):  # pragma: no cover - the assertion is that it is never reached
        raise AssertionError("a process was started by a dry run")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(inventory, "run_inventory", refuse)

    exit_code = inventory.main(["--output-dir", str(tmp_path / "out"), "--dry-run"])
    printed = capsys.readouterr().out
    assert exit_code == 0
    assert not (tmp_path / "out").exists()
    for index, argument in enumerate(inventory.DEFAULT_CLAUDE_ARGV):
        assert f"[{index}] {argument!r}" in printed
    assert "--approved-by" in printed


# ---------------------------------------------------------------------------
# 10. The command line starts nothing it should not
# ---------------------------------------------------------------------------


def test_a_missing_approval_starts_no_process(tmp_path, monkeypatch):
    """Not "exits nonzero after starting a Collector" -- starts nothing at all."""
    def refuse(*args, **kwargs):  # pragma: no cover - the assertion is that it is never reached
        raise AssertionError("a process was started without an approval")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(inventory, "run_inventory", refuse)

    exit_code = inventory.main(["--output-dir", str(tmp_path / "out")])
    assert exit_code == 2
    assert not (tmp_path / "out").exists()


def test_a_blank_approval_is_not_an_approval(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(inventory, "run_inventory", lambda **_: pytest.fail("started"))
    assert inventory.main(["--output-dir", str(tmp_path / "out"), "--approved-by", "   "]) == 2
    assert "Refusing to launch an agent" in capsys.readouterr().err


def test_a_refused_agent_command_starts_no_process(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(inventory, "run_inventory", lambda **_: pytest.fail("started"))
    exit_code = inventory.main(
        [
            "--output-dir", str(tmp_path / "out"),
            "--approved-by", "a reviewer",
            "--agent-command", "claude -p 'hi' --dangerously-skip-permissions",
        ]
    )
    assert exit_code == 2
    assert "Refusing this agent command" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 11. The output directory
# ---------------------------------------------------------------------------


def test_an_existing_output_directory_is_not_overwritten_by_default(tmp_path):
    output = tmp_path / "reports"
    output.mkdir()
    (output / inventory.REPORT_JSON_NAME).write_text("previous run", encoding="utf-8")
    with pytest.raises(inventory.OutputDirectoryRefused) as error:
        inventory.prepare_output_directory(output)
    assert "refusing to overwrite" in str(error.value)
    assert (output / inventory.REPORT_JSON_NAME).read_text(encoding="utf-8") == "previous run"


def test_replace_existing_replaces_only_this_harnesss_own_reports(tmp_path):
    output = tmp_path / "reports"
    output.mkdir()
    for name in inventory.OWNED_REPORT_NAMES:
        (output / name).write_text("previous run", encoding="utf-8")
    inventory.prepare_output_directory(output, replace_existing=True)
    assert sorted(item.name for item in output.iterdir()) == []


def test_replace_existing_still_refuses_a_directory_holding_anything_else(tmp_path):
    output = tmp_path / "reports"
    output.mkdir()
    (output / "operator-notes.md").write_text("evidence", encoding="utf-8")
    with pytest.raises(inventory.OutputDirectoryRefused) as error:
        inventory.prepare_output_directory(output, replace_existing=True)
    assert "did not write" in str(error.value)
    assert (output / "operator-notes.md").read_text(encoding="utf-8") == "evidence"


def test_a_refused_output_directory_starts_no_process(tmp_path, monkeypatch, capsys):
    output = tmp_path / "reports"
    output.mkdir()
    (output / inventory.REPORT_JSON_NAME).write_text("previous run", encoding="utf-8")

    def refuse(*args, **kwargs):  # pragma: no cover - never reached
        raise AssertionError("a Collector was started over an existing inventory")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(certification, "collector_binary", lambda: Path("otelcol"))
    monkeypatch.setattr(
        certification, "collector_version", lambda _: certification.CERTIFIED_COLLECTOR_VERSION
    )
    exit_code = inventory.main(["--output-dir", str(output), "--approved-by", "a reviewer"])
    assert exit_code == 2
    assert "refusing to overwrite" in capsys.readouterr().err
    assert (output / inventory.REPORT_JSON_NAME).read_text(encoding="utf-8") == "previous run"


def test_a_file_where_the_output_directory_should_be_is_refused(tmp_path):
    path = tmp_path / "reports"
    path.write_text("not a directory", encoding="utf-8")
    with pytest.raises(inventory.OutputDirectoryRefused):
        inventory.prepare_output_directory(path)


def test_the_reports_are_written_with_lf_endings_on_every_platform(tmp_path):
    """A report that hashes differently per platform is a report nobody can compare."""
    output = tmp_path / "reports"
    inventory.prepare_output_directory(output)
    inventory.write_reports(document_for(_sample_output()), output)
    for name in inventory.OWNED_REPORT_NAMES:
        assert b"\r\n" not in (output / name).read_bytes(), name


# ---------------------------------------------------------------------------
# 12. Cleanup on every path
# ---------------------------------------------------------------------------


@requires_collector
def test_a_collector_that_never_becomes_ready_is_reaped_and_still_reported(tmp_path):
    result = inventory.run_inventory(
        binary=COLLECTOR,
        workdir=tmp_path / "run",
        output_dir=tmp_path / "out",
        agent=_payload_agent(),
        manifest=MANIFEST,
        startup_timeout=0.01,
    )
    assert result.canary.collector_returncode is not None, "the Collector was left running"
    assert any("readiness" in error for error in result.canary.errors)
    assert result.passed is False
    assert result.json_path.exists() and result.markdown_path.exists()
    assert result.candidate_path.exists()
    assert result.document["gate6"]["verdict"] == "FAIL"


@requires_collector
def test_an_agent_that_outlives_its_timeout_is_terminated_and_the_collector_reaped(tmp_path):
    agent = canary.subprocess_agent(
        [sys.executable, "-c", "import time; time.sleep(300)"], {}, timeout=1.0
    )
    started = time.monotonic()
    result = inventory.run_inventory(
        binary=COLLECTOR,
        workdir=tmp_path / "run",
        output_dir=tmp_path / "out",
        agent=agent,
        manifest=MANIFEST,
        settle_timeout=30.0,
    )
    assert time.monotonic() - started < 240
    assert result.canary.collector_returncode is not None
    assert result.canary.agent_returncode is not None
    assert result.passed is False


@requires_collector
def test_a_failure_building_the_report_still_leaves_nothing_running(tmp_path, monkeypatch):
    """Report generation happens after the Collector is reaped, so it cannot leak a process."""
    original = inventory.build_document

    def explode(report, manifest):
        if report.output:
            raise RuntimeError("boom")
        # The recovery path re-enters `build_document` with an emptied run, which must still work.
        return original(report, manifest)

    monkeypatch.setattr(inventory, "build_document", explode)

    result = inventory.run_inventory(
        binary=COLLECTOR,
        workdir=tmp_path / "run",
        output_dir=tmp_path / "out",
        agent=_payload_agent(),
        manifest=MANIFEST,
    )
    assert any("boom" in error for error in result.errors)
    assert result.canary.collector_returncode == 0
    assert result.passed is False
    assert result.json_path.exists() and result.markdown_path.exists()
    assert result.document["gate6"]["verdict"] == "FAIL"


def test_the_claude_agent_removes_its_temporary_working_directory(tmp_path, monkeypatch):
    """The child's cwd is temporary and isolated: it is not the repository and it does not survive.

    This launches `python`, not Claude. What it exercises is the wrapper -- the child environment
    builder, the isolated directory, and its removal on the way out.
    """
    created: list[str] = []
    real_mkdtemp = inventory.tempfile.mkdtemp

    def record(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(inventory.tempfile, "mkdtemp", record)
    agent = inventory.claude_agent([sys.executable, "-c", "pass"], timeout=60.0)
    returncode = agent({"grpc": 4317, "http": 4318, "internal": 8888}, tmp_path)

    assert returncode == 0
    assert created and not Path(created[0]).exists()
    assert Path(created[0]).parent != certification.REPO_ROOT


def test_the_claude_agent_builds_its_child_environment_through_the_certified_builder():
    source = Path(inventory.__file__).read_text(encoding="utf-8")
    assert "build_claude_child_environment" in source
    # There is exactly one child-environment builder, and this module does not add a second.
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in source
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in source


# ---------------------------------------------------------------------------
# 13. The rest of the suite is unaffected
# ---------------------------------------------------------------------------


def test_the_shipped_collector_configuration_still_passes_gate_3():
    assert certification.certify_configuration(certification.load_configuration()) == []


def test_the_inventory_does_not_modify_the_shipped_configuration():
    shipped = certification.load_configuration()
    before = copy.deepcopy(shipped)
    inventory.inventory_configuration()
    assert certification.load_configuration() == before


def test_no_production_module_imports_the_inventory_harness():
    """Normal Forge execution stays unwired: this is test-only apparatus."""
    importers = [
        path.name
        for path in sorted(SOURCE_ROOT.glob("*.py"))
        if "otel_inventory" in path.read_text(encoding="utf-8")
        or "claude-field-approvals" in path.read_text(encoding="utf-8")
    ]
    assert importers == []


def test_the_runner_still_carries_no_agent_telemetry_wiring():
    source = (SOURCE_ROOT / "runner.py").read_text(encoding="utf-8")
    for token in ("agent_telemetry", "otel_inventory", "OTEL_", "CLAUDE_CODE", "TRACEPARENT"):
        assert token not in source, token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _condition(document: dict, name: str) -> bool:
    for condition in document["gate6"]["conditions"]:
        if condition["condition"] == name:
            return condition["met"]
    raise AssertionError(f"{name} is not a Gate 6 condition")


def _replace_manifest(manifest: inventory.Manifest, **overrides) -> inventory.Manifest:
    values = {
        "path": manifest.path,
        "version": manifest.version,
        "subject": manifest.subject,
        "review": manifest.review,
        "vocabulary": manifest.vocabulary,
        "static_values": manifest.static_values,
        "enum_values": manifest.enum_values,
        "redacted_keys": manifest.redacted_keys,
        "fields": manifest.fields,
    }
    values.update(overrides)
    return inventory.Manifest(**values)


def _manifest_with_field(replacement: inventory.ManifestField) -> inventory.Manifest:
    fields = dict(MANIFEST.fields)
    fields[replacement.id] = replacement
    return _replace_manifest(MANIFEST, fields=fields)


def _approving_manifest() -> inventory.Manifest:
    """A manifest a human has signed, used only to prove the APPROVED path works.

    It is built in memory. `ops/otel/claude-field-approvals.yaml` approves nothing, and no test
    writes an approval back to it.
    """
    fields = {}
    for identifier, declared in MANIFEST.fields.items():
        if declared.status == inventory.STATUS_UNAPPROVED:
            fields[identifier] = inventory.ManifestField(
                id=declared.id,
                status=inventory.STATUS_APPROVED,
                expected=False,
                note=declared.note,
                constraints=declared.constraints,
            )
        else:
            fields[identifier] = declared
    return _replace_manifest(
        MANIFEST,
        review={"approved_by": "a test", "state": "approved", "reviewed_on": "2026-01-01"},
        fields=fields,
    )
