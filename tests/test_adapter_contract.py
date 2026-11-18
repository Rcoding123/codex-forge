import copy
import hashlib
import json
from pathlib import Path

import pytest

from codex_forge.adapter.contract import (
    FINAL_SCHEMA,
    RequestError,
    build_prompt,
    compact_handoff,
    normalize_usage,
    parse_final,
    parse_request,
    summarize_diagnostics,
)


def request_data(tmp_path: Path) -> dict:
    return {
        "contract_version": "adapter/1", "caller_id": "factory", "job_id": "J1", "attempt_id": "J1.a1",
        "role": "implementation", "workspace": {"path": str(tmp_path), "base_revision": "a" * 40,
                                                   "expected_state": "clean"},
        "task": {"objective": "Keep exactly: π\n  indented", "acceptance": ["a\n b"],
                 "constraints": [], "scope": ["source"], "out_of_scope": []},
        "policy": {"sandbox": "workspace-write", "allowed_paths": ["src/**"], "tool_network": "unspecified",
                   "search": False, "mcp": False, "plugins": False},
        "context": [], "model": "caller-model", "effort": "high", "deadline_s": 30,
        "artifact_root": str(tmp_path / "artifacts"),
    }


def final_data(**updates) -> dict:
    result = {"status": "done", "summary": "summary", "blockers": [], "decisions": [], "assumptions": [],
              "patterns": []}
    result.update(updates)
    return result


def add_context(data: dict, tmp_path: Path, content: str, **options) -> Path:
    path = tmp_path / "source.py"
    path.write_bytes(content.encode("utf-8"))
    data["context"].append({"path": "source.py", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "max_bytes": 5000, **options})
    return path


def test_parse_defaults_and_lossless_task(tmp_path):
    raw = request_data(tmp_path)
    original = copy.deepcopy(raw)
    parsed = parse_request(raw)
    assert raw == original
    assert parsed["task"] == original["task"]
    assert parsed["handoff_chars"] == 8000
    assert parsed["validation"] == {"authoritative": False, "commands": []}
    assert parsed["output_bytes"] == 8 * 1024 * 1024


@pytest.mark.parametrize("patch", [
    {"unexpected": 1}, {"contract_version": "adapter/0"}, {"role": "review"}, {"resume": "latest"},
    {"deadline_s": True}, {"deadline_s": float("nan")}, {"deadline_s": 1801}, {"output_bytes": -1},
    {"handoff_chars": 1.5}, {"effort": "unlimited"}, {"model": ""}, {"context": {}},
])
def test_invalid_top_level_rejected(tmp_path, patch):
    raw = request_data(tmp_path)
    raw.update(patch)
    with pytest.raises(RequestError):
        parse_request(raw)


@pytest.mark.parametrize("path", ["../secret", "C:/secret", "src/../secret", "/secret", "file:stream", "a//b"])
def test_context_traversal_rejected(tmp_path, path):
    raw = request_data(tmp_path)
    raw["context"] = [{"path": path, "sha256": "a" * 64, "max_bytes": 10}]
    with pytest.raises(RequestError):
        parse_request(raw)


def test_closed_nested_contracts_and_required_security_values(tmp_path):
    for field, member in (("workspace", "extra"), ("task", "extra"), ("policy", "extra")):
        raw = request_data(tmp_path)
        raw[field][member] = 1
        with pytest.raises(RequestError):
            parse_request(raw)
    for field in ("search", "mcp", "plugins"):
        raw = request_data(tmp_path)
        raw["policy"][field] = True
        with pytest.raises(RequestError, match="must be false"):
            parse_request(raw)


def test_context_range_and_complete_file_hash(tmp_path):
    raw = request_data(tmp_path)
    add_context(raw, tmp_path, "first\nπ😀\nlast\n", start_line=2, end_line=2)
    parsed = parse_request(raw)
    prompt, metadata = build_prompt(parsed, tmp_path)
    assert "π😀\n" in prompt
    assert "first\n" not in prompt
    assert metadata[0]["hash_scope"] == "complete_source_file"
    assert metadata[0]["included_bytes"] == len("π😀\n".encode())
    assert json.dumps(raw["task"], ensure_ascii=False, separators=(",", ":")) in prompt


