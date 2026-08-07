"""The post-redaction Codex surface the successful Gate 7 run exported, distilled to a table.

Gate 8 inventories fields, and a field is a *name* -- an attribute key, a scope, a span, an event, a
metric. Which trace carried it, when, and what it measured are not fields, so none of them are here.
What is here is every distinct name the `debug` exporter printed at the end of the certified
`traces` and `metrics` pipelines on 2026-08-07, in the run named by :data:`RUN`, plus the bounded
descriptors attached to those names: a span's kind and status code, a metric's data type,
temporality, monotonicity, unit, and description, and an attribute's OTLP value type.

**This is an observation, never an approval.** `ops/otel/codex-field-approvals.yaml` holds the
approvals, and the two are compared by `tests/test_otel_codex_field_inventory.py`. Keeping the
observation in a table rather than a copy of the Collector log is what makes it reviewable: the
109 span names below are readable in one sitting, and no trace identifier, span
identifier, timestamp, measurement, or invocation identifier from that run is reproduced anywhere in
this file. The three identifier-shaped values :func:`render_export` needs are built out of `"0"` at
import time, so the repository holds no hexadecimal run that could be mistaken for a real one.

Seven event names *are* reproduced verbatim, and they are the reason Gate 8 blocks them: Codex
0.146.0 names a span event after the Rust source location that emitted it, so the name carries a
relative source path, a space, and a colon. An event name is not an attribute, so `keep_keys` in
`transform/privacy` cannot reach it. They are here because a fixture that omitted them would be
inventorying a surface the run did not have; they are BLOCKED in the manifest, and the inventory
reports them by SHA-256 fingerprint, so no report and no manifest ever prints one.
"""

from __future__ import annotations

# The supervised run this table was read off, and the two versions it pins.
RUN = "codex-final-20260807-121944"
RUN_DATE = "2026-08-07"
AGENT = "codex-cli"
AGENT_VERSION = "0.146.0"
COLLECTOR_VERSION = "0.158.0"
CONTRACT_VERSION = "1.0"

# Built rather than written: a literal here would be a hexadecimal run in the repository, and the
# point of the identifier policy is that those are never worth having lying around.
PLACEHOLDER_TRACE_ID = "0" * 32
PLACEHOLDER_SPAN_ID = "0" * 16
PLACEHOLDER_INVOCATION_ID = "0" * 32

# One `service.name` and one `service.version`, identical on both signals. Everything Codex put on
# its resource beyond these two was removed by `keep_keys` before the exporter saw it.
RESOURCE_ATTRIBUTES = (
    ("service.name", "Str", "codex_exec"),
    ("service.version", "Str", AGENT_VERSION),
)

# Instrumentation scopes, by signal. The third is the Collector's own count connector, which is
# Forge's diagnostic rather than Codex's telemetry.
TRACE_SCOPE = "codex_exec"
METRIC_SCOPE = "codex"
DIAGNOSTIC_SCOPE = (
    "github.com/open-telemetry/opentelemetry-collector-contrib/connector/countconnector"
)

# On every span, and nothing else: Forge's two correlation attributes, arriving through the
# `-c otel.span_attributes` override and surviving `keep_keys` because the allowlist names them.
SPAN_ATTRIBUTES = (
    ("forge.invocation.id", "Str", PLACEHOLDER_INVOCATION_ID),
    ("forge.telemetry.contract.version", "Str", CONTRACT_VERSION),
)

# (span name, attribute key, OTLP type, value) for attributes that are not on every span. Codex
# 0.146.0 puts the model's own token counts on the one span that talks to the API.
EXTRA_SPAN_ATTRIBUTES = (
    ('handle_responses', 'gen_ai.usage.input_tokens', 'Int', '1'),
    ('handle_responses', 'gen_ai.usage.output_tokens', 'Int', '1'),
)

