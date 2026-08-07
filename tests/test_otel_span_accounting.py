"""Certification for the bounded span-name diagnostic and the canary span accounting.

Gate 3 proved that what the Collector exports carries no identity or content. It did not answer a
question a canary raises immediately: when the receiver accepts more spans than the exporter sends,
*which* spans went missing? The honest answer requires naming them, and naming them by printing them
is what the privacy policy forbids -- the spans in question are the ones carrying a link, the one
carrier no 0.158.0 processor can redact.

These tests certify the answer this repository gives instead: a `count` connector that converts
spans to bare counters keyed by an allowlisted span-name vocabulary, so a run can say "one
`claude_code.llm_request` did not survive" without any part of that span reaching an exporter.

Nothing here calls Claude, Codex, a Forge task, or any service off the loopback interface, and
nothing here downloads a Collector. The agent in every test below posts synthetic OTLP.

    $env:OTELCOL_PATH = 'C:\\Tools\\OpenTelemetry\\otelcol-contrib\\0.158.0\\otelcol-contrib.exe'
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_otel_span_accounting.py
"""

from __future__ import annotations

import copy
import sys
import time

import pytest

import otel_canary as canary
import otel_certification as certification

try:
    COLLECTOR = certification.collector_binary()
    UNAVAILABLE = ""
except certification.CollectorUnavailable as error:  # pragma: no cover - depends on the machine
    COLLECTOR = None
    UNAVAILABLE = str(error)

requires_collector = pytest.mark.skipif(COLLECTOR is None, reason=UNAVAILABLE or "no Collector")

ACCEPTED_EXPECTED, EXPORTED_EXPECTED = certification.diagnostic_expected_counts()


def _payload_agent():
    return canary.otlp_payload_agent(
        [
            ("/v1/traces", certification.diagnostic_trace_payload()),
            ("/v1/metrics", certification.hostile_metric_payload()),
        ]
    )


@pytest.fixture(scope="module")
def canary_run(tmp_path_factory):
    """One canary over the shipped configuration plus the diagnostic, fed hostile spans."""
    return canary.run_canary(
        binary=COLLECTOR,
        workdir=tmp_path_factory.mktemp("canary"),
        agent=_payload_agent(),
    )


# ---------------------------------------------------------------------------
# The diagnostic configuration, before anything runs
# ---------------------------------------------------------------------------


def test_the_diagnostic_is_built_from_the_shipped_configuration_without_touching_it():
    shipped = certification.load_configuration()
    before = copy.deepcopy(shipped)
    certification.diagnostic_configuration(shipped)
    assert shipped == before
    # And the shipped file itself still passes the Gate 3 certification unchanged.
    assert certification.certify_configuration(certification.load_configuration()) == []


def test_the_diagnostic_configuration_satisfies_its_own_privacy_invariants():
    diagnostic = certification.diagnostic_configuration(certification.load_configuration())
    assert certification.certify_diagnostic_configuration(diagnostic) == []


def test_the_diagnostic_deviates_from_the_certified_pipeline_in_exactly_the_known_ways():
    """The diagnostic bypasses `filter/unredactable` on one pipeline. That must stay the only news.

    `traces/diagnostic_accepted` deliberately omits the filter, because a pipeline that drops the
    spans is a pipeline that cannot count them. The bypass is safe only because that pipeline ends
    in a counter. Enumerating every deviation here means a future edit that relaxes the *measured*
    pipeline, or points the bypass at an exporter that can print a span, fails this test.
    """
    diagnostic = certification.diagnostic_configuration(certification.load_configuration())
    deviations = set(certification.certify_configuration(diagnostic))
    assert deviations == set(certification.DIAGNOSTIC_EXPECTED_DEVIATIONS)


def test_no_count_connector_declares_span_attributes():
    """The `attributes` block is the only way a span attribute could become a metric dimension."""
    diagnostic = certification.diagnostic_configuration(certification.load_configuration())
    for name, connector in diagnostic["connectors"].items():
        for surface, metrics in connector.items():
            for metric, definition in metrics.items():
                assert "attributes" not in definition, f"{name}/{surface}/{metric}"


def test_every_declared_diagnostic_metric_name_is_in_the_vocabulary():
    """Each connector declares one counter per bucket for its own stage, and nothing else."""
    diagnostic = certification.diagnostic_configuration(certification.load_configuration())
    for stage, name in certification.DIAGNOSTIC_CONNECTORS.items():
        expected = {
            certification.diagnostic_metric_name(stage, bucket)
            for bucket in certification.DIAGNOSTIC_BUCKETS
        }
        assert set(diagnostic["connectors"][name]["spans"]) == expected
    declared = {
        metric
        for connector in diagnostic["connectors"].values()
        for metric in connector["spans"]
    }
    assert declared == set(certification.diagnostic_metric_names())