def test_unicode_byte_caps_are_valid_utf8_with_explicit_omission(tmp_path):
    raw = request_data(tmp_path)
    add_context(raw, tmp_path, "😀😀😀", max_bytes=5)
    raw["context_bytes"] = 5
    prompt, metadata = build_prompt(parse_request(raw), tmp_path)
    assert "😀😀" not in prompt
    assert metadata[0]["included_bytes"] == 4
    assert metadata[0]["omitted_bytes"] == 8
    assert "omitted or clipped" in prompt
    assert "SOURCE source.py" in prompt


def test_stale_pin_checked_even_when_context_fully_omitted(tmp_path):
    raw = request_data(tmp_path)
    path = add_context(raw, tmp_path, "original")
    raw["context_bytes"] = 0
    parsed = parse_request(raw)
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(RequestError) as caught:
        build_prompt(parsed, tmp_path)
    assert caught.value.code == "stale_source"


def test_line_range_exceeding_file_rejected(tmp_path):
    raw = request_data(tmp_path)
    add_context(raw, tmp_path, "one\n", start_line=1, end_line=2)
    with pytest.raises(RequestError) as caught:
        build_prompt(parse_request(raw), tmp_path)
    assert caught.value.code == "stale_source"


def test_context_symlink_escape_rejected(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    raw = request_data(tmp_path)
    raw["context"] = [{"path": "link", "sha256": hashlib.sha256(b"secret").hexdigest(), "max_bytes": 10}]
    with pytest.raises(RequestError) as caught:
        build_prompt(parse_request(raw), tmp_path)
    assert caught.value.code == "unsafe_context"


def test_request_cap_never_clips_task(tmp_path):
    raw = request_data(tmp_path)
    raw["task"]["objective"] = "mandatory " * 1000
    raw["request_bytes"] = 500
    with pytest.raises(RequestError) as caught:
        build_prompt(parse_request(raw), tmp_path)
    assert caught.value.code == "request_too_large"


def test_aggregate_prompt_bytes_including_provenance_are_bounded(tmp_path):
    raw = request_data(tmp_path)
    add_context(raw, tmp_path, "😀" * 10000)
    raw["request_bytes"] = 1600
    prompt, _ = build_prompt(parse_request(raw), tmp_path)
    assert len(prompt.encode()) <= 1600


@pytest.mark.parametrize("raw", ["", "not JSON", "[]", '{"status":"done","status":"blocked"}',
                                 json.dumps(final_data(extra=True)), json.dumps(final_data(blockers="bad")),
                                 json.dumps(final_data(status="accepted"))])
def test_untrusted_final_has_one_strict_decode_boundary(raw):
    with pytest.raises(RequestError) as caught:
        parse_final(raw)
    assert caught.value.code == "invalid_final"


def test_parse_final_accepts_closed_result_and_schema_matches():
    result = final_data(status="blocked", blockers=["Needs a decision"])
    assert parse_final(json.dumps(result)) == result
    assert set(FINAL_SCHEMA["required"]) == set(result)
    assert FINAL_SCHEMA["additionalProperties"] is False


def test_aggregate_handoff_prioritizes_blockers_and_retains_slots():
    raw = final_data(summary="s" * 100, blockers=["b" * 5, "important"], decisions=["d" * 100],
                     assumptions=["a" * 100])
    compact = compact_handoff(raw, 6)
    assert compact["status"] == "done"
    assert compact["blockers"] == ["bbbbb", "i"]
    assert compact["decisions"] == [""]
    assert compact["summary"] == ""
    assert compact["clipping"]["returned_chars"] == 6
    assert compact["clipping"]["generation_cap"] is False
    assert raw["summary"] == "s" * 100


def test_handoff_slots_and_disabled_text_cap():
    compact = compact_handoff(final_data(blockers=[str(i) for i in range(20)]), 0)
    assert len(compact["blockers"]) == 8
    assert "13 entries omitted" in compact["blockers"][-1]
    assert compact["clipping"]["omitted_items"]["blockers"] == 13
    assert compact["summary"] == "summary"


def test_diagnostics_find_failure_in_middle_instead_of_tail():
    lines = [f"normal {index}" for index in range(200)]
    lines[50] = "fatal error: broken.cpp"
    summary = summarize_diagnostics("\n".join(lines))
    assert "normal 48" in summary and "fatal error: broken.cpp" in summary and "normal 52" in summary
    assert "normal 199" not in summary
    assert len(summary.splitlines()) <= 40
    assert summarize_diagnostics("ok\n") == "ok\n"


def test_single_enormous_diagnostic_line_is_bounded():
    summary = summarize_diagnostics("error: " + "x" * 1000000)
    assert len(summary) < 1000
    assert "line clipped" in summary


def usage_event(**usage):
    return {"type": "turn.completed", "usage": usage}


def test_usage_subset_arithmetic_and_missing_fields_not_zero():
    result = normalize_usage([usage_event(input_tokens=100, cached_input_tokens=60, output_tokens=20,
                                          reasoning_output_tokens=5)])
    assert result["derived"]["fresh_input_tokens"] == {"state": "known", "value": 40}
    assert result["derived"]["raw_total_tokens"]["value"] == 120
    assert result["derived"]["fresh_input_plus_output"]["value"] == 60
    assert result["counts"]["cache_write_input_tokens"] == {"state": "unknown", "value": None}
    assert result["billing_or_subscription_allowance"] == "not_determined"


@pytest.mark.parametrize("bad", [True, -1, "20", 20.5])
def test_invalid_usage_cannot_be_coerced_to_zero(bad):
    result = normalize_usage([usage_event(input_tokens=bad, output_tokens=20)])
    assert result["counts"]["input_tokens"]["state"] == "invalid"
    assert result["derived"]["raw_total_tokens"]["state"] == "invalid"


def test_inconsistent_usage_subsets_marked_invalid():
    result = normalize_usage([usage_event(input_tokens=5, cached_input_tokens=10, output_tokens=1,
                                          reasoning_output_tokens=2)])
    assert result["counts"]["cached_input_tokens"]["state"] == "invalid"
    assert result["counts"]["reasoning_output_tokens"]["state"] == "invalid"
    assert result["derived"]["fresh_input_tokens"]["state"] == "invalid"


def test_empty_and_multi_event_usage_aggregates_are_unknown():
    assert normalize_usage([])["counts"]["input_tokens"]["value"] is None
    events = [usage_event(input_tokens=10, output_tokens=2), usage_event(input_tokens=30, output_tokens=4)]
    result = normalize_usage(events)
    assert result["event_count"] == 2
    assert result["counts"]["input_tokens"]["state"] == "unknown"
    assert result["raw_events"] == [event["usage"] for event in events]


def test_adapter_contract_has_no_legacy_controller_dependencies():
    import ast
    import inspect
    import codex_forge.adapter.contract as module

    imports = [node for node in ast.walk(ast.parse(inspect.getsource(module)))
               if isinstance(node, (ast.Import, ast.ImportFrom))]
    names = [node.module or "" for node in imports if isinstance(node, ast.ImportFrom)]
    assert not any(any(part in name for part in ("orchestrator", ".db", "task_submission")) for name in names)


def _schema(name: str) -> dict:
    from importlib.resources import files

    return json.loads(files("codex_forge").joinpath("schemas", name).read_text(encoding="utf-8"))


def test_published_final_schema_is_exact_native_output_contract():
    from strict_schema import instance_violations, structural_strict_schema_violations

    published = _schema("adapter-final.schema.json")
    assert published == FINAL_SCHEMA
    assert structural_strict_schema_violations(published) == []
    assert instance_violations(final_data(), published) == []
    assert instance_violations(final_data(unexpected=True), published)


def test_published_request_shape_defaults_and_types_match_parser(tmp_path):
    import re
    from strict_schema import instance_violations

    schema = _schema("adapter-request.schema.json")
    raw = request_data(tmp_path)
    assert instance_violations(raw, schema) == []
    parsed = parse_request(raw)
    assert instance_violations(parsed, schema) == []
    for key in ("handoff_chars", "context_bytes", "request_bytes", "output_bytes", "required_capabilities", "validation"):
        assert schema["properties"][key]["default"] == parsed[key]
    objective_schema = schema["properties"]["task"]["properties"]["objective"]
    assert re.search(objective_schema["pattern"], raw["task"]["objective"])
    for invalid in ({**raw, "unexpected": True}, {**raw, "role": "review"}, {**raw, "deadline_s": True}):
        assert instance_violations(invalid, schema)
        with pytest.raises(RequestError):
            parse_request(invalid)


def test_result_schema_covers_preflight_rejection_without_claiming_acceptance():
    from strict_schema import instance_violations
    from codex_forge.adapter.engine import run_job

    schema = _schema("adapter-result.schema.json")
    result = run_job({"job_id": "example"})
    assert result["adapter_status"] == "rejected_preflight"
    assert result["factory_acceptance"] == "not_determined"
    assert instance_violations(result, schema) == []
    assert instance_violations({**result, "factory_acceptance": "accepted"}, schema)
