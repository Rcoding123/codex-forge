"""Gate 3: synthetic hostile-payload certification against a real installed Collector.

A static assertion about YAML cannot prove that redaction happens. These tests start the installed
`otelcol-contrib` on temporary loopback ports with the repository's own configuration, push OTLP
payloads carrying deliberately hostile values on every telemetry surface, and read what the debug
exporter printed *after* the privacy processor ran.

Nothing here calls Claude, Codex, a Forge task, or any service off the loopback interface, and
nothing here downloads a Collector. The binary is located through `OTELCOL_PATH` (or
`FORGE_OTELCOL_PATH`) only; without one, these tests skip and the structural certification in
tests/test_otel_collector.py still runs.

    $env:OTELCOL_PATH = 'C:\\Tools\\OpenTelemetry\\otelcol-contrib\\0.158.0\\otelcol-contrib.exe'
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_otel_redaction_certification.py
"""

from __future__ import annotations

import copy

import pytest

import otel_certification as certification

try:
    COLLECTOR = certification.collector_binary()
    UNAVAILABLE = ""
except certification.CollectorUnavailable as error:  # pragma: no cover - depends on the machine
    COLLECTOR = None
    UNAVAILABLE = str(error)

requires_collector = pytest.mark.skipif(COLLECTOR is None, reason=UNAVAILABLE or "no Collector")

ALL_SURFACES = certification.TRACE_SURFACES + certification.METRIC_SURFACES


@pytest.fixture(scope="module")
def certification_run(tmp_path_factory):
    """One Collector run over the shipped configuration, fed both hostile payloads."""
    return certification.run_collector(
        certification.load_configuration(),
        [
            ("/v1/traces", certification.hostile_trace_payload()),
            ("/v1/metrics", certification.hostile_metric_payload()),
        ],
        binary=COLLECTOR,
        workdir=tmp_path_factory.mktemp("certification"),
        expect_markers=("ResourceSpans #0", "ResourceMetrics #0"),
    )


@requires_collector
def test_the_installed_collector_is_the_version_this_slice_was_certified_against():
    """Contract section 10 makes a Collector upgrade a drift event, not a silent detail."""
    installed = certification.collector_version(COLLECTOR)
    assert installed == certification.CERTIFIED_COLLECTOR_VERSION, (
        f"certified against {certification.CERTIFIED_COLLECTOR_VERSION}, found {installed}. "
        "Re-run this certification and update CERTIFIED_COLLECTOR_VERSION and docs/TELEMETRY.md."
    )


@requires_collector
def test_the_repository_configuration_loads_in_the_real_collector():
    result = certification.validate_configuration(certification.CONFIG_PATH, COLLECTOR)
    assert result.returncode == 0, result.stderr or result.stdout


@requires_collector
def test_both_hostile_payloads_were_accepted_and_the_collector_exited_cleanly(certification_run):
    assert certification_run.status_for("/v1/traces") == 200
    assert certification_run.status_for("/v1/metrics") == 200
    # A crash mid-run would make an absence-of-sentinels assertion meaningless.
    assert certification_run.returncode == 0
    assert "ResourceSpans #0" in certification_run.output
    assert "ResourceMetrics #0" in certification_run.output


@requires_collector
@pytest.mark.parametrize("surface", ALL_SURFACES)
def test_no_hostile_value_survives_any_surface(certification_run, surface):
    """Span, span-event, metric data-point, resource, and scope attributes, one surface per case."""
    survivors = [
        value for value in certification.sentinel_values((surface,)) if value in certification_run.output
    ]
    assert survivors == [], f"{len(survivors)} hostile value(s) survived the {surface} surface"


@requires_collector
@pytest.mark.parametrize("key", certification.FORBIDDEN_ATTRIBUTE_KEYS)
def test_no_forbidden_attribute_key_survives(certification_run, key):
    # The debug exporter prints one attribute per line as `-> <key>: <value>`, so this matches the
    # key exactly: `-> error:` cannot match the permitted `-> error.type:`.
    assert f"-> {key}:" not in certification_run.output


@requires_collector
@pytest.mark.parametrize("key", sorted(certification.UNKNOWN_HOSTILE_KEYS))
def test_an_unknown_attribute_is_dropped_rather_than_passed_through(certification_run, key):
    """Contract section 10: a field nobody has reviewed is denied, not exported because it is new."""
    assert f"-> {key}:" not in certification_run.output


@requires_collector
def test_the_correlation_identifier_survives(certification_run):
    """Without `forge.invocation.id` there is no join between a phase and agent metrics."""
    assert certification_run.output.count(f"-> forge.invocation.id: Str({certification.INVOCATION_ID})") >= 4