def test_the_vocabulary_covers_the_agreed_span_names_and_nothing_else():
    assert certification.DIAGNOSTIC_BUCKETS == (
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
        "other",
    )


def test_an_unknown_span_name_maps_to_other_rather_than_being_published():
    diagnostic = certification.diagnostic_configuration(certification.load_configuration())
    spans = diagnostic["connectors"][certification.DIAGNOSTIC_CONNECTORS["accepted"]]["spans"]
    other = spans[certification.diagnostic_metric_name("accepted", "other")]
    condition = other["conditions"][0]
    for name in certification.DIAGNOSTIC_SPAN_NAMES:
        assert f'span.name != "{name}"' in condition


def test_the_diagnostic_adds_no_logs_pipeline_and_no_external_exporter():
    diagnostic = certification.diagnostic_configuration(certification.load_configuration())
    assert "logs" not in diagnostic["service"]["pipelines"]
    assert set(diagnostic["exporters"]) == {"debug"}
    assert set(diagnostic["connectors"]) == set(certification.DIAGNOSTIC_CONNECTORS.values())


def test_the_certified_processor_chain_is_unchanged_on_both_measured_pipelines():
    diagnostic = certification.diagnostic_configuration(certification.load_configuration())
    for name in ("traces", "metrics"):
        assert diagnostic["service"]["pipelines"][name]["processors"] == [
            "memory_limiter",
            "filter/unredactable",
            "transform/privacy",
            "batch",
        ]


def _accepted_spans(config: dict) -> dict:
    return config["connectors"][certification.DIAGNOSTIC_CONNECTORS["accepted"]]["spans"]


def _carry_span_attributes(config: dict) -> None:
    """The one configuration change that would put a span attribute on a counter."""
    name = certification.diagnostic_metric_name("accepted", "forge.task")
    _accepted_spans(config)[name]["attributes"] = [{"key": "user.id"}]


def _add_metric_outside_the_vocabulary(config: dict) -> None:
    _accepted_spans(config)["claude.raw.prompt.count"] = {"conditions": ['span.name != ""']}


def _drop_the_scrub(config: dict) -> None:
    del config["processors"][certification.DIAGNOSTIC_SCRUB_PROCESSOR]


def _empty_the_vocabulary_filter(config: dict) -> None:
    config["processors"][certification.DIAGNOSTIC_VOCABULARY_FILTER]["metrics"] = {"metric": []}


def _weaken_the_measured_pipeline(config: dict) -> None:
    config["service"]["pipelines"]["traces"]["processors"].remove(certification.UNREDACTABLE_FILTER)


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (_carry_span_attributes, "declares span attributes"),
        (_add_metric_outside_the_vocabulary, "outside the vocabulary"),
        (_drop_the_scrub, "is not declared"),
        (_empty_the_vocabulary_filter, "does not admit"),
        (_weaken_the_measured_pipeline, "certified processor chain was modified"),
    ],
)
def test_the_diagnostic_certification_fails_closed(mutate, expected):
    """Each mutation is a way the diagnostic could become a leak. Each must be reported."""
    diagnostic = certification.diagnostic_configuration(certification.load_configuration())
    mutate(diagnostic)
    problems = certification.certify_diagnostic_configuration(diagnostic)
    assert any(expected in problem for problem in problems), problems


# ---------------------------------------------------------------------------
# Against a real Collector
# ---------------------------------------------------------------------------


@requires_collector
def test_the_diagnostic_configuration_loads_in_the_real_collector(tmp_path):
    diagnostic = certification.diagnostic_configuration(certification.load_configuration())
    rebound, _ = certification.temporary_ports(diagnostic)
    path = certification.write_configuration(rebound, tmp_path / "diagnostic.yaml")
    result = certification.validate_configuration(path, COLLECTOR)
    assert result.returncode == 0, result.stderr or result.stdout


@requires_collector
def test_the_run_accounts_for_every_accepted_span(canary_run):
    """The arithmetic the canary exists to produce, against a payload shaped like the reported one."""
    accounting = canary_run.accounting
    assert accounting is not None
    assert accounting.accepted == len(certification.DIAGNOSTIC_PAYLOAD_SPANS)
    assert accounting.exported == sum(EXPORTED_EXPECTED.values())
    assert accounting.filtered == accounting.accepted - accounting.exported
    assert accounting.refused == 0
    assert accounting.send_failed == 0
    assert accounting.unresolved == 0
    assert accounting.settled is True
    # The connectors and the Collector's own obsreport counted the same spans independently.
    assert accounting.diagnostic_disagreements == ()
    assert canary_run.passed is True


