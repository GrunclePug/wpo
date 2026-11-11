"""The review brief: what a WP changed, so a reviewer starts from the change instead of whole files.

It holds the diff of everything the worktree changed since its branch left main (committed or not,
untracked files as additions) and an index of the symbols the diff adds, removes or changes near
(the definition a hunk header names), each with every textual whole-word match in the tree as
`file: lines`. Git failures raise: a brief that cannot be built must not become a review without one.

Symbols come from per-extension definition patterns applied to changed lines and to git's hunk
headers (the enclosing function; git's built-in funcname drivers are switched on for the languages
it knows). Matches are textual, so a same-named symbol elsewhere shows up too; the brief says so.

    brief = build_review_brief(tree, "main", config)
    brief.full_text        # index, file list and diff
    brief.index_text       # index and file list only
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .project import git
from .wpfile import glob_to_regex

RUST_QUALIFIERS = r"(?:pub(?:\([^)]*\))?\s+)?(?:(?:const|async|unsafe|extern\s+\"[^\"]*\")\s+)*"
DEFAULT_SYMBOL_PATTERNS: dict[str, str] = {
    ".rs": rf"^\s*{RUST_QUALIFIERS}(?:(?:fn|struct|enum|trait|type|union|mod|const|static)\s+|macro_rules!\s*)(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    ".py": r"^\s*(?:async\s+)?(?:def|class)\s+(?P<name>[A-Za-z_]\w*)",
    ".cs": (r"^\s*(?:public|private|protected|internal)\s+(?:(?:static|sealed|abstract|partial|readonly|override|virtual|async|unsafe|new)\s+)*"
            r"(?:(?:class|struct|interface|enum|record)\s+|[\w<>\[\],.?]+\s+)(?P<name>[A-Za-z_]\w*)"),
}
SCRIPT_PATTERN = (r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
                  r"(?:function\*?|class|interface|type|enum|const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)")
DEFAULT_SYMBOL_PATTERNS.update({extension: SCRIPT_PATTERN for extension in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue")})
# git's built-in hunk-header drivers, so a header names the enclosing function and not the nearest
# unindented line. Repository .gitattributes outrank this file.
FUNCNAME_ATTRIBUTES = "*.rs diff=rust\n*.py diff=python\n*.cs diff=csharp\n*.go diff=golang\n*.java diff=java\n*.cpp diff=cpp\n*.h diff=cpp\n"

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@ ?(.*)$")
BACKTICK_RUN = re.compile(r"`+")


@dataclass
class FileChange:
    path: str
    added: int = 0
    removed: int = 0
    binary: bool = False
    status: str = "modified"
    diff_lines: list[str] = field(default_factory=list)


@dataclass
class SymbolSite:
    """Where the diff touches a symbol: 'added', 'removed' or 'changed inside' at path:line."""

    how: str
    path: str
    line: int


@dataclass(frozen=True)
class ReviewBrief:
    full_text: str
    index_text: str


def symbol_patterns(overrides: Mapping[str, str]) -> dict[str, re.Pattern[str]]:
    patterns = {**DEFAULT_SYMBOL_PATTERNS, **overrides}
    compiled = {extension: re.compile(pattern) for extension, pattern in patterns.items()}
    missing = [extension for extension, pattern in compiled.items() if "name" not in pattern.groupindex]
    if missing:
        raise ValueError(f"review_symbol_patterns for {', '.join(missing)} lack a named group 'name'")
    return compiled


def text_lines(data: bytes) -> list[str]:
    """Lines split at "\\n" only, as git counts them; a lone "\\r" or "\\x0c" stays inside its line."""
    lines = data.decode("utf-8", errors="replace").split("\n")
    return lines[:-1] if lines and lines[-1] == "" else lines


def tracked_diff(tree: Path, base: str) -> list[str]:
    with tempfile.TemporaryDirectory() as scratch:
        attributes = Path(scratch) / "attributes"
        attributes.write_text(FUNCNAME_ATTRIBUTES, encoding="utf-8")
        completed = subprocess.run(["git", "-C", str(tree), "-c", "core.quotepath=off", "-c", f"core.attributesFile={attributes.as_posix()}",
                                    "diff", "--no-color", "--no-ext-diff", "--no-renames", "-U3", base], capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(f"git diff {base} in {tree} failed: {completed.stderr.decode('utf-8', errors='replace').strip()}")
    return text_lines(completed.stdout)


def untracked_diff(tree: Path, max_bytes: int) -> tuple[list[str], list[str]]:
    """Untracked files as whole-file additions, without touching the index; returns the diff lines
    and the files left out (not a regular file, or larger than `max_bytes`)."""
    chunks: list[str] = []
    left_out: list[str] = []
    for path in git(tree, "-c", "core.quotepath=off", "ls-files", "-z", "--others", "--exclude-standard").stdout.split("\0"):
        if not path:
            continue
        file = tree / path
        if not file.is_file():
            left_out.append(f"- {path}: untracked, not a regular file")
            continue
        size = file.stat().st_size
        if size > max_bytes:
            left_out.append(f"- {path}: untracked, {size:,} bytes, over review_untracked_max_bytes")
            continue
        content = file.read_bytes()
        chunks.append(f"diff --git a/{path} b/{path}\nnew file (untracked)")
        if b"\0" in content:
            chunks.append(f"Binary files /dev/null and b/{path} differ")
            continue
        lines = text_lines(content)
        if not lines:
            continue  # an empty file has no hunk
        chunks.append(f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@")
        chunks.extend(f"+{line}" for line in lines)
    return ("\n".join(chunks).split("\n") if chunks else []), left_out


def header_path(header: str) -> str:
    """The path in `diff --git a/P b/P`. Without renames both sides name the same P, so its length
    follows from the header's; splitting at " b/" would break on a path that contains it."""
    sides = header.removeprefix("diff --git ")
    length = (len(sides) - len("a/ b/")) // 2
    path = sides[2:2 + length]
    if sides != f"a/{path} b/{path}":
        raise ValueError(f"unexpected diff header (a quoted or renamed path?): {header}")
    return path


