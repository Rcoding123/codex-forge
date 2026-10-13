from __future__ import annotations

import json
from pathlib import Path

from .models import Project, Task, ValidationResult


BASE_POLICY = """
You are an autonomous software-engineering worker inside an isolated VPS.
Do not ask for permission to run commands, edit files, build, test, install project dependencies, or continue.
Work until the task is completed, objectively blocked, or requires a genuinely non-inferable product decision.
Make reasonable, reversible engineering decisions from repository conventions and record them under assumptions.
Do not place live trades, access brokerage credentials, deploy to production, force-push shared branches, or modify unrelated repositories.
Stay within the assigned Git worktree and task scope. Preserve existing behavior unless the task explicitly changes it.
Use targeted repository search before opening large files. Use RTK for noisy commands when available; retain exact errors when diagnosis requires them.
Do not repeat successful commands, unchanged file reads, or identical failed commands without new evidence or changed inputs.
Run all relevant validation. Never claim success without evidence.
Keep the final structured response concise; detailed logs already live on disk.
""".strip()


def _contract_sections(task: Task) -> str:
    """The specification contract beyond the objective, rendered verbatim.

    Scope and out-of-scope bound the task; architecture decisions and constraints are binding
    implementation requirements. Every present field is reproduced exactly as persisted — never
    summarized — and an absent field contributes no section, so tasks submitted before these
    fields existed render exactly as they always did.
    """
    sections: list[str] = []
    if task.scope:
        sections.append(f"SCOPE\n{task.scope}")
    if task.out_of_scope:
        sections.append(f"OUT OF SCOPE\n{task.out_of_scope}")
    if task.architecture_decisions:
        decisions = "\n".join(f"- {item}" for item in task.architecture_decisions)
        sections.append(f"ARCHITECTURE DECISIONS (binding)\n{decisions}")
    if task.constraints:
        constraints = "\n".join(f"- {item}" for item in task.constraints)
        sections.append(f"CONSTRAINTS (binding)\n{constraints}")
    if not sections:
        return ""
    hierarchy = (
        "TASK CONTRACT\n"
        "Scope and out-of-scope bound what this task may change. Architecture decisions and "
        "constraints are binding implementation requirements; implement them exactly as written. "
        "Acceptance criteria state what must be demonstrably true. Tests are evidence for this "
        "contract; tests do not redefine it."
    )
    return "\n\n".join([hierarchy, *sections])


def base_authority(project: Project, task: Task) -> str:
    """The revision the model must treat as this task's comparison base.

    A task admitted from an exact commit is measured against that commit by terminal evidence, so
    model-facing prose must name the same authority. Naming the project's default branch there
    would point the reviewer at a movable ref while the recorded diff, changed-file set and
    candidate delta are all computed from the immutable base. A task admitted without one keeps
    the default-branch wording it has always had.
    """
    return task.admission_base_commit or task.execution_base_commit or project.default_branch


def _base_line(project: Project, task: Task) -> str:
    if task.admission_base_commit:
        return (
            f"- Base commit: {task.admission_base_commit}\n"
            "- That commit is this task's immutable admission base. Compare against it, not "
            "against the default branch, which may have moved since admission."
        )
    if task.execution_base_commit:
        return (
            f"- Execution base commit: {task.execution_base_commit}\n"
            "- This is the controller's immutable comparison base. Compare against it, not a movable branch."
        )
    return f"- Base branch: {project.default_branch}"


def implementation_prompt(
    project: Project,
    task: Task,
    worktree: Path,
    patterns: list[dict],
    previous_failure: str | None = None,
    review_findings: list[dict] | None = None,
) -> str:
    acceptance = "\n".join(f"- {item}" for item in task.acceptance) or "- Satisfy the objective without unrelated changes."
    validations = "\n".join(f"- `{item}`" for item in task.validation_commands) or "- Infer and run the repository's authoritative checks."
    learned = json.dumps(patterns, indent=2, ensure_ascii=False) if patterns else "None."
    failure = previous_failure or "None."
    review = json.dumps(review_findings or [], indent=2, ensure_ascii=False)
    contract = _contract_sections(task)
    contract_block = f"\n\n{contract}" if contract else ""
    return f"""
{BASE_POLICY}

PROJECT
- Name: {project.name}
- Repository: {worktree}
{_base_line(project, task)}

TASK
- ID: {task.id}
- Title: {task.title}
- Risk: {task.risk}
- Tags: {', '.join(task.tags) or 'none'}

OBJECTIVE
{task.objective}{contract_block}

ACCEPTANCE CRITERIA
{acceptance}

AUTHORITATIVE VALIDATION
{validations}

RELEVANT VALIDATED/CANDIDATE PATTERNS
{learned}

PREVIOUS FAILURE EVIDENCE
{failure}

REVIEW FINDINGS TO REPAIR
{review}

Execute the task now. Inspect the repository, implement the smallest complete solution, run validation, repair failures, and leave the worktree ready for independent review.
If an external runtime prevents complete validation, perform every available offline check and report the exact missing evidence.
""".strip()


