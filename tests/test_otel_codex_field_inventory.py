"""Certification for the Gate 8 Codex field inventory.

Four questions, and they are not the same question:

* **What did the certified pipeline export for Codex?** Answered by `tests/otel_codex_evidence.py`,
  the distilled surface of the successful Gate 7 run, and by Gate 6's parser reading it.
* **What has a human approved?** Answered by `ops/otel/codex-field-approvals.yaml`.
* **Do the two agree, field for field?** Answered by the verdict: no UNKNOWN, no UNAPPROVED, no
  unresolved remainder, and every refused field accounted for.
* **Can the approval be widened by accident?** Answered by the fail-closed tests at the end, which
  break the manifest in one place at a time and require the verdict to notice.

Nothing here calls Codex, calls Claude, starts a Collector, opens a socket, or reads a file outside
this repository. Gate 8 is a re-reading of evidence Gate 7 already produced.

    $env:PYTHONIOENCODING = 'utf-8'
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_otel_codex_field_inventory.py
"""

from __future__ import annotations

import ast
import copy
import re
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

import otel_certification as certification
import otel_codex_evidence as evidence
import otel_codex_inventory as gate8
import otel_inventory as inventory

MANIFEST = gate8.load_codex_manifest()
DOCUMENT = gate8.build_codex_document()
MANIFEST_TEXT = gate8.MANIFEST_PATH.read_text(encoding="utf-8")

# The counts the 2026-08-07 run produced. Written down so a change to the fixture that quietly adds
# or drops a field is a failing test rather than a wider approval.
OBSERVED_FIELDS = 381
APPROVED_FIELDS = 372
REFUSED_FIELDS = 9
SPAN_NAMES = 109
METRIC_NAMES = 36
EVENT_NAMES = 7

# Every check `assess_identifier` may be waived by putting a name in the manifest vocabulary. A name
# that fails anything else is carrying a path, an address, a credential, or a control character, and
# vocabulary membership must never readmit one.
WAIVABLE_IDENTIFIER_CHECKS = frozenset(
    {
        "identifier carries no content-bearing word",
        "identifier matches the conservative grammar",
    }
)
# What a vocabulary entry must look like whatever the policy said: ASCII words, dots, hyphens, and
# the slash an instrumentation scope and a Codex route both use.
SAFE_VOCABULARY_SHAPE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_.-]+)*")


def _field(document, identifier):
    for item in document["fields"]:
        if item["id"] == identifier:
            return item
    raise AssertionError(f"no field {identifier!r} in the document")


def _condition(document, name):
    for condition in document[gate8.GATE]["conditions"]:
        if condition["condition"] == name:
            return condition["met"]
    raise AssertionError(f"no condition {name!r} in the verdict")


def _manifest_from(raw: dict, tmp_path: Path) -> inventory.Manifest:
    path = tmp_path / "codex-field-approvals.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return gate8.load_codex_manifest(path)


def _raw_manifest() -> dict:
    return copy.deepcopy(yaml.safe_load(MANIFEST_TEXT))


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


def test_gate_8_passes_on_the_recorded_gate_7_evidence():
    assert DOCUMENT[gate8.GATE]["verdict"] == "PASS"
    assert [c["condition"] for c in DOCUMENT[gate8.GATE]["conditions"] if not c["met"]] == []


def test_every_observed_field_is_decided():
    totals = DOCUMENT["totals"]
    assert totals["observed"] == OBSERVED_FIELDS
    assert totals["approved"] == APPROVED_FIELDS
    assert totals["blocked"] == REFUSED_FIELDS
    assert totals["unknown"] == 0
    assert totals["unapproved"] == 0
    assert totals["redacted"] == 0
    assert totals["expected_not_observed"] == 0
    assert totals["unresolved_remainder"] == 0
    assert totals["approved"] + totals["blocked"] == totals["observed"]


def test_the_document_is_pinned_to_the_two_certified_versions():
    subject = DOCUMENT["subject"]
    assert subject["agent"] == "codex-cli"
    assert subject["agent_version"] == "0.146.0"
    assert subject["collector_version"] == certification.CERTIFIED_COLLECTOR_VERSION == "0.158.0"
    assert subject["contract_version"] == "1.0"
    assert DOCUMENT["schema"] == "forge.codex.field-inventory/1"


