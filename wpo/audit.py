"""`wpo audit`: the weekly planning audit of every open WP.

The manual audit of 2026-11-01 found open WPs that restated done ones, owned the same files as WPs
they could run beside, depended on blocked WPs, ran forbidden Verify commands and carried stale
Context. The mechanical classes are `wpo lint`'s checks; this command runs them over every open WP
into a dated report, then hands the judgment classes (work a done WP's Handoff already delivered,
contradictions with the decisions log and the instructions file, criteria that contradict each other
across WPs or cannot be checked, oversize WPs, stale Context) to a read-only review session on a
trusted route, appends its answer to the report and journals `audit-done`, which wakes the
orchestrator. Explicit standalone scheduling supports Windows tasks and Linux user timers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from . import wpfile
from .context import Context
from .lint import LINT_OPEN_STATUSES, Finding, counts_by_check, lint_all, summary_lines
from .orchestrator import (
    EXIT_NO_ROUTE,
    NoTrustedFamilyError,
    dispatch_session,
    trusted_families,
)
from .session import NoRouteError, describe_next
from .similarity import SimilarityIndex

AUDITS_DIRECTORY = "audits"
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
SCHEDULE_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# schtasks refuses a /TR command longer than this.
SCHTASKS_COMMAND_LIMIT = 261
TASK_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
# Pairs listed in the report: the similarity threshold's pairs with an open side, up to this many.
REPORT_PAIR_LIMIT = 40


class AuditError(Exception):
    """An audit that cannot run as configured (a bad schedule, no trusted route family)."""


def report_path(runs_dir: Path, day: date) -> Path:
    """`<runs>/audits/YYYY-MM-DD.md`; a second audit on one day gets `-2`, `-3`, ... so none is overwritten."""
    directory = runs_dir / AUDITS_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{day.isoformat()}.md"
    number = 2
    while candidate.exists():
        candidate = directory / f"{day.isoformat()}-{number}.md"
        number += 1
    return candidate


def open_pairs(similarity: SimilarityIndex, wps: dict[str, wpfile.WorkPackage], threshold: float) -> list[tuple[str, str, float]]:
    """Pairs at or above the threshold with at least one open WP, most similar first."""
    open_ids = [wp_id for wp_id, wp in wps.items() if wp.status in LINT_OPEN_STATUSES]
    return similarity.pairs_above(threshold, involving=open_ids)


def mechanical_report(project_name: str, started: datetime, findings: list[Finding], wps: dict[str, wpfile.WorkPackage],
                      pairs: list[tuple[str, str, float]], threshold: float | None) -> str:
    """The report's lint part: counts by check, the findings grouped by check, the similar pairs."""
    open_wps = sorted((wp for wp in wps.values() if wp.status in LINT_OPEN_STATUSES), key=lambda wp: wp.id)
    lines = [f"# Planning audit of {project_name}, {started:%Y-%m-%d %H:%M}", "",
             f"{len(open_wps)} open WPs (todo, in flight or blocked) of {len(wps)}. `wpo lint` over every open WP:", "",
             *(f"- {line}" for line in summary_lines(findings)), "", "## Lint findings", ""]
    if not findings:
        lines += ["None.", ""]
    for check in counts_by_check(findings):
        lines += [f"### {check}", ""]
        lines += [f"- **{finding.wp_id}** {finding.severity}: {finding.message}" for finding in findings if finding.check == check]
        lines.append("")
    lines += ["## Most similar pairs", ""]
    if threshold is None:
        lines += ["The similarity check is off (`lint.similarity_warn` is null).", ""]
    elif not pairs:
        lines += [f"No pair with an open WP reaches {threshold:.2f}.", ""]
    else:
        lines += [f"TF-IDF cosine at or above {threshold:.2f}, with at least one open WP"
                  + (f" (the first {REPORT_PAIR_LIMIT} of {len(pairs)})" if len(pairs) > REPORT_PAIR_LIMIT else "") + ":", "",
                  "| score | WP | status | WP | status |", "|---|---|---|---|---|"]
        for first, second, score in pairs[:REPORT_PAIR_LIMIT]:
            lines.append(f"| {score:.2f} | {first} {wps[first].title} | {wps[first].status} | {second} {wps[second].title} | {wps[second].status} |")
        lines.append("")
    lines += ["## Open WPs", ""]
    lines += [f"- {wp.id} [{wp.status}, lane {wp.lane or '-'}, {wp.milestone}] {wp.title}: `{wp.path.as_posix()}`" for wp in open_wps]
    return "\n".join(lines) + "\n"


