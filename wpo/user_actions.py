"""What the user does from the dashboard: unblock a WP, answer a session's approval request,
message a WP, or ask for new work. The server (dashboard_server.py) only routes requests here, and
every action calls the code the orchestrator's own commands use (`wpfile`, `lanes`, `approvals`,
`inbox`, `commit-main`, `dispatch`), so the page can do nothing `wpo` could not.

    unblock       a dated "User note" bullet in the WP's Context, the status back to todo (review
                  when it was blocked in review), its held lane released, `wpo lanes resume` for it
    approve/deny  `approvals.decide`, as `wpo approve|deny`
    message       to the WP's live session when one reads messages (as `wpo message --wp`);
                  otherwise, and for a request about no WP, to a placement agent

The placement agent runs detached (`wpo place-request`): a code-hard session, never on a free or
local route, reads the request and the WP files and answers with a decision record; the tooling
applies it. The request goes into the Context of the open WP it belongs to (the WP's worktree copy
when it has one, else main's file, committed under the integration mutex), or into the new WP file
the agent drafted from the project's template, committed the same way. A blocked target is
unblocked as above. Edits in the main checkout are committed in one hold of the integration mutex
(`orchestrator.edit_and_commit_main`), so no fast-forward meets them uncommitted.

Every message is journaled with its text (`user-message`, `user-message-queued`), and every
placement ends in an attention event (`user-message-placed` or `user-message-unplaced`), so the
orchestrator's `wpo events wait` wakes and nothing the user wrote is lost.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import secrets
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import SKILL_ROOT, lanes, wpfile
from .approvals import ApprovalError, decide
from .context import Context
from .inbox import deliver_if_live
from .orchestrator import (
    MainCommitError,
    NoTrustedFamilyError,
    dispatch_session,
    edit_and_commit_main,
    trusted_families,
)
from .platform import spawn_detached
from .project import ProjectError, git
from .session import NoRouteError, describe_next

USER_NOTE_LABEL = "User note"
CONTEXT_SECTION = "Context"
# The section a missing Context is added before (the template's order).
CONTEXT_FOLLOWED_BY = "Deliverables"
# A WP the user may unblock: blocked, handed back with changes requested, or left in review by a
# review that gave no verdict or no open finding (`wp-blocked` while in review).
UNBLOCKABLE_STATUSES = ("blocked", "changes_requested", "review")
UNBLOCK_TARGETS = ("todo", "review")
PLACEMENT_SPEC = "code-hard"
PLACEMENTS_DIRECTORY = "placements"
REQUEST_FILE = "request.json"
DECISION_FILE = "decision.json"
PLACEMENT_LOG = "placement.log"
MAX_TEXT_CHARS = 20000
EXCERPT_CHARS = 160
EXIT_NO_ROUTE = 4
WP_ID = re.compile(r"^WP-[\w.-]+$")
PLACEMENT_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
DECISION_BLOCK = re.compile(r"```json[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.DOTALL)
DECISION_KINDS = ("existing", "new")

# `lanes` reports by printing; its output is captured per action, one action at a time.
_lane_output_lock = threading.Lock()


class UserActionError(Exception):
    """An action that cannot be done as asked: empty text, a WP in a state the action does not
    apply to, a malformed placement decision."""


class UnknownTargetError(UserActionError):
    """The WP or approval the action names does not exist."""


class ActionConflictError(UserActionError):
    """The action would race work in progress: a supervisor runs the WP now."""


# ------------------------------------------------------------------------------------- helpers


def excerpt(text: str) -> str:
    """The text on one line, cut for an event message; the whole text goes in the event's fields."""
    flat = " ".join(text.split())
    return flat if len(flat) <= EXCERPT_CHARS else flat[:EXCERPT_CHARS - 1] + "…"