def test_the_run_facts_come_from_the_gate_7_report_and_no_new_request():
    span_accounting = DOCUMENT["span_accounting"]
    assert span_accounting["accepted"] == evidence.RECORDED["spans_accepted"] == 224
    assert span_accounting["exported"] == evidence.RECORDED["spans_exported"] == 224
    assert span_accounting["unresolved"] == 0
    assert span_accounting["balanced"] is True
    assert DOCUMENT["run"]["canary_passed"] is True
    assert DOCUMENT["run"]["collector_returncode"] == 0
    assert DOCUMENT["run"]["agent_returncode"] == 0
    assert MANIFEST.review["evidence"] == evidence.RUN


# ---------------------------------------------------------------------------
# The observed surface
# ---------------------------------------------------------------------------


def test_the_fixture_reproduces_every_surface_the_run_exported():
    kinds = {}
    for item in DOCUMENT["fields"]:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    assert kinds == {
        "resource.attribute": 4,
        "scope": 3,
        "span": SPAN_NAMES,
        "span.attribute": 220,
        "span.event": REFUSED_FIELDS,
        "metric": METRIC_NAMES,
    }
    # No data point carried an attribute, so the kind is absent rather than empty-and-approved.
    assert "metric.datapoint.attribute" not in kinds
    assert "unresolved" not in kinds
    assert len({event for _, event in evidence.SPAN_EVENTS}) == EVENT_NAMES


def test_the_only_attribute_keys_that_survived_are_the_six_that_were_approved():
    keys = {item["key"] for item in DOCUMENT["fields"] if item["kind"].endswith("attribute")}
    assert keys == {
        "service.name",
        "service.version",
        "forge.invocation.id",
        "forge.telemetry.contract.version",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
    }


def test_only_the_contract_mandated_fields_are_expected():
    """`expected` is a contract requirement, not a record of what one session happened to do."""
    assert {declared.id for declared in MANIFEST.fields.values() if declared.expected} == {
        "resource.attribute/traces/service.name",
        "resource.attribute/traces/service.version",
        "resource.attribute/metrics/service.name",
        "resource.attribute/metrics/service.version",
        "span/codex.exec",
        "span.attribute/codex.exec/forge.invocation.id",
        "span.attribute/codex.exec/forge.telemetry.contract.version",
    }


def test_no_redacted_key_survived_the_privacy_processor():
    keys = {item["key"] for item in DOCUMENT["fields"] if item["kind"].endswith("attribute")}
    assert keys & MANIFEST.redacted_keys == set()


def test_no_unredactable_carrier_reached_the_inventory():
    assert DOCUMENT["carriers"]["span_links"] == 0
    assert DOCUMENT["carriers"]["metric_exemplars"] == 0


def test_the_correlation_attributes_are_approved_as_shapes_not_values():
    identifier = _field(DOCUMENT, "span.attribute/codex.exec/forge.invocation.id")
    assert identifier["status"] == inventory.STATUS_APPROVED
    assert identifier["value_classes"] == [inventory.CLASS_HEX32]
    assert "values" not in identifier
    version = _field(DOCUMENT, "span.attribute/codex.exec/forge.telemetry.contract.version")
    assert version["value_classes"] == [inventory.CLASS_STATIC]
    assert version["values"] == ["1.0"]


def test_native_token_counters_are_approved_but_only_as_observations():
    for direction in ("input", "output"):
        entry = _field(DOCUMENT, f"span.attribute/handle_responses/gen_ai.usage.{direction}_tokens")
        assert entry["status"] == inventory.STATUS_APPROVED
        assert entry["value_classes"] == [inventory.CLASS_NONNEGATIVE_INTEGER]
        assert "observational only" in entry["note"]
        assert "Forge persisted usage remains the authoritative basis" in entry["note"]
    assert "observational only" in _field(DOCUMENT, "metric/codex.turn.token_usage")["note"]
    assert MANIFEST.review["accounting_basis"] == "forge-persisted-usage"
    assert MANIFEST.review["native_token_metrics"] == "observational"
    assert _condition(DOCUMENT, "Forge persisted usage remains the accounting basis")
    assert _condition(DOCUMENT, "native Codex token metrics remain observational")


# ---------------------------------------------------------------------------
# The refusal: Codex span-event names
# ---------------------------------------------------------------------------


def test_every_span_event_is_refused_and_none_is_named():
    refused = gate8.refused_fields(DOCUMENT)
    assert len(refused) == REFUSED_FIELDS
    for item in refused:
        assert item["kind"] == "span.event"
        assert item["status"] == inventory.STATUS_BLOCKED
        assert item["identifier_status"] == inventory.IDENTIFIER_UNPRINTABLE
        assert item["fingerprint_algorithm"] == "sha256"
        assert item["name"] == f"sha256-{item['fingerprint']}"
        assert "identifier has no path shape" in item["identifier_findings"]
    assert gate8.unprintable_fields(DOCUMENT) == refused


