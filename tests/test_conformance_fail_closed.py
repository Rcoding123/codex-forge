"""Specification conformance fails closed: the report is an exact bijection of the contract.

Closure of the fail-open blockers found in review of the contract extension:

1. Exactly one report per required item. A duplicate report is itself invalid review
   evidence, whether the copies agree or contradict each other; the gate never collapses
   entries into a last-entry-wins map that could hide a contradiction.
2. Verbatim matching. Task intake already normalized the stored contract text, so review
   conformance requires the reviewer to echo that exact string: Unicode re-normalization,
   whitespace rewrapping, and paraphrases are not coverage.
3. Bijection. Reported texts must equal expected texts with multiplicity exactly one. An
   entry that matches no contract item is rejected, never ignored: a contradictory
   paraphrase of an item cannot ride alongside the exact met entry it shadows, and green
   candidate-test evidence on the exact entry cannot override that conformance finding.
4. The review prompt carries the task's validation commands verbatim as deterministic
   evidence requirements, completing the implementation contract the reviewer sees.
5. Old tasks stay reviewable, not byte-frozen. The clarified backward-compatibility law:
   old rows load, empty binding fields require no conformance report, and historical
   prompt/evidence artifacts are never rewritten — while a FUTURE review of an old task
   intentionally uses the current, stronger review prompt. There is no byte-for-byte
   prompt pin to a historical baseline.

Every fail-closed rule is exercised for BOTH `acceptance_criteria` and
`binding_requirements`, which share one enforcement path.

**No test in this file makes a model request.** Review results are canned finals.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from pathlib import Path

import pytest
from test_binding_contract import OLD_TASKS_TABLE

from codex_forge.db import ForgeDB
from codex_forge.models import Project, Task, TaskState
from codex_forge.orchestrator import enforce_specification_conformance
from codex_forge.prompts import review_prompt


ITEM = "Supported levels are low, medium, and high."
OTHER_ITEM = "Signals persist in a single SQLite database file."
# Stored NFC-composed, exactly as intake normalization persists contract text.
ACCENTED_ITEM = "Café names render as NFC-composed UTF-8."


def _task(**overrides) -> Task:
    fields: dict = dict(
        id="TASK-FAIL-CLOSED",
        project_id="project-1",
        title="Signal Shelf CLI",
        objective="Record operational signals with add/list/search/stats.",
        acceptance=[],
        validation_commands=[],
        state=TaskState.REVIEWING,
    )
    fields.update(overrides)
    return Task(**fields)


def _acceptance_case(items: list[str], entries: list) -> tuple[Task, dict]:
    task = _task(acceptance=list(items))
    final = {"verdict": "pass", "summary": "Reviewed.", "acceptance_criteria": entries, "findings": []}
    return task, final


def _binding_case(items: list[str], entries: list) -> tuple[Task, dict]:
    task = _task(architecture_decisions=list(items))
    final = {
        "verdict": "pass",
        "summary": "Reviewed.",
        "acceptance_criteria": [],
        "binding_requirements": entries,
        "findings": [],
    }
    return task, final


DOMAINS = [
    pytest.param(_acceptance_case, "criterion", "acceptance criterion", id="acceptance-criteria"),
    pytest.param(_binding_case, "text", "binding requirement", id="binding-requirements"),
]


def _entry(key: str, text: str, met: bool = True, evidence: str = "Verified against the diff.") -> dict:
    return {key: text, "met": met, "evidence": evidence}


def _heading(label: str) -> str:
    return label[:1].upper() + label[1:]


def test_cross_section_binding_text_is_one_requirement_but_duplicate_reports_still_fail():
    task = _task(architecture_decisions=[ITEM], constraints=[ITEM, OTHER_ITEM])
    final = {
        "verdict": "pass", "findings": [], "acceptance_criteria": [],
        "binding_requirements": [_entry("text", ITEM), _entry("text", OTHER_ITEM)],
    }
    assert task.binding_requirements == [ITEM, OTHER_ITEM]
    assert enforce_specification_conformance(task, final) == final
    duplicate = {**final, "binding_requirements": [*final["binding_requirements"], _entry("text", ITEM)]}
    assert enforce_specification_conformance(task, duplicate)["verdict"] == "changes_required"
    assert task.architecture_decisions == [ITEM]
    assert task.constraints == [ITEM, OTHER_ITEM]
    project = Project(id="project-1", name="example", repo_path=Path("repo"))
    prompt = review_prompt(project, task, Path("checkout"))
    canonical = prompt.split("BINDING REQUIREMENTS TO REPORT", 1)[1]
    assert canonical.count(ITEM) == 1
    assert "Report it once." in canonical


# --------------------------------------------------------------------------------------------
# Part 1 - exactly one verbatim report per required item, with evidence
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("build, key, label", DOMAINS)
def test_an_exact_met_item_with_evidence_is_accepted(build, key, label):
    task, final = build([ITEM], [_entry(key, ITEM)])
    assert enforce_specification_conformance(task, final) == final


@pytest.mark.parametrize("evidence", ["", "   \n\t "], ids=["empty", "whitespace-only"])
@pytest.mark.parametrize("build, key, label", DOMAINS)
def test_a_met_item_without_evidence_fails_closed(build, key, label, evidence):
    task, final = build([ITEM], [_entry(key, ITEM, evidence=evidence)])
    effective = enforce_specification_conformance(task, final)
    assert effective["verdict"] == "changes_required"
    assert any(
        f"{_heading(label)} reported met without evidence: {ITEM}" in finding["issue"]
        for finding in effective["findings"]
    )


@pytest.mark.parametrize("build, key, label", DOMAINS)
def test_an_unmet_item_fails_closed(build, key, label):
    task, final = build([ITEM], [_entry(key, ITEM, met=False, evidence="Contradicted by cli.py.")])
    effective = enforce_specification_conformance(task, final)
    assert effective["verdict"] == "changes_required"
    unmet = [f for f in effective["findings"] if f"Unmet {label}: {ITEM}" in f["issue"]]
    assert unmet and all(finding["severity"] == "critical" for finding in unmet)


@pytest.mark.parametrize("build, key, label", DOMAINS)
def test_a_missing_item_fails_closed(build, key, label):
    task, final = build([ITEM], [])
    effective = enforce_specification_conformance(task, final)
    assert effective["verdict"] == "changes_required"
    assert any(
        f"{_heading(label)} not verified by review: {ITEM}" in finding["issue"]
        for finding in effective["findings"]
    )


@pytest.mark.parametrize("build, key, label", DOMAINS)
def test_a_duplicate_report_where_the_last_copy_says_met_fails_closed(build, key, label):
    """`last entry wins` would accept this shape; a duplicate is invalid review evidence."""
    task, final = build(
        [ITEM],
        [
            _entry(key, ITEM, met=False, evidence="Contradicted by cli.py."),
            _entry(key, ITEM, met=True, evidence="Confirmed."),
        ],
    )
    effective = enforce_specification_conformance(task, final)
    assert effective["verdict"] == "changes_required"
    assert any(
        f"{_heading(label)} reported 2 times by review: {ITEM}" in finding["issue"]
        for finding in effective["findings"]
    )


@pytest.mark.parametrize("build, key, label", DOMAINS)
def test_a_duplicate_report_fails_closed_even_when_both_copies_agree(build, key, label):
    task, final = build(
        [ITEM],
        [
            _entry(key, ITEM, met=True, evidence="Checked cli.py."),
            _entry(key, ITEM, met=True, evidence="Checked cli.py again."),
        ],
    )
    effective = enforce_specification_conformance(task, final)
    assert effective["verdict"] == "changes_required"
    assert any(
        f"{_heading(label)} reported 2 times by review: {ITEM}" in finding["issue"]
        for finding in effective["findings"]
    )


# --------------------------------------------------------------------------------------------
# Part 2 - verbatim means verbatim: no paraphrase, rewrap, or Unicode re-normalization
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("build, key, label", DOMAINS)
def test_a_paraphrased_echo_is_not_coverage(build, key, label):
    task, final = build([ITEM], [_entry(key, "The supported levels are low/medium/high.")])
    effective = enforce_specification_conformance(task, final)
    assert effective["verdict"] == "changes_required"
    assert any(
        f"{_heading(label)} not verified by review: {ITEM}" in finding["issue"]
        for finding in effective["findings"]
    )


@pytest.mark.parametrize("build, key, label", DOMAINS)
def test_a_whitespace_altered_echo_is_not_coverage(build, key, label):
    task, final = build([ITEM], [_entry(key, ITEM.replace(", ", ",\n  "))])
    effective = enforce_specification_conformance(task, final)
    assert effective["verdict"] == "changes_required"
    assert any(
        f"{_heading(label)} not verified by review: {ITEM}" in finding["issue"]
        for finding in effective["findings"]
    )


@pytest.mark.parametrize("build, key, label", DOMAINS)
def test_a_unicode_renormalized_echo_is_not_coverage(build, key, label):
    """NFD renders identically to the stored NFC text but is not the same bytes."""
    decomposed = unicodedata.normalize("NFD", ACCENTED_ITEM)
    assert decomposed != ACCENTED_ITEM  # the fixture really exercises a byte difference
    task, final = build([ACCENTED_ITEM], [_entry(key, decomposed)])
    effective = enforce_specification_conformance(task, final)
    assert effective["verdict"] == "changes_required"
    assert any(
        f"{_heading(label)} not verified by review: {ACCENTED_ITEM}" in finding["issue"]
        for finding in effective["findings"]
    )


@pytest.mark.parametrize("build, key, label", DOMAINS)
def test_an_unexpected_extra_report_does_not_satisfy_a_missing_required_item(build, key, label):
    task, final = build([ITEM], [_entry(key, OTHER_ITEM)])
    effective = enforce_specification_conformance(task, final)
    assert effective["verdict"] == "changes_required"
    assert any(
        f"{_heading(label)} not verified by review: {ITEM}" in finding["issue"]
        for finding in effective["findings"]
    )


# --------------------------------------------------------------------------------------------
# Part 3 - bijection: entries that match no contract item are rejected, never ignored
# --------------------------------------------------------------------------------------------

CONTRADICTORY_PARAPHRASE = "The supported levels are low/medium/high."


@pytest.mark.parametrize("build, key, label", DOMAINS)
def test_a_contradictory_paraphrase_cannot_ride_alongside_the_exact_met_item(build, key, label):
    """The regression: the exact item is reported met on green-test evidence while a
    paraphrased contradictory second version of the same item is reported unmet. Filtering
    the report to exact matches would complete the task; the bijection refuses it."""
    task, final = build(
        [ITEM],
        [
            _entry(key, ITEM, met=True, evidence="All candidate tests pass."),
            _entry(
                key,
                CONTRADICTORY_PARAPHRASE,
                met=False,
                evidence="cli.py accepts info|warning|critical; --level low exits 2.",
            ),
        ],
    )
    effective = enforce_specification_conformance(task, final)
    assert effective["verdict"] == "changes_required"
    extra = [f for f in effective["findings"] if CONTRADICTORY_PARAPHRASE in f["issue"]]
    assert extra and all("does not match any contract item" in f["issue"] for f in extra)
    # A rejected entry that also claims nonconformance is a contradiction, not noise.
    assert all(f["severity"] == "critical" for f in extra)


@pytest.mark.parametrize("build, key, label", DOMAINS)
def test_an_agreeing_paraphrase_alongside_the_exact_item_still_fails_closed(build, key, label):
    task, final = build([ITEM], [_entry(key, ITEM), _entry(key, CONTRADICTORY_PARAPHRASE)])
    effective = enforce_specification_conformance(task, final)
    assert effective["verdict"] == "changes_required"
    assert any(
        "does not match any contract item" in f["issue"] and CONTRADICTORY_PARAPHRASE in f["issue"]
        for f in effective["findings"]
    )


@pytest.mark.parametrize("build, key, label", DOMAINS)
def test_an_unrelated_extra_entry_alongside_a_fully_met_report_fails_closed(build, key, label):
    task, final = build([ITEM], [_entry(key, ITEM), _entry(key, OTHER_ITEM)])
    effective = enforce_specification_conformance(task, final)
    assert effective["verdict"] == "changes_required"
    assert any(
        "does not match any contract item" in f["issue"] and OTHER_ITEM in f["issue"]
        for f in effective["findings"]
    )


@pytest.mark.parametrize(
    "malformed",
    ["not an entry object", {"met": True, "evidence": "No text key at all."}],
    ids=["non-dict", "missing-text"],
)
@pytest.mark.parametrize("build, key, label", DOMAINS)
def test_a_malformed_report_entry_fails_closed(build, key, label, malformed):
    task, final = build([ITEM], [_entry(key, ITEM), malformed])
    effective = enforce_specification_conformance(task, final)
    assert effective["verdict"] == "changes_required"
    assert any("does not match any contract item" in f["issue"] for f in effective["findings"])


@pytest.mark.parametrize("build, key, label", DOMAINS)
def test_an_unhashable_report_text_fails_closed_instead_of_raising(build, key, label):
    task, final = build([ITEM], [_entry(key, ITEM), _entry(key, ["not", "a", "string"])])
    effective = enforce_specification_conformance(task, final)
    assert effective["verdict"] == "changes_required"
    assert any("does not match any contract item" in f["issue"] for f in effective["findings"])


@pytest.mark.parametrize("build, key, label", DOMAINS)
def test_report_order_does_not_matter_for_a_bijective_report(build, key, label):
    """The bijection is over texts, not sequence: a permuted but exact report passes."""
    task, final = build([ITEM, OTHER_ITEM], [_entry(key, OTHER_ITEM), _entry(key, ITEM)])
    assert enforce_specification_conformance(task, final) == final


# --------------------------------------------------------------------------------------------
# Part 4 - the review prompt carries validation commands verbatim
# --------------------------------------------------------------------------------------------


def test_the_review_prompt_carries_validation_commands_verbatim(tmp_path: Path):
    project = Project(id="project-1", name="signal-shelf", repo_path=tmp_path)
    task = _task(
        acceptance=["The existing test suite passes."],
        validation_commands=["python -m pytest -q", 'python -c "import codex_forge"'],
    )
    prompt = review_prompt(project, task, tmp_path)
    assert "AUTHORITATIVE VALIDATION:" in prompt
    for command in task.validation_commands:
        assert command in prompt
    # The commands are evidence requirements for the specification, never a redefinition of it.
    assert "deterministic evidence requirements" in prompt
    assert "they do not redefine the specification" in prompt


# --------------------------------------------------------------------------------------------
# Part 5 - old tasks stay reviewable under the current prompt; no byte-frozen baseline
# --------------------------------------------------------------------------------------------

OLD_CRITERION = "The old criterion still holds."

# The review shape a pre-extension reviewer produces: acceptance entries, no binding report.
PRE_EXTENSION_REVIEW_FINAL = {
    "verdict": "pass",
    "summary": "Reviewed.",
    "acceptance_criteria": [
        {"criterion": OLD_CRITERION, "met": True, "evidence": "Verified against the diff."}
    ],
    "findings": [],
}


def test_an_old_task_row_still_loads_and_reviews_under_the_current_prompt(tmp_path: Path):
    """The clarified backward-compatibility law: an old row loads through the additive
    migration, its empty binding fields require no conformance report, and a future review
    of it uses the current, stronger review prompt — never a byte-frozen historical
    rendering. Historical prompt/evidence artifacts on disk are never rewritten; only what
    a new review would produce changes."""
    database_path = tmp_path / "data" / "forge.db"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute(OLD_TASKS_TABLE)
        connection.execute(
            "INSERT INTO tasks(id,project_id,title,objective,acceptance_json,validation_json,"
            "state,tags_json,dependencies_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "TASK-OLD",
                "project-1",
                "Old task",
                "An objective recorded before the contract fields existed.",
                f'["{OLD_CRITERION}"]',
                "[]",
                "READY",
                "[]",
                "[]",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
    db = ForgeDB(database_path)
    db.initialize()
    task = db.get_task("TASK-OLD")
    assert task is not None
    assert task.acceptance == [OLD_CRITERION]
    assert task.architecture_decisions == []
    assert task.constraints == []
    assert task.validation_commands == []

    project = Project(id="project-1", name="legacy", repo_path=tmp_path)
    prompt = review_prompt(project, task, tmp_path / "worktree")
    # Fields the task never had render as no sections at all.
    assert "TASK CONTRACT" not in prompt
    assert "BINDING REQUIREMENT CONFORMANCE" not in prompt
    assert "AUTHORITATIVE VALIDATION" not in prompt
    # The prompt is the CURRENT one: a future review of an old task is held to the
    # strengthened conformance contract, not to a pinned historical byte rendering.
    assert "ACCEPTANCE CONFORMANCE" in prompt
    assert "Report each criterion exactly once" in prompt
    assert OLD_CRITERION in prompt

    # Empty binding fields require no `binding_requirements` report at all.
    effective = enforce_specification_conformance(task, PRE_EXTENSION_REVIEW_FINAL)
    assert effective == PRE_EXTENSION_REVIEW_FINAL
