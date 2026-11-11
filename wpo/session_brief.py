"""What a session knows before its task prompt: the project's instructions, the user's carried
rules, working rules, and the skills its role loads.

Claude-CLI sessions start with CLAUDE.md discovery, the skill listing and auto-memory switched off
(they cost every turn of every session), so the brief carries exactly what the role needs instead,
as an appended system prompt. OpenCode sessions read the project's AGENTS.md themselves; they get
the rest of the brief at the top of their prompt. Native T3 sessions of the families in
`t3.native_instructions` load instruction files themselves; their brief leaves those out.

    brief = build_brief(context, tree, ["coding"])
    brief.system_text()   # Claude-CLI harnesses
    brief.prompt_prefix() # OpenCode and scripted harnesses
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .context import Context

FRONT_MATTER = re.compile(r"(?s)^---\r?\n(.*?)\r?\n---\r?\n(.*)$")
SKILLS_FIELD = re.compile(r"(?m)^skills:[ \t]*(.*)$")
# The instructions file OpenCode loads from the project root by itself.
OPENCODE_INSTRUCTIONS_FILE = "AGENTS.md"

# Every turn re-sends the whole conversation, so the number of turns multiplies its size.
WORKING_RULES = """## Working rules
- Batch your calls: put several reads, searches, edits or commands in one message where you can, biasing a little
  towards reading more tokens than calling more tools. Every turn re-sends the whole conversation.
- Find before you read: start with Grep for the symbols, paths or strings you need, then Read the files or line ranges
  it points to. Do not list directories or read whole files to look around.
- Edit narrowly: an Edit's old_string is the smallest unique span around the change. Repeat a change with replace_all
  or several small edits, never by rewriting a large block.
- Test narrowly while you work (`cargo test -p <crate>`, a test filter), not the whole workspace.
- Regenerate evidence (captures, reports, measurements) only when its inputs changed since it was last made.
- Wait for a background command by ending your turn: you are woken when it finishes. Do not poll it."""


def file_header(path: str, section: str | None = None) -> str:
    """The line that opens an included file's contents in a prompt. Never `@path`: the Claude CLI
    takes an `@path` in a prompt as a file mention and attaches the whole file again (measured
    2026-10-29: +16k tokens per session for a spec section and the WP file)."""
    return f"File {path}{f' (section {section})' if section else ''}:"


def included_files(tree: Path, entries: Iterable[str]) -> str:
    """Files a session needs, inlined so it spends no turns reading them. An entry is a path relative
    to `tree`, or `path#heading` for that `## heading...` section only (its heading starts with the text)."""
    blocks = []
    for entry in entries:
        relative, _, heading = entry.partition("#")
        path = tree / relative
        if not path.is_file():
            raise BriefError(f"included file {path} does not exist")
        text = path.read_text(encoding="utf-8").strip()
        if heading:
            text = markdown_section(text, heading, path)
        blocks.append(f"{file_header(relative, heading or None)}\n{text}")
    return "\n\n".join(blocks)


def markdown_section(text: str, heading: str, path: Path) -> str:
    """The `##` section whose heading starts with `heading`, up to the next `##` heading."""
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.startswith("## ") and line[3:].startswith(heading)), None)
    if start is None:
        raise BriefError(f"{path} has no '## {heading}...' section")
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).strip()


class BriefError(Exception):
    """A skill, instructions file or carried rule section the configuration names but cannot be found."""


@dataclass(frozen=True)
class Skill:
    name: str
    path: Path

    def text(self) -> str:
        """The skill body under its path, which its relative references use."""
        match = FRONT_MATTER.match(self.path.read_text(encoding="utf-8"))
        body = (match.group(2) if match else self.path.read_text(encoding="utf-8")).strip()
        return f"{file_header(self.path.as_posix())}\n{body}"


@dataclass(frozen=True)
class SessionBrief:
    """The parts of a session's standing context, kept apart because harnesses load them differently."""

    instructions_name: str | None
    instructions: str
    guidance: str
    # The whole brief for a session that loads instruction files itself (t3.native_instructions),
    # without those files; None when none are configured.
    native_text: str | None = None

    def system_text(self, native: bool = False) -> str:
        """For harnesses whose own CLAUDE.md discovery is off: the project's instructions and the rest.
        With `native`, for one that discovers instruction files itself: only what it does not load."""
        if native and self.native_text is not None:
            return self.native_text
        header = f"{file_header(self.instructions_name)}\n{self.instructions.strip()}" if self.instructions_name else ""
        return "\n\n".join(part for part in (header, self.guidance) if part)

    def prompt_prefix(self) -> str:
        """For OpenCode and scripted harnesses. OpenCode discovers AGENTS.md itself; any other
        instructions file would go unseen, so it is included."""
        if self.instructions_name in (None, OPENCODE_INSTRUCTIONS_FILE):
            return self.guidance
        return self.system_text()