def test_no_observed_event_name_reaches_the_manifest_or_the_report():
    rendered = inventory.render_json(DOCUMENT)
    for _, event in evidence.SPAN_EVENTS:
        assert event not in MANIFEST_TEXT
        assert event not in rendered
    assert MANIFEST.vocabulary["event_names"] == frozenset()


def test_no_forbidden_value_appears_in_the_report_or_the_manifest():
    assert DOCUMENT["privacy_scan"]["passed"] is True
    assert DOCUMENT["privacy_scan"]["patterns_found"] == []
    assert inventory.scan_forbidden_output(inventory.render_json(DOCUMENT)) == ()
    assert inventory.scan_forbidden_output(MANIFEST_TEXT) == ()


# ---------------------------------------------------------------------------
# The vocabulary override, and its bound
# ---------------------------------------------------------------------------


def test_every_vocabulary_name_is_shape_safe_whatever_the_policy_waived():
    for section, names in MANIFEST.vocabulary.items():
        for name in names:
            category = next(
                key for key, value in inventory.IDENTIFIER_VOCABULARY.items() if value == section
            )
            waived = set(inventory.assess_identifier(category, name).failures)
            assert waived <= WAIVABLE_IDENTIFIER_CHECKS, section
            assert SAFE_VOCABULARY_SHAPE.fullmatch(name), section
            assert name.isascii(), section


def test_the_vocabulary_waiver_is_used_and_only_for_names_that_name_code():
    waived = sorted(
        name
        for section, names in MANIFEST.vocabulary.items()
        for name in names
        if inventory.assess_identifier(
            next(key for key, value in inventory.IDENTIFIER_VOCABULARY.items() if value == section),
            name,
        ).failures
    )
    # The deletion set is listed in `attribute_keys` on purpose, and every one of its keys trips the
    # content-word check -- that is what those keys are. Naming a key so a survivor is reported by
    # name is not approving it: each is REDACTED by `redacted_keys`, which is judged first.
    named_for_refusal = [name for name in waived if name in MANIFEST.redacted_keys]
    code_names = [name for name in waived if name not in MANIFEST.redacted_keys]
    assert set(named_for_refusal) == {
        key for key in MANIFEST.redacted_keys if inventory.assess_identifier("span.attribute.key", key).failures
    }
    assert len(code_names) == 16, code_names
    assert "thread/start" in code_names
    assert "build_prompt" in code_names
    assert "codex.responses_api_overhead.duration_ms" in code_names


def test_no_redacted_key_is_allowlisted_for_printing():
    for allowlist in (MANIFEST.static_values, MANIFEST.enum_values):
        assert set(allowlist) & MANIFEST.redacted_keys == set()


def test_the_two_manifests_cannot_be_loaded_in_each_other_s_place():
    with pytest.raises(inventory.ManifestError):
        inventory.load_manifest(gate8.MANIFEST_PATH)
    with pytest.raises(inventory.ManifestError):
        gate8.load_codex_manifest(inventory.MANIFEST_PATH)


# ---------------------------------------------------------------------------
# Failing closed
# ---------------------------------------------------------------------------


def test_an_unprintable_identifier_may_be_refused_but_never_approved():
    """The one Gate 6 rule Gate 8 needed refined, in both directions."""
    entry = gate8.refused_fields(DOCUMENT)[0]
    for status, expected in (
        (inventory.STATUS_BLOCKED, inventory.STATUS_BLOCKED),
        (inventory.STATUS_REDACTED, inventory.STATUS_REDACTED),
        (inventory.STATUS_APPROVED, inventory.STATUS_UNAPPROVED),
        (inventory.STATUS_UNAPPROVED, inventory.STATUS_UNAPPROVED),
    ):
        declared = replace(MANIFEST.fields[entry["id"]], status=status)
        manifest = replace(MANIFEST, fields={**MANIFEST.fields, entry["id"]: declared})
        judged = _field(gate8.build_codex_document(manifest=manifest), entry["id"])
        assert judged["status"] == expected, status
        assert (
            "the identifier failed the safe-identifier policy and cannot be approved"
            in judged["reasons"]
        )


def test_approving_a_blocked_event_fails_the_gate():
    entry = gate8.refused_fields(DOCUMENT)[0]
    declared = replace(MANIFEST.fields[entry["id"]], status=inventory.STATUS_APPROVED)
    manifest = replace(MANIFEST, fields={**MANIFEST.fields, entry["id"]: declared})
    document = gate8.build_codex_document(manifest=manifest)
    assert document["totals"]["unapproved"] == 1
    assert _condition(document, "no observed field is UNAPPROVED") is False
    assert _condition(document, "every unprintable identifier is refused") is False
    assert document[gate8.GATE]["verdict"] == "FAIL"


