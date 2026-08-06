"""Span accounting for a supervised agent canary, and the canary harness that produces it.

The problem this exists to solve: a Collector run can report more spans accepted by the receiver
than sent by the exporter, and the two obvious explanations -- `filter/unredactable` deliberately
dropping a span it cannot redact, and a batch that had not flushed when the counters were read --
produce *identical* numbers. Reading the counters once, by hand, cannot tell them apart, and the
thing that would tell them apart directly is printing the missing spans, which the privacy policy
forbids.

So this module reads four numbers instead of two, and refuses to call a run clean unless they add up:

    receiver accepted  =  exporter sent  +  deliberately filtered  +  refused  +  send-failed
                          + unresolved remainder

A nonzero unresolved remainder fails the canary. It is the only honest verdict available: something
left the pipeline for a reason nothing in the run accounts for.

Alongside the totals, the bounded span-name diagnostic in :mod:`otel_certification` reports the same
two ends broken down by an allowlisted span-name vocabulary, so a remainder can be attributed to a
bucket without exporting anything from the spans themselves.

**This module never starts Claude, Codex, or a Forge task.** It starts a Collector and then calls an
`agent` callable supplied by the caller. The test suite supplies one that posts synthetic OTLP. The
command-line entry point can supply one that launches a real agent, and refuses to do so without an
explicit `--approved-by` argument naming the human who approved that run.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import otel_certification as certification

# The debug exporter's own descriptor fields. Every other `-> key:` line inside a diagnostic record
# would be an attribute, which the diagnostic must never carry.
DESCRIPTOR_FIELDS = frozenset(
    {"Name", "Description", "Unit", "DataType", "IsMonotonic", "AggregationTemporality"}
)

COUNT_CONNECTOR_SCOPE = "connector/countconnector"

# Strings whose presence anywhere in the exporter output means an unredactable carrier reached it.
UNREDACTABLE_MARKERS = ("Link #", "Exemplar")


@dataclass(frozen=True)
class SpanAccounting:
    """Every span the receiver accepted, and where each of them went."""

    accepted: int
    exported: int
    filtered: int
    refused: int
    send_failed: int
    accepted_by_name: Mapping[str, int] = field(default_factory=dict)
    exported_by_name: Mapping[str, int] = field(default_factory=dict)
    settled: bool = True

    @property
    def unresolved(self) -> int:
        """Spans the receiver took in that nothing in this run explains the fate of."""
        return self.accepted - self.exported - self.filtered - self.refused - self.send_failed

    @property
    def balanced(self) -> bool:
        return self.unresolved == 0

    @property
    def diagnostic_disagreements(self) -> tuple[str, ...]:
        """Where the bounded diagnostic and the Collector's own counters do not agree.

        Two independent measurements of the same two numbers: the connectors count spans, the
        Collector counts them again in its receiver and exporter obsreport. A disagreement means one
        of them is not measuring what this report claims it is, and the breakdown cannot be trusted
        to attribute a remainder.
        """
        found = []
        for label, total, by_name in (
            ("accepted", self.accepted, self.accepted_by_name),
            ("exported", self.exported, self.exported_by_name),
        ):
            counted = sum(by_name.values())
            if counted != total:
                found.append(f"{label}: counter says {total}, diagnostic says {counted}")
        return tuple(found)

    def missing_by_name(self) -> dict[str, int]:
        """Per bucket, how many spans the certified pipeline did not export.

        This does not say *why*. `filtered` says how many were dropped deliberately; when the two
        agree, the buckets here name which spans those were.
        """
        buckets = set(self.accepted_by_name) | set(self.exported_by_name)
        missing = {
            bucket: self.accepted_by_name.get(bucket, 0) - self.exported_by_name.get(bucket, 0)
            for bucket in buckets
        }
        return {bucket: count for bucket, count in sorted(missing.items()) if count}


@dataclass(frozen=True)
class PrivacyCheck:
    """The result of inspecting what the exporter actually printed.

    This runs on every path, including the failing ones, because a run that crashed is exactly when
    nobody looks -- and it is a run whose output nobody has cleared.
    """

    checks: tuple[str, ...]
    findings: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.findings


@dataclass(frozen=True)
class CanaryReport:
    accounting: SpanAccounting | None
    privacy: PrivacyCheck
    collector_returncode: int | None
    agent_returncode: int | None
    errors: tuple[str, ...] = ()
    # What the privacy check was run against. Kept so a reviewer can re-derive the check's verdict
    # from the same bytes rather than trusting it; `render` never prints it.
    output: str = ""

    @property
    def passed(self) -> bool:
        return (
            not self.errors
            and self.accounting is not None
            and self.accounting.settled
            and self.accounting.balanced
            and not self.accounting.diagnostic_disagreements
            and self.privacy.passed
            and self.collector_returncode == 0
        )

    def render(self) -> str:
        """The full report. The privacy section is unconditional; there is no path that omits it."""
        lines = ["SPAN ACCOUNTING"]
        if self.accounting is None:
            lines.append("  unavailable - the run did not reach a countable state")
        else:
            accounting = self.accounting
            lines += [
                f"  receiver accepted      {accounting.accepted}",
                f"  exporter sent          {accounting.exported}",
                f"  deliberately filtered  {accounting.filtered}",
                f"  receiver refused       {accounting.refused}",
                f"  exporter send-failed   {accounting.send_failed}",
                f"  unresolved remainder   {accounting.unresolved}",
                f"  counts settled         {'yes' if accounting.settled else 'NO'}",
            ]
            for stage in certification.DIAGNOSTIC_STAGES:
                counts = accounting.accepted_by_name if stage == "accepted" else accounting.exported_by_name
                rendered = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
                lines.append(f"  by name, {stage:<8}      {rendered}")
            missing = accounting.missing_by_name()
            lines.append(
                "  not exported, by name  "
                + (", ".join(f"{k}={v}" for k, v in missing.items()) or "none")
            )
            for disagreement in accounting.diagnostic_disagreements:
                lines.append(f"  DISAGREEMENT           {disagreement}")

        lines.append("")
        lines.append("PRIVACY CHECK")
        for check in self.privacy.checks:
            lines.append(f"  ran  {check}")
        if self.privacy.findings:
            for finding in self.privacy.findings:
                lines.append(f"  FAIL {finding}")
        else:
            lines.append("  pass no finding on any check above")

        lines.append("")
        lines.append("PROCESSES")
        lines.append(f"  collector exit {self.collector_returncode}")
        lines.append(f"  agent exit     {self.agent_returncode}")
        for error in self.errors:
            lines.append(f"  ERROR {error}")

        lines.append("")
        lines.append(f"VERDICT {'PASS' if self.passed else 'FAIL'}")
        return "\n".join(lines)


def check_privacy(output: str) -> PrivacyCheck:
    """Inspect exporter output for anything the diagnostic is forbidden to carry.

    Every check is named in the result whether or not it found something, so a report cannot look
    clean because a check silently did not run.
    """
    checks = (
        "output was captured",
        "no forbidden attribute key printed",
        "no diagnostic metric outside the bounded vocabulary",
        "no attribute of any kind inside a diagnostic record",
        "no span link or metric exemplar reached the exporter",
    )
    findings: list[str] = []

    if not output.strip():
        findings.append("no Collector output was captured, so nothing could be cleared")
        return PrivacyCheck(checks=checks, findings=tuple(findings))

    for key in certification.FORBIDDEN_ATTRIBUTE_KEYS:
        if f"-> {key}:" in output:
            findings.append(f"forbidden attribute key printed: {key}")

    for name in certification.unknown_diagnostic_metric_names(output):
        findings.append(f"diagnostic metric outside the vocabulary: {name}")

    for record in _records(output):
        if COUNT_CONNECTOR_SCOPE not in record:
            continue
        for line in record.splitlines():
            stripped = line.strip()
            if not stripped.startswith("->"):
                continue
            field_name = stripped[2:].split(":", 1)[0].strip()
            if field_name not in DESCRIPTOR_FIELDS:
                findings.append(f"attribute inside a diagnostic record: {field_name}")

    for marker in UNREDACTABLE_MARKERS:
        if marker in output:
            findings.append(f"unredactable carrier reached the exporter: {marker.strip()}")

    return PrivacyCheck(checks=checks, findings=tuple(dict.fromkeys(findings)))


def _records(output: str) -> list[str]:
    """Split Collector output into log records. Each detailed exporter dump is one record."""
    records: list[str] = []
    current: list[str] = []
    for line in output.splitlines():
        if line[:4].isdigit() and line[4:5] == "-" and current:
            records.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        records.append("\n".join(current))
    return records


def read_accounting(port: int, output: str, *, settled: bool = True) -> SpanAccounting:
    """Read the five totals from the Collector's internal telemetry, plus the bounded breakdown."""
    series = certification.scrape_internal_metrics(port)
    counts = certification.parse_diagnostic_counts(output)
    return SpanAccounting(
        accepted=certification.counter_total(series, certification.COUNTER_ACCEPTED),
        exported=certification.counter_total(series, certification.COUNTER_SENT, exporter="debug"),
        # Only the contract's own filter counts as deliberate. The diagnostic's vocabulary filter
        # runs on metrics and must never be allowed to explain away a missing span.
        filtered=certification.counter_total(
            series, certification.COUNTER_FILTERED, filter=certification.UNREDACTABLE_FILTER
        ),
        refused=certification.counter_total(series, certification.COUNTER_REFUSED),
        send_failed=certification.counter_total(
            series, certification.COUNTER_SEND_FAILED, exporter="debug"
        ),
        accepted_by_name=dict(counts["accepted"]),
        exported_by_name=dict(counts["exported"]),
        settled=settled,
    )


