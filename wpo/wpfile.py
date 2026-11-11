"""Work package files: front matter, fixed sections, status changes and log appends.

A WP file is markdown that starts with front matter between `---` lines. The front matter uses
a small YAML subset: `key: scalar` and inline lists `key: [a, b]`, with `#` comments.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

STATUSES = ("todo", "in_progress", "review", "changes_requested", "blocked", "done")
OPEN_STATUSES = ("todo", "in_progress", "review", "changes_requested")
REQUIRED_FIELDS = ("id", "title", "milestone", "status", "depends_on", "builder", "reviewer", "owns")
LIST_FIELDS = ("depends_on", "owns")
FIELD_PATTERN = re.compile(r"^([a-z_]+):\s*(.*)$")
STATUS_LINE = re.compile(r"(?m)^status:[ \t]*\S+")
TEMPLATE_NAME = "WP-TEMPLATE.md"


class WorkPackageError(Exception):
    """A WP file that cannot be parsed or edited as asked."""


def _parse_value(raw: str) -> str | list[str]:
    value = re.sub(r"\s+#.*$", "", raw).strip()
    if value.startswith("[") and value.endswith("]"):
        return [item.strip() for item in value[1:-1].split(",") if item.strip()]
    return value


@dataclass
class WorkPackage:
    """One parsed WP file. Re-read it after any session that may have edited it."""

    path: Path
    fields: dict = field(repr=False)

    @property
    def id(self) -> str:
        return self.fields["id"]

    @property
    def title(self) -> str:
        return self.fields["title"]

    @property
    def milestone(self) -> str:
        return self.fields["milestone"]

    @property
    def status(self) -> str:
        return self.fields["status"]

    @property
    def depends_on(self) -> list[str]:
        return list(self.fields["depends_on"])

    @property
    def builder(self) -> str:
        return self.fields["builder"]

    @property
    def reviewer(self) -> str:
        return self.fields["reviewer"]

    @property
    def owns(self) -> list[str]:
        return list(self.fields["owns"])

    @property
    def lane(self) -> str:
        return self.fields.get("lane", "")

    def delegate_tier(self, project_default: str | None) -> str | None:
        """The tier of the builder's workers; None when the WP is kept whole (`delegate: none`)."""
        value = self.fields.get("delegate", project_default)
        return None if value in (None, "none") else value

    @property
    def skills(self) -> list[str]:
        """Skills this WP's code-writing sessions load besides the project's (`skills: [name]`)."""
        value = self.fields.get("skills", [])
        if not isinstance(value, list):
            raise WorkPackageError(f"{self.path.name}: 'skills' must be an inline list like [a, b]")
        return list(value)

    @property
    def mode(self) -> str:
        """"single" (one builder session per attempt) or "staged" (planned small tasks)."""
        return self.fields.get("mode", "single")

    @property
    def file_name(self) -> str:
        return self.path.name

    def text(self) -> str:
        return read_text(self.path)

    def section(self, heading: str) -> list[str]:
        return section_lines(self.text(), heading)

    def verify_commands(self) -> list[str]:
        commands, in_fence = [], False
        for line in self.section("Verify"):
            if line.startswith("```"):
                in_fence = not in_fence
            elif in_fence and line.strip() and not line.strip().startswith("#"):
                commands.append(line.strip())
        return commands

    def round_checks(self, round_checks: list[str], merge_checks: Iterable[str] = ()) -> list[str]:
        """What verifies a round: the WP's own Verify commands, then the project's round checks
        (`verify_round`) it does not already list, so no WP has to repeat them. An own command
        that is one of the merge-only checks (`verify_workspace`, the whole test suite) is left to
        the merge into main: older WP files list it."""
        merge_only = set(merge_checks) - set(round_checks)
        own = [command for command in self.verify_commands() if command not in merge_only]
        return own + [command for command in round_checks if command not in own]

    def latest_progress(self) -> str:
        entries = [line for line in self.section("Progress log") if line.startswith("- ")]
        return entries[-1][2:] if entries else ""


def read_text(path: Path) -> str:
    """The file's text with its line endings untouched, so edits keep CRLF or LF as found."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return handle.read()


def write_text(path: Path, text: str) -> None:
    """Writes UTF-8 without a BOM. Python opens files sharing reads, so an editor holding the file
    open does not block the write."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def newline_of(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def parse(path: Path) -> WorkPackage:
    return parse_text(read_text(path), path)


def parse_text(text: str, path: Path) -> WorkPackage:
    """Parses WP text; `path` names the file it came from (or stands for, when read from git)."""
    lines = text.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        raise WorkPackageError(f"{path.name}: missing front matter opening '---'")
    try:
        closing = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        raise WorkPackageError(f"{path.name}: missing front matter closing '---'") from None
    fields: dict = {}
    for line in lines[1:closing]:
        if match := FIELD_PATTERN.match(line):
            fields[match.group(1)] = _parse_value(match.group(2))
    missing = [name for name in REQUIRED_FIELDS if name not in fields]
    if missing:
        raise WorkPackageError(f"{path.name}: front matter lacks {', '.join(missing)}")
    for name in LIST_FIELDS:
        if not isinstance(fields[name], list):
            raise WorkPackageError(f"{path.name}: '{name}' must be an inline list like [a, b]")
    if fields["status"] not in STATUSES:
        raise WorkPackageError(f"{path.name}: invalid status '{fields['status']}'")
    return WorkPackage(path, fields)