# Every distinct span name, with the kind the exporter printed. Every span in the run carried
# status code `Unset`.
SPANS = (
    ('add_collaboration_tools', 'Internal'),
    ('add_core_utility_tools', 'Internal'),
    ('add_dynamic_tools', 'Internal'),
    ('add_extension_tools', 'Internal'),
    ('add_mcp_resource_tools', 'Internal'),
    ('add_shell_tools', 'Internal'),
    ('add_tool_sources', 'Internal'),
    ('agents_md.refresh', 'Internal'),
    ('app_server.serialized_request_queue', 'Internal'),
    ('app_server.thread_start.attach_listener', 'Internal'),
    ('app_server.thread_start.config_snapshot', 'Internal'),
    ('app_server.thread_start.create_thread', 'Internal'),
    ('app_server.thread_start.notify_started', 'Internal'),
    ('app_server.thread_start.resolve_status', 'Internal'),
    ('app_server.thread_start.send_response', 'Internal'),
    ('app_server.thread_start.upsert_thread', 'Internal'),
    ('append_tool_search_executor', 'Internal'),
    ('auth', 'Internal'),
    ('build_extension_turn_input_items', 'Internal'),
    ('build_mcp_tool_runtimes', 'Internal'),
    ('build_model_visible_specs_and_registry', 'Internal'),
    ('build_prompt', 'Internal'),
    ('build_skill_injections', 'Internal'),
    ('build_skill_outcome', 'Internal'),
    ('build_skills_and_plugins', 'Internal'),
    ('build_tool_call', 'Internal'),
    ('build_tool_router', 'Internal'),
    ('build_tool_specs_and_registry', 'Internal'),
    ('built_tools', 'Internal'),
    ('built_tools.load_discoverable_tools', 'Internal'),
    ('built_tools.load_plugins', 'Internal'),
    ('capability_roots.resolve', 'Internal'),
    ('capability_roots.snapshot_for_step', 'Internal'),
    ('codex.exec', 'Internal'),
    ('collect_explicit_skill_mentions', 'Internal'),
    ('drain_in_flight', 'Internal'),
    ('endpoint_session.execute_with', 'Internal'),
    ('environments.snapshot', 'Internal'),
    ('extension_tool_executors', 'Internal'),
    ('from_tools', 'Internal'),
    ('get_default_model', 'Internal'),
    ('get_model_info', 'Internal'),
    ('handle_output_item_done', 'Internal'),
    ('handle_responses', 'Internal'),
    ('hosted_model_tool_specs', 'Internal'),
    ('initialize', 'Server'),
    ('list_models', 'Internal'),
    ('list_skills', 'Internal'),
    ('load', 'Internal'),
    ('load_with_cli_overrides', 'Internal'),
    ('mcp.runtime.refresh', 'Internal'),
    ('mcp.runtime.resolve_for_step', 'Internal'),
    ('mcp.runtime_config.project_for_step', 'Internal'),
    ('merge_connector_selection', 'Internal'),
    ('merge_into_namespaces', 'Internal'),
    ('model_client.stream_responses_websocket', 'Internal'),
    ('model_client.websocket_connection', 'Internal'),
    ('op.dispatch.shutdown', 'Internal'),
    ('op.dispatch.user_input', 'Internal'),
    ('persist_rollout_items', 'Internal'),
    ('plugins_for_config', 'Internal'),
    ('receiving', 'Internal'),
    ('receiving_stream', 'Internal'),
    ('record_context_updates_and_set_reference_context_item', 'Internal'),
    ('record_conversation_items', 'Internal'),
    ('regular_task.prepare_run_turn', 'Internal'),
    ('render_available_skills', 'Internal'),
    ('responses_websocket.connect', 'Internal'),
    ('responses_websocket.stream_request', 'Internal'),
    ('run_hooks_and_record_inputs', 'Internal'),
    ('run_legacy_after_agent_hook', 'Internal'),
    ('run_pending_session_start_hooks', 'Internal'),
    ('run_pre_sampling_compact', 'Internal'),
    ('run_sampling_request', 'Internal'),
    ('run_session_end_hooks', 'Internal'),
    ('run_turn', 'Internal'),
    ('run_turn.collect_post_sampling_state', 'Internal'),
    ('run_turn.prepare_sampling_request_input', 'Internal'),
    ('run_turn_stop_hooks', 'Internal'),
    ('send_raw_response_items', 'Internal'),
    ('session.flush_rollout', 'Internal'),
    ('session_init', 'Internal'),
    ('session_init.auth_mcp', 'Internal'),
    ('session_init.mcp_manager_init', 'Internal'),
    ('session_init.plugin_skill_warmup', 'Internal'),
    ('session_init.required_mcp_wait', 'Internal'),
    ('session_init.state_db', 'Internal'),
    ('session_init.thread_name_lookup', 'Internal'),
    ('session_init.thread_persistence', 'Internal'),
    ('session_loop', 'Internal'),
    ('session_task.run', 'Internal'),
    ('session_task.turn', 'Internal'),
    ('set_previous_turn_settings', 'Internal'),
    ('shell_snapshot', 'Internal'),
    ('skills.executor.catalog_snapshot', 'Internal'),
    ('skills_for_config', 'Internal'),
    ('startup_prewarm', 'Internal'),
    ('startup_prewarm.resolve', 'Internal'),
    ('step_context.capture', 'Internal'),
    ('stream_request', 'Internal'),
    ('thread/start', 'Server'),
    ('thread/unsubscribe', 'Server'),
    ('thread_spawn', 'Internal'),
    ('track_turn_resolved_config_analytics', 'Internal'),
    ('try_run_sampling_request', 'Internal'),
    ('turn/start', 'Server'),
    ('turn_context.build', 'Internal'),
    ('turn_diff_display_roots', 'Internal'),
    ('world_state.build', 'Internal'),
)
SPAN_STATUS_CODE = "Unset"

