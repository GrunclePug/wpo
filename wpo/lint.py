"""`wpo lint`: checks a WP file before it is scheduled, for the defects that cost the most rounds.

The efficiency review of 2026-10-26 traced a quarter of all failed rounds to how WPs were written:
`owns` missing files the criteria need (a builder stops at the boundary), two open WPs owning the
same files without an order between them (semantic conflicts at merge), unknown dependencies, and
reverse-engineering goals with no method or contract (9 failed rounds against 2.5 for the rest).
The planning audit of 2026-11-01 added the rest of what is mechanical: Verify commands the project
forbids, dependency cycles, chains through a blocked WP or a later milestone, new Rust modules no
owned file declares, WP ids that no longer exist, and WPs restating work that is done or planned
(TF-IDF similarity, `similarity.py`). The WP-writing practice in SKILL.md covers the rest (grep
the callers of what a criterion changes, check numbers against the source data), and `wpo audit`
hands the judgment parts to a review session.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from . import wpfile
from .context import Context
from .similarity import SimilarityIndex

REQUIRED_SECTIONS = ("Goal", "Context", "Deliverables", "Steps", "Acceptance criteria", "Verify")
GLOB_CHARACTERS = re.compile(r"[*?\[]")
NUMBERED_CRITERION = re.compile(r"^\s*\d+\.\s")
REVERSE_ENGINEERING = re.compile(r"(?i)reverse[- ]engineer|decompil|disassembl|bytecode|ghidra|static analysis")
METHOD_OR_CONTRACT = re.compile(r"(?i)\bmethod\b|\bcontract\b|exact[- ]shape|\binvariant\b|\bprototype\b|\bspike\b")
# Statuses lint treats as open: a blocked WP is unblocked into the same files and the same chain.
LINT_OPEN_STATUSES = (*wpfile.OPEN_STATUSES, "blocked")
WP_REFERENCE = re.compile(r"\bWP-\d{3,}\b")
# A milestone compares with another when both are one prefix and a number (M3 < M10); others ('M?') do not.
MILESTONE = re.compile(r"^(\D*)(\d+)$")
RUST_MODULE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Rust module roots: files named so are never declared by a parent with `mod`.
MODULE_ROOT_FILES = ("lib.rs", "main.rs", "mod.rs")
SEVERITIES = ("error", "warning")
LINT_KEYS = ("forbidden_verify", "similarity_warn", "similarity_top")

# Check names, as findings carry them and summaries count them.
CHECK_SECTIONS = "sections"
CHECK_VERIFY = "verify"
CHECK_CRITERIA = "criteria"
CHECK_DEPENDENCIES = "dependencies"
CHECK_OWNS = "owns"
CHECK_MODULE = "module-declaration"
CHECK_OVERLAP = "overlap"
CHECK_REVERSE_ENGINEERING = "reverse-engineering"
CHECK_SIMILARITY = "similarity"
CHECK_STALE_REFERENCE = "stale-reference"


class LintConfigError(Exception):
    """The project's `lint` settings are malformed (an unknown key, a bad regex or severity)."""


@dataclass(frozen=True)
class Finding:
    """One lint finding: an error stops scheduling until fixed; a warning asks for a decision.
    `check` names the check that made it, so reports count findings by kind."""

    wp_id: str
    severity: str
    check: str
    message: str

    def __str__(self) -> str:
        return f"{self.wp_id} {self.severity}: {self.message}"


@dataclass(frozen=True)
class ForbiddenVerify:
    """A Verify command the project forbids (`lint.forbidden_verify`): a regex searched in each command."""

    pattern: re.Pattern
    message: str
    severity: str


