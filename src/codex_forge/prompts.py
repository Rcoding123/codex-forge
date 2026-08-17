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
    return f"""
{BASE_POLICY}

PROJECT
- Name: {project.name}
- Repository: {worktree}
- Base branch: {project.default_branch}

TASK
- ID: {task.id}
- Title: {task.title}
- Risk: {task.risk}
- Tags: {', '.join(task.tags) or 'none'}

OBJECTIVE
{task.objective}

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


def review_prompt(
    project: Project,
    task: Task,
    worktree: Path,
    previous_findings: list[dict] | None = None,
) -> str:
    acceptance = "\n".join(f"- {item}" for item in task.acceptance)
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
Review the complete diff against `{project.default_branch}` in {worktree}.
Focus on correctness, regressions, concurrency/resource issues, security, missing validation, and whether the task is actually complete.
Do not report style preferences unless they create a concrete defect.

TASK {task.id}: {task.title}
OBJECTIVE:
{task.objective}

ACCEPTANCE:
{acceptance}

ACCEPTANCE CONFORMANCE
The acceptance criteria above are the task contract. Tests are evidence for the specification;
tests do not redefine the specification. Compare the final candidate against every acceptance
criterion exactly as written, independently of what candidate-authored tests assert. Report each
criterion in `acceptance_criteria` with the criterion text copied verbatim, whether the candidate
meets it, and the concrete evidence you checked. A candidate that does not satisfy a criterion
requires `changes_required` with a finding naming that criterion, even when every candidate-authored
test passes; tests that encode a contract contradicting a criterion are themselves a defect.

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