@requires_collector
def test_the_diagnostic_names_which_spans_did_not_survive(canary_run):
    """The answer the totals cannot give: the missing spans, by name, with nothing else about them."""
    accounting = canary_run.accounting
    assert dict(accounting.accepted_by_name) == ACCEPTED_EXPECTED
    assert dict(accounting.exported_by_name) == EXPORTED_EXPECTED
    assert accounting.missing_by_name() == {"claude_code.llm_request": 1, "claude_code.tool": 1}


@requires_collector
def test_the_diagnostic_publishes_only_bounded_names_and_counts(canary_run):
    """Nothing outside the vocabulary is emitted, including the connector's own default metric."""
    output = canary_run.output
    assert certification.unknown_diagnostic_metric_names(output) == []
    # The `count` connector emits `trace.span.event.count` unless displaced; it must not appear.
    assert "trace.span.event.count" not in output
    assert f"{certification.DIAGNOSTIC_METRIC_PREFIX}.spanevent.suppressed" not in output
    printed = {
        line.split("-> Name:")[1].strip()
        for line in output.splitlines()
        if "-> Name:" in line and certification.DIAGNOSTIC_METRIC_PREFIX in line
    }
    assert printed <= set(certification.diagnostic_metric_names())
    assert printed, "the diagnostic printed nothing, so this proves nothing"


@requires_collector
@pytest.mark.parametrize("surface", certification.TRACE_SURFACES + certification.METRIC_SURFACES)
def test_no_hostile_value_reaches_the_diagnostic_output(canary_run, surface):
    """Span, span-event, span-link, resource, and scope sentinels, one surface per case."""
    output = canary_run.output
    survivors = [v for v in certification.sentinel_values((surface,)) if v in output]
    assert survivors == [], f"{len(survivors)} hostile value(s) survived the {surface} surface"


@requires_collector
def test_a_diagnostic_record_carries_no_attribute_of_any_kind(canary_run):
    """Not merely no *hostile* attribute: the counters carry no resource, scope, or point attribute.

    A `count` connector propagates the resource of the spans it counted, so this is a property of
    `transform/diagnostic_scrub` rather than of the connector. The negative control below removes
    that processor and shows the attributes reappear.
    """
    records = [r for r in canary._records(canary_run.output) if canary.COUNT_CONNECTOR_SCOPE in r]
    assert records, "no diagnostic record was printed, so this proves nothing"
    for record in records:
        assert "Resource attributes:" not in record
        assert "Scope attributes:" not in record
        for line in record.splitlines():
            stripped = line.strip()
            if stripped.startswith("->"):
                assert stripped[2:].split(":", 1)[0].strip() in canary.DESCRIPTOR_FIELDS, stripped


@requires_collector
def test_a_diagnostic_record_carries_no_identifier(canary_run):
    for record in canary._records(canary_run.output):
        if canary.COUNT_CONNECTOR_SCOPE not in record:
            continue
        for forbidden in ("Trace ID", "Span ID", "Parent ID", "Link #", "Exemplar"):
            assert forbidden not in record


@requires_collector
def test_linked_spans_are_still_blocked_from_the_normal_export_path(canary_run):
    """The diagnostic must not become a way for an unredactable carrier to reach an exporter."""
    output = canary_run.output
    assert "Link #" not in output
    # `claude_code.tool` appears once in the payload and carries a link, so it must not be exported.
    assert "Name           : claude_code.tool" not in output
    # `claude_code.llm_request` appears twice, one with a link: exactly one may survive.
    assert output.count("Name           : claude_code.llm_request") == 1
    assert "Name           : forge.phase" in output


@requires_collector
def test_exemplars_are_still_blocked(canary_run):
    output = canary_run.output
    assert "Exemplar" not in output
    assert f"Value: {certification.EXEMPLAR_DATAPOINT_VALUE}" not in output
    # The sibling data point without an exemplar survived, so this is selective, not a blanket drop.
    assert "Value: 1200" in output


@requires_collector
def test_the_counters_the_accounting_needs_exist_at_the_shipped_telemetry_level(tmp_path):
    """The accounting must not require the shipped internal-telemetry configuration to be widened.

    `metrics.level: basic` is what `ops/otel/collector-config.yaml` sets. The filter counter in
    particular is the one the first canary never read, and it was available at that level all along.
    The send-failure counter is absent here because nothing failed; an absent counter reads as zero.
    """
    seen: list[str] = []

    def agent(ports, workdir):
        result = _payload_agent()(ports, workdir)
        # After a settle, so the exporter counter has a series to find.
        canary.wait_until_settled(ports["internal"], batch_timeout=5.0, timeout=60.0)
        seen.extend(name for name, _labels, _value in certification.scrape_internal_metrics(ports["internal"]))
        return result

    canary.run_canary(binary=COLLECTOR, workdir=tmp_path / "counters", agent=agent)

    assert certification.load_configuration()["service"]["telemetry"]["metrics"]["level"] == "basic"
    for counter in (
        certification.COUNTER_ACCEPTED,
        certification.COUNTER_REFUSED,
        certification.COUNTER_SENT,
        certification.COUNTER_FILTERED,
    ):
        assert counter in seen, counter