def checked_text(text: str, what: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise UserActionError(f"the {what} is empty")
    if len(stripped) > MAX_TEXT_CHARS:
        raise UserActionError(f"the {what} has {len(stripped)} characters; at most {MAX_TEXT_CHARS} are taken")
    return stripped


def checked_wp_id(wp_id: str) -> str:
    if not WP_ID.match(wp_id):
        raise UnknownTargetError(f"'{wp_id}' is not a WP id")
    return wp_id


def lane_output(function: Callable[..., object], *arguments: object) -> str:
    """Runs a `lanes` function and returns what it printed."""
    with _lane_output_lock, contextlib.redirect_stdout(io.StringIO()) as printed:
        function(*arguments)
    return printed.getvalue().strip()


def effective_wp(context: Context, wp_id: str) -> tuple[wpfile.WorkPackage, bool]:
    """The file holding the WP's live state, as `Context.effective_wps` reads it: its worktree's
    copy when the worktree has one and main does not hold the WP as done, else the main checkout's;
    and whether it is the worktree's."""
    checked_wp_id(wp_id)
    try:
        main_copy = wpfile.find(context.project.wp_dir(), wp_id)
    except wpfile.WorkPackageError as error:
        raise UnknownTargetError(f"{wp_id}: {error}") from None
    if main_copy.status == "done":
        return main_copy, False
    worktree_copy = context.project.wp_dir(context.project.worktree(wp_id)) / main_copy.file_name
    if worktree_copy.exists():
        return wpfile.parse(worktree_copy), True
    return main_copy, False


def note_lines(text: str, written_at: datetime) -> list[str]:
    """The note as one dated Context bullet; further lines are indented into the same item."""
    first, *rest = text.strip().splitlines()
    return [f"- {written_at:%Y-%m-%d %H:%M} {USER_NOTE_LABEL}: {first.rstrip()}", *(f"  {line.rstrip()}" if line.strip() else "" for line in rest)]


def write_note(wp: wpfile.WorkPackage, note: str, status: str | None) -> wpfile.WorkPackage:
    """Adds the note to the WP file's Context (when there is one) and sets the status (when given)."""
    if note:
        wpfile.ensure_section(wp, CONTEXT_SECTION, before=CONTEXT_FOLLOWED_BY)
        wpfile.append_to_section(wpfile.parse(wp.path), CONTEXT_SECTION, note_lines(note, datetime.now()))
    updated = wpfile.parse(wp.path)
    if status is not None and updated.status != status:
        updated = wpfile.set_status(updated, status)
    return updated


def add_note(context: Context, wp_id: str, note: str, status: str | None, commit_message: str) -> str:
    """Writes the note and status into the WP's live file: in its worktree (the merge commits it),
    or in main, edited and committed under the integration mutex. Returns where it went."""
    wp, in_worktree = effective_wp(context, wp_id)
    if in_worktree:
        write_note(wp, note, status)
        return f"in its worktree's WP file ({wp.path})"
    relative = f"{context.project.wp_dir_relative}/{wp.file_name}"

    def edit() -> list[str]:
        # Read again under the mutex: the file may have changed since it was looked up.
        write_note(wpfile.parse(wp.path), note, status)
        return [relative]

    return f"in main ({edit_and_commit_main(context, commit_message, edit)})"


def restart_lane(context: Context, wp: wpfile.WorkPackage) -> list[str]:
    """Releases the WP's lane when it is held and resumes it, as `wpo lanes release` and
    `wpo lanes resume <lane>` do; returns what they reported."""
    if not wp.lane:
        return [f"{wp.id} has no lane; `wpo supervise --milestone {wp.milestone}` runs it"]
    reports = []
    if wp.lane in lanes.read_holds(context):
        reports.append(lane_output(lanes.release, context, wp.lane))
    reports.append(lane_output(lanes.resume, context, False, [wp.lane]))
    return reports


# ------------------------------------------------------------------------------------- actions


def unblock(context: Context, wp_id: str, note: str, status: str | None = None) -> str:
    """Puts a blocked WP back to work: the note in its Context, its status back to `status`
    (default: todo, or review when it was left in review), its lane released and resumed."""
    wp, _ = effective_wp(context, wp_id)
    if wp.status not in UNBLOCKABLE_STATUSES:
        raise UserActionError(f"{wp_id} is {wp.status}; only a {', '.join(UNBLOCKABLE_STATUSES)} WP is unblocked")
    target = status or ("review" if wp.status == "review" else "todo")
    if target not in UNBLOCK_TARGETS:
        raise UserActionError(f"a WP is unblocked to {' or '.join(UNBLOCK_TARGETS)}, not '{target}'")
    if lanes.claimed_elsewhere(context, wp_id):
        raise ActionConflictError(f"a supervisor is running {wp_id} now; send it a message instead")
    note = note.strip()
    if note:
        checked_text(note, "note")
    reports = []
    if note or target != wp.status:
        where = add_note(context, wp_id, note, target, f"{wp_id}: unblocked by the user ({wp.status} -> {target})")
        reports.append(f"{wp_id}: {wp.status} -> {target}{', note added' if note else ''} {where}")
    else:
        reports.append(f"{wp_id}: stays in {target}; nothing to write")
    reports += restart_lane(context, wp)
    context.journal.emit("wp-unblocked", f"{wp_id} unblocked by the user ({wp.status} -> {target})" + (f": {excerpt(note)}" if note else ""),
                         {"wp": wp_id, **({"text": note} if note else {})})
    return "\n".join(reports)


def answer_approval(context: Context, approval_id: str, approved: bool, text: str) -> str:
    """`wpo approve <id> --note` or `wpo deny <id> --reason`, from the page."""
    if not approved and not text.strip():
        raise UserActionError("a denial needs a reason; the session reads it")
    try:
        return decide(context, approval_id, approved, text.strip())
    except ApprovalError as error:
        if "no approval request" in str(error) or "is not an approval id" in str(error):
            raise UnknownTargetError(str(error)) from None
        raise UserActionError(str(error)) from None


def send_message(context: Context, wp_id: str | None, text: str,
                 spawn: Callable[[Context, str], int] | None = None) -> str:
    """A message about a WP, or a request about none: delivered to the WP's live session when one
    reads messages, otherwise handed to a placement agent."""
    text = checked_text(text, "message")
    if wp_id is not None:
        wp, _ = effective_wp(context, wp_id)
        if deliver_if_live(context.project.wp_run_dir(wp.id), text):
            context.journal.emit("user-message", f"{wp.id}: the user's message went to its live session: {excerpt(text)}", {"wp": wp.id, "text": text})
            return f"{wp.id}: delivered; the session reads it at its next turn"
    placement_id = queue_placement(context, wp_id, text, spawn or start_placement)
    subject = f"{wp_id} has no session that reads messages" if wp_id else "a request about no WP"
    return f"{subject}: placement agent {placement_id} decides where it belongs and journals user-message-placed"


# ----------------------------------------------------------------------------------- placement


def placements_dir(context: Context) -> Path:
    return context.project.runs_dir / PLACEMENTS_DIRECTORY


def placement_dir(context: Context, placement_id: str) -> Path:
    if not PLACEMENT_ID.match(placement_id):
        raise UnknownTargetError(f"'{placement_id}' is not a placement id")
    return placements_dir(context) / placement_id


def start_placement(context: Context, placement_id: str) -> int:
    """Starts `wpo place-request` detached: the session outlives the request that asked for it."""
    from .dashboard import server_environment
    directory = placement_dir(context, placement_id)
    return spawn_detached([*context.machine.wpo_command(), "--project", str(context.project.root), "place-request", "--id", placement_id],
                          context.project.root, env=server_environment(), log_path=directory / PLACEMENT_LOG)


def queue_placement(context: Context, wp_id: str | None, text: str, spawn: Callable[[Context, str], int]) -> str:
    """Records the request where the placement agent reads it, starts the agent, journals it."""
    placement_id = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    directory = placement_dir(context, placement_id)
    directory.mkdir(parents=True)
    request = {"id": placement_id, "wp": wp_id, "text": text, "requested": datetime.now().isoformat(timespec="seconds")}
    (directory / REQUEST_FILE).write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
    pid = spawn(context, placement_id)
    subject = f"{wp_id}: no live session reads messages" if wp_id else "a request about no WP"
    context.journal.emit("user-message-queued", f"{subject}; placement agent {placement_id} (pid {pid}) places it: {excerpt(text)}",
                         {**({"wp": wp_id} if wp_id else {}), "placement": placement_id, "text": text})
    return placement_id


def placement_families(context: Context) -> list[str]:
    """The router's families a placement may run on: none whose routes are free or local."""
    try:
        return trusted_families(context)
    except NoTrustedFamilyError as error:
        raise UserActionError(f"{error}; a placement cannot run") from None


def template_path(context: Context) -> Path:
    """The project's WP template (in its WP directory), else the skill's."""
    project_template = context.project.wp_dir() / wpfile.TEMPLATE_NAME
    return project_template if project_template.exists() else SKILL_ROOT / "templates" / wpfile.TEMPLATE_NAME


def placement_prompt(context: Context, request: dict) -> str:
    """The placement agent's task: decide where the user's request belongs, draft a WP only when
    no open one fits, and answer with a decision record the tooling applies."""
    project = context.project
    wp_dir = project.wp_dir_relative
    effective = context.effective_wps()
    open_lines = "\n".join(f"- {wp.id} [{wp.status}] lane {wp.lane or '-'}: {wp.title} (owns {', '.join(wp.owns) or 'nothing'})"
                           for wp in effective if wp.status != "done") or "- none"
    done_count = sum(1 for wp in effective if wp.status == "done")
    known_lanes = ", ".join(sorted({wp.lane for wp in effective if wp.lane})) or "none yet"
    asked_about = request.get("wp")
    if asked_about:
        wp, in_worktree = effective_wp(context, asked_about)
        where = f"its live copy is {wp.path.as_posix()}" + (" (in its worktree)" if in_worktree else "")
        subject = (f"The user wrote this about {wp.id} ({wp.title}), which is {wp.status}; {where}.\n"
                   + (f"{wp.id} is not done, so the request belongs to it: decide `existing` {wp.id}. Read it and the code it "
                      "touches only far enough to write a pointer the builder needs (the files, criteria or steps the request bears on)."
                      if wp.status != "done" else
                      f"{wp.id} is done and merged, so the request is new work: find the open WP below that handles similar work "
                      "(same files, same system) and decide `existing` it; when none fits, draft a new WP and decide `new`."))
    else:
        subject = ("The user wrote this request about no particular WP. Find the open WP below that handles it (same files, same "
                   "system, same goal) and decide `existing` it; when none fits, draft a new WP and decide `new`.")
    return f"""You place a request the user typed into the orchestration dashboard of the project {project.name}.

{subject}

The user's request, verbatim:
<<<
{request['text']}
>>>

Open work packages (id [status] lane: title (owns)); {done_count} more are done. Every WP file, done ones included, is in
`{wp_dir}/` of the main checkout; read the ones that could fit (Goal, Context, owns) with your file tools before deciding.
{open_lines}

Rules:
- Do not edit any existing WP file and do not put the request into one: the tooling adds it verbatim, as a dated
  "{USER_NOTE_LABEL}" bullet in the chosen WP's Context, after you answer. It also resets a blocked status and resumes its lane.
- A new WP is drafted only when no open WP fits. Write it as `{wp_dir}/WP-NNNN-short-slug.md` from the template
  {template_path(context).as_posix()}: NNNN is the highest number among every WP file in `{wp_dir}/` plus one, with the
  same digit count; `status: todo`; a lane from those in use ({known_lanes}) that does similar work; `depends_on` only
  for WPs it truly needs that are not done; tiers for `builder` and `reviewer` like similar WPs use; `owns` listing every
  path it changes (grep the callers of what it changes); every section filled, criteria numbered and checkable, Verify
  commands scoped to it. Do not write the user's request into it; the tooling adds it.
- Write nothing else, run no command that changes anything, commit nothing: the tooling commits what you drafted.

Finish your answer with your decision record: exactly one fenced ```json block, the last thing you write, of the form
{{"decision": "existing", "wp": "WP-0123", "why": "one or two sentences", "pointer": "what the builder needs to know, or empty"}}
or
{{"decision": "new", "wp": "WP-0456", "file": "{wp_dir}/WP-0456-short-slug.md", "why": "one or two sentences: which open WPs you weighed and why none fits"}}"""


def dirty_paths(context: Context) -> set[str]:
    """Paths in the main checkout's WP directory that differ from HEAD (untracked included)."""
    listing = git(context.project.root, "status", "--porcelain", "-z", "--untracked-files=all", "--", f"{context.project.wp_dir_relative}/").stdout
    paths: set[str] = set()
    entries = iter(listing.split("\0"))
    for entry in entries:
        if not entry:
            continue
        paths.add(entry[3:])
        if entry[0] in "RC":
            next(entries, None)  # a rename's or copy's source path follows
    return paths


def parse_decision(answer: str) -> dict:
    """The decision record ending the placement agent's answer, checked field by field."""
    blocks = DECISION_BLOCK.findall(answer)
    if not blocks:
        raise UserActionError("the placement agent's answer has no ```json decision record")
    try:
        decision = json.loads(blocks[-1])
    except ValueError as error:
        raise UserActionError(f"the placement agent's decision record is not JSON: {error}") from None
    if not isinstance(decision, dict):
        raise UserActionError("the placement agent's decision record is not a JSON object")
    kind, wp_id, why = decision.get("decision"), decision.get("wp"), decision.get("why")
    if kind not in DECISION_KINDS:
        raise UserActionError(f"the decision is '{kind}', not one of {', '.join(DECISION_KINDS)}")
    if not isinstance(wp_id, str) or not WP_ID.match(wp_id):
        raise UserActionError(f"the decision names no WP id ({wp_id!r})")
    if not isinstance(why, str) or not why.strip():
        raise UserActionError("the decision gives no reason ('why')")
    pointer = decision.get("pointer", "")
    file = decision.get("file", "")
    if not isinstance(pointer, str) or not isinstance(file, str):
        raise UserActionError("'pointer' and 'file' must be strings")
    if kind == "new" and not file:
        raise UserActionError("a 'new' decision names no file")
    return {"decision": kind, "wp": wp_id, "why": why.strip(), "pointer": pointer.strip(), "file": file.strip().replace("\\", "/")}


def placed_note(request: dict, decision: dict) -> str:
    """The request verbatim, with the agent's pointer and, when it came from another WP, where from."""
    parts = [request["text"]]
    if request.get("wp") and request["wp"] != decision["wp"]:
        parts.append(f"(Asked about {request['wp']}; placed here: {decision['why']})")
    if decision["pointer"]:
        parts.append(f"(Placement pointer: {decision['pointer']})")
    return "\n".join(parts)


def apply_decision(context: Context, request: dict, decision: dict, written: list[str]) -> str:
    """Places the request as decided; returns what was done. Edits in the main checkout are
    committed under the integration mutex: the agent's drafted files and the note together."""
    placement_id = request["id"]
    note = placed_note(request, decision)
    target = decision["wp"]
    wp_dir = context.project.wp_dir_relative
    if decision["decision"] == "new":
        relative = decision["file"]
        if relative not in written:
            raise UserActionError(f"the decision names {relative}, which the placement agent did not write (it wrote: {', '.join(written) or 'nothing'})")
        if not wpfile.is_wp_file(relative, wp_dir):
            raise UserActionError(f"{relative} is not a WP file in {wp_dir}/")
        drafted = wpfile.parse(context.project.root / relative)
        if drafted.id != target:
            raise UserActionError(f"{relative} has id {drafted.id}, not {target}")
        if target in {wp.id for wp in context.main_wps()}:
            raise UserActionError(f"{target} already exists on {context.project.main_branch}; the draft would duplicate it")

        def edit_new() -> list[str]:
            write_note(wpfile.parse(context.project.root / relative), note, None)
            return written

        commit = edit_and_commit_main(context, f"{target}: drafted from a user request (placement {placement_id})", edit_new)
        from .lint import lint
        findings = lint(context, [target])
        errors = sum(1 for finding in findings if finding.severity == "error")
        checked = (f"wpo lint: {errors} errors, {len(findings) - errors} warnings ({'; '.join(finding.message for finding in findings)})"
                   if findings else "wpo lint: clean")
        return f"drafted {target} ({relative}) with the request in its Context; committed {commit}; {checked}"
    wp, _ = effective_wp(context, target)
    if wp.status == "done":
        raise UserActionError(f"the decision places it in {target}, which is done; a request goes to an open WP or a new one")
    reports = []
    if written:
        # The agent was told to write nothing but a new WP; whatever it wrote is committed, not left to break a fast-forward.
        stray = edit_and_commit_main(context, f"placement {placement_id}: files the placement agent wrote", lambda: written)
        reports.append(f"committed the agent's own edits to {', '.join(written)} ({stray})")
    if wp.status == "blocked":
        reports.append(unblock(context, target, note))
    else:
        reports.append(f"note added to {target} " + add_note(context, target, note, None, f"{target}: a user request placed in its Context (placement {placement_id})"))
    return "; ".join(reports)


def place_request(context: Context, placement_id: str) -> int:
    """`wpo place-request --id <id>`: runs the placement agent on a queued request and applies its
    decision; journals user-message-placed, or user-message-unplaced with the request's text."""
    directory = placement_dir(context, placement_id)
    request = json.loads((directory / REQUEST_FILE).read_text(encoding="utf-8"))
    fields = {**({"wp": request["wp"]} if request.get("wp") else {}), "placement": placement_id, "text": request["text"]}

    def unplaced(why: str) -> None:
        context.journal.emit("user-message-unplaced", f"placement {placement_id} could not place the user's request ({why}); "
                             f"place it by hand: {excerpt(request['text'])}", fields)

    try:
        before = dirty_paths(context)
        result = dispatch_session(context, PLACEMENT_SPEC, placement_prompt(context, request), "docs", [f"{context.project.wp_dir_relative}/**"],
                                  placement_families(context), context.project.root, directory, {"placement": placement_id})
        written = sorted(dirty_paths(context) - before)
        decision = {"route": result.route["id"], "written": written}
        decision.update(parse_decision(result.result_text))
        (directory / DECISION_FILE).write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
        done = apply_decision(context, request, decision, written)
    except NoRouteError as error:
        unplaced(f"no route for {PLACEMENT_SPEC} until {describe_next(error.next_available_at)}: {error}")
        return EXIT_NO_ROUTE
    except (UserActionError, MainCommitError, ProjectError, wpfile.WorkPackageError, OSError) as error:
        unplaced(f"{type(error).__name__}: {error}")
        return 1
    except Exception as error:
        unplaced(f"{type(error).__name__}: {error}")
        raise
    kind = "a new WP" if decision["decision"] == "new" else "an open WP"
    context.journal.emit("user-message-placed", f"placement {placement_id} put the user's request in {decision['wp']} ({kind}): {decision['why']} "
                         f"[{done}] Request: {excerpt(request['text'])}",
                         {**fields, "wp": decision["wp"], "decision": decision["decision"], **({"asked_about": request["wp"]} if request.get("wp") else {})})
    print(done)
    return 0