@dataclass(frozen=True)
class LintSettings:
    """What the checks need from the project's configuration."""

    round_checks: tuple[str, ...]
    forbidden_verify: tuple[ForbiddenVerify, ...]
    # None disables the similarity check.
    similarity_warn: float | None
    similarity_top: int

    @classmethod
    def from_config(cls, config: Mapping) -> LintSettings:
        lint_config = config["lint"]
        unknown = set(lint_config) - set(LINT_KEYS)
        if unknown:
            raise LintConfigError(f"unknown 'lint' keys {sorted(unknown)}")
        rules = []
        for index, entry in enumerate(lint_config["forbidden_verify"]):
            if not isinstance(entry, dict) or not isinstance(entry.get("pattern"), str) or not isinstance(entry.get("message"), str):
                raise LintConfigError(f"lint.forbidden_verify[{index}] must be an object with string 'pattern' and 'message'")
            extra = set(entry) - {"pattern", "message", "severity"}
            if extra:
                raise LintConfigError(f"lint.forbidden_verify[{index}] has unknown keys {sorted(extra)}")
            severity = entry.get("severity", "error")
            if severity not in SEVERITIES:
                raise LintConfigError(f"lint.forbidden_verify[{index}] severity '{severity}' is not one of {', '.join(SEVERITIES)}")
            try:
                pattern = re.compile(entry["pattern"])
            except re.error as error:
                raise LintConfigError(f"lint.forbidden_verify[{index}] pattern {entry['pattern']!r}: {error}") from None
            rules.append(ForbiddenVerify(pattern, entry["message"], severity))
        threshold = lint_config["similarity_warn"]
        if threshold is not None and (isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold <= 1):
            raise LintConfigError(f"lint.similarity_warn must be a number in (0, 1] or null, not {threshold!r}")
        top = lint_config["similarity_top"]
        if isinstance(top, bool) or not isinstance(top, int) or top < 1:
            raise LintConfigError(f"lint.similarity_top must be a positive integer, not {top!r}")
        return cls(tuple(config["verify_round"]), tuple(rules), None if threshold is None else float(threshold), top)


def fixed_prefix(pattern: str) -> str:
    """The part of an owns pattern before its first wildcard: the directory or file it is rooted at."""
    match = GLOB_CHARACTERS.search(pattern)
    return pattern if match is None else pattern[:match.start()]


def patterns_overlap(first: str, second: str) -> bool:
    """Whether two owns patterns can name the same file: one's fixed part contains the other's.
    Conservative for patterns with wildcards in the middle, which is the safe side for a warning."""
    first_prefix, second_prefix = fixed_prefix(first), fixed_prefix(second)
    if first_prefix == first and second_prefix == second:
        return first == second
    if first_prefix == first:
        return wpfile.glob_to_regex(second).match(first) is not None
    if second_prefix == second:
        return wpfile.glob_to_regex(first).match(second) is not None
    return first_prefix.startswith(second_prefix) or second_prefix.startswith(first_prefix)


def ordered(first: str, second: str, wps: dict[str, wpfile.WorkPackage]) -> bool:
    """Whether one WP depends on the other, directly or through other WPs."""
    def reaches(start: str, goal: str) -> bool:
        seen, pending = set(), [start]
        while pending:
            current = pending.pop()
            if current == goal:
                return True
            if current in seen or current not in wps:
                continue
            seen.add(current)
            pending.extend(wps[current].depends_on)
        return False
    return reaches(first, second) or reaches(second, first)


def upstream(wp: wpfile.WorkPackage, wps: dict[str, wpfile.WorkPackage]) -> list[wpfile.WorkPackage]:
    """Every WP this one depends on, directly or through others."""
    seen: dict[str, wpfile.WorkPackage] = {}
    pending = list(wp.depends_on)
    while pending:
        current = pending.pop()
        if current in seen or current not in wps:
            continue
        seen[current] = wps[current]
        pending.extend(wps[current].depends_on)
    return list(seen.values())


def dependency_cycle(wp: wpfile.WorkPackage, wps: dict[str, wpfile.WorkPackage]) -> list[str] | None:
    """The shortest dependency path from the WP back to itself (`[A, B, A]`), or None.
    Breadth first, so the path found is the shortest one."""
    parents: dict[str, str] = {}
    visited = {wp.id}
    frontier = [wp.id]
    while frontier:
        next_frontier = []
        for current in frontier:
            for dependency in wps[current].depends_on if current in wps else ():
                if dependency == wp.id:
                    path = [current]
                    while path[-1] != wp.id:
                        path.append(parents[path[-1]])
                    return [*reversed(path), wp.id]
                if dependency in visited or dependency not in wps:
                    continue
                visited.add(dependency)
                parents[dependency] = current
                next_frontier.append(dependency)
        frontier = next_frontier
    return None