def _totals(port: int) -> tuple[int, ...]:
    series = certification.scrape_internal_metrics(port)
    return (
        certification.counter_total(series, certification.COUNTER_ACCEPTED),
        certification.counter_total(series, certification.COUNTER_SENT, exporter="debug"),
        certification.counter_total(
            series, certification.COUNTER_FILTERED, filter=certification.UNREDACTABLE_FILTER
        ),
        certification.counter_total(series, certification.COUNTER_REFUSED),
        certification.counter_total(series, certification.COUNTER_SEND_FAILED, exporter="debug"),
    )


def wait_until_settled(
    port: int,
    *,
    batch_timeout: float,
    poll_seconds: float = 0.5,
    stable_polls: int = 4,
    timeout: float = 180.0,
) -> bool:
    """Block until the counters stop moving, or give up and say so.

    `otelcol_exporter_sent_spans` does not exist as a series at all until the first batch flushes,
    so an early read reports zero exported and an apparent loss of every span. Waiting one batch
    interval is not enough either: the last batch of a run starts its own interval. This waits for
    the totals to be unchanged across `stable_polls` consecutive reads *and* for at least one full
    batch interval to have passed since the last change.
    """
    deadline = time.monotonic() + timeout
    previous: tuple[int, ...] | None = None
    unchanged = 0
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        try:
            current = _totals(port)
        except (urllib.error.URLError, OSError, TimeoutError):
            current = None
        if current is not None:
            if current == previous:
                unchanged += 1
            else:
                unchanged = 0
                last_change = time.monotonic()
                previous = current
            quiet_for = time.monotonic() - last_change
            if unchanged >= stable_polls and quiet_for >= batch_timeout + 1.0:
                return True
        time.sleep(poll_seconds)
    return False


