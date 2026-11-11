"""`wpo audit`: the weekly planning audit of every open WP.

The manual audit of 2026-11-01 found open WPs that restated done ones, owned the same files as WPs
they could run beside, depended on blocked WPs, ran forbidden Verify commands and carried stale
Context. The mechanical classes are `wpo lint`'s checks; this command runs them over every open WP
into a dated report, then hands the judgment classes (work a done WP's Handoff already delivered,
contradictions with the decisions log and the instructions file, criteria that contradict each other
across WPs or cannot be checked, oversize WPs, stale Context) to a read-only review session on a
trusted route, appends its answer to the report and journals `audit-done`, which wakes the
orchestrator. `--install-schedule` registers a weekly Windows task that runs it.
"""

from __future__ import annotations

import re
import subprocess
import sys
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
    """The Windows task's name: one per project."""
    return f"wpo-audit-{TASK_NAME_UNSAFE.sub('-', context.project.name).strip('-')}"


def schedule_arguments(context: Context) -> list[str]:
    """The `schtasks /Create` call registering the weekly audit for this project root."""
    schedule = context.project.config["audit"]["schedule"]
    day, time = schedule.get("day"), schedule.get("time")
    if day not in WEEKDAYS:
        raise AuditError(f"audit.schedule.day '{day}' is not one of {', '.join(WEEKDAYS)}")
    if not isinstance(time, str) or not SCHEDULE_TIME.match(time):
        raise AuditError(f"audit.schedule.time '{time}' is not a local time HH:MM")
    command = subprocess.list2cmdline([*context.machine.wpo_command(), "--project", str(context.project.root), "audit"])
    if len(command) > SCHTASKS_COMMAND_LIMIT:
        raise AuditError(f"the task's command is {len(command)} characters, over schtasks' {SCHTASKS_COMMAND_LIMIT}: {command}")
    return ["schtasks", "/Create", "/TN", task_name(context), "/SC", "WEEKLY", "/D", day, "/ST", time, "/TR", command, "/F"]


def remove_arguments(context: Context) -> list[str]:
    return ["schtasks", "/Delete", "/TN", task_name(context), "/F"]


def change_schedule(context: Context, install: bool, show_only: bool) -> int:
    """Registers (or removes) the weekly task, or with `show_only` prints the command and changes nothing."""
    if sys.platform != "win32" and not show_only:
        raise AuditError("the weekly schedule uses the Windows Task Scheduler; on this system run `wpo audit` from cron")
    arguments = schedule_arguments(context) if install else remove_arguments(context)
    print(subprocess.list2cmdline(arguments))
    if show_only:
        return 0
    completed = subprocess.run(arguments, capture_output=True, text=True, errors="replace", check=False)
    print((completed.stdout + completed.stderr).strip())
    return completed.returncode