def milestone_key(milestone: str) -> tuple[str, int] | None:
    match = MILESTONE.match(milestone.strip())
    return None if match is None else (match.group(1), int(match.group(2)))


def milestone_earlier(first: str, second: str) -> bool:
    """Whether milestone `first` comes before `second`; milestones of different forms are not ordered."""
    first_key, second_key = milestone_key(first), milestone_key(second)
    if first_key is None or second_key is None or first_key[0] != second_key[0]:
        return False
    return first_key[1] < second_key[1]


def module_declaration_files(module_path: str) -> tuple[str, list[str]] | None:
    """For a Rust module at `module_path` (a directory, or a `.rs` file) under a `src/` directory:
    its name and the files one of which must declare it with `mod <name>;` (the crate root for a
    top-level module, else the parent module's `mod.rs` or `<parent>.rs`). None when the path is
    no declared module (a crate root, `src/bin/`, outside `src/`)."""
    parts = module_path.strip("/").split("/")
    if "src" not in parts[:-1]:
        return None
    inside = parts[parts.index("src") + 1:]
    if inside[0] == "bin" or inside[-1] in MODULE_ROOT_FILES:
        return None
    name = inside[-1].removesuffix(".rs")
    if not RUST_MODULE_NAME.fullmatch(name):
        return None
    parent = "/".join(parts[:-1])
    if len(inside) == 1:
        return name, [f"{parent}/lib.rs", f"{parent}/main.rs"]
    return name, [f"{parent}/mod.rs", f"{parent}.rs"]


def new_module_of(pattern: str) -> str | None:
    """The module path an owns pattern names, when it is a module directory (`.../src/x/**`) or one
    `.rs` file; None for other patterns."""
    path = pattern.replace("\\", "/")
    if path.endswith("/**") and not GLOB_CHARACTERS.search(path[:-3]):
        return path[:-3]
    if path.endswith(".rs") and not GLOB_CHARACTERS.search(path):
        return path
    return None


def declares_module(path: Path, name: str) -> bool:
    if not path.is_file():
        return False
    declaration = re.compile(rf"(?m)^\s*(pub(\([^)]*\))?\s+)?mod\s+{re.escape(name)}\s*;")
    return declaration.search(path.read_text(encoding="utf-8", errors="replace")) is not None


def owned_by(path: str, patterns: Iterable[str]) -> bool:
    return any(wpfile.glob_to_regex(pattern).match(path) for pattern in patterns)


def undeclared_module(pattern: str, owns: list[str], root: Path) -> tuple[str, list[str]] | None:
    """A new module an owns pattern creates whose declaring file is neither owned nor already
    declaring it: (its name, the files that could declare it). None when nothing is missing."""
    module_path = new_module_of(pattern)
    declaration = None if module_path is None else module_declaration_files(module_path)
    if declaration is None or (root / module_path).exists():
        return None
    if not module_path.endswith(".rs") and (root / f"{module_path}.rs").exists():
        return None  # the module exists as `<dir>.rs`; the directory is for its submodules
    name, declaring_files = declaration
    if any(owned_by(candidate, owns) for candidate in declaring_files):
        return None
    if any(declares_module(root / candidate, name) for candidate in declaring_files):
        return None
    return name, declaring_files


def cites(wp: wpfile.WorkPackage, other_id: str, text: str) -> bool:
    """Whether a WP depends on or names another, so its drafter already weighed it."""
    return other_id in wp.depends_on or re.search(rf"\b{re.escape(other_id)}\b", text) is not None