@requires_collector
def test_the_privacy_check_passes_and_names_every_check_it_ran(canary_run):
    assert canary_run.privacy.passed
    assert canary_run.privacy.findings == ()
    assert len(canary_run.privacy.checks) == 5


# ---------------------------------------------------------------------------
# Metric accounting: the exporter is shared, so the receiver is not the only source
# ---------------------------------------------------------------------------
#
# The first Gate 7 run reported `exporter sent 98` against `receiver accepted 96` and called the
# difference an unresolved remainder of -2. Nothing was lost and nothing appeared from nowhere: the
# diagnostic `count` connectors feed `metrics/diagnostic` into the same `debug` exporter the OTLP
# `metrics` pipeline uses, so the exporter counter covers a population the receiver counter cannot
# see. These tests pin both halves of that -- the measurement of the connector-made points, and the
# equation that consumes it.

# One `count` connector emission as the debug exporter prints it: a bare delta sum, one data point,
# no attribute of any kind. `Value: 224` is one metric point that says 224, not 224 metric points.
_DIAGNOSTIC_BLOCK = """\
Metric #0
Descriptor:
     -> Name: {name}
     -> Description: spans named outside the diagnostic vocabulary, counted {stage}
     -> Unit:
     -> DataType: Sum
     -> IsMonotonic: true
     -> AggregationTemporality: Delta
NumberDataPoints #0
StartTimestamp: 2026-08-07 17:19:45.1644117 +0000 UTC
Timestamp: 2026-08-07 17:19:48.4971326 +0000 UTC
Value: {value}
"""


def _diagnostic_output(*blocks: tuple[str, str, int]) -> str:
    return "\n".join(
        _DIAGNOSTIC_BLOCK.format(
            name=certification.diagnostic_metric_name(stage, bucket), stage=stage, value=value
        )
        for stage, bucket, value in blocks
    )


def test_a_diagnostic_block_is_one_metric_point_however_large_its_value():
    """The count and the counter are different numbers, and the accounting needs the count."""
    output = _diagnostic_output(("accepted", "other", 224), ("exported", "other", 224))

    assert certification.count_diagnostic_metric_points(output) == 2
    # The value-total parser still reads the same blocks as 224 spans per stage. Both are correct;
    # they answer different questions, and conflating them is the bug this file exists to pin.
    counts = certification.parse_diagnostic_counts(output)
    assert counts == {"accepted": {"other": 224}, "exported": {"other": 224}}


def test_every_diagnostic_block_a_run_flushed_is_counted():
    """Several flushes mean several blocks per bucket, and every one reached the exporter."""
    output = _diagnostic_output(
        ("accepted", "forge.phase", 3),
        ("accepted", "other", 1),
        ("exported", "forge.phase", 3),
        ("accepted", "forge.phase", 5),
    )
    assert certification.count_diagnostic_metric_points(output) == 4


def test_a_metric_that_is_not_the_diagnostic_is_not_credited_to_the_connectors():
    """Only the bounded vocabulary counts. An agent metric point came in through the receiver."""
    output = """\
Metric #0
Descriptor:
     -> Name: forge.certification.token.usage
     -> DataType: Sum
NumberDataPoints #0
Value: 1200
"""
    assert certification.count_diagnostic_metric_points(output) == 0


def test_output_with_no_diagnostic_at_all_generates_nothing():
    """A topology without the connectors must collapse to the receiver-only equation."""
    assert certification.count_diagnostic_metric_points("") == 0
    accounting = canary.MetricAccounting(accepted=40, exported=40, refused=0, send_failed=0)
    assert accounting.internally_generated == 0
    assert accounting.available == 40
    assert accounting.unresolved == 0
    assert accounting.balanced


def test_the_recorded_gate_7_run_balances_once_the_connectors_are_credited():
    """The real 2026-08-07 Codex run, re-evaluated. 96 + 2 - 98 = 0.

    These are the numbers that run actually reported: 96 metric points accepted at the OTLP
    receiver, 98 sent by the debug exporter, nothing refused and nothing send-failed, and two
    diagnostic data points in its `collector.log` — one `forge.diagnostic.span.accepted.other`
    and one `forge.diagnostic.span.exported.other`, each a single delta point carrying 224.
    """
    accounting = canary.MetricAccounting(
        accepted=96, exported=98, refused=0, send_failed=0
    ).with_internally_generated(
        certification.count_diagnostic_metric_points(
            _diagnostic_output(("accepted", "other", 224), ("exported", "other", 224))
        )
    )

    assert accounting.internally_generated == 2
    assert accounting.available == 98
    assert accounting.unresolved == 0
    assert accounting.balanced