def review_prompt(context: Context, report_file: Path, report_text: str) -> str:
    """The judgment half of the audit, for a read-only session; its answer is appended to the report."""
    project = context.project
    context_files = [str(project.root / name) for name in project.config["audit"]["context_files"]]
    reading = "\n".join(f"- {path}" for path in context_files) or "- (no audit.context_files configured)"
    return f"""You are auditing the open work packages (WPs) of {project.name} for the planning defects a mechanical lint cannot find.
You are read-only: change no file and run nothing. Your answer is appended to the audit report {report_file}.

Read first, from the main checkout:
{reading}

WP files are markdown with front matter in {project.root / project.wp_dir_relative}. The report below lists every open WP with the
path of its live copy (a WP in flight is read from its worktree). Done WPs are in the same directory; each one's `## Handoff`
says what it delivered and what it left. The report's lint findings and similar pairs are already known: do not repeat them,
but use the pairs and overlaps as leads.

For every open WP, judge:
1. Duplicates: work a done WP's Handoff says is delivered, or that another open WP also plans. Name both WPs and the overlap.
2. Contradictions with the decisions log or the project's rules (AGENTS.md and the files above): quote both sides.
3. Contradictory criteria across WPs: two WPs asking for incompatible results on the same thing.
4. Criteria that cannot be checked: no deterministic test, no tolerance, judged by eye, or a quantity nobody measures.
5. Oversize WPs: more than one builder can finish and a reviewer can judge in a few rounds; propose the split.
6. Stale Context: facts the code or a merged WP has since changed (name the file, symbol or WP that shows it).

Answer in markdown. One `### WP-xxxx` heading per WP that has findings (leave clean WPs out); each finding one bullet: the
class (1-6), the evidence (quoted, with file and section), and the change you propose to its WP file. End with `### Summary`:
the findings ranked by what they cost if left (a wasted round, a wrong merge, a stalled chain).

The mechanical report:

{report_text}"""


def run_audit(context: Context, mechanical_only: bool, today: date | None = None) -> int:
    """Writes the dated report; unless `mechanical_only`, runs the review session, appends its
    answer and journals audit-done. Prints the report's path."""
    started = datetime.now()
    try:
        findings, similarity, wps = lint_all(context, [])
        threshold = context.project.config["lint"]["similarity_warn"]
        pairs = open_pairs(similarity, wps, threshold) if similarity is not None else []
        path = report_path(context.project.runs_dir, today or started.date())
        text = mechanical_report(context.project.name, started, findings, wps, pairs, threshold)
        path.write_text(text, encoding="utf-8")
    except Exception as error:
        # A scheduled audit has nobody watching its console: the journal is where a crash is seen.
        context.journal.emit("audit-failed", f"audit: the lint part failed: {type(error).__name__}: {error}")
        raise
    counts = "; ".join(summary_lines(findings))
    print(f"lint: {counts}\nreport: {path}")
    if mechanical_only:
        return 0
    spec = context.project.config["audit"]["spec"]
    run_dir = path.with_suffix("")
    try:
        families = trusted_families(context)
        result = dispatch_session(context, spec, review_prompt(context, path, text), "read", [], families, context.project.root,
                                  run_dir, {"audit": path.stem})
    except NoRouteError as error:
        with path.open("a", encoding="utf-8") as report:
            report.write(f"\n## Judgment review\n\nNot run: no route for {spec} until {describe_next(error.next_available_at)} ({error}).\n")
        context.journal.emit("audit-done", f"audit {path.stem}: lint {counts}; the judgment review did not run (no route for {spec}); report {path}",
                             {"report": str(path), "review": "no-route"})
        print(f"NO ROUTE for {spec}; the report has the lint part only")
        return EXIT_NO_ROUTE
    except NoTrustedFamilyError as error:
        context.journal.emit("audit-failed", f"audit {path.stem}: {error}; the lint part is in {path}", {"report": str(path)})
        print(f"{error}: the judgment review cannot run")
        return 1
    except Exception as error:
        context.journal.emit("audit-failed", f"audit {path.stem}: {type(error).__name__}: {error}; the lint part is in {path}",
                             {"report": str(path)})
        raise
    with path.open("a", encoding="utf-8") as report:
        report.write(f"\n## Judgment review\n\nBy {result.route['id']}{result.fallback_note}; session logs in `{run_dir}`.\n\n{result.result_text.strip()}\n")
    context.journal.emit("audit-done", f"audit {path.stem}: lint {counts}; judgment review by {result.route['id']}; report {path}",
                         {"report": str(path), "review": "done", "route": result.route["id"]})
    print(f"judgment review by {result.route['id']} appended")
    return 0