def all_in(directory: Path) -> list[WorkPackage]:
    """Every WP file in a directory, sorted by file name (which starts with the id)."""
    files = sorted(path for path in directory.glob("WP-*.md") if path.name != TEMPLATE_NAME)
    return [parse(path) for path in files]


def find(directory: Path, wp_id: str) -> WorkPackage:
    matches = [path for path in directory.glob(f"{wp_id}-*.md")] + [path for path in directory.glob(f"{wp_id}.md")]
    if len(matches) != 1:
        raise WorkPackageError(f"expected one file for {wp_id} in {directory}, found {len(matches)}")
    return parse(matches[0])


def section_lines(text: str, heading: str) -> list[str]:
    lines = text.splitlines()
    try:
        start = lines.index(f"## {heading}")
    except ValueError:
        return []
    section = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        section.append(line)
    return section


def set_status(wp: WorkPackage, status: str) -> WorkPackage:
    if status not in STATUSES:
        raise WorkPackageError(f"invalid status '{status}'")
    text = wp.text()
    updated, count = STATUS_LINE.subn(f"status: {status}", text, count=1)
    if count != 1:
        raise WorkPackageError(f"{wp.path.name}: no status line")
    write_text(wp.path, updated)
    return parse(wp.path)


def set_field(wp: WorkPackage, name: str, value: str) -> WorkPackage:
    """Replaces a scalar front-matter field (the first line starting with `name:`)."""
    if name in LIST_FIELDS or name == "status":
        raise WorkPackageError(f"'{name}' has its own editor")
    text = wp.text()
    updated, count = re.subn(rf"(?m)^{re.escape(name)}:[ \t]*[^\r\n]*", lambda _: f"{name}: {value}", text, count=1)
    if count != 1:
        raise WorkPackageError(f"{wp.path.name}: no '{name}' line")
    write_text(wp.path, updated)
    return parse(wp.path)


def ensure_section(wp: WorkPackage, heading: str, before: str) -> None:
    """Adds an empty `## heading` section before `## before` (or at the end) when it is missing."""
    text = wp.text()
    newline = newline_of(text)
    lines = text.splitlines()
    if f"## {heading}" in lines:
        return
    insert_at = lines.index(f"## {before}") if f"## {before}" in lines else len(lines)
    separated = insert_at == 0 or not lines[insert_at - 1].strip()
    lines[insert_at:insert_at] = ([] if separated else [""]) + [f"## {heading}", ""]
    write_text(wp.path, newline.join(lines) + newline)


def append_to_section(wp: WorkPackage, heading: str, new_lines: Iterable[str]) -> None:
    """Appends lines at the end of a section, keeping one blank line before the next heading."""
    text = wp.text()
    newline = newline_of(text)
    lines = text.splitlines()
    try:
        start = lines.index(f"## {heading}")
    except ValueError:
        raise WorkPackageError(f"{wp.id}: no '## {heading}' section") from None
    insert_at = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    addition = list(new_lines)
    followed_by_blank = insert_at < len(lines) and not lines[insert_at].strip()
    if insert_at < len(lines) and not followed_by_blank:
        addition.append("")
    lines[insert_at:insert_at] = addition
    write_text(wp.path, newline.join(lines) + newline)


def log_progress(wp: WorkPackage, message: str, actor: str = "runner") -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    append_to_section(wp, "Progress log", [f"- {stamp} [{actor}] {message}"])


def glob_to_regex(pattern: str) -> re.Pattern:
    """`**` crosses directories, `*` does not; matched against repo-relative '/' paths."""
    escaped = re.escape(pattern.replace("\\", "/"))
    return re.compile("^" + escaped.replace(r"\*\*", ".*").replace(r"\*", "[^/]*") + "$")


def path_owned(relative_path: str, wp: WorkPackage, wp_dir_relative: str, shared_files: Iterable[str]) -> bool:
    """Whether a WP may change this repo-relative path: its owns, its own file, the shared files."""
    path = relative_path.replace("\\", "/")
    if path == f"{wp_dir_relative}/{wp.file_name}" or path in set(shared_files):
        return True
    return any(glob_to_regex(pattern).match(path) for pattern in wp.owns)


def is_wp_file(relative_path: str, wp_dir_relative: str) -> bool:
    path = relative_path.replace("\\", "/")
    return path.startswith(f"{wp_dir_relative}/WP-") and path.endswith(".md") and path.count("/") == wp_dir_relative.count("/") + 1