def test_crediting_the_connectors_does_not_explain_away_a_real_loss():
    """The whole point of a remainder is that it still fires. Two points explain two points."""
    accounting = canary.MetricAccounting(
        accepted=96, exported=90, refused=0, send_failed=0
    ).with_internally_generated(2)

    assert accounting.available == 98
    assert accounting.unresolved == 8
    assert accounting.balanced is False


def test_a_surplus_the_connectors_do_not_cover_is_still_a_negative_remainder():
    """Not a clamp. If the exporter sent more than both sources produced, the report says so."""
    accounting = canary.MetricAccounting(
        accepted=96, exported=101, refused=0, send_failed=0
    ).with_internally_generated(2)

    assert accounting.unresolved == -3
    assert accounting.balanced is False


def test_refused_and_send_failed_still_settle_the_equation():
    accounting = canary.MetricAccounting(
        accepted=96, exported=90, refused=5, send_failed=3
    ).with_internally_generated(2)

    assert accounting.available == 98
    assert accounting.unresolved == 0
    assert accounting.balanced


def test_with_internally_generated_does_not_mutate_the_reading_it_came_from():
    """The counters are read once, while the Collector answers. That reading stays as read."""
    read = canary.MetricAccounting(accepted=96, exported=98, refused=0, send_failed=0)
    joined = read.with_internally_generated(2)

    assert read.internally_generated == 0
    assert read.unresolved == -2, "the uncorrected reading is preserved, not rewritten in place"
    assert joined is not read
    assert (joined.accepted, joined.exported) == (read.accepted, read.exported)


def _metric_datapoints(payload: dict) -> tuple[int, int]:
    """(data points in this payload, how many of them carry an exemplar).

    Derived from the payload rather than written out, for the same reason
    `diagnostic_expected_counts` is: the two must not drift apart.
    """
    total = exemplars = 0
    for resource in payload["resourceMetrics"]:
        for scope in resource["scopeMetrics"]:
            for metric in scope["metrics"]:
                for body in metric.values():
                    if not isinstance(body, dict):
                        continue
                    for point in body.get("dataPoints", ()):
                        total += 1
                        exemplars += bool(point.get("exemplars"))
    return total, exemplars


def _without_exemplars(payload: dict) -> dict:
    """The same hostile payload with the one unredactable carrier removed.

    Everything else stays hostile. Only the exemplar goes, because `filter/unredactable` drops the
    data point carrying it and this variant is for measuring a pipeline where nothing is dropped.
    """
    clean = copy.deepcopy(payload)
    for resource in clean["resourceMetrics"]:
        for scope in resource["scopeMetrics"]:
            for metric in scope["metrics"]:
                for body in metric.values():
                    if isinstance(body, dict) and "dataPoints" in body:
                        body["dataPoints"] = [
                            point for point in body["dataPoints"] if not point.get("exemplars")
                        ]
    return clean


def _probed_metric_accounting(workdir, metric_payload) -> tuple[canary.MetricAccounting, int]:
    """One live canary posting spans and ``metric_payload``. Returns the corrected accounting.

    The counters are read through the probe, while the Collector still answers; the connector
    points are counted off the complete log afterwards. That is the same two-moment join the Codex
    canary does, exercised here without Claude, Codex, or a Forge task.
    """
    probed: dict[str, canary.MetricAccounting] = {}
    report = canary.run_canary(
        binary=COLLECTOR,
        workdir=workdir,
        agent=canary.otlp_payload_agent(
            [
                ("/v1/traces", certification.diagnostic_trace_payload()),
                ("/v1/metrics", metric_payload),
            ]
        ),
        probe=lambda ports: probed.setdefault(
            "metrics", canary.read_metric_accounting(int(ports["internal"]))
        ),
    )
    assert report.collector_returncode == 0
    generated = certification.count_diagnostic_metric_points(report.output)
    return probed["metrics"].with_internally_generated(generated), generated


@requires_collector
def test_the_exporter_total_covers_both_metric_pipelines_against_a_real_collector(tmp_path):
    """The topology claim itself, proven by a Collector rather than argued from the config.

    Synthetic OTLP only — no Claude, no Codex, no Forge task. The receiver sees the posted metric
    points; the exporter sees those *and* the points the connectors made out of the posted spans.
    With nothing deliberately dropped, the difference between the two counters is exactly the
    diagnostic data points printed in the log, and the corrected equation balances to zero.
    """
    payload = _without_exemplars(certification.hostile_metric_payload())
    posted, exemplars = _metric_datapoints(payload)
    assert exemplars == 0, "this variant exists to have nothing the filter would drop"

    accounting, generated = _probed_metric_accounting(tmp_path / "metric-balance", payload)

    # The connectors really did manufacture points, so this run exercises the corrected term
    # rather than passing because the term happened to be zero.
    assert generated > 0
    assert accounting.accepted == posted
    assert accounting.exported == posted + generated, (
        "the exporter total must cover both metric pipelines: "
        f"accepted={accounting.accepted} generated={generated} exported={accounting.exported}"
    )
    assert accounting.available == accounting.exported
    assert accounting.unresolved == 0
    assert accounting.balanced