def task_name(context: Context) -> str:
    """Stable machine-local identity; equally named projects never share a schedule."""
    root = os.path.normcase(str(context.project.root.resolve()))
    digest = hashlib.sha256(root.encode()).hexdigest()[:16]
    name = TASK_NAME_UNSAFE.sub("-", context.project.name).strip("-")[:48] or "project"
    return f"wpo-audit-{name}-{digest}"


def schedule_time(context: Context) -> tuple[str, str]:
    schedule = context.project.config["audit"]["schedule"]
    day, time = schedule.get("day"), schedule.get("time")
    if day not in WEEKDAYS:
        raise AuditError(f"audit.schedule.day '{day}' is not one of {', '.join(WEEKDAYS)}")
    if not isinstance(time, str) or not SCHEDULE_TIME.fullmatch(time):
        raise AuditError(f"audit.schedule.time '{time}' is not a local time HH:MM")
    return day, time


def audit_command(context: Context) -> list[str]:
    command = [*context.machine.wpo_command(), "--project", str(context.project.root), "audit", "--from-os-schedule"]
    for argument in command:
        if any(ord(character) < 32 or ord(character) == 127 for character in argument):
            raise AuditError("schedule command arguments cannot contain control characters")
    return command


def schedule_arguments(context: Context) -> list[str]:
    """The `schtasks /Create` call registering the weekly audit for this project root."""
    day, time = schedule_time(context)
    command = subprocess.list2cmdline(audit_command(context))
    if len(command) > SCHTASKS_COMMAND_LIMIT:
        raise AuditError(f"the task's command is {len(command)} characters, over schtasks' {SCHTASKS_COMMAND_LIMIT}: {command}")
    return ["schtasks", "/Create", "/TN", task_name(context), "/SC", "WEEKLY", "/D", day, "/ST", time, "/TR", command, "/F"]


def remove_arguments(context: Context) -> list[str]:
    return ["schtasks", "/Delete", "/TN", task_name(context), "/F"]


def systemd_quote(value: str, *, command: bool = False) -> str:
    """Quote one systemd token, including specifier and ExecStart variable expansion."""
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise AuditError("systemd values cannot contain control characters")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return f'"{value}"'


@dataclass(frozen=True)
class SchedulePlan:
    directory: Path
    name: str
    files: dict[str, str]
    commands: list[list[str]]

    @property
    def receipt(self) -> Path:
        return self.directory / f".{self.name}.json"