def _review_base_note(task: Task) -> str:
    if not task.admission_base_commit:
        if task.execution_base_commit:
            return "\nThat commit is the controller's immutable execution base and terminal evidence comparison authority."
        return ""
    return (
        "\nThat commit is this task's immutable admission base and the exact base its terminal "
        "evidence is measured from, so the changed files, the patch scope and the candidate delta "
        "you reason about are the ones in that diff. The project's default branch may have moved "
        "since admission and is not the comparison authority."
    )


def review_prompt(
    project: Project,
    task: Task,
    worktree: Path,
    previous_findings: list[dict] | None = None,
) -> str:
    acceptance = "\n".join(f"- {item}" for item in task.acceptance)
    contract = _contract_sections(task)
    contract_block = f"\n\n{contract}" if contract else ""
    validation_block = ""
    if task.validation_commands:
        validations = "\n".join(f"- `{item}`" for item in task.validation_commands)
        validation_block = (
            "\n\nAUTHORITATIVE VALIDATION:\n"
            f"{validations}\n"
            "The validation commands above are deterministic evidence requirements; the controller\n"
            "executes them exactly as written. Validation commands and tests are evidence for the\n"
            "specification; they do not redefine the specification."
        )
    binding_block = ""
    if task.architecture_decisions or task.constraints:
        binding_conformance = """
BINDING REQUIREMENT CONFORMANCE
The architecture decisions and constraints above are binding implementation requirements, exactly
like acceptance criteria. Compare the final candidate against each of them exactly as written and
report every one in `binding_requirements` with the requirement text copied verbatim into `text`,
whether the candidate meets it in `met`, and the concrete evidence you checked in `evidence`.
Report each requirement exactly once and report nothing else in `binding_requirements`: no
paraphrases, no reworded variants, no entries that are not in the contract above.
A candidate that does not satisfy a binding requirement requires `changes_required` with a finding
naming that requirement, even when every candidate-authored test passes. Scope and out-of-scope are
reviewer context: report work that crosses those boundaries as ordinary findings.
""".strip()
        canonical = "\n".join(f"- {text}" for text in task.binding_requirements)
        binding_block = (
            f"\n\n{binding_conformance}\n\n"
            "BINDING REQUIREMENTS TO REPORT (ordered exact-text union)\n"
            "Identical text in both source sections is one requirement. Report it once.\n"
            f"{canonical}"
        )
    follow_up = ""
    if previous_findings:
        follow_up = f"""
FOLLOW-UP REVIEW
This worktree was already reviewed. Verify the repaired findings below and adjacent regression risk;
do not repeat unrelated repository exploration.
{json.dumps(previous_findings, indent=2, ensure_ascii=False)}
""".strip()
    return f"""
You are the independent reviewer for an autonomous coding task. Do not modify files.
Review the complete diff against `{base_authority(project, task)}` in {worktree}.{_review_base_note(task)}
Focus on correctness, regressions, concurrency/resource issues, security, missing validation, and whether the task is actually complete.
Do not report style preferences unless they create a concrete defect.

TASK {task.id}: {task.title}
OBJECTIVE:
{task.objective}{contract_block}

ACCEPTANCE:
{acceptance}{validation_block}

ACCEPTANCE CONFORMANCE
The acceptance criteria above are the task contract. Tests are evidence for the specification;
tests do not redefine the specification. Compare the final candidate against every acceptance
criterion exactly as written, independently of what candidate-authored tests assert. Report each
criterion in `acceptance_criteria` with the criterion text copied verbatim, whether the candidate
meets it, and the concrete evidence you checked. Report each criterion exactly once and report
nothing else in `acceptance_criteria`: no paraphrases, no reworded variants, no entries that are
not in the acceptance list above. A candidate that does not satisfy a criterion
requires `changes_required` with a finding naming that criterion, even when every candidate-authored
test passes; tests that encode a contract contradicting a criterion are themselves a defect.{binding_block}

{follow_up}

Return `changes_required` only for actionable defects that should block completion. Include exact file and line when available.
""".strip()


def validation_failure_summary(results: list[ValidationResult]) -> str:
    chunks: list[str] = []
    for result in results:
        if result.passed:
            continue
        chunks.append(
            f"COMMAND: {result.command}\nEXIT: {result.exit_code}\nOUTPUT:\n{result.summary}"
        )
    return "\n\n".join(chunks)