def skill_search_dirs(context: Context) -> list[Path]:
    return [Path(directory).expanduser() for directory in context.machine.raw["skills"]["dirs"]]


def resolve_skill(context: Context, name: str) -> Skill:
    """Finds a skill by name: an explicit path in the machine config, else `<dir>/<name>/SKILL.md`."""
    explicit = context.machine.raw["skills"]["paths"].get(name)
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise BriefError(f"skill '{name}' is configured at {path}, which does not exist")
        return Skill(name, path)
    for directory in skill_search_dirs(context):
        candidate = directory / name / "SKILL.md"
        if candidate.is_file():
            return Skill(name, candidate)
    searched = ", ".join(str(directory) for directory in skill_search_dirs(context))
    raise BriefError(f"skill '{name}' not found in {searched}; add its SKILL.md to skills.paths in the machine config")


def agent_skills(agent_file: Path) -> list[str]:
    """The skills an agent definition's front matter declares (`skills: a`, `skills: a, b` or
    `skills: [a, b]`). No agent file means no reviewer agent, as for its instructions
    (runner.reviewer_instructions): reviews then carry neither."""
    if not agent_file.is_file():
        return []
    match = FRONT_MATTER.match(agent_file.read_text(encoding="utf-8"))
    if not match:
        raise BriefError(f"{agent_file} has no front matter")
    field = SKILLS_FIELD.search(match.group(1))
    if not field:
        return []
    names = [name.strip() for name in field.group(1).strip().strip("[]").split(",") if name.strip()]
    if not names:
        raise BriefError(f"{agent_file}: 'skills:' has no inline value; write 'skills: [a, b]' (block lists are not read)")
    return names


def carried_user_rules(context: Context, skip: frozenset[Path] = frozenset()) -> str:
    """Carry ordered instruction files or exact sections, preserving the legacy disabled form. Files
    in `skip` (resolved paths a session loads itself) are validated but not carried."""
    settings = context.machine.raw["user_rules"]
    if isinstance(settings, dict):
        entries = [settings]
        legacy = True
    elif isinstance(settings, list):
        entries = settings
        legacy = False
    else:
        raise BriefError("machine config user_rules must be an object or a list of file objects")
    carried = []
    for index, entry in enumerate(entries):
        location = "user_rules" if legacy else f"user_rules[{index}]"
        if not isinstance(entry, dict):
            raise BriefError(f"machine config {location} must be an object with file and optional sections")
        unknown = entry.keys() - {"file", "sections"}
        if unknown and not legacy:
            raise BriefError(f"machine config {location} has unknown keys: {', '.join(sorted(unknown))}")
        sections = entry.get("sections", [] if legacy else None)
        if legacy and sections is None:
            sections = []
        if sections is not None and (not isinstance(sections, list) or any(not isinstance(heading, str) or not heading.strip() for heading in sections)):
            raise BriefError(f"machine config {location}.sections must be a list of nonempty headings or null")
        filename = entry.get("file")
        if not (legacy and sections == []) and (not isinstance(filename, str) or not filename.strip()):
            raise BriefError(f"machine config {location}.file must be a nonempty path string")
        if sections == []:
            continue
        source = Path(filename).expanduser()
        if not source.is_file():
            raise BriefError(f"user rules file {source} does not exist (machine config {location}.file)")
        if source.resolve() in skip:
            continue
        text = source.read_text(encoding="utf-8").strip()
        if sections is None:
            carried.append(text)
            continue
        lines = text.splitlines()
        for heading in sections:
            try:
                start = lines.index(f"## {heading}")
            except ValueError:
                raise BriefError(f"{source} has no '## {heading}' section (machine config {location}.sections)") from None
            end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
            carried.append("\n".join(lines[start:end]).strip())
    return "\n\n".join(carried)