def linux_plan(context: Context, install: bool) -> SchedulePlan:
    configured = os.environ.get("XDG_CONFIG_HOME")
    directory = Path(configured).expanduser() if configured else Path.home() / ".config"
    if not directory.is_absolute():
        raise AuditError("XDG_CONFIG_HOME must be an absolute path")
    directory = directory / "systemd" / "user"
    name = task_name(context)
    timer = f"{name}.timer"
    files = {}
    if install:
        day, time = schedule_time(context)
        command = audit_command(context)
        if not Path(command[0]).is_absolute():
            raise AuditError("configure machine.python as an absolute interpreter path for systemd")
        files = {
            f"{name}.service": (
                "# Managed by wpo audit; do not edit without removing the schedule first.\n"
                "[Unit]\nDescription=WPO planning audit\n\n[Service]\nType=oneshot\n"
                f"WorkingDirectory={systemd_quote(str(context.project.root))}\n"
                f"ExecStart={' '.join(systemd_quote(part, command=True) for part in command)}\n"
            ),
            timer: (
                "# Managed by wpo audit; do not edit without removing the schedule first.\n"
                "[Unit]\nDescription=Weekly WPO planning audit\n\n[Timer]\n"
                f"OnCalendar={day.title()} *-*-* {time}:00\nPersistent=true\nUnit={name}.service\n"
                "\n[Install]\nWantedBy=timers.target\n"
            ),
        }
        machine_config = os.environ.get("WPO_MACHINE_CONFIG")
        if machine_config:
            files[f"{name}.service"] += f"Environment={systemd_quote(f'WPO_MACHINE_CONFIG={Path(machine_config).expanduser().resolve()}')}\n"
        commands = [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", timer]]
    else:
        commands = [["systemctl", "--user", "disable", "--now", timer],
                    ["systemctl", "--user", "clean", "--what=state", timer], ["systemctl", "--user", "daemon-reload"]]
    return SchedulePlan(directory, name, files, commands)


def run_schedule_command(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, errors="replace", check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AuditError(f"cannot run {arguments[0]}: {error}; see references/scheduling.md") from error
    if check and result.returncode:
        raise AuditError(f"{shlex.join(arguments)} failed ({result.returncode}): {(result.stdout + result.stderr).strip()}")
    return result


def write_schedule_file(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise AuditError(f"refusing schedule symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as temporary:
        temporary.write(data)
        temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def schedule_receipt(context: Context, plan: SchedulePlan) -> dict | None:
    if plan.receipt.is_symlink():
        raise AuditError(f"refusing schedule receipt symlink: {plan.receipt}")
    if not plan.receipt.exists():
        return None
    try:
        receipt = json.loads(plan.receipt.read_text(encoding="utf-8"))
        if receipt["project"] != str(context.project.root.resolve()) or receipt["name"] != plan.name or receipt["version"] != 1:
            raise ValueError("ownership mismatch")
        return receipt
    except (ValueError, KeyError, TypeError) as error:
        raise AuditError(f"invalid schedule ownership receipt {plan.receipt}: {error}") from error


def backup_schedule(plan: SchedulePlan, snapshot: dict[str, bytes | None]) -> None:
    saved = {name: data for name, data in snapshot.items() if data is not None}
    if not saved:
        return
    parent = plan.directory / ".wpo-schedule-backups"
    if parent.is_symlink() or (parent / plan.name).is_symlink():
        raise AuditError("refusing schedule backup symlink")
    directory = parent / plan.name / datetime.now().strftime("%Y%m%dT%H%M%S%f")
    for name, data in saved.items():
        write_schedule_file(directory / name, data)
    print(f"schedule backup: {directory}")


def restore_schedule(plan: SchedulePlan, snapshot: dict[str, bytes | None]) -> None:
    for name, data in snapshot.items():
        path = plan.directory / name
        if data is None:
            path.unlink(missing_ok=True)
        else:
            write_schedule_file(path, data)


def change_linux_schedule(context: Context, plan: SchedulePlan, install: bool) -> int:
    names = [f"{plan.name}.service", f"{plan.name}.timer"]
    paths = [*(plan.directory / name for name in names), plan.receipt]
    if any(path.is_symlink() for path in paths):
        raise AuditError("refusing schedule symlinks; inspect the user unit directory manually")
    receipt = schedule_receipt(context, plan)
    snapshot = {path.name: path.read_bytes() if path.exists() else None for path in paths}
    if receipt is None:
        if any(snapshot[name] is not None for name in names):
            raise AuditError("unowned schedule files already exist; nothing overwritten")
        if not install:
            print("No WPO-owned schedule is installed; nothing removed.")
            return 0
    else:
        expected = receipt.get("files")
        actual = {name: hashlib.sha256(snapshot[name]).hexdigest() if snapshot[name] is not None else None for name in names}
        if expected != actual:
            raise AuditError("schedule files have drifted from their ownership receipt; nothing changed")
        if install and all(snapshot[name] == plan.files[name].encode() for name in names):
            print("Schedule already matches; nothing changed.")
            return 0
    timer = f"{plan.name}.timer"
    for name in names:
        listing = run_schedule_command(["systemctl", "--user", "list-unit-files", name, "--no-legend", "--no-pager"])
        if listing.stdout.strip():
            loaded = run_schedule_command(["systemctl", "--user", "show", "--property=FragmentPath", "--value", name])
            fragment = loaded.stdout.strip()
            if not fragment or Path(fragment).resolve() != (plan.directory / name).resolve():
                raise AuditError(f"another schedule shadows {name}: {fragment or '(no persistent path)'}; nothing changed")
    enabled = run_schedule_command(["systemctl", "--user", "is-enabled", timer], check=False).returncode == 0
    active = run_schedule_command(["systemctl", "--user", "is-active", timer], check=False).returncode == 0
    backup_schedule(plan, snapshot)
    try:
        if install:
            for name, text in plan.files.items():
                write_schedule_file(plan.directory / name, text.encode())
            run_schedule_command(plan.commands[0])
            run_schedule_command(plan.commands[1])
            receipt = {"version": 1, "project": str(context.project.root.resolve()), "name": plan.name,
                       "files": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in plan.files.items()}}
            write_schedule_file(plan.receipt, (json.dumps(receipt, indent=2) + "\n").encode())
        else:
            run_schedule_command(plan.commands[0])
            run_schedule_command(plan.commands[1])
            for path in paths:
                path.unlink()
            run_schedule_command(plan.commands[2])
    except (AuditError, OSError) as error:
        restore_schedule(plan, snapshot)
        recovery = [["systemctl", "--user", "daemon-reload"],
                    ["systemctl", "--user", "enable" if enabled else "disable", timer],
                    ["systemctl", "--user", "start" if active else "stop", timer]]
        failures = []
        for command in recovery:
            try:
                run_schedule_command(command)
            except AuditError as rollback_error:
                failures.append(str(rollback_error))
        suffix = f"; manager rollback needs attention: {'; '.join(failures)}" if failures else ""
        raise AuditError(f"schedule change failed; prior files restored: {error}{suffix}") from error
    return 0


def change_windows_schedule(context: Context, install: bool) -> int:
    if install and os.environ.get("WPO_MACHINE_CONFIG"):
        raise AuditError("Windows tasks cannot inherit WPO_MACHINE_CONFIG; use the default machine config or schedule a configured wrapper manually")
    directory = Path(context.machine.raw["state_dir"]).expanduser() / "schedules"
    plan = SchedulePlan(directory, task_name(context), {}, [])
    receipt = schedule_receipt(context, plan)
    query = ["schtasks", "/Query", "/TN", plan.name, "/XML"]
    current = run_schedule_command(query, check=False)
    old_xml = current.stdout if current.returncode == 0 else None
    if receipt is not None and (old_xml is None or receipt.get("xml_sha256") != hashlib.sha256(old_xml.encode()).hexdigest()):
        raise AuditError("scheduled task has drifted or is unreachable; nothing changed")
    if receipt is None and old_xml is not None:
        raise AuditError("an unowned scheduled task already exists; nothing overwritten or removed")
    if receipt is None and not install:
        print("No WPO-owned schedule is installed; nothing removed.")
        return 0
    arguments = schedule_arguments(context) if install else remove_arguments(context)
    if receipt and install and receipt.get("arguments") == arguments:
        print("Schedule already matches; nothing changed.")
        return 0
    snapshot = {plan.receipt.name: plan.receipt.read_bytes() if receipt else None, f"{plan.name}.xml": old_xml.encode() if old_xml else None}
    backup_schedule(plan, snapshot)
    changed = receipt is not None
    try:
        # Without a receipt /F is forbidden: even a failed Query cannot authorize replacing a task.
        run_schedule_command(arguments if receipt or not install else arguments[:-1])
        changed = True
        if install:
            installed = run_schedule_command(query).stdout
            document = {"version": 1, "project": str(context.project.root.resolve()), "name": plan.name,
                        "xml_sha256": hashlib.sha256(installed.encode()).hexdigest(), "arguments": arguments}
            write_schedule_file(plan.receipt, (json.dumps(document, indent=2) + "\n").encode())
        else:
            plan.receipt.unlink()
    except (AuditError, OSError) as error:
        rollback_error = None
        if changed:
            try:
                if old_xml is None:
                    run_schedule_command(remove_arguments(context))
                else:
                    directory.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(dir=directory) as temporary:
                        xml = Path(temporary) / "task.xml"
                        xml.write_text(old_xml, encoding="utf-16")
                        run_schedule_command(["schtasks", "/Create", "/TN", plan.name, "/XML", str(xml), "/F"])
            except (AuditError, OSError) as recovery_error:
                rollback_error = recovery_error
        restore_schedule(plan, {plan.receipt.name: snapshot[plan.receipt.name]})
        recovery = f"manager rollback needs attention: {rollback_error}" if rollback_error else "prior registration restored"
        raise AuditError(f"schedule change failed; {recovery}: {error}") from error
    return 0


def change_schedule(context: Context, install: bool, show_only: bool) -> int:
    """Plan without writes; change only explicitly selected standalone schedules."""
    if context.machine.execution_mode == "t3":
        day, time = schedule_time(context)
        guidance = (
            f"T3 owns scheduling: {'schedule' if install else 'remove'} the weekly audit for {context.project.root} "
            f"on {day} at {time} in this machine's local timezone using native schedule_task (or its native removal tool) "
            "in the existing repository coordinator thread. The task should run `wpo audit` and honor existing holds. "
            f"For installation use bindToCurrentThread=true and schedule={{type:'fixed_time',timeOfDay:'{time}',"
            f"weekdays:[{(WEEKDAYS.index(day) + 1) % 7}]}}. Retain the returned scheduledTaskId and report nextRunAt. "
            "For removal use delete_scheduled_task with that stored ID. "
            "No OS schedule or T3 task was created or removed."
        )
        print(guidance)
        if not show_only:
            raise AuditError("use native T3 scheduling in the repository thread; see references/scheduling.md")
        return 0
    if sys.platform not in ("win32", "linux"):
        raise AuditError(f"OS scheduling is unsupported on {sys.platform}; use native T3 scheduling or configure a scheduler manually")
    if sys.platform == "win32" and install and os.environ.get("WPO_MACHINE_CONFIG"):
        raise AuditError("Windows tasks cannot inherit WPO_MACHINE_CONFIG; use the default machine config or schedule a configured wrapper manually")
    plan = linux_plan(context, install) if sys.platform == "linux" else None
    if plan:
        for name, text in plan.files.items():
            print(f"--- {plan.directory / name}\n{text}", end="")
        print(f"ownership receipt: {plan.receipt}")
        if not install:
            print("Remove only matching, unchanged WPO-owned unit files and receipt:")
            for name in (f"{plan.name}.service", f"{plan.name}.timer", plan.receipt.name):
                print(plan.directory / name)
        for command in plan.commands:
            print(shlex.join(command))
        if install:
            print("Persistent=true can trigger a missed audit immediately when this timer is enabled.")
    else:
        print(subprocess.list2cmdline(schedule_arguments(context) if install else remove_arguments(context)))
        print("Existing tasks require a matching WPO ownership receipt; first installation does not overwrite.")
    if show_only:
        return 0
    from .platform import FileLock
    lock = Path(context.machine.raw["state_dir"]).expanduser() / "schedules" / f"{task_name(context)}.lock"
    with FileLock(lock):
        return change_linux_schedule(context, plan, install) if plan else change_windows_schedule(context, install)