def test_refusing_something_that_is_not_an_unprintable_event_fails_the_gate():
    """A manifest cannot quieten an ordinary field by blocking it instead of approving it."""
    identifier = "metric/codex.turn.token_usage"
    declared = replace(MANIFEST.fields[identifier], status=inventory.STATUS_BLOCKED)
    manifest = replace(MANIFEST, fields={**MANIFEST.fields, identifier: declared})
    document = gate8.build_codex_document(manifest=manifest)
    assert _condition(document, "every refused field is an unprintable span event") is False
    assert document[gate8.GATE]["verdict"] == "FAIL"


def test_dropping_a_field_entry_leaves_it_unknown_and_fails_the_gate():
    fields = dict(MANIFEST.fields)
    del fields["metric/codex.thread.started"]
    document = gate8.build_codex_document(manifest=replace(MANIFEST, fields=fields))
    assert _field(document, "metric/codex.thread.started")["status"] == inventory.DISPOSITION_UNKNOWN
    assert _condition(document, "every observed field is in the manifest") is False
    assert document[gate8.GATE]["verdict"] == "FAIL"


def test_widening_an_approval_past_its_constraints_fails_the_gate():
    identifier = "span/codex.exec"
    declared = replace(MANIFEST.fields[identifier], constraints={"kinds": ("Client",)})
    manifest = replace(MANIFEST, fields={**MANIFEST.fields, identifier: declared})
    document = gate8.build_codex_document(manifest=manifest)
    entry = _field(document, identifier)
    assert entry["status"] == inventory.STATUS_UNAPPROVED
    assert any("kinds:" in reason for reason in entry["reasons"])
    assert document[gate8.GATE]["verdict"] == "FAIL"


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param({"review": {"approved_by": "", "state": "approved"}}, id="no reviewer"),
        pytest.param({"review": {"approved_by": "someone", "state": "draft"}}, id="not approved"),
        pytest.param({"subject": {"agent_version": "0.147.0"}}, id="agent upgraded"),
        pytest.param({"subject": {"collector_version": "0.159.0"}}, id="collector upgraded"),
        pytest.param({"review": {"accounting_basis": "codex-native"}}, id="accounting moved"),
        pytest.param({"review": {"native_token_metrics": "authoritative"}}, id="metrics promoted"),
    ],
)
def test_the_gate_fails_when_the_approval_no_longer_says_what_it_said(mutation, tmp_path):
    raw = _raw_manifest()
    for section, changes in mutation.items():
        raw[section] = {**raw[section], **changes}
    document = gate8.build_codex_document(manifest=_manifest_from(raw, tmp_path))
    assert document[gate8.GATE]["verdict"] == "FAIL"


def test_an_expected_field_that_stops_appearing_is_reported_rather_than_passed(tmp_path):
    raw = _raw_manifest()
    raw["fields"].append({"id": "metric/codex.retired.instrument", "status": "APPROVED", "expected": True})
    document = gate8.build_codex_document(manifest=_manifest_from(raw, tmp_path))
    assert document["expected_not_observed"] == ["metric/codex.retired.instrument"]
    assert _condition(document, "every expected field was observed") is False
    assert document[gate8.GATE]["verdict"] == "FAIL"


# ---------------------------------------------------------------------------
# What Gate 8 does not change
# ---------------------------------------------------------------------------


def test_gate_8_wires_nothing_into_the_forge_runtime():
    source = certification.REPO_ROOT / "src" / "codex_forge"
    for module in sorted(source.glob("*.py")):
        text = module.read_text(encoding="utf-8")
        assert "codex-field-approvals" not in text, module.name
        assert "otel_codex_inventory" not in text, module.name
        assert "otel_codex_evidence" not in text, module.name


def test_gate_8_starts_no_process_and_reads_no_file_outside_the_repository():
    permitted = {
        "__future__",
        "pathlib",
        "otel_canary",
        "otel_certification",
        "otel_codex_evidence",
        "otel_inventory",
    }
    for module in (gate8, evidence):
        text = Path(module.__file__).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        assert imported <= permitted, module.__name__
        # The one absolute path Gate 8 could have been tempted to read: the run's output directory.
        assert "CodexData" not in text, module.__name__


def test_the_claude_gate_6_manifest_is_untouched_by_gate_8():
    claude = inventory.load_manifest()
    assert claude.subject["agent"] == "claude-code"
    assert claude.subject["agent_version"] == "2.1.223"
    assert claude.path != gate8.MANIFEST_PATH
