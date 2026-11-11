"""Commands a session needs that its sandbox denies: the session asks, the orchestrator decides, and
an approved command runs once, exactly as approved.

    wpo ask-approval '<command>' '<why>'      (in a WP session) ask; carry on with other work
    wpo approve <id> | wpo deny <id> --reason  (the orchestrator) decide; the session gets a message
    wpo run-approved <id>                      (in the WP's session) run the approved command once

A session cannot wait on a person mid-call, so a request is asynchronous: it is journaled as an
attention event, the decision reaches the session through its inbox (inbox.py), and the approved
command runs through `wpo run-approved`, which every session may call. Each approval holds one exact
command for one WP and is spent when it runs; nothing widens the session's allowlist.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from .context import Context
from .inbox import live_session, post_message
from .session_commands import SessionCommandError, session_wp

APPROVALS_DIRECTORY = "approvals"
STATES = ("pending", "approved", "denied", "used")


class ApprovalError(Exception):
    """An approval that does not exist, belongs to another WP, or is not in the state the command needs."""


def approvals_dir(context: Context, wp_id: str) -> Path:
    return context.project.wp_run_dir(wp_id) / APPROVALS_DIRECTORY


def approval_path(context: Context, approval_id: str) -> Path:
    wp_id, separator, _ = approval_id.rpartition("-A")
    if not separator or not wp_id:
        raise ApprovalError(f"'{approval_id}' is not an approval id (WP-xxxx-An)")
    return approvals_dir(context, wp_id) / f"{approval_id}.json"


def load(context: Context, approval_id: str) -> dict:
    path = approval_path(context, approval_id)
    if not path.exists():
        raise ApprovalError(f"no approval request {approval_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def save(context: Context, approval: dict) -> None:
    path = approval_path(context, approval["id"])
    staging = path.with_suffix(".json.tmp")
    staging.write_text(json.dumps(approval, indent=2) + "\n", encoding="utf-8")
    os.replace(staging, path)


def request(context: Context, command: str, why: str) -> dict:
    """Records a session's request and journals it for the orchestrator."""
    command, why = command.strip(), why.strip()
    if not command or not why:
        raise SessionCommandError("give the exact command and why the work needs it")
    wp, tree = session_wp(context)
    directory = approvals_dir(context, wp.id)
    directory.mkdir(parents=True, exist_ok=True)
    number = 1 + max((int(path.stem.rpartition("-A")[2]) for path in directory.glob(f"{wp.id}-A*.json")), default=0)
    approval = {"id": f"{wp.id}-A{number}", "wp": wp.id, "command": command, "why": why, "tree": str(tree),
                "route": os.environ.get("WPO_ROUTE", ""), "role": os.environ.get("WPO_ROLE", ""),
                "requested": datetime.now().isoformat(timespec="seconds"), "state": "pending", "decision": ""}
    save(context, approval)
    context.journal.emit("approval-requested", f"{wp.id} asks to run `{command}`: {why} "
                         f"(wpo approve {approval['id']} | wpo deny {approval['id']} --reason '...')", {"wp": wp.id})
    return approval


def decide(context: Context, approval_id: str, approved: bool, reason: str) -> str:
    """Approves or denies a pending request and tells the WP's running session."""
    approval = load(context, approval_id)
    if approval["state"] != "pending":
        raise ApprovalError(f"{approval_id} is {approval['state']}, not pending")
    approval["state"] = "approved" if approved else "denied"
    approval["decision"] = reason
    approval["decided"] = datetime.now().isoformat(timespec="seconds")
    save(context, approval)
    if approved:
        text = (f"Your request {approval_id} is approved: run `wpo run-approved {approval_id}` to run `{approval['command']}` "
                f"once, exactly as asked.{f' Note: {reason}' if reason else ''}")
    else:
        text = (f"Your request {approval_id} to run `{approval['command']}` is denied: {reason or 'no reason given'}. Do the work "
                "without it; if it cannot proceed without it, set 'status: blocked' and log why.")
    run_dir = context.project.wp_run_dir(approval["wp"])
    delivered = live_session(run_dir) is not None
    if delivered:
        post_message(run_dir, text)
    context.journal.emit("approval-decided", f"{approval_id} {approval['state']}: `{approval['command']}`"
                         f"{'' if delivered else ' (no live session to tell; the next session finds it in the run directory)'}",
                         {"wp": approval["wp"]})
    return f"{approval_id} {approval['state']}; " + ("the session was told" if delivered else "no session that reads messages is running")


def run_approved(context: Context, approval_id: str) -> int:
    """Runs an approved command once, in the session's tree, through bash (the shell sessions'
    Bash tools use); its output streams through and its exit code is returned."""
    wp, tree = session_wp(context)
    approval = load(context, approval_id)
    if approval["wp"] != wp.id:
        raise ApprovalError(f"{approval_id} was approved for {approval['wp']}, not {wp.id}")
    if approval["state"] != "approved":
        raise ApprovalError(f"{approval_id} is {approval['state']}; only an approved request runs")
    bash = shutil.which("bash")
    if bash is None:
        raise ApprovalError("bash is not on PATH; the approved command cannot run")
    # Spent before it runs: a crash mid-run must not leave a second use available.
    approval["state"] = "used"
    approval["used"] = datetime.now().isoformat(timespec="seconds")
    save(context, approval)
    timeout = float(context.project.config["command_timeout_minutes"]) * 60
    completed = subprocess.run([bash, "-c", approval["command"]], cwd=tree, timeout=timeout)
    return completed.returncode
