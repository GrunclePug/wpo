"""Messages from the orchestrator to a running session, and pause requests (`wpo message`, `wpo wrap-up`).

A WP's run directory holds an `inbox/`: each message is one file, delivered by the session watcher
(session.py) into the session's stream-json stdin between turns, then moved to `inbox/delivered/`.
Only Claude-CLI sessions read a stream; while one runs for the WP, `inbox/live.json` names it.

A pause request (`pause-requested` in the run directory) tells the runner to end the WP's run after
the current session, without counting the round; `wpo wrap-up` posts it with a message asking the
session to log its progress and stop at the next safe point.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .platform import pid_alive

INBOX_DIRECTORY = "inbox"
DELIVERED_DIRECTORY = "delivered"
LIVE_FILE = "live.json"
PAUSE_FILE = "pause-requested"
MESSAGE_PREFIX = "Message from the orchestrator (it arrived while you were working; act on it now):\n\n"
RUNNING_LINE = re.compile(r"\brunning (WP-[\w.-]+) \(")
EXIT_LINE = re.compile(r"\b(WP-[\w.-]+) runner exit\b")
WRAP_UP_MESSAGE = (
    "Wrap up now: the orchestrator is pausing this work package. At the next safe point, finish or revert the edit in "
    "progress so the tree is consistent, then record with `wpo log` exactly what is done, what is half done and what "
    "comes next, so a later session can continue from it. Leave the status as it is (do not set review "
    "unless every step and criterion is already done and verified). Do not start new work. Collect any workers you "
    "started. Then end with the line 'RESULT: paused'.")


def inbox_of(run_dir: Path) -> Path:
    return run_dir / INBOX_DIRECTORY


def post_message(run_dir: Path, text: str) -> Path:
    """Queues a message for the WP's running session; written whole before it becomes visible."""
    inbox = inbox_of(run_dir)
    inbox.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}.md"
    staging = inbox / f".{name}.tmp"
    staging.write_text(text, encoding="utf-8")
    os.replace(staging, inbox / name)
    return inbox / name


def pending_messages(inbox: Path) -> list[Path]:
    if not inbox.is_dir():
        return []
    return sorted(path for path in inbox.glob("*.md") if path.is_file())


def mark_delivered(message: Path) -> None:
    delivered = message.parent / DELIVERED_DIRECTORY
    delivered.mkdir(exist_ok=True)
    os.replace(message, delivered / message.name)


def write_live(inbox: Path, log_path: Path, pid: int) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / LIVE_FILE).write_text(json.dumps({"log": str(log_path), "pid": pid, "started": time.time()}), encoding="utf-8")


def clear_live(inbox: Path) -> None:
    (inbox / LIVE_FILE).unlink(missing_ok=True)


def live_session(run_dir: Path) -> dict | None:
    """The streaming session running for this WP, if its process is alive."""
    path = inbox_of(run_dir) / LIVE_FILE
    if not path.exists():
        return None
    live = json.loads(path.read_text(encoding="utf-8"))
    return live if pid_alive(int(live["pid"])) else None


def deliver_if_live(run_dir: Path, text: str) -> bool:
    """`wpo message --wp`: queues the message when a session that reads messages runs for the WP;
    False (nothing queued) when none does."""
    if live_session(run_dir) is None:
        return False
    post_message(run_dir, text)
    return True


def request_pause(run_dir: Path, reason: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / PAUSE_FILE).write_text(reason, encoding="utf-8")


def pause_requested(run_dir: Path) -> str | None:
    path = run_dir / PAUSE_FILE
    return path.read_text(encoding="utf-8") if path.exists() else None


def clear_pause(run_dir: Path) -> None:
    (run_dir / PAUSE_FILE).unlink(missing_ok=True)


def lane_running_wp(runs_dir: Path, lane: str) -> str | None:
    """The WP a lane's supervisor is running now, from its log: the last 'running <WP>' line with no
    'runner exit' for that WP after it."""
    log = runs_dir / f"supervisor-{lane}.log"
    if not log.exists():
        return None
    return running_wp_in_log(log.read_text(encoding="utf-8", errors="replace"))


def running_wp_in_log(text: str) -> str | None:
    """The WP a supervisor log shows running: its last 'running <WP>' line not followed by that WP's
    'runner exit' line."""
    running: str | None = None
    for line in text.splitlines():
        if (started := RUNNING_LINE.search(line)) is not None:
            running = started.group(1)
        elif (ended := EXIT_LINE.search(line)) is not None and ended.group(1) == running:
            running = None
    return running
