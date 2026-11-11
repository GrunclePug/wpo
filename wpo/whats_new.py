"""What merged into main lately and how to try it: `wpo whats-new [--since DATE] [--last N]`.

A WP reaches main as an integration commit whose subject starts with its id (`WP-0504: ...`). For each one this prints when it merged, its title, its `## Try it`
section as main has it now, and the evidence files the project's `try_it_evidence` globs find
(screenshots, captures, reports). WPs merged before Try it existed say so and point at the Handoff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import wpfile
from .project import Project, git

INTEGRATION_SUBJECT = re.compile(r"^(WP-\d+): ")
TRY_IT_SECTION = "Try it"
HTML_COMMENT = re.compile(r"(?s)<!--.*?-->")
# integrate.py logs "<date> <time> main fast-forwarded to wp/<id> (<sha>) ..." on every merge.
FAST_FORWARD_LINE = re.compile(r"(?m)^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) main fast-forwarded to ")


# How a Try it section says there is nothing to run by hand, in place of a command block.
NOTHING_TO_TRY = "Nothing to try by hand:"


def try_it_text(wp: wpfile.WorkPackage) -> str:
    """The WP's Try it section without the template's comment; empty when unwritten."""
    return HTML_COMMENT.sub("", "\n".join(wp.section(TRY_IT_SECTION))).strip()


def try_it_problem(wp: wpfile.WorkPackage) -> str | None:
    """Why the WP's Try it section cannot go to review, or None. Checked before anything slow runs,
    so a builder hears at once what a reviewer would otherwise send back a round later: the section
    must hold a fenced block of the commands the user runs, or say why nothing can be tried."""
    text = try_it_text(wp)
    if not text:
        return (f"The '## {TRY_IT_SECTION}' section is missing or empty. Fill it before '## Handoff': a fenced block of the exact "
                "commands the user runs from the main checkout once this is merged, and what to look for.")
    commands, in_fence = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
        elif in_fence and line.strip() and not line.strip().startswith("#"):
            commands.append(line.strip())
    if commands or any(line.strip().startswith(NOTHING_TO_TRY) for line in text.splitlines()):
        return None
    return (f"The '## {TRY_IT_SECTION}' section has no commands. Add a fenced block (```) of the exact commands the user runs from "
            f"the main checkout to see the work (open the app, run the replay or example that shows it), then what to look for; "
            f"or, when nothing can be tried by hand, a line starting '{NOTHING_TO_TRY}' saying why.")


@dataclass(frozen=True)
class Merged:
    wp_id: str
    merged_at: datetime
    commit: str


def merge_time(project: Project, wp_id: str) -> datetime | None:
    """When main last fast-forwarded to the WP, from its integrate log (local run state)."""
    log = project.runs_dir / wp_id / "integrate.log"
    if not log.exists():
        return None
    stamps = FAST_FORWARD_LINE.findall(log.read_text(encoding="utf-8", errors="replace"))
    return datetime.strptime(stamps[-1], "%Y-%m-%d %H:%M:%S").astimezone() if stamps else None


def merged_wps(project: Project, since: datetime | None, last: int | None) -> list[Merged]:
    """Newest merge first, one entry per WP. All of main's history, not its first parents: main
    fast-forwards to a WP branch that merged main in, which puts the WPs merged before it on the
    second-parent side. A WP's commit can predate its merge by hours (a held WP), so the time is
    the fast-forward's from the integrate log, else the commit's."""
    found: dict[str, Merged] = {}
    for line in git(project.root, "log", "--format=%H%x00%cI%x00%s", project.main_branch).stdout.splitlines():
        commit, stamp, subject = line.split("\0", 2)
        match = INTEGRATION_SUBJECT.match(subject)
        if match and match.group(1) not in found:
            wp_id = match.group(1)
            found[wp_id] = Merged(wp_id, merge_time(project, wp_id) or datetime.fromisoformat(stamp), commit)
    merged = sorted(found.values(), key=lambda entry: entry.merged_at, reverse=True)
    if since is not None:
        merged = [entry for entry in merged if entry.merged_at >= since.astimezone()]
    return merged[:last] if last is not None else merged


def evidence(project: Project, wp_id: str) -> list[str]:
    files: set[str] = set()
    for template in project.config["try_it_evidence"]:
        pattern = template.replace("{wp}", wp_id).replace("{wp_lower}", wp_id.lower())
        files.update(path.relative_to(project.root).as_posix() for path in project.root.glob(pattern) if path.is_file())
    return sorted(files)


def describe(project: Project, entry: Merged) -> str:
    wp = wpfile.find(project.wp_dir(), entry.wp_id)
    wp_path = f"{project.wp_dir_relative}/{wp.file_name}"
    try_it = try_it_text(wp)
    body = try_it or f"(No '## {TRY_IT_SECTION}' section: merged before it existed. The Handoff in {wp_path} says what it delivered.)"
    files = evidence(project, entry.wp_id)
    shown = "\n".join(f"  {path}" for path in files[:12]) + (f"\n  ... and {len(files) - 12} more" if len(files) > 12 else "")
    evidence_text = f"\nEvidence:\n{shown}" if files else ""
    return f"## {entry.wp_id}: {wp.title}\nMerged {entry.merged_at:%Y-%m-%d %H:%M} ({entry.commit[:10]}). {wp_path}\n\n{body}{evidence_text}\n"


def whats_new(project: Project, since: datetime | None, last: int | None) -> str:
    entries = merged_wps(project, since, last)
    if not entries:
        return "Nothing merged in that range."
    return "\n".join(describe(project, entry) for entry in entries)