def batch_timeout_seconds(config: dict) -> float:
    """The configured batch interval, in seconds. Falls back to the Collector's own default."""
    raw = str(((config.get("processors") or {}).get("batch") or {}).get("timeout", "200ms")).strip()
    if raw.endswith("ms"):
        return float(raw[:-2]) / 1000.0
    if raw.endswith("m"):
        return float(raw[:-1]) * 60.0
    if raw.endswith("s"):
        return float(raw[:-1])
    return float(raw)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def otlp_payload_agent(payloads: Sequence[tuple[str, dict]]) -> Callable[[dict, Path], int]:
    """An agent that posts synthetic OTLP over loopback. This is what the test suite uses."""

    def agent(ports: dict, workdir: Path) -> int:
        for path, body in payloads:
            request = urllib.request.Request(
                f"http://127.0.0.1:{ports['http']}{path}",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=30):
                    pass
            except urllib.error.HTTPError:
                # A refusal is a result the accounting should show, not an exception to hide.
                pass
        return 0

    return agent


def subprocess_agent(
    argv: Sequence[str], environment: Mapping[str, str], *, timeout: float = 900.0
) -> Callable[[dict, Path], int]:
    """An agent that launches a real child process and always reaps it.

    Nothing in this module builds a Claude command line or a Claude environment. Both are the
    caller's, and the caller is the command-line entry point below, which will not assemble either
    without a named human approval.
    """

    def agent(ports: dict, workdir: Path) -> int:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        log = (workdir / "agent.log").open("w", encoding="utf-8", errors="replace")
        process = subprocess.Popen(
            list(argv),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(workdir),
            env=dict(environment),
            creationflags=flags,
        )
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return _terminate(process)
        finally:
            if process.poll() is None:
                _terminate(process)
            log.close()

    return agent