@requires_collector
def test_a_deliberately_dropped_metric_point_is_still_an_unresolved_remainder(tmp_path):
    """Crediting the connectors must not turn into crediting everything.

    The hostile payload carries one data point with an exemplar, which `filter/unredactable` drops
    whole. That drop has no counter this harness credits — `otelcol_processor_filter_spans_filtered`
    is span-oriented — so it is left as a remainder rather than explained away. The remainder is
    exactly one point: the connectors' contribution covered the connectors' points and nothing else.
    """
    payload = certification.hostile_metric_payload()
    posted, exemplars = _metric_datapoints(payload)
    assert exemplars == 1, "the payload is supposed to carry exactly one unredactable data point"

    accounting, generated = _probed_metric_accounting(tmp_path / "metric-drop", payload)

    assert generated > 0
    assert accounting.accepted == posted
    assert accounting.exported == posted - exemplars + generated
    assert accounting.unresolved == exemplars
    assert accounting.balanced is False
    # And the point that did not make it is the one carrying the exemplar, not a random casualty.
    assert f"Value: {certification.EXEMPLAR_DATAPOINT_VALUE}" not in _read_log(tmp_path / "metric-drop")


def _read_log(workdir) -> str:
    return (workdir / "collector.log").read_text(encoding="utf-8", errors="replace")


@requires_collector
def test_the_connectors_are_invisible_to_the_receiver_counter(tmp_path):
    """Why the old equation could not have worked: a connector is not a receiver.

    If the connector-made points were counted as accepted, the receiver total would already
    include them and the original `accepted == exported` reading would have balanced. It does not,
    and this pins the reason so a Collector upgrade that changes it fails here rather than silently
    double-counting the diagnostic.
    """
    seen: list[tuple[str, dict, float]] = []

    def agent(ports, workdir):
        result = _payload_agent()(ports, workdir)
        canary.wait_until_settled(ports["internal"], batch_timeout=5.0, timeout=60.0)
        seen.extend(certification.scrape_internal_metrics(ports["internal"]))
        return result

    canary.run_canary(binary=COLLECTOR, workdir=tmp_path / "receiver-labels", agent=agent)

    receivers = {
        labels.get("receiver")
        for name, labels, _value in seen
        if name == canary.COUNTER_ACCEPTED_POINTS
    }
    assert receivers, f"{canary.COUNTER_ACCEPTED_POINTS} was never reported"
    assert receivers == {"otlp"}, f"a non-OTLP source appeared in the receiver counter: {receivers}"


# ---------------------------------------------------------------------------
# Negative controls: the layers are load-bearing
# ---------------------------------------------------------------------------


@requires_collector
def test_without_the_scrub_the_counters_carry_resource_attributes(tmp_path):
    """Without this, the clean result above could be a property of the connector, not the design."""
    diagnostic = certification.diagnostic_configuration(certification.load_configuration())
    diagnostic["service"]["pipelines"][certification.DIAGNOSTIC_METRICS_PIPELINE]["processors"].remove(
        certification.DIAGNOSTIC_SCRUB_PROCESSOR
    )
    report = canary.run_canary(
        binary=COLLECTOR, workdir=tmp_path / "unscrubbed", agent=_payload_agent(), config=diagnostic
    )
    records = [r for r in canary._records(report.output) if canary.COUNT_CONNECTOR_SCOPE in r]
    assert records
    assert any("Resource attributes:" in record for record in records)
    # And the privacy check catches it rather than reporting a clean run.
    assert not report.privacy.passed
    assert any("attribute inside a diagnostic record" in f for f in report.privacy.findings)
    assert report.passed is False


@requires_collector
def test_without_the_vocabulary_filter_an_unbounded_metric_name_reaches_the_output(tmp_path):
    """The connector's own default metric is the concrete case; the filter is the general answer."""
    diagnostic = certification.diagnostic_configuration(certification.load_configuration())
    diagnostic["service"]["pipelines"][certification.DIAGNOSTIC_METRICS_PIPELINE]["processors"].remove(
        certification.DIAGNOSTIC_VOCABULARY_FILTER
    )
    for connector in diagnostic["connectors"].values():
        del connector["spanevents"]
    report = canary.run_canary(
        binary=COLLECTOR, workdir=tmp_path / "unfiltered", agent=_payload_agent(), config=diagnostic
    )
    assert "trace.span.event.count" in report.output


# ---------------------------------------------------------------------------
# Settling, and the timing hypothesis it exists to rule out
# ---------------------------------------------------------------------------


