"""Gate 8 field inventory: what codex-cli 0.146.0 exported, after the certified privacy pipeline.

Gate 7 answered *whether one Codex invocation could be observed at all*. Gate 8 asks the Gate 6
question about the fields that invocation carried: for every resource attribute, scope, span name,
span attribute, event, and metric that survived redaction, what is it, what shape does it carry, and
has a human approved it.

**No second Codex request was made, and none was needed.** The 2026-08-07 run recorded in
`docs/TELEMETRY.md` printed its entire post-redaction surface through the `debug` exporter at
`verbosity: detailed`. `tests/otel_codex_evidence.py` is that surface, distilled to a table of names
and bounded descriptors; this module compares it against `ops/otel/codex-field-approvals.yaml`.

Almost nothing here is new. The parser, the value classifier, the safe-identifier policy, the
manifest reader, the comparison, the forbidden-output scan, and the document builder are Gate 6's,
in `tests/otel_inventory.py`, used unchanged. Three things are Codex's own and cannot be shared:

* **its manifest**, under a schema of its own so neither agent's approvals can be loaded in the
  other's place;
* **its run facts**, taken from the Gate 7 report rather than re-measured;
* **its verdict**, because Gate 6's conditions name Claude Code's version and require every observed
  identifier to be printable -- and Codex's span-event names are not. Gate 8 replaces that condition
  with the two that actually hold: every unprintable identifier is refused, and every refused field
  is an unprintable span event.

This module starts nothing. It opens no socket, launches no agent, and reads no file outside the
repository.
"""

from __future__ import annotations

from pathlib import Path

import otel_canary as canary
import otel_certification as certification
import otel_codex_evidence as evidence
import otel_inventory as inventory

# The report format, and the manifest format it reads. Distinct from Claude's on both counts.
SCHEMA_VERSION = "forge.codex.field-inventory/1"
MANIFEST_SCHEMA = "forge.codex.field-approvals/1"

MANIFEST_PATH = certification.REPO_ROOT / "ops" / "otel" / "codex-field-approvals.yaml"

GATE = "gate8"

# The privacy checks the Gate 7 run reported, named so the document says which ran rather than only
# that they passed. Restated rather than re-run: re-running them would need the raw run output.
RECORDED_PRIVACY_CHECKS = (
    "no log record was accepted",
    "no log signal reached the exporter",
    "no redacted key survived the privacy processor",
    "no forbidden value appears in the report",
    "the invocation identifier is never printed",
)


def load_codex_manifest(path: Path = MANIFEST_PATH) -> inventory.Manifest:
    """Read the Codex approval manifest, refusing one that declares any other schema."""
    return inventory.load_manifest(path, schema=MANIFEST_SCHEMA)


def recorded_report() -> canary.CanaryReport:
    """The Gate 7 run, restated as the report the Gate 6 document builder consumes.

    Every number comes from `evidence.RECORDED`, which is that run's own report. The span
    breakdown is the one it published -- all 224 spans in the `other` bucket, the diagnostic and the
    Collector's counters in agreement.
    """
    recorded = evidence.RECORDED
    accounting = canary.SpanAccounting(
        accepted=recorded["spans_accepted"],
        exported=recorded["spans_exported"],
        filtered=recorded["spans_filtered"],
        refused=recorded["spans_refused"],
        send_failed=recorded["spans_send_failed"],
        accepted_by_name={"other": recorded["spans_accepted"]},
        exported_by_name={"other": recorded["spans_exported"]},
        settled=True,
    )
    return canary.CanaryReport(
        accounting=accounting,
        privacy=canary.PrivacyCheck(checks=RECORDED_PRIVACY_CHECKS, findings=()),
        collector_returncode=recorded["collector_returncode"],
        agent_returncode=recorded["codex_returncode"],
        output=evidence.render_export(),
    )


def build_codex_document(
    report: canary.CanaryReport | None = None,
    manifest: inventory.Manifest | None = None,
) -> dict:
    """The Gate 8 document: Gate 6's inventory of the Codex surface, under Gate 8's verdict."""
    return inventory.build_document(
        report if report is not None else recorded_report(),
        manifest if manifest is not None else load_codex_manifest(),
        schema=SCHEMA_VERSION,
        gate=GATE,
        verdict=codex_verdict,
    )


def refused_fields(body: dict) -> list[dict]:
    """Every field the manifest refuses -- BLOCKED or REDACTED -- as observed."""
    return [
        item
        for item in body["fields"]
        if item["status"] in (inventory.STATUS_BLOCKED, inventory.STATUS_REDACTED)
    ]


def unprintable_fields(body: dict) -> list[dict]:
    return [
        item
        for item in body["fields"]
        if item.get("identifier_status") == inventory.IDENTIFIER_UNPRINTABLE
    ]


def codex_verdict(body: dict, manifest: inventory.Manifest) -> dict:
    """Every condition Gate 8 requires, each reported by name so a FAIL says which one failed."""
    totals = body["totals"]
    run = body["run"]
    unprintable = unprintable_fields(body)
    refused = refused_fields(body)
    conditions = {
        "manifest subject is codex-cli 0.146.0": (
            manifest.subject.get("agent") == evidence.AGENT
            and manifest.subject.get("agent_version") == evidence.AGENT_VERSION
        ),
        "manifest subject is Collector 0.158.0": (
            manifest.subject.get("collector_version") == certification.CERTIFIED_COLLECTOR_VERSION
        ),
        "a human recorded an approval in the manifest": manifest.is_human_approved,
        "at least one field was observed": totals["observed"] > 0,
        "every observed field is in the manifest": totals["unknown"] == 0,
        "no observed field is UNAPPROVED": totals["unapproved"] == 0,
        "no REDACTED key survived the pipeline": totals["redacted"] == 0,
        "every expected field was observed": totals["expected_not_observed"] == 0,
        "the inventory remainder is zero": totals["unresolved_remainder"] == 0,
        # Gate 6 requires every identifier to be printable. Codex names a span event after a source
        # location, so Gate 8 cannot -- and refuses them instead. These two conditions are what that
        # costs: an identifier nobody can print is never approved, and the set of refused fields is
        # exactly that set. A manifest that blocked anything else would fail the second.
        "every unprintable identifier is refused": all(
            item["status"] == inventory.STATUS_BLOCKED for item in unprintable
        ),
        "every refused field is an unprintable span event": all(
            item["kind"] == "span.event"
            and item.get("identifier_status") == inventory.IDENTIFIER_UNPRINTABLE
            for item in refused
        ),
        "no span link reached the inventory": body["carriers"]["span_links"] == 0,
        "no metric exemplar reached the inventory": body["carriers"]["metric_exemplars"] == 0,
        "no forbidden value appears in this report": body["privacy_scan"]["passed"],
        "the span accounting balanced": body["span_accounting"]["balanced"],
        "the Gate 7 run passed": run["canary_passed"],
        "the Collector exited cleanly": run["collector_returncode"] == 0,
        "Codex exited cleanly": run["agent_returncode"] == 0,
        # Neither is derivable from the export, and both are what a reader of a PASS most needs to
        # know. They are recorded in the manifest so the approval carries them.
        "Forge persisted usage remains the accounting basis": (
            manifest.review.get("accounting_basis") == "forge-persisted-usage"
        ),
        "native Codex token metrics remain observational": (
            manifest.review.get("native_token_metrics") == "observational"
        ),
    }
    return {
        "verdict": "PASS" if all(conditions.values()) else "FAIL",
        "conditions": [{"condition": name, "met": met} for name, met in sorted(conditions.items())],
    }