def _terminate(process: subprocess.Popen) -> int:
    if process.poll() is not None:
        return process.returncode
    if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
        with suppress(OSError):
            os.kill(process.pid, signal.CTRL_BREAK_EVENT)
    else:  # pragma: no cover - the canary is supervised on Windows
        with suppress(OSError):
            process.terminate()
    try:
        return process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=15)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def run_canary(
    *,
    binary: Path,
    workdir: Path,
    agent: Callable[[dict, Path], int | None],
    config: dict | None = None,
    startup_timeout: float = 60.0,
    settle_timeout: float = 180.0,
) -> CanaryReport:
    """Start a Collector, run ``agent`` against it, and account for every span it accepted.

    ``config`` is the complete Collector configuration to run. Omit it for the shipped configuration
    plus the bounded diagnostic, which is what a canary uses; supply one to run a deliberately
    weakened variant, which is what the negative controls do.

    The Collector is stopped and reaped on every path, including a failing agent, a Collector that
    never became ready, and an exception from anywhere in the body. The report is assembled in a
    `finally` block, so the privacy check runs and is included even when the run failed -- a run
    that crashed is precisely the one whose output nobody has cleared.
    """
    # `config` is the complete configuration to run, already carrying whatever diagnostic it needs.
    # Rebuilding the diagnostic over a supplied configuration would silently undo the very mutation
    # a negative control is trying to make.
    effective = (
        certification.diagnostic_configuration(certification.load_configuration())
        if config is None
        else config
    )
    batch_timeout = batch_timeout_seconds(effective)
    rebound, ports = certification.temporary_ports(effective)

    workdir.mkdir(parents=True, exist_ok=True)
    config_path = certification.write_configuration(rebound, workdir / "canary-config.yaml")
    log_path = workdir / "collector.log"

    errors: list[str] = []
    accounting: SpanAccounting | None = None
    agent_returncode: int | None = None
    collector_returncode: int | None = None
    settled = False

    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with log_path.open("w", encoding="utf-8", errors="replace") as sink:
        process = subprocess.Popen(
            [str(binary), "--config", str(config_path)],
            stdout=sink,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(workdir),
            creationflags=flags,
        )
        try:
            if not certification.wait_for_marker(
                log_path, "Everything is ready", process, timeout=startup_timeout
            ):
                raise certification.CollectorFailed(
                    f"the Collector never reported readiness (exit {process.poll()})"
                )
            try:
                agent_returncode = agent(ports, workdir)
            except Exception as error:  # noqa: BLE001 - recorded, never swallowed
                errors.append(f"agent raised {type(error).__name__}: {error}")

            settled = wait_until_settled(
                ports["internal"], batch_timeout=batch_timeout, timeout=settle_timeout
            )
            if not settled:
                errors.append("the counters never settled, so no total here is final")
            try:
                # Read the totals before shutdown: the endpoint stops answering once the Collector
                # exits. The wait above is what makes this read a terminal one rather than a sample.
                accounting = read_accounting(
                    ports["internal"],
                    log_path.read_text(encoding="utf-8", errors="replace"),
                    settled=settled,
                )
            except (urllib.error.URLError, OSError, TimeoutError) as error:
                errors.append(f"internal telemetry could not be read: {error}")
        except Exception as error:  # noqa: BLE001 - the report is the deliverable, not a traceback
            errors.append(f"{type(error).__name__}: {error}")
        finally:
            collector_returncode = certification.stop_collector(process)

    output = log_path.read_text(encoding="utf-8", errors="replace")
    if accounting is not None:
        # Shutdown flushes the final batch, so the by-name breakdown is re-read from the complete
        # log. The totals stay as read while the endpoint was alive.
        counts = certification.parse_diagnostic_counts(output)
        accounting = SpanAccounting(
            accepted=accounting.accepted,
            exported=accounting.exported,
            filtered=accounting.filtered,
            refused=accounting.refused,
            send_failed=accounting.send_failed,
            accepted_by_name=dict(counts["accepted"]),
            exported_by_name=dict(counts["exported"]),
            settled=accounting.settled,
        )

    return CanaryReport(
        accounting=accounting,
        privacy=check_privacy(output),
        collector_returncode=collector_returncode,
        agent_returncode=agent_returncode,
        errors=tuple(errors),
        output=output,
    )


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