def lint_wp(wp: wpfile.WorkPackage, wps: dict[str, wpfile.WorkPackage], root: Path, settings: LintSettings,
            similarity: SimilarityIndex | None = None, reported_overlaps: set[frozenset[str]] | None = None) -> list[Finding]:
    """Every finding for one WP. `similarity` (an index of every WP) enables the similarity check.
    `reported_overlaps` is shared across one lint run: an overlap is reported once per pair, on
    the WP linted first."""
    findings: list[Finding] = []

    def add(severity: str, check: str, message: str) -> None:
        findings.append(Finding(wp.id, severity, check, message))

    text = wp.text()
    for heading in REQUIRED_SECTIONS:
        if not wpfile.section_lines(text, heading):
            add("error", CHECK_SECTIONS, f"no '## {heading}' section, or it is empty")

    commands = wp.verify_commands()
    if not commands:
        if settings.round_checks:
            add("error", CHECK_VERIFY, "the Verify block has no commands: only the round checks (format, lint) would run, so name the "
                                       "scoped tests that prove its criteria")
        else:
            add("error", CHECK_VERIFY, "nothing verifies it: no Verify commands and no round checks")
    for command in commands:
        for rule in settings.forbidden_verify:
            if rule.pattern.search(command):
                add(rule.severity, CHECK_VERIFY, f"Verify runs `{command}`: {rule.message}")

    if not any(NUMBERED_CRITERION.match(line) for line in wp.section("Acceptance criteria")):
        add("error", CHECK_CRITERIA, "the acceptance criteria are not numbered (1., 2., ...), so reviews cannot cite them")

    for dependency in wp.depends_on:
        if dependency not in wps:
            add("error", CHECK_DEPENDENCIES, f"depends on {dependency}, which has no WP file")
            continue
        other = wps[dependency]
        # A blocked WP behind a blocked one is one stalled chain, reported where it can still start.
        if other.status == "blocked" and wp.status not in ("done", "blocked"):
            add("warning", CHECK_DEPENDENCIES, f"depends on {dependency}, which is blocked: the chain stalls until it is unblocked "
                                               "(unblock it, or drop or replace the dependency)")
        if milestone_earlier(wp.milestone, other.milestone):
            add("warning", CHECK_DEPENDENCIES, f"is in {wp.milestone} but depends on {dependency} in the later {other.milestone}: "
                                               "move one of them, or drop the dependency")
    cycle = dependency_cycle(wp, wps)
    if cycle:
        add("error", CHECK_DEPENDENCIES, f"is in a dependency cycle ({' -> '.join(cycle)}): no WP in it can ever start")

    created_upstream = [pattern for earlier in upstream(wp, wps) for pattern in earlier.owns]
    for pattern in wp.owns:
        if any(patterns_overlap(pattern, earlier) for earlier in created_upstream):
            continue  # a dependency creates it
        prefix = fixed_prefix(pattern)
        anchor = root / prefix
        if prefix == pattern and not anchor.exists() and not anchor.parent.exists():
            add("warning", CHECK_OWNS, f"owns '{pattern}', but neither it nor its directory exists (a typo, or a new crate?)")
        elif prefix != pattern and prefix and not anchor.exists() and not anchor.parent.exists():
            add("warning", CHECK_OWNS, f"owns '{pattern}', whose fixed part '{prefix}' does not exist")
        missing_declaration = undeclared_module(pattern, wp.owns, root)
        if missing_declaration is not None:
            name, declaring_files = missing_declaration
            add("warning", CHECK_MODULE, f"owns the new module '{pattern}' but not the file that must declare it (`mod {name};` in "
                                         f"{' or '.join(declaring_files)}): add that file to owns, or have a dependency declare it")

    for other in wps.values():
        # A lane runs one WP at a time, so two WPs of one lane never change the same files at once.
        same_lane = bool(wp.lane) and wp.lane == other.lane
        pair = frozenset((wp.id, other.id))
        if other.id == wp.id or other.status not in LINT_OPEN_STATUSES or same_lane or ordered(wp.id, other.id, wps):
            continue
        if reported_overlaps is not None and pair in reported_overlaps:
            continue
        shared = sorted({mine for mine in wp.owns for theirs in other.owns if patterns_overlap(mine, theirs)})
        if shared:
            if reported_overlaps is not None:
                reported_overlaps.add(pair)
            add("warning", CHECK_OVERLAP, f"shares owned paths with open {other.id} ({other.status}; {', '.join(shared)}) with no "
                                          "dependency between them: order them, or narrow one so they cannot change the same files")

    goal_and_context = "\n".join(wp.section("Goal") + wp.section("Context"))
    if REVERSE_ENGINEERING.search(goal_and_context) and not METHOD_OR_CONTRACT.search(goal_and_context):
        add("warning", CHECK_REVERSE_ENGINEERING, "a reverse-engineering or static-analysis goal with no stated method or contract: say "
                                                  "how the finding is made and what exactly counts (an exact shape to match, an "
                                                  "invariant), or put a prototype step first")

    # Unknown dependencies are already errors above.
    stale = sorted({reference for reference in WP_REFERENCE.findall(text)
                    if reference != wp.id and reference not in wps and reference not in wp.depends_on})
    if stale:
        add("warning", CHECK_STALE_REFERENCE, f"names {', '.join(stale)}, which {'has' if len(stale) == 1 else 'have'} no WP file "
                                              "(renumbered, merged into another, or a typo?)")

    if similarity is not None and settings.similarity_warn is not None and wp.id in similarity.vectors:
        similar = similarity.most_similar(wp.id, settings.similarity_warn)[:settings.similarity_top]
        if similar:
            described = []
            for other_id, score in similar:
                relation = "cited here" if cites(wp, other_id, text) else "not cited here"
                described.append(f"{other_id} '{wps[other_id].title}' ({wps[other_id].status}, {score:.2f}, {relation})")
            add("warning", CHECK_SIMILARITY, f"reads like {'; '.join(described)}: check whether its work is already done or planned "
                                             "there; merge it into that WP, or cite it in Context and say what this WP adds")
    return findings