def parse_diff(diff_lines: list[str]) -> list[FileChange]:
    """Splits a unified diff per file. Lines before a file's first hunk are its header; inside
    hunks every line is content, whatever it starts with."""
    files: list[FileChange] = []
    current: FileChange | None = None
    in_hunk = False
    for line in diff_lines:
        if line.startswith("diff --git "):
            current = FileChange(header_path(line))
            files.append(current)
            in_hunk = False
            continue
        if current is None:
            continue
        current.diff_lines.append(line)
        if HUNK_HEADER.match(line):
            in_hunk = True
        elif not in_hunk:
            if line.startswith("new file"):
                current.status = "added"
            elif line.startswith("deleted file"):
                current.status = "deleted"
            elif line.startswith("Binary files "):
                current.binary = True
        elif line.startswith("+"):
            current.added += 1
        elif line.startswith("-"):
            current.removed += 1
    return files


def symbol_sites(change: FileChange, pattern: re.Pattern[str]) -> dict[str, list[SymbolSite]]:
    """Symbols defined on changed lines, and the enclosing definition named by each hunk header."""
    sites: dict[str, list[SymbolSite]] = {}
    old_line = new_line = 0

    def note(name: str, how: str, line: int) -> None:
        entries = sites.setdefault(name, [])
        if not any(entry.how == how and entry.line == line for entry in entries):
            entries.append(SymbolSite(how, change.path, line))

    in_hunk = False
    for line in change.diff_lines:
        header = HUNK_HEADER.match(line)
        if header:
            in_hunk = True
            old_line, new_line = int(header.group(1)), int(header.group(2))
            # The header names the last definition above the hunk's first line: the change is inside
            # it or just after it (a new function right below), hence "near".
            enclosing = pattern.match(header.group(3))
            if enclosing:
                note(enclosing.group("name"), "changed near", new_line)
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            if match := pattern.match(line[1:]):
                note(match.group("name"), "added", new_line)
            new_line += 1
        elif line.startswith("-"):
            if match := pattern.match(line[1:]):
                note(match.group("name"), "removed", old_line)
            old_line += 1
        elif line.startswith(" "):
            old_line += 1
            new_line += 1
    return sites