# (span name, event name). Nine pairs over seven distinct event names.
SPAN_EVENTS = (
    ('endpoint_session.execute_with', 'event model-provider\\src\\models_endpoint.rs:228'),
    ('model_client.websocket_connection', 'event otel\\src\\events\\session_telemetry.rs:636'),
    ('op.dispatch.user_input', 'event otel\\src\\events\\session_telemetry.rs:980'),
    ('responses_websocket.stream_request', 'event otel\\src\\events\\session_telemetry.rs:687'),
    ('session_init', 'event otel\\src\\events\\session_telemetry.rs:485'),
    ('startup_prewarm', 'event otel\\src\\events\\session_telemetry.rs:236'),
    ('startup_prewarm.resolve', 'event otel\\src\\events\\session_telemetry.rs:236'),
    ('thread/start', 'event otel\\src\\events\\session_telemetry.rs:236'),
    ('try_run_sampling_request', 'event otel\\src\\events\\session_telemetry.rs:252'),
)

# (name, data type, aggregation temporality, monotonicity or None, unit, description).
AGENT_METRICS = (
    ('codex.conversation.turn.count', 'Sum', 'Delta', 'true', '', ''),
    ('codex.feature.state', 'Sum', 'Delta', 'true', '', ''),
    ('codex.process.start', 'Sum', 'Delta', 'true', '', ''),
    ('codex.remote_models.fetch_update.duration_ms', 'Histogram', 'Delta', None, 'ms', 'Duration in milliseconds.'),
    ('codex.remote_models.load_cache.duration_ms', 'Histogram', 'Delta', None, 'ms', 'Duration in milliseconds.'),
    ('codex.responses_api_engine_iapi_tbt.duration_ms', 'Histogram', 'Delta', None, 'ms', 'Duration in milliseconds.'),
    ('codex.responses_api_engine_iapi_ttft.duration_ms', 'Histogram', 'Delta', None, 'ms', 'Duration in milliseconds.'),
    ('codex.responses_api_engine_service_tbt.duration_ms', 'Histogram', 'Delta', None, 'ms', 'Duration in milliseconds.'),
    ('codex.responses_api_engine_service_ttft.duration_ms', 'Histogram', 'Delta', None, 'ms', 'Duration in milliseconds.'),
    ('codex.responses_api_inference_time.duration_ms', 'Histogram', 'Delta', None, 'ms', 'Duration in milliseconds.'),
    ('codex.responses_api_overhead.duration_ms', 'Histogram', 'Delta', None, 'ms', 'Duration in milliseconds.'),
    ('codex.shell_snapshot', 'Sum', 'Delta', 'true', '', ''),
    ('codex.shell_snapshot.duration_ms', 'Histogram', 'Delta', None, 'ms', 'Duration in milliseconds.'),
    ('codex.sqlite.init.count', 'Sum', 'Delta', 'true', '', ''),
    ('codex.sqlite.init.duration_ms', 'Histogram', 'Delta', None, 'ms', 'Duration in milliseconds.'),
    ('codex.startup.phase.duration_ms', 'Histogram', 'Delta', None, 'ms', 'Duration in milliseconds.'),
    ('codex.startup_prewarm.age_at_first_turn_ms', 'Histogram', 'Delta', None, 'ms', 'Duration in milliseconds.'),
    ('codex.startup_prewarm.duration_ms', 'Histogram', 'Delta', None, 'ms', 'Duration in milliseconds.'),
    ('codex.thread.skills.description_truncated_chars', 'Histogram', 'Delta', None, '', ''),
    ('codex.thread.skills.enabled_total', 'Histogram', 'Delta', None, '', ''),
    ('codex.thread.skills.kept_total', 'Histogram', 'Delta', None, '', ''),
    ('codex.thread.skills.truncated', 'Histogram', 'Delta', None, '', ''),
    ('codex.thread.started', 'Sum', 'Delta', 'true', '', ''),
    ('codex.turn.e2e_duration_ms', 'Histogram', 'Delta', None, 'ms', 'Duration in milliseconds.'),
    ('codex.turn.memory', 'Sum', 'Delta', 'true', '', ''),
    ('codex.turn.network_proxy', 'Sum', 'Delta', 'true', '', ''),
    ('codex.turn.token_usage', 'Histogram', 'Delta', None, '', ''),
    ('codex.turn.tool.call', 'Histogram', 'Delta', None, '', ''),
    ('codex.turn.ttfm.duration_ms', 'Histogram', 'Delta', None, 'ms', 'Duration in milliseconds.'),
    ('codex.turn.ttft.duration_ms', 'Histogram', 'Delta', None, 'ms', 'Duration in milliseconds.'),
    ('codex.websocket.event', 'Sum', 'Delta', 'true', '', ''),
    ('codex.websocket.event.duration_ms', 'Histogram', 'Delta', None, 'ms', 'Duration in milliseconds.'),
    ('codex.websocket.request', 'Sum', 'Delta', 'true', '', ''),
    ('codex.websocket.request.duration_ms', 'Histogram', 'Delta', None, 'ms', 'Duration in milliseconds.'),
)

