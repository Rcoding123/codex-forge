"""Token-usage normalization for persisted Codex run records.

Every function here is pure. Aggregation reads the `usage_json` a run already persisted rather than
a normalized column, so a historical task reports the same categories as a task that runs today and
no backfill can disagree with the record it was derived from.

Observed usage contract, Codex CLI 0.146.0 `turn.completed` event:

    {"input_tokens", "cached_input_tokens", "cache_write_input_tokens",
     "output_tokens", "reasoning_output_tokens"}

`cached_input_tokens` is the cached *subset* of `input_tokens`, and `reasoning_output_tokens` is
the reasoning *subset* of `output_tokens`. Both mirror the provider fields the installed binary
carries verbatim — `input_tokens_details.cached_tokens` and `output_tokens_details.reasoning_tokens`
— which are documented subtotals of their parent count. Neither is a sibling category, so neither is
ever added into a total: `raw_total_tokens` stays `input_tokens + output_tokens`, the raw traffic a
task moved.

Raw traffic is not the task budget. Only new work consumes a budget, so the enforced quantity is
`fresh_input_tokens + output_tokens` — see `budget_used_tokens`. A cached input read is work the
provider already did; it moves bytes, not budget.

This module deliberately reports categories only. It assigns no price, no billable-token value, and
no cache discount, because those differ by model, plan, and date and none of them are observable
from a persisted run record.
"""

from __future__ import annotations

import json
import math
from typing import Any, Iterable, Mapping


# `accounting_basis` names what the numbers are: the categories the worker reported, not a derived
# billing model. `budget_basis` names the one quantity the token budget is enforced against, so a
# reader never has to guess which of the reported categories a limit applies to.
ACCOUNTING_BASIS = "reported_token_categories"
BUDGET_BASIS = "fresh_input_plus_output"

# Fields taken verbatim from a run's reported usage. Anything else in the record is ignored, so a
# usage object that later gains a member cannot silently join a total under this contract.
REPORTED_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

# Fields this module computes from the reported ones.
DERIVED_FIELDS = ("fresh_input_tokens", "raw_total_tokens")


def coerce_token_count(value: Any) -> int:
    """Return one reported usage field as a non-negative integer count.

    Persisted usage is whatever a worker wrote at the time, so every unusable value — missing, null,
    boolean, non-numeric text, non-finite, or negative — degrades to `0` instead of raising. Reading
    a task must never fail because one historical run recorded a malformed count, and no coercion
    may produce a negative number that would then flow into a derived total.
    """
    if isinstance(value, bool):
        # `bool` is an `int` subclass. A flag is not a token count.
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value)) if math.isfinite(value) else 0
    if isinstance(value, str):
        try:
            return max(0, int(value.strip()))
        except ValueError:
            return 0
    return 0


def parse_usage_document(usage: Any) -> Mapping[str, Any]:
    """Return a persisted `usage_json` value as a mapping, or an empty mapping if it is unusable.

    A row whose `usage_json` is empty, malformed JSON, or a JSON value that is not an object counts
    as zero usage rather than as a read failure.
    """
    if isinstance(usage, Mapping):
        return usage
    if isinstance(usage, (str, bytes, bytearray)):
        try:
            decoded = json.loads(usage)
        except (ValueError, TypeError):
            return {}
        return decoded if isinstance(decoded, Mapping) else {}
    return {}


def normalize_run_usage(usage: Any) -> dict[str, int]:
    """Normalize one persisted run usage record into reported and derived token categories.

    `fresh_input_tokens` is clamped per run: a record claiming more cached input than input is
    inconsistent, and it must not subtract from another run's fresh count.
    """
    document = parse_usage_document(usage)
    counts = {field: coerce_token_count(document.get(field)) for field in REPORTED_FIELDS}
    counts["fresh_input_tokens"] = max(0, counts["input_tokens"] - counts["cached_input_tokens"])
    counts["raw_total_tokens"] = counts["input_tokens"] + counts["output_tokens"]
    return counts


def budget_used_tokens(usage: Any) -> int:
    """Return the tokens of new work a task has consumed, which is what a token budget bounds.

    This is the single definition of the budgeted quantity, `fresh_input_tokens + output_tokens`.
    Enforcement and the reporting around it read it from here rather than restating the sum, so the
    basis cannot drift between the value that stops a task and the value `budget_basis` advertises.

    Excluded by construction:

    * `cached_input_tokens`, because a cached read is input the provider already had. It is inside
      `input_tokens` and outside `fresh_input_tokens`, so it consumes no budget at all.
    * `reasoning_output_tokens`, because it is a subset of `output_tokens` and adding it would
      charge reasoning twice.
    * `cache_write_input_tokens`, because the reported contract carries it inside `input_tokens`,
      so the fresh-input term already accounts for it. Adding it separately would double-count.

    Accepts an `aggregate_token_usage` total or a single `normalize_run_usage` record. A record that
    reports the categories without the derived field is counted from its own input minus its cached
    subset, so a missing derivation can never silently reduce the count to output alone.
    """
    counts = parse_usage_document(usage)
    fresh = counts.get("fresh_input_tokens")
    if fresh is None:
        fresh = max(
            0,
            coerce_token_count(counts.get("input_tokens"))
            - coerce_token_count(counts.get("cached_input_tokens")),
        )
    return coerce_token_count(fresh) + coerce_token_count(counts.get("output_tokens"))


def cache_hit_ratio(cached_input_tokens: int, input_tokens: int) -> float:
    """Return cached input as a fraction of reported input.

    A task with no reported input reports `0.0` rather than dividing by zero, and inconsistent data
    cannot report a ratio outside `[0.0, 1.0]`.
    """
    if input_tokens <= 0:
        return 0.0
    return round(min(1.0, max(0.0, cached_input_tokens / input_tokens)), 6)


def aggregate_token_usage(usages: Iterable[Any]) -> dict[str, Any]:
    """Aggregate persisted run usage records into one categorized task total.

    `total_tokens` keeps its established meaning — the raw `input_tokens + output_tokens` sum, equal
    to `raw_total_tokens` — so every existing consumer and the schema 1.0 projection observe exactly
    the value they observed before. That sum is traffic, reported for diagnostics; the budgeted
    quantity is `budget_used_tokens`, which `budget_basis` names.
    """
    totals = dict.fromkeys(REPORTED_FIELDS + DERIVED_FIELDS, 0)
    for usage in usages:
        for field, value in normalize_run_usage(usage).items():
            totals[field] += value
    return {
        "input_tokens": totals["input_tokens"],
        "cached_input_tokens": totals["cached_input_tokens"],
        "fresh_input_tokens": totals["fresh_input_tokens"],
        "cache_write_input_tokens": totals["cache_write_input_tokens"],
        "output_tokens": totals["output_tokens"],
        "reasoning_output_tokens": totals["reasoning_output_tokens"],
        "raw_total_tokens": totals["raw_total_tokens"],
        "total_tokens": totals["raw_total_tokens"],
        "cache_hit_ratio": cache_hit_ratio(totals["cached_input_tokens"], totals["input_tokens"]),
        "accounting_basis": ACCOUNTING_BASIS,
        "budget_basis": BUDGET_BASIS,
    }


def empty_token_usage() -> dict[str, Any]:
    """Return the categorized usage of a task that has no completed run yet."""
    return aggregate_token_usage(())