def references(tree: Path, name: str) -> dict[str, list[int]]:
    """Every whole-word match of `name` in the tree, tracked or untracked (ignored files excluded)."""
    # -z separates path, line number and text with NULs: paths and text may hold colons. Bytes,
    # because text mode turns a lone carriage return inside a matched line into a record break.
    found = subprocess.run(["git", "-C", str(tree), "-c", "core.quotepath=off", "grep", "-z", "-n", "-w", "-F", "-I", "--untracked", "-e", name],
                           capture_output=True)
    if found.returncode not in (0, 1):
        raise RuntimeError(f"git grep for '{name}' in {tree} failed: {found.stderr.decode('utf-8', errors='replace').strip()}")
    by_file: dict[str, list[int]] = {}
    for record in filter(None, found.stdout.split(b"\n")):
        path, number, _ = record.split(b"\0", 2)
        by_file.setdefault(path.decode("utf-8", errors="replace"), []).append(int(number))
    return by_file


def fenced(body: str, language: str) -> str:
    longest = max((len(run) for run in BACKTICK_RUN.findall(body)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{body}\n{fence}"


def describe_references(by_file: dict[str, list[int]], changed: set[str], limit: int) -> str:
    total = sum(len(lines) for lines in by_file.values())
    if not total:
        return "no references in the tree"
    mark = lambda path: f"{path}*" if path in changed else path
    if total > limit:
        # A common name (new, class, len) matches all over the tree; only the WP's own files say
        # anything about this change.
        own = ", ".join(f"{path} ({len(lines)})" for path, lines in sorted(by_file.items()) if path in changed)
        return f"{total} matches in {len(by_file)} files, too common to list; in changed files: {own or 'none'}"
    return "; ".join(f"{mark(path)}: {', '.join(map(str, lines))}" for path, lines in sorted(by_file.items()))


def build_review_brief(tree: Path, main_branch: str, config: dict) -> ReviewBrief:
    base = git(tree, "merge-base", "HEAD", main_branch).stdout.strip()
    untracked_lines, untracked_left_out = untracked_diff(tree, int(config["review_untracked_max_bytes"]))
    changes = parse_diff([*tracked_diff(tree, base), *untracked_lines])
    excluded = [glob_to_regex(pattern) for pattern in config["review_diff_exclude"]]
    is_excluded = lambda path: any(pattern.match(path) for pattern in excluded)
    patterns = symbol_patterns(config["review_symbol_patterns"])
    changed_paths = {change.path for change in changes}

    symbols: dict[str, list[SymbolSite]] = {}
    unindexed: list[str] = []
    for change in changes:
        if change.binary or is_excluded(change.path):
            continue
        pattern = patterns.get(PurePosixPath(change.path).suffix)
        if pattern is None:
            unindexed.append(change.path)
            continue
        for name, sites in symbol_sites(change, pattern).items():
            symbols.setdefault(name, []).extend(sites)

    limit = int(config["review_reference_limit"])
    symbol_lines = []
    for name, sites in sorted(symbols.items(), key=lambda item: (item[1][0].path, item[1][0].line)):
        where = ", ".join(f"{site.how} {site.path}:{site.line}" for site in sites)
        symbol_lines.append(f"- `{name}`: {where}. Used: {describe_references(references(tree, name), changed_paths, limit)}")

    shown = [change for change in changes if not change.binary and not is_excluded(change.path)]
    left_out = [f"- {change.path}: {'binary' if change.binary else f'+{change.added} -{change.removed}, excluded by review_diff_exclude'}"
                for change in changes if change.binary or is_excluded(change.path)] + untracked_left_out
    file_lines = [f"- {change.path} ({change.status}, +{change.added} -{change.removed})" for change in changes]

    index_parts = [
        "# Review brief",
        f"Everything this WP changed relative to {base[:12]}, where its branch left {main_branch}: committed, uncommitted and untracked files.",
        "## Changed symbols",
        "Each symbol the diff adds, removes or changes inside, then every whole-word match of its name in the tree as file: lines. "
        "Files the WP changed are marked *. Matches are textual, so same-named symbols elsewhere appear too.",
        "\n".join(symbol_lines) or "No symbols found in the changed files.",
    ]
    if unindexed:
        index_parts.append(f"No symbol patterns for: {', '.join(unindexed)} (their changes are in the diff).")
    index_parts += ["## Changed files", "\n".join(file_lines) or "none"]
    if left_out:
        index_parts += ["## Left out of the diff", "\n".join(left_out)]
    index_text = "\n\n".join(index_parts)
    diff_text = "\n".join(f"diff --git a/{change.path} b/{change.path}\n" + "\n".join(change.diff_lines) for change in shown)
    full_text = f"{index_text}\n\n## Diff\n\n{fenced(diff_text, 'diff')}" if diff_text else index_text
    return ReviewBrief(full_text, index_text)