# Forge's own span-count connectors, which run inside the Collector rather than inside Codex.
DIAGNOSTIC_METRICS = (
    ('forge.diagnostic.span.accepted.other', 'Sum', 'Delta', 'true', '', 'spans named outside the diagnostic vocabulary, counted accepted'),
    ('forge.diagnostic.span.exported.other', 'Sum', 'Delta', 'true', '', 'spans named outside the diagnostic vocabulary, counted exported'),
)

# What the Gate 7 report recorded for this run. Gate 8 reuses these numbers rather than re-deriving
# them, because re-deriving them would mean another Codex request.
RECORDED = {
    "spans_accepted": 224,
    "spans_exported": 224,
    "spans_filtered": 0,
    "spans_refused": 0,
    "spans_send_failed": 0,
    "metric_points_accepted": 96,
    "metric_points_internally_generated": 2,
    "metric_points_exported": 98,
    "log_records_accepted": 0,
    "collector_returncode": 0,
    "codex_returncode": 0,
}

_STAMP = "2026-08-07T12:19:49.990-0500"
# The structured trailer the Collector appends after a dump, reduced to the one property the parser
# reads from it: that the dump has ended.
_TRAILER = "\t{}"


def _attribute(key: str, otlp_type: str, value: str) -> str:
    return f"     -> {key}: {otlp_type}({value})"