APPROVAL_REQUIRED = (
    "Refusing to launch an agent. A canary is a supervised event: re-run with --approved-by naming "
    "the human who approved this run. Without it this harness starts nothing but the Collector."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="otel_canary",
        description=(
            "Run one supervised agent canary against the local Collector and account for every span "
            "it accepted. Launches nothing without --approved-by."
        ),
    )
    parser.add_argument("--workdir", type=Path, required=True, help="where the run writes its files")
    parser.add_argument(
        "--agent-command",
        default="",
        help="the agent command line to run, as a single quoted string",
    )
    parser.add_argument(
        "--approved-by",
        default="",
        help="the human who approved this supervised run; required before anything is launched",
    )
    parser.add_argument(
        "--invocation-id",
        default="",
        help="forge.invocation.id for the child; a fresh random one is used when omitted",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.agent_command and not arguments.approved_by:
        print(APPROVAL_REQUIRED, file=sys.stderr)
        return 2

    sys.path.insert(0, str(certification.REPO_ROOT / "src"))
    from codex_forge import agent_telemetry

    try:
        binary = certification.collector_binary()
        installed = certification.collector_version(binary)
    except (certification.CollectorUnavailable, certification.CollectorFailed) as error:
        print(error, file=sys.stderr)
        return 2
    if installed != certification.CERTIFIED_COLLECTOR_VERSION:
        print(
            f"Collector {installed} is not the certified {certification.CERTIFIED_COLLECTOR_VERSION}; "
            "re-run Gate 3 before a canary.",
            file=sys.stderr,
        )
        return 2

    def agent(ports: dict, workdir: Path) -> int | None:
        if not arguments.agent_command:
            return None
        context = agent_telemetry.InvocationContext(
            invocation_id=arguments.invocation_id or agent_telemetry.new_invocation_id(),
            contract_version=agent_telemetry.AGENT_TELEMETRY_CONTRACT_VERSION,
            traceparent=None,
        )
        environment = agent_telemetry.build_claude_child_environment(
            os.environ,
            context,
            endpoint=f"http://127.0.0.1:{ports['grpc']}",
        )
        return subprocess_agent(shlex.split(arguments.agent_command), environment)(ports, workdir)

    report = run_canary(binary=binary, workdir=arguments.workdir, agent=agent)
    print(report.render())
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