@requires_collector
def test_an_early_read_reports_a_loss_that_the_settled_read_does_not(tmp_path):
    """The reason the harness waits, demonstrated rather than asserted.

    `otelcol_exporter_sent_spans` has no series at all until the first batch flushes, so a read
    taken when the agent exits shows every accepted span as missing. That produces the same shape as
    a deliberate filter drop, which is why a canary that reads once cannot tell the two apart.
    """
    samples: list[tuple[int, ...]] = []
    post = _payload_agent()

    def agent(ports, workdir):
        result = post(ports, workdir)
        for _ in range(3):
            samples.append(canary._totals(ports["internal"]))
            time.sleep(0.3)
        return result

    report = canary.run_canary(binary=COLLECTOR, workdir=tmp_path / "timing", agent=agent)

    accepted, exported, filtered, _refused, _failed = samples[0]
    assert accepted == len(certification.DIAGNOSTIC_PAYLOAD_SPANS)
    assert exported == 0, "the batch flushed sooner than the configuration allows"
    # Read this early, two spans look deliberately dropped and four look lost. Both are wrong.
    assert filtered == 2
    assert accepted - exported - filtered == 4

    assert report.accounting.settled is True
    assert report.accounting.exported == sum(EXPORTED_EXPECTED.values())
    assert report.accounting.unresolved == 0
    assert report.passed is True


def test_the_settle_wait_gives_up_rather_than_reporting_a_number_it_does_not_have():
    """A port nothing answers on must not settle at zero and be reported as a clean run."""
    unreachable = _closed_port()
    assert (
        canary.wait_until_settled(unreachable, batch_timeout=0.1, poll_seconds=0.05, timeout=1.0)
        is False
    )


def test_the_batch_interval_is_read_from_the_configuration_being_run():
    assert canary.batch_timeout_seconds(certification.load_configuration()) == 5.0
    assert canary.batch_timeout_seconds({"processors": {"batch": {"timeout": "250ms"}}}) == 0.25
    assert canary.batch_timeout_seconds({"processors": {"batch": {"timeout": "2m"}}}) == 120.0
    assert canary.batch_timeout_seconds({}) == 0.2


# ---------------------------------------------------------------------------
# The report itself
# ---------------------------------------------------------------------------


def _accounting(**overrides) -> canary.SpanAccounting:
    values = {
        "accepted": 6,
        "exported": 4,
        "filtered": 2,
        "refused": 0,
        "send_failed": 0,
        # Consistent with the totals above by construction: a breakdown that does not sum to its
        # own counter is itself a failure, covered by its own test below.
        "accepted_by_name": {"claude_code.llm_request": 2, "forge.phase": 3, "other": 1},
        "exported_by_name": {"claude_code.llm_request": 1, "forge.phase": 3},
    }
    values.update(overrides)
    return canary.SpanAccounting(**values)


def _report(**overrides) -> canary.CanaryReport:
    values = {
        "accounting": _accounting(),
        "privacy": canary.PrivacyCheck(checks=("output was captured",), findings=()),
        "collector_returncode": 0,
        "agent_returncode": 0,
    }
    values.update(overrides)
    return canary.CanaryReport(**values)


def test_the_report_states_all_four_required_numbers():
    rendered = _report().render()
    assert "receiver accepted      6" in rendered
    assert "exporter sent          4" in rendered
    assert "deliberately filtered  2" in rendered
    assert "unresolved remainder   0" in rendered
    assert "VERDICT PASS" in rendered


def test_a_diagnostic_that_disagrees_with_the_collector_counters_fails_the_canary():
    """Two independent measurements of the same numbers. If they differ, neither can be trusted."""
    report = _report(accounting=_accounting(exported_by_name={"claude_code.llm_request": 1}))
    assert report.accounting.balanced is True
    assert report.accounting.diagnostic_disagreements == (
        "exported: counter says 4, diagnostic says 1",
    )
    assert report.passed is False
    assert "DISAGREEMENT           exported: counter says 4, diagnostic says 1" in report.render()


def test_a_nonzero_unresolved_remainder_fails_the_canary():
    report = _report(accounting=_accounting(filtered=1))
    assert report.accounting.unresolved == 1
    assert report.passed is False
    assert "unresolved remainder   1" in report.render()
    assert "VERDICT FAIL" in report.render()


def test_counts_that_never_settled_fail_the_canary_even_when_they_balance():
    report = _report(accounting=_accounting(settled=False))
    assert report.accounting.balanced is True
    assert report.passed is False
    assert "counts settled         NO" in report.render()


def test_a_privacy_finding_fails_the_canary_even_when_the_arithmetic_balances():
    report = _report(privacy=canary.PrivacyCheck(checks=("c",), findings=("leaked user.id",)))
    assert report.accounting.balanced is True
    assert report.passed is False
    assert "FAIL leaked user.id" in report.render()