@requires_collector
@pytest.mark.parametrize("key", certification.REQUIRED_ALLOWED_ATTRIBUTE_KEYS)
def test_approved_bounded_attributes_survive(certification_run, key):
    """Every approved key, not a sample: the token, phase, duration, and outcome vocabulary."""
    assert f"-> {key}:" in certification_run.output


@requires_collector
def test_the_span_status_and_metric_values_survive_redaction(certification_run):
    """Redaction must not cost the operational signal the slice exists to carry."""
    assert "Status code    : Error" in certification_run.output
    assert "Value: 1200" in certification_run.output
    assert "Count: 1" in certification_run.output


@requires_collector
def test_unredactable_carriers_are_refused_rather_than_exported(certification_run):
    """Span links and metric exemplars cannot be reached by any 0.158.0 redaction processor.

    Nothing can strip their attribute maps, so the records carrying them are dropped whole. The
    sibling span and data point that carry no link or exemplar are untouched, which is what
    separates this from simply discarding the signal.
    """
    assert certification.LINKED_SPAN_NAME not in certification_run.output
    assert "Link #0" not in certification_run.output
    assert f"Value: {certification.EXEMPLAR_DATAPOINT_VALUE}" not in certification_run.output
    assert "Exemplar" not in certification_run.output
    # The siblings survived, so the filter is selective rather than a blanket drop.
    assert "Name           : forge.phase" in certification_run.output
    assert "Value: 1200" in certification_run.output


@requires_collector
def test_three_signals_cannot_become_a_logs_pipeline(tmp_path):
    """No logs pipeline means the receiver never registers /v1/logs at all.

    This is the second, fail-closed barrier of contract section 5: the hostile log body is refused
    at the door rather than accepted and redacted.
    """
    run = certification.run_collector(
        certification.load_configuration(),
        [("/v1/logs", certification.hostile_log_payload())],
        binary=COLLECTOR,
        workdir=tmp_path / "logs",
        settle_seconds=6.0,
    )
    assert run.status_for("/v1/logs") == 404
    assert run.returncode == 0
    assert "ResourceLog" not in run.output
    for value in certification.sentinel_values(("log-resource", "log-record")):
        assert value not in run.output
    assert certification.HOSTILE_SENTINELS["prompt"] not in run.output


@requires_collector
def test_removing_the_privacy_processor_leaks_the_hostile_payload(tmp_path):
    """The negative control: without this, the assertions above could be passing vacuously.

    Same binary, same payloads, same harness -- only the privacy processor removed. Every sentinel
    reaches the exporter, which is exactly what the shipped configuration prevents.
    """
    stripped = (certification.PRIVACY_PROCESSOR, certification.UNREDACTABLE_FILTER)
    unprotected = copy.deepcopy(certification.load_configuration())
    for name in stripped:
        del unprotected["processors"][name]
    for pipeline in unprotected["service"]["pipelines"].values():
        pipeline["processors"] = [name for name in pipeline["processors"] if name not in stripped]

    run = certification.run_collector(
        unprotected,
        [
            ("/v1/traces", certification.hostile_trace_payload()),
            ("/v1/metrics", certification.hostile_metric_payload()),
        ],
        binary=COLLECTOR,
        workdir=tmp_path / "unprotected",
        expect_markers=("ResourceSpans #0", "ResourceMetrics #0"),
    )

    leaked = [value for value in certification.sentinel_values(ALL_SURFACES) if value in run.output]
    assert leaked, "the harness cannot detect a leak, so its clean results prove nothing"
    # Every surface leaks, so the harness reads all of them.
    for surface in ALL_SURFACES:
        assert any(value in run.output for value in certification.sentinel_values((surface,))), surface
    assert certification.certify_configuration(unprotected) != []


@requires_collector
def test_a_pipeline_that_references_a_missing_privacy_processor_refuses_to_start(tmp_path):
    """Contract section 9 fail-closed: it must not start, and must not degrade to passing data."""
    broken = copy.deepcopy(certification.load_configuration())
    del broken["processors"][certification.PRIVACY_PROCESSOR]

    rebound, _ = certification.temporary_ports(broken)
    config_path = certification.write_configuration(rebound, tmp_path / "broken.yaml")

    result = certification.validate_configuration(config_path, COLLECTOR)

    assert result.returncode != 0
    assert certification.PRIVACY_PROCESSOR in (result.stderr + result.stdout)


def test_the_certification_harness_reports_clearly_when_no_collector_is_installed():
    """Structural coverage stays active on a machine with no Collector; the skip says why."""
    if COLLECTOR is None:  # pragma: no cover - depends on the machine
        assert "OTELCOL_PATH" in UNAVAILABLE
    else:
        assert COLLECTOR.is_file()