def lint_targets(wps: dict[str, wpfile.WorkPackage], wp_ids: list[str], include_done: bool) -> list[wpfile.WorkPackage]:
    """The named WPs, else every open WP (blocked included), else with `include_done` every WP."""
    unknown = [wp_id for wp_id in wp_ids if wp_id not in wps]
    if unknown:
        raise SystemExit(f"no WP file for {', '.join(unknown)}")
    if wp_ids:
        return [wps[wp_id] for wp_id in wp_ids]
    return [wp for wp in wps.values() if include_done or wp.status in LINT_OPEN_STATUSES]


def lint_all(context: Context, wp_ids: list[str], include_done: bool = False) -> tuple[list[Finding], SimilarityIndex | None, dict[str, wpfile.WorkPackage]]:
    """`lint`, also returning the similarity index and the WPs it read, for reports that list pairs."""
    wps = {wp.id: wp for wp in context.effective_wps()}
    targets = lint_targets(wps, wp_ids, include_done)
    settings = LintSettings.from_config(context.project.config)
    similarity = SimilarityIndex.of_wps(wps.values()) if settings.similarity_warn is not None else None
    reported_overlaps: set[frozenset[str]] = set()
    findings = [finding for wp in sorted(targets, key=lambda wp: wp.id)
                for finding in lint_wp(wp, wps, context.project.root, settings, similarity, reported_overlaps)]
    return findings, similarity, wps


def lint(context: Context, wp_ids: list[str], include_done: bool = False) -> list[Finding]:
    """Lints the named WPs, or every open WP (todo, in flight or blocked) when none are named, or
    every WP with `include_done`. Similarity compares each against every WP, done ones included."""
    return lint_all(context, wp_ids, include_done)[0]


def counts_by_check(findings: Iterable[Finding]) -> dict[str, dict[str, int]]:
    """{check: {"error": n, "warning": m}}, checks sorted by name."""
    counted = Counter((finding.check, finding.severity) for finding in findings)
    checks = sorted({check for check, _ in counted})
    return {check: {severity: counted[(check, severity)] for severity in SEVERITIES} for check in checks}


def summary_lines(findings: list[Finding]) -> list[str]:
    """'by check: overlap 3 warnings, verify 1 error, ...' (when there are findings), then the totals."""
    parts = []
    for check, counted in counts_by_check(findings).items():
        pieces = [f"{count} {severity}{'s' if count != 1 else ''}" for severity, count in counted.items() if count]
        parts.append(f"{check} {' + '.join(pieces)}")
    errors = sum(1 for finding in findings if finding.severity == "error")
    return ([f"by check: {', '.join(parts)}"] if parts else []) + [f"{errors} errors, {len(findings) - errors} warnings"]


def findings_json(findings: list[Finding]) -> dict:
    errors = sum(1 for finding in findings if finding.severity == "error")
    return {"findings": [asdict(finding) for finding in findings], "counts": counts_by_check(findings),
            "errors": errors, "warnings": len(findings) - errors}