def test_a_collector_that_did_not_exit_cleanly_fails_the_canary():
    assert _report(collector_returncode=1).passed is False


def test_the_privacy_section_is_rendered_even_when_there_is_no_accounting():
    """The run that crashed is the one whose output nobody cleared. It still gets a privacy check."""
    report = _report(
        accounting=None,
        privacy=canary.PrivacyCheck(checks=("output was captured",), findings=("no output",)),
        errors=("agent raised RuntimeError: boom",),
    )
    rendered = report.render()
    assert "PRIVACY CHECK" in rendered
    assert "ran  output was captured" in rendered
    assert "FAIL no output" in rendered
    assert "unavailable - the run did not reach a countable state" in rendered
    assert "ERROR agent raised RuntimeError: boom" in rendered
    assert report.passed is False


def test_the_privacy_check_refuses_to_pass_on_an_empty_output():
    check = canary.check_privacy("")
    assert check.passed is False
    assert check.checks, "a check that ran nothing must still say what it would have run"


@pytest.mark.parametrize("key", certification.FORBIDDEN_ATTRIBUTE_KEYS)
def test_the_privacy_check_detects_every_forbidden_key(key):
    check = canary.check_privacy(f"2026-01-01T00:00:00Z info ResourceSpans #0\n     -> {key}: Str(x)\n")
    assert not check.passed
    assert any(key in finding for finding in check.findings)


def test_the_privacy_check_detects_an_attribute_smuggled_into_a_diagnostic_record():
    record = (
        "2026-01-01T00:00:00Z info ResourceMetrics #0\n"
        "Resource attributes:\n"
        "     -> forge.invocation.id: Str(deadbeef)\n"
        f"InstrumentationScope {canary.COUNT_CONNECTOR_SCOPE}\n"
        "Metric #0\n"
        "     -> Name: forge.diagnostic.span.accepted.forge_task\n"
    )
    check = canary.check_privacy(record)
    assert not check.passed
    assert any("forge.invocation.id" in finding for finding in check.findings)


def test_the_privacy_check_detects_a_metric_name_outside_the_vocabulary():
    record = (
        "2026-01-01T00:00:00Z info ResourceMetrics #0\n"
        f"InstrumentationScope {canary.COUNT_CONNECTOR_SCOPE}\n"
        "     -> Name: forge.diagnostic.span.accepted.claude_secret_prompt\n"
    )
    check = canary.check_privacy(record)
    assert not check.passed
    assert any("outside the vocabulary" in finding for finding in check.findings)


@pytest.mark.parametrize("marker", ["Link #0", "Exemplars"])
def test_the_privacy_check_detects_an_unredactable_carrier(marker):
    check = canary.check_privacy(f"2026-01-01T00:00:00Z info ResourceSpans #0\n{marker}\n")
    assert not check.passed


# ---------------------------------------------------------------------------
# Cleanup on every path
# ---------------------------------------------------------------------------


@requires_collector
def test_an_agent_that_raises_still_produces_a_report_and_reaps_the_collector(tmp_path):
    def agent(ports, workdir):
        raise RuntimeError("boom")

    report = canary.run_canary(binary=COLLECTOR, workdir=tmp_path / "raising", agent=agent)

    assert report.collector_returncode is not None, "the Collector was left running"
    assert any("boom" in error for error in report.errors)
    assert "PRIVACY CHECK" in report.render()
    assert report.passed is False


@requires_collector
def test_a_collector_that_never_becomes_ready_is_still_reaped(tmp_path):
    """A startup timeout must not leave a Collector holding a loopback port."""
    report = canary.run_canary(
        binary=COLLECTOR,
        workdir=tmp_path / "slow",
        agent=_payload_agent(),
        startup_timeout=0.01,
    )
    assert report.collector_returncode is not None
    assert any("readiness" in error for error in report.errors)
    assert "PRIVACY CHECK" in report.render()
    assert report.passed is False


def test_a_child_process_that_outlives_its_timeout_is_terminated(tmp_path):
    """The agent is a child of this harness; it must never survive the run that started it."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    agent = canary.subprocess_agent(
        [sys.executable, "-c", "import time; time.sleep(300)"], {}, timeout=1.0
    )
    started = time.monotonic()
    returncode = agent({"http": 0, "grpc": 0, "internal": 0}, tmp_path)
    assert time.monotonic() - started < 60
    assert returncode is not None


def test_the_command_line_refuses_to_launch_an_agent_without_a_named_approval(capsys):
    """A canary is a supervised event. The harness will not start one on its own."""
    exit_code = canary.main(["--workdir", ".", "--agent-command", "claude -p CANARY"])
    assert exit_code == 2
    assert "Refusing to launch an agent" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _closed_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return port