def skills_location_note(context: Context) -> str:
    directories = ", ".join(directory.as_posix() for directory in skill_search_dirs(context))
    return f"## Other skills\nOther skills are in {directories} as <name>/SKILL.md; read one when the task needs it."


def build_brief(context: Context, tree: Path, skills: Iterable[str]) -> SessionBrief:
    """The brief of one session working in `tree`, loading `skills` (names, deduplicated in order)."""
    names = list(dict.fromkeys(skills))
    loaded = [resolve_skill(context, name) for name in names]
    instructions_name = context.project.config["instructions_file"]
    instructions = ""
    if instructions_name:
        # From the main checkout, like the configuration: a worktree's copy can predate a fix.
        instructions_path = context.project.root / instructions_name
        if not instructions_path.is_file():
            raise BriefError(f"the project's instructions file {instructions_path} does not exist (project config instructions_file)")
        instructions = instructions_path.read_text(encoding="utf-8")
    directory = (f"## Working directory\nYour working directory is {tree.as_posix()}: every command already runs there. Never start a "
                 f"command with `cd {tree.as_posix()} &&`; pass other directories as paths.")

    def guidance(skip: frozenset[Path]) -> str:
        parts = [WORKING_RULES, directory, carried_user_rules(context, skip),
                 *(skill.text() for skill in loaded if skill.path.resolve() not in skip), skills_location_note(context)]
        return "\n\n".join(part for part in parts if part)

    brief = SessionBrief(instructions_name, instructions, guidance(frozenset()))
    native = native_instruction_files(context, tree)
    if not native:
        return brief
    # The session reads its checkout's instructions file itself. The brief carries the main checkout's
    # unless that is the same file or text: a worktree's own copy can predate a fix on main.
    checkout_file = tree / instructions_name if instructions_name else None
    loads_instructions = (checkout_file is not None and checkout_file.is_file() and checkout_file.resolve() in native
                          and (checkout_file.resolve() == instructions_path.resolve() or checkout_file.read_text(encoding="utf-8") == instructions))
    header = f"{file_header(instructions_name)}\n{instructions.strip()}" if instructions_name and not loads_instructions else ""
    return SessionBrief(instructions_name, instructions, brief.guidance, "\n\n".join(part for part in (header, guidance(native)) if part))


def native_instruction_files(context: Context, tree: Path) -> frozenset[Path]:
    """The instruction files a session in `tree` loads itself when its route family is one of
    `t3.native_instructions.families`, resolved: each file named in `names` in `tree` or a
    directory above it (as Claude Code finds CLAUDE.md), and the fixed `files` (a user's global
    file). Empty when no family is configured."""
    settings = context.machine.raw.get("t3", {}).get("native_instructions") or {}
    if not settings.get("families"):
        return frozenset()
    for key in ("families", "names", "files"):
        value = settings.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            raise BriefError(f"machine config t3.native_instructions.{key} must be a list of nonempty strings")
    found = {Path(file).expanduser().resolve() for file in settings.get("files", []) if Path(file).expanduser().is_file()}
    for directory in (tree, *tree.parents):
        found.update((directory / name).resolve() for name in settings.get("names", []) if (directory / name).is_file())
    return frozenset(found)


def loads_instructions_natively(context: Context, family: str) -> bool:
    """Whether native sessions of a route family load instruction files themselves."""
    return family in (context.machine.raw.get("t3", {}).get("native_instructions") or {}).get("families", [])


def builder_skills(context: Context, wp_skills: Iterable[str]) -> list[str]:
    """Skills of every session that writes a WP's code: the project's, then the WP's own."""
    return [*context.project.config["builder_skills"], *wp_skills]


def reviewer_skills(context: Context) -> list[str]:
    """The project's `reviewer_skills`, else the skills the user's reviewer agent declares (its body
    is the review prompt's preamble). A project whose AGENTS.md carries its standards sets [] so
    reviews of, say, Rust do not load a skill written for other languages."""
    configured = context.project.config["reviewer_skills"]
    return list(configured) if configured is not None else agent_skills(context.machine.reviewer_agent)