def _metric_block(index: int, metric: tuple) -> list[str]:
    name, data_type, temporality, monotonic, unit, description = metric
    lines = [
        f"Metric #{index}",
        "Descriptor:",
        f"     -> Name: {name}",
        f"     -> Description: {description}",
        f"     -> Unit: {unit}",
        f"     -> DataType: {data_type}",
    ]
    if monotonic is not None:
        lines.append(f"     -> IsMonotonic: {monotonic}")
    lines.append(f"     -> AggregationTemporality: {temporality}")
    if data_type == "Histogram":
        lines += [
            "HistogramDataPoints #0",
            "StartTimestamp: 0",
            "Timestamp: 0",
            "Count: 1",
            "Sum: 1.000000",
        ]
    else:
        lines += ["NumberDataPoints #0", "StartTimestamp: 0", "Timestamp: 0", "Value: 1"]
    return lines


def _span_block(index: int, name: str, kind: str) -> list[str]:
    lines = [
        f"Span #{index}",
        f"    Trace ID       : {PLACEHOLDER_TRACE_ID}",
        f"    Parent ID      : {PLACEHOLDER_SPAN_ID}",
        f"    ID             : {PLACEHOLDER_SPAN_ID}",
        f"    Name           : {name}",
        f"    Kind           : {kind}",
        "    Start time     : 0",
        "    End time       : 0",
        f"    Status code    : {SPAN_STATUS_CODE}",
        "    Status message :",
        "Attributes:",
    ]
    lines += [_attribute(*attribute) for attribute in SPAN_ATTRIBUTES]
    lines += [
        _attribute(key, otlp_type, value)
        for span, key, otlp_type, value in EXTRA_SPAN_ATTRIBUTES
        if span == name
    ]
    events = [event for span, event in SPAN_EVENTS if span == name]
    if events:
        lines.append("Events:")
        for position, event in enumerate(events):
            lines += [
                f"SpanEvent #{position}",
                f"     -> Name: {event}",
                "     -> Timestamp: 0",
                "     -> DroppedAttributesCount: 0",
            ]
    return lines


def render_export() -> str:
    """Reproduce the `debug` exporter's detailed output for the table above.

    The grammar is the 0.158.0 exporter's, so `otel_inventory.parse_export` reads this the way it
    read the run: three dumps -- Codex's metrics, Forge's diagnostic metrics, Codex's spans -- each
    introduced by a Collector log line and closed by the structured trailer.
    """
    lines: list[str] = []

    lines.append(f"{_STAMP}\tinfo\tResourceMetrics #0")
    lines.append("Resource SchemaURL:")
    lines.append("Resource attributes:")
    lines += [_attribute(*attribute) for attribute in RESOURCE_ATTRIBUTES]
    lines.append("ScopeMetrics #0")
    lines.append("ScopeMetrics SchemaURL:")
    lines.append(f"InstrumentationScope {METRIC_SCOPE}")
    for index, metric in enumerate(AGENT_METRICS):
        lines += _metric_block(index, metric)
    lines.append(_TRAILER)

    lines.append(f"{_STAMP}\tinfo\tResourceMetrics #0")
    lines.append("Resource SchemaURL:")
    lines.append("ScopeMetrics #0")
    lines.append("ScopeMetrics SchemaURL:")
    lines.append(f"InstrumentationScope {DIAGNOSTIC_SCOPE}")
    for index, metric in enumerate(DIAGNOSTIC_METRICS):
        lines += _metric_block(index, metric)
    lines.append(_TRAILER)

    lines.append(f"{_STAMP}\tinfo\tResourceSpans #0")
    lines.append("Resource SchemaURL:")
    lines.append("Resource attributes:")
    lines += [_attribute(*attribute) for attribute in RESOURCE_ATTRIBUTES]
    lines.append("ScopeSpans #0")
    lines.append("ScopeSpans SchemaURL:")
    lines.append(f"InstrumentationScope {TRACE_SCOPE}")
    for index, (name, kind) in enumerate(SPANS):
        lines += _span_block(index, name, kind)
    lines.append(_TRAILER)

    return "\n".join(lines) + "\n"
