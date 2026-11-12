"""Runs one work package through build, verify and review, in its worktree, until it passes.

A failed round (the builder stopped short, verification failed, the review requested changes) goes
straight back to the builder with its feedback. When `evaluate_after_failures` rounds have failed
since the last re-evaluation, or the builder reports itself blocked, a re-evaluation session on a
strong tier finds out why the WP is not converging and decides: continue from the feedback, revise
the WP file (scope, smaller steps, a prototype first), escalate the builder tier, split the WP, or
ask the user. After `orchestrator_after_evaluations` re-evaluations the orchestrator takes over.

A WP in `review` (its reviewer gave no verdict, or the review session failed) is re-verified and
re-reviewed without a builder. When no route can serve a session, the WP's status is restored and
the run stops with exit code 4 without counting the round.
Changes are read from git (everything the worktree changed since it left main), so changes outside
the WP's owns are caught whoever made them.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import findings, wpfile
from .autofix import configured_fixers, run_fixers
from .scope import scoped_checks
from .commands import CommandFailure, build_slot, changed_paths, matches_main, infrastructure_failure, run_commands_detailed, worktree_tree_id
from .context import Context
from .harness import SESSION_WORKER_AGENT, SHELL_CHAINING, CLAUDE_CLI_HARNESSES, command_prefixes, handoff_limit
from .inbox import clear_pause, inbox_of, pause_requested
from .platform import kill_tree, pid_alive
from .project import TASKS_RELATIVE_PATH, git
from .review_brief import ReviewBrief, build_review_brief
from .routing import Route, RoutingError, model_family_of, record_wp_mode, resolve_work_spec, spec_mode, with_mode, wp_mode
from .session import CompactionStop, NoRouteError, ResumeFailed, ResumeTarget, SessionResult, run_session, session_facts
from .session_brief import builder_skills, included_files, reviewer_skills
from .session_commands import HANDOFF_FILE, SESSION_VERIFIED_TREE_FILE
from .whats_new import NOTHING_TO_TRY, TRY_IT_SECTION, try_it_problem

EXIT_NO_ROUTE = 4
# The file keeps its historical name so ledgers written before rounds replaced attempts still load.
LEDGER_FILE = "attempts.json"
VERDICT = re.compile(r"VERDICT:\s*(PASS|CHANGES_REQUESTED)")
# Tolerates markdown around the line (**DECISION: continue**); the argument is trimmed of it.
DECISION = re.compile(r"(?m)^[ \t*_`>]*DECISION:[ \t*_`]*(continue|revise|escalate|split|needs-user)\b[ \t]*(.*)$")
DECISION_ARGUMENT_TRIM = " \t*_`.'\""
REEVALUATION_SECTION = "Re-evaluation"
# Failures the builder fixes best from where it stopped; a blocked builder is re-evaluated instead.
RESUMABLE_FAILURES = ("verification failed", "review requested changes", "builder stopped short", "session interrupted")
NO_ROUTE_FILE = "no-route.json"
VERIFIED_TREE_FILE = "verified-tree.txt"


# Sessions whose work an independent review judges.
WRITER_ROLES = ("builder", "executor", "assembler")


class RoundLedger:
    """Builder rounds, failures since the last re-evaluation, and the latest feedback, kept across
    runner restarts. A WP set back to todo (new, or reset by the orchestrator) starts afresh.

    Rounds are not a budget: a failure is a signal that the WP was harder or worse specified than
    planned. The runner retries straight from the feedback, and once failures pile up without a
    pass it asks a re-evaluation session why and acts on its decision.

    A round is open from its count until its builder session ends. A runner that finds an open
    round (its predecessor was stopped or crashed mid-build) does not count it."""

    HISTORY_ENTRIES = 6
    HISTORY_CHARACTERS = 3000

    def __init__(self, path: Path) -> None:
        self.path = path
        self.state = self.migrated(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else self.fresh()
        self.interrupted = bool(self.state.get("open"))
        if self.interrupted:
            self.uncount()
            # The tree holds a partial round no recorded session knows of: the next round starts fresh.
            self.record_builder(None)

    @staticmethod
    def fresh() -> dict:
        return {"rounds": 0, "failures": 0, "evaluations": 0, "feedback": "", "guidance": "", "history": [], "escalated_to": None, "open": False,
                "last_builder": None, "interruptions": 0, "handoff": None, "handoffs": 0, "writing": None, "builder_model_family": None}

    @property
    def last_builder(self) -> dict | None:
        """The latest builder session a next round may continue: route, harness, session id,
        largest context, the tree's HEAD and the builder spec when it ended."""
        return self.state["last_builder"]

    def record_builder(self, session: dict | None) -> None:
        self.state["last_builder"] = session
        self.save()

    @classmethod
    def migrated(cls, state: dict) -> dict:
        """Ledgers written as attempt counts: every counted attempt that left feedback failed."""
        if "rounds" in state:
            return {**cls.fresh(), **state}
        attempts = int(state.get("attempts", 0))
        return {**cls.fresh(), "rounds": attempts, "failures": attempts if state.get("feedback") else 0,
                "feedback": state.get("feedback", ""), "open": bool(state.get("open"))}

    @staticmethod
    def escalation(path: Path) -> str | None:
        """The spec a re-evaluation escalated the WP to, read without opening the ledger."""
        return json.loads(path.read_text(encoding="utf-8")).get("escalated_to") if path.exists() else None

    def save(self) -> None:
        self.path.write_text(json.dumps(self.state, indent=2) + "\n", encoding="utf-8")

    def reset(self) -> None:
        self.state = self.fresh()
        self.save()

    def close(self) -> None:
        """The open round's builder session ended; the round happened."""
        self.state["open"] = False
        self.save()

    @property
    def rounds(self) -> int:
        return int(self.state["rounds"])

    @property
    def failures(self) -> int:
        return int(self.state["failures"])

    @property
    def evaluations(self) -> int:
        return int(self.state["evaluations"])

    @property
    def feedback(self) -> str:
        return self.state["feedback"]

    @property
    def history(self) -> list[dict]:
        return self.state["history"]

    @property
    def escalated_to(self) -> str | None:
        return self.state["escalated_to"]

    @property
    def interruptions(self) -> int:
        """Builder sessions cut off in a row (each continued without counting a round)."""
        return int(self.state["interruptions"])

    def interrupted_round(self, feedback: str) -> None:
        """The builder session ended short of review or blocked without a failure the run can name
        (a turn that stopped mid-task): not judged as a failed round. The round is not counted and no failure is recorded; the history
        keeps it, so the next round continues that session."""
        self.state["rounds"] = max(0, self.rounds - 1)
        self.state["open"] = False
        self.state["interruptions"] = self.interruptions + 1
        self.state["feedback"] = feedback
        entry = {"round": self.rounds + 1, "kind": "session interrupted", "at": f"{datetime.now():%Y-%m-%d %H:%M}", "feedback": feedback[:self.HISTORY_CHARACTERS]}
        self.state["history"] = [*self.history, entry][-self.HISTORY_ENTRIES:]
        self.save()

    def finished_session(self) -> None:
        """A builder session ended on its own (it reached review, or failed on the work)."""
        self.state["interruptions"] = 0
        self.state["handoffs"] = 0
        self.save()

    @property
    def handoff(self) -> dict | None:
        """The handoff the next fresh builder session continues from: {"text", "why"}."""
        return self.state["handoff"]

    @property
    def handoffs(self) -> int:
        """Builder sessions of the open round that handed off to a fresh one."""
        return int(self.state["handoffs"])

    def store_handoff(self, text: str, why: str) -> None:
        self.state["handoff"] = {"text": text, "why": why}
        self.save()

    def handoff_delivered(self) -> None:
        """A builder session started from the stored handoff and ended: its own Progress log and
        handoff, if any, supersede it."""
        self.state["handoff"] = None
        self.save()

    def handed_off(self, text: str, why: str) -> None:
        """The builder session wrote a handoff and ended before finishing: the round goes on in a
        fresh session that starts from the handoff, without counting another round."""
        self.state["rounds"] = max(0, self.rounds - 1)
        self.state["open"] = False
        self.state["handoffs"] = self.handoffs + 1
        self.state["handoff"] = {"text": text, "why": why}
        self.state["last_builder"] = None
        self.save()

    def count(self) -> int:
        self.state["rounds"] = self.rounds + 1
        self.state["open"] = True
        self.save()
        return self.rounds

    @property
    def writing(self) -> dict | None:
        """Builder sessions writing since they last ended cleanly: {"since": epoch seconds the first
        of them started, "deleted_before": paths already deleted then, "why": why the latest was cut
        off, or None while one runs}. Present when a round begins means the previous session was cut
        off (a cap, a crash, a dead runner) and may have stopped mid-write."""
        return self.state["writing"]

    def writing_started(self, since: float, deleted_before: list[str]) -> None:
        """A builder session starts writing; a chain of cut-off sessions keeps its first start."""
        if self.writing is None:
            self.state["writing"] = {"since": since, "deleted_before": deleted_before, "why": None}
            self.save()

    def cut_off(self, why: str) -> None:
        if self.writing is not None:
            self.state["writing"] = {**self.writing, "why": why}
            self.save()

    def wrote_cleanly(self) -> None:
        """The builder session ended on its own terms: its harness closed its last turn itself."""
        self.state["writing"] = None
        self.save()

    def uncount(self) -> None:
        self.state["rounds"] = max(0, self.rounds - 1)
        self.state["open"] = False
        self.save()

    def fail(self, kind: str, feedback: str, force_evaluation_after: int | None = None) -> None:
        """Records a failed round; `force_evaluation_after` makes the next round re-evaluate first."""
        self.state["feedback"] = feedback
        self.state["interruptions"] = 0
        self.state["handoffs"] = 0
        self.state["failures"] = max(self.failures + 1, force_evaluation_after or 0)
        entry = {"round": self.rounds, "kind": kind, "at": f"{datetime.now():%Y-%m-%d %H:%M}", "feedback": feedback[:self.HISTORY_CHARACTERS]}
        self.state["history"] = [*self.history, entry][-self.HISTORY_ENTRIES:]
        self.save()

    @property
    def builder_feedback(self) -> str:
        """What the next builder is told: the latest failure, then the latest re-evaluation, which
        stays in force until the next one."""
        return "\n\n".join(part for part in (self.feedback, self.state["guidance"]) if part)

    def evaluated(self, decision: str, guidance: str, escalated_to: str | None) -> None:
        self.state["failures"] = 0
        self.state["evaluations"] = self.evaluations + 1
        self.state["guidance"] = guidance
        last = self.last_builder
        if last is not None:
            # 'continue' keeps the plan: the builder resumes with the guidance. Any other decision
            # changes the plan, the WP file or the tier: the previous session writes a handoff and
            # a fresh session starts from it.
            if decision == "continue":
                self.state["last_builder"] = {**last, "evaluated_continue": True}
            else:
                self.state["last_builder"] = {**last, "fresh_reason": f"the re-evaluation decided '{decision}'"}
        if escalated_to:
            self.state["escalated_to"] = escalated_to
        self.save()

    def unblocked_start(self, clear_evaluations: bool) -> None:
        """Prepares for the WP being blocked and later unblocked by the orchestrator or the user."""
        self.state["failures"] = 0
        # Whoever unblocks it may change the WP or the tree: the next round starts fresh, from the
        # previous session's handoff.
        if self.last_builder is not None:
            self.state["last_builder"] = {**self.last_builder, "fresh_reason": "the WP was blocked and has been unblocked"}
        if clear_evaluations:
            self.state["evaluations"] = 0
        self.save()


def last_verdict(body: str) -> str | None:
    """The verdict on the review's last verdict line; earlier mentions do not count."""
    matches = VERDICT.findall(body)
    return matches[-1] if matches else None
FRONT_MATTER_BODY = re.compile(r"(?s)^---.*?\r?\n---\r?\n(.*)$")


@dataclass(frozen=True)
class CheckResult:
    """One verification: the feedback for the builder (None when it passed), the failing commands
    and the files changed outside owns."""

    feedback: str | None
    failures: list[CommandFailure]
    outside: list[str]


def is_mechanical(result: CheckResult, prefixes: list[str], rejects: list[str]) -> bool:
    """Whether every failure is a lint, format or style check and nothing else is wrong: each
    failing command is a single command (no ';', '&&', '|') whose words begin with one of
    `prefixes`' words, its output holds none of `rejects` (a compile or type error, which lint
    tools also report), and no file is outside owns."""
    if not result.failures or result.outside:
        return False
    return all(single_command(failure.command) and any(failure.command.split()[:len(prefix.split())] == prefix.split() for prefix in prefixes)
               and not any(marker in failure.output for marker in rejects) for failure in result.failures)


def single_command(command: str) -> bool:
    return not any(separator in command for separator in (";", "&&", "||", "|"))


def cold_cache(last: dict, settings: dict, now: datetime) -> bool:
    """Whether continuing the session would re-read a large context with its prompt cache expired:
    it ended longer ago than `cold_after_minutes` and its context reached `cold_context_tokens`.
    A fresh session starting from a handoff is then cheaper than one uncached re-read."""
    ended = last.get("ended")
    if not ended or last["context_tokens"] < settings.get("cold_context_tokens", 100000):
        return False
    return (now - datetime.fromisoformat(ended)).total_seconds() > 60 * settings.get("cold_after_minutes", 60)


def resume_refusal(last: dict, last_kind: str, settings: dict, builder_spec: str, head: str, handoff_tokens: int,
                   now: datetime | None = None) -> str | None:
    """Why the previous builder session must not be continued, or None when it may be. Past its
    harness's handoff point every continued turn re-reads more than a fresh session starting from
    a handoff would accumulate; after a long pause a large context is re-read without its cache."""
    limit = handoff_tokens
    now = now or datetime.now()
    reasons = [
        (bool(last.get("fresh_reason")), last.get("fresh_reason") or ""),
        (last_kind not in RESUMABLE_FAILURES and not last.get("evaluated_continue"), f"the last round ended '{last_kind}'"),
        (last["harness"] not in settings["harnesses"], f"harness '{last['harness']}' is not resumed"),
        (last["spec"] != builder_spec, f"the builder tier changed ({last['spec']} -> {builder_spec})"),
        (last["context_tokens"] >= limit, f"its context ({last['context_tokens']:,} tokens) reached the handoff point ({limit:,})"),
        (last["head"] != head, "the tree's HEAD moved since (a merge from main)"),
        (cold_cache(last, settings, now),
         f"it ended at {last.get('ended')} with {last['context_tokens']:,} tokens of context, past its prompt cache"),
    ]
    return next((text for refused, text in reasons if refused), None)


def assistant_texts(log_path: Path) -> list[str]:
    """The top-level assistant text blocks of a stream-json session log, in order."""
    texts: list[str] = []
    if not log_path.is_file():
        return texts
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "assistant" or event.get("parent_tool_use_id"):
            continue
        for block in event.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip():
                texts.append(block["text"].strip())
    return texts


def review_text(final_text: str, log_path: Path, review_number: int) -> str:
    """The review to record: the session's final answer, or, when that answer gives a verdict but no
    finding in the protocol's format (a reviewer that re-checked something after writing its review
    and then closed with a bare verdict), the latest earlier message holding both its findings and a
    verdict. Without this the findings live only in the log and the ledger sees none."""
    body = final_text.strip()
    if last_verdict(body) != "CHANGES_REQUESTED" or findings.parse_review(body, review_number).findings:
        return body
    for text in reversed(assistant_texts(log_path)):
        if last_verdict(text) is not None and findings.parse_review(text, review_number).findings:
            return text
    return body


class RunStopped(Exception):
    """No route could serve a session; the WP's status has been restored."""


class RunPaused(Exception):
    """The orchestrator asked to pause the WP (`wpo wrap-up`); `result` is the session that just
    ended, None when the pause came before a session started."""

    def __init__(self, result: SessionResult | None) -> None:
        super().__init__("paused at the orchestrator's request")
        self.result = result


@dataclass
class RunOptions:
    builder_override: str | None = None
    reviewer_override: str | None = None
    families: tuple[str, ...] = ()
    idle_minutes: float | None = None
    lane: str | None = None


def session_env(context: Context, wp_id: str, tree: Path) -> dict[str, str]:
    """Environment of every session and command of a WP: the project's, plus what `wpo delegate`
    needs to find its WP from inside a builder session."""
    return {**context.project.env_for(wp_id), "WPO_WORK_PACKAGE": wp_id, "WPO_TREE": str(tree), "WPO_PROJECT": str(context.project.root)}


def reviewer_instructions(context: Context) -> str:
    """The user's reviewer agent body, prepended to every review so any route keeps its standards."""
    agent_file = context.machine.reviewer_agent
    if not agent_file.exists():
        return ""
    match = FRONT_MATTER_BODY.match(agent_file.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"{agent_file} has no front matter followed by a body")
    return match.group(1).strip()


def shared_files_sentence(config: dict) -> str:
    shared = [entry["path"] for entry in config["shared_files"]]
    return f", and the shared files {', '.join(shared)} within their rules (in {config['instructions_file']})" if shared else ""


def lead_instructions(context: Context, wp: wpfile.WorkPackage, delegate_tier: str) -> str:
    """A builder with delegation is a lead: workers on free models type out well-specified pieces
    and the builder plans, checks and fixes, so subscriptions pay for judgment, not volume."""
    tasks = f"{TASKS_RELATIVE_PATH.as_posix()}/{wp.id}"
    return f"""
## Delegating to free models
You are this WP's lead. Workers on free models cost the subscriptions nothing, so hand them every piece of
the WP you can specify precisely, and keep for yourself what needs judgment: design, cross-file contracts,
reverse engineering, subtle numerics, and anything whose mistakes are hard to spot.
- Good pieces: a new module or file whose types, signatures and behaviour you spell out; the tests for code
  that exists; mechanical conversions, tables and data files; documentation drafts from notes you give.
- Workers know only the WP file and the project's rules, so state the goal, the exact files, the public API with
  signatures, the behaviour and edge cases, the tests and the commands that must pass.
- Start one: `wpo delegate start --name <name> --write-paths <globs inside owns> '<task>'` (a long task can go in a
  file under {tasks}/, gitignored, passed with --prompt-file). Workers on disjoint paths run in parallel; leave a
  running worker's paths alone.
- Collect it: run `wpo delegate wait --name <name>` in the background; you are woken when the worker finishes (workers
  have no time limit). Then check its diff, fix what falls short and log the outcome: you own the result.
- Collect every worker before you finish; one still running when your session ends is stopped. A task that ends
  no-route or failed is yours to do. Workers use the tier {delegate_tier} (--tier research for documentation).
"""


def wp_relative(context: Context, wp: wpfile.WorkPackage) -> str:
    return f"{context.project.wp_dir_relative}/{wp.file_name}"


def wp_files(context: Context, wp: wpfile.WorkPackage, tree: Path, reading: list[str]) -> str:
    """The role's reading from the main checkout (current, like the configuration), then the WP file
    from the tree (the live copy the session works on)."""
    return "\n\n".join(part for part in (included_files(context.project.root, reading), included_files(tree, [wp_relative(context, wp)])) if part)


def shell_rules(context: Context, access: str, lead: bool) -> str:
    """How a session runs commands: what the sandbox allows, and how a denial is handled."""
    allowed = ", ".join(command_prefixes(access, context.project.config, lead))
    return (f"- Shell: commands run from the repository root. {SHELL_CHAINING} "
            f"Allowed: {allowed}. Inspect files with Read, Glob and Grep. A denied command that an allowed tool replaces (a shell "
            "read, listing or search: use Read, Glob, Grep) is no blocker, for you or a worker you lead: use the allowed tool and "
            "go on. Set 'status: blocked' only when the work needs an effect no allowed tool gives, logging which command and why; "
            "never reach that effect another way.")


def builder_rules(context: Context, wp: wpfile.WorkPackage, lead: bool) -> str:
    """The WP protocol for a builder: progress, checks, shell, Try it, status."""
    fixers = ", ".join(fixer.name for fixer in configured_fixers(context.project.config))
    fixed = f" It applies the runner's fixers first ({fixers}): formatting they handle needs no work from you." if fixers else ""
    environment = "".join(f"\n  - {line}" for line in context.project.config["environment"])
    environment = f"\n- This machine (known facts; do not spend turns re-discovering them):{environment}" if environment else ""
    return f"""- Record progress with `wpo log '<what was done; what is next; blockers>'` when a step completes, adding `--step N`
  to tick step N, chained with your next command (`wpo log '...' --step 1 && cargo test ...`). Quote the message in single quotes and write no apostrophe in it. It writes the dated Progress log line;
  never edit the Progress log yourself.
{shell_rules(context, "builder", lead)}
- Before finishing, fill the '## {TRY_IT_SECTION}' section (add it before '## Handoff' if missing): a fenced block of the exact
  commands the user runs from the main checkout once this is merged, and what to look for; or, when nothing can be tried by
  hand, a line starting '{NOTHING_TO_TRY}' saying why.
- A criterion's test must be able to fail: it fails on code that breaks the criterion. When a criterion compares with
  behaviour before this WP, record that behaviour from the unchanged code before you edit it and pin the recorded values;
  a test comparing the new code path with itself proves nothing and is rejected in review.
- When every step and acceptance criterion is done, run `wpo verify`: it checks the Try it section, runs the WP's Verify
  commands and owns check, prints only what failed, and sets the status to review when everything passes. Fix what it
  reports and run it again.{fixed} Do not run the Verify commands, a formatter or a linter yourself just before it: it runs them.
  Finish in as few calls as you can: write the evidence, Try it and Handoff, then `wpo log '...' && wpo verify`.
- An exact file in 'owns' does not cover new files beside it: put new tests inline (`#[cfg(test)] mod tests {{ ... }}`) unless
  'owns' has a glob there.
- Set 'status: blocked' only for something outside the WP's control (a rule, a missing tool or install, a decision only the
  user can make), logging why; hard but in-scope work is not a block. When an over-reading of the Goal is the only obstacle
  and every criterion is met, explain the gap in the Handoff and run `wpo verify`.
- After three experiments in a row that teach you nothing new, stop: log what you tried and what you would need, and
  set 'status: blocked' if nothing else in the WP can proceed. Do not re-reason the same hypothesis. Tune numbers by
  sweeping every parameter over a wide grid in one run (one printed line per point) before changing a design.{environment}"""


HANDOFF_CONTENTS = (
    "what is done; what is half done and exactly where it stands; what comes next, in order; what you learned that the files do "
    "not say (why things failed, approaches that did not work and why, facts about APIs, data and tools, the commands that "
    "matter and their results); and the open questions. Name files, functions and commands. The next session reads the WP "
    "file, its Progress log and the tree itself, so do not repeat them")


def handoff_request(reason: str) -> str:
    """The message a running builder gets when its handoff is due (session.HandoffTrigger)."""
    return (f"Message from the orchestrator: hand off now, because {reason}. A fresh session continues this work package from "
            "what you write. At the next safe point, finish or revert the edit in progress so the tree is consistent, record "
            "progress with `wpo log`, and collect any workers you started. Do not start new work and do not run `wpo verify`. "
            f"Then record the handoff with `wpo handoff - <<'EOF'` (the text, then a line EOF): {HANDOFF_CONTENTS}. Then end your turn.")


def handoff_turn_prompt(wp: wpfile.WorkPackage, why: str, feedback: str) -> str:
    """Resumes an ended builder session once, for the handoff the next round's fresh session starts from."""
    failed = (f"\nYour round did not pass. The fresh session gets this feedback too, so make the handoff serve fixing it:\n{feedback}\n"
              if feedback else "")
    return (f"The runner starts the next builder round of work package {wp.id} in a fresh session, because {why}. Do not continue "
            f"the work and do not edit any file. Record the handoff the fresh session starts from with `wpo handoff - <<'EOF'` "
            f"(the text, then a line EOF): {HANDOFF_CONTENTS}.\n{failed}\nThen end your turn.")


def take_handoff(run_dir: Path) -> str | None:
    """The handoff a session recorded with `wpo handoff`, removed so no later session reuses it."""
    path = run_dir / HANDOFF_FILE
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    path.unlink()
    return text or None


def builder_prompt(context: Context, wp: wpfile.WorkPackage, tree: Path, round_number: int, feedback: str, delegate_tier: str | None,
                   handoff: dict | None = None, recovery: str = "") -> str:
    config = context.project.config
    files = wp_files(context, wp, tree, config["builder_reading"])
    rules = "\n".join(config["prompt_rules"])
    rejected = f"\nThe previous round did not pass. Fix these problems first:\n{feedback}\n" if feedback else ""
    lead = lead_instructions(context, wp, delegate_tier) if delegate_tier else ""
    continued = (f"\nThe previous builder session of this work package handed off to you ({handoff['why']}). Continue from its handoff; "
                 f"the tree and the Progress log are the record of what it did:\n<handoff>\n{handoff['text']}\n</handoff>\n" if handoff else "")
    return f"""{files}

You are the builder of work package {wp.id}, whose file is above; its status is already in_progress. Implement it as specified,
changing only its 'owns' paths, its WP file{shared_files_sentence(config)}. Work already under the owns paths may come from an earlier
session: the Progress log says where it stopped. This is builder round {round_number}; a '## {REEVALUATION_SECTION}' section, if any, says
what changed since earlier rounds.
{builder_rules(context, wp, delegate_tier is not None)}
{rules}{continued}{recovery_section(recovery)}{rejected}{lead}
You are done when `wpo verify` passes: it is the only way to review, and it sets the status itself (never set
'status: review' by hand). If you cannot proceed, set 'status: blocked' and log the reason with `wpo log`.
"""


def elapsed_text(seconds: float) -> str:
    """A duration as a reader takes it in at a glance: '40 s', '12 min', '3 h 5 min', '2 d 4 h'."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def recovery_section(report: str) -> str:
    return f"\n{report}\n" if report else ""


def resume_prompt(context: Context, wp: wpfile.WorkPackage, round_number: int, feedback: str, recovery: str = "") -> str:
    """Continues the previous builder session: only what changed since its last turn and what to fix."""
    fixers = ", ".join(fixer.name for fixer in configured_fixers(context.project.config)) or "none"
    rules = "\n".join(context.project.config["prompt_rules"])
    return f"""Builder round {round_number} for work package {wp.id}. You are continuing your own session from the previous round, which did not pass.
Fix these problems first:
{feedback}
{recovery_section(recovery)}
Since your last turn the runner set the WP's status back to in_progress and appended to its Progress log; its fixers ({fixers}) may
have reformatted files you changed, and a repair session may have edited the files its diagnostics named. Re-read a file before
editing it when your edit depends on its exact text; do not re-read the codebase otherwise.
Record progress with `wpo log '<message>' [--step N]` (single quotes, no apostrophe in the message). When every step and
acceptance criterion is done, run `wpo verify`.
{rules}
You are done when `wpo verify` passes: it is the only way to review, and it sets the status itself (never set
'status: review' by hand). If you cannot proceed, set 'status: blocked' and log the reason with `wpo log`.
"""


def clipped(output: str, limit: int) -> str:
    """Head and tail of a long output: lint lists its first diagnostics first, its summary last."""
    if len(output) <= limit:
        return output
    half = limit // 2
    return f"{output[:half]}\n[... {len(output) - limit:,} characters cut ...]\n{output[-half:]}"


def repair_prompt(context: Context, wp: wpfile.WorkPackage, failures: list[CommandFailure]) -> str:
    """A closed task for a cheap session: these diagnostics, these files, no exploration."""
    config = context.project.config
    rules = "\n".join(config["prompt_rules"])
    limit = int(config["repair_output_chars"])
    checks = "\n\n".join(f"### `{failure.command}` (exit {failure.exit_code})\n```\n{clipped(failure.output, limit)}\n```" for failure in failures)
    return f"""You are a repair session on work package {wp.id} in this repository. Its Verify commands failed only on the
mechanical checks below (lint, format, style). Fix exactly these diagnostics, in the files they name, and nothing else.
- Do not explore the codebase, refactor, rename, or change behaviour: make the smallest edit that satisfies each diagnostic.
- If a diagnostic cannot be fixed without changing behaviour or design, leave it and say which and why.
- Never edit the WP file. You may change only files matching: {', '.join(wp.owns)}
Shell commands run from the repository root. {SHELL_CHAINING} Re-run each failing command to confirm.
{rules}

## Failing checks

{checks}

End with a short report: the files you changed, and whether each command now passes.
"""


def review_brief_section(context: Context, brief: ReviewBrief, brief_path: Path) -> str:
    """The brief itself when it fits the prompt budget; otherwise its symbol index and where the diff
    is (`brief_path` relative to the reviewer's tree)."""
    if len(brief.full_text) <= int(context.project.config["review_inline_chars"]):
        return brief.full_text
    return (f"{brief.index_text}\n\n## Diff\n\nThe diff is too large for this prompt ({len(brief.full_text):,} characters with the index). "
            f"It is in {brief_path.as_posix()} after the '## Diff' heading: read it in ranges, file by file.")


def review_prompt(context: Context, wp: wpfile.WorkPackage, tree: Path, brief_section: str, review_number: int, run_dir: Path) -> str:
    config = context.project.config
    files = wp_files(context, wp, tree, config["reviewer_reading"])
    # A reviewer once applied the root Cargo.toml's rule to a crate's own Cargo.toml the WP owns.
    shared_rules = [f"Changes to the repository path {entry['path']} (that exact path; files of the same name elsewhere follow the WP's owns) "
                    f"must be limited to {entry['rule']}; anything else there is BLOCKING." for entry in config["shared_files"] if entry.get("rule")]
    commands = ", ".join(f"'{command}'" for command in config["reviewer_commands"])
    rules = "\n".join([*config["review_rules"], *shared_rules])
    return f"""{reviewer_instructions(context)}

{files}

Review work package {wp.id} ({wp.title}), whose file is above.
Start from the review brief below: the diff of everything the WP changed, and for each symbol it adds, removes or changes,
where it is used. Read beyond the diff only where it leaves a question open (surrounding code, a caller the index points
to, a contract), and then only the lines you need.
Check correctness, the project's rules, the coding standards, and whether each acceptance criterion is genuinely met. Check that the
WP's '## {TRY_IT_SECTION}' section gives commands that exist and would show the work (a wrong or missing command is BLOCKING). Do not modify any file.
The runner has already run the WP's Verify commands and they passed. You may run {commands} to confirm a finding, from the current directory. {SHELL_CHAINING}
{rules}

{findings.REVIEW_PROTOCOL}

This is review {review_number} of {wp.id}: number new findings R{review_number}-F1, R{review_number}-F2, and so on.
{findings.ledger_text(run_dir)}

{findings.REVIEW_OUTPUT_FORMAT.replace("{review}", str(review_number))}

{brief_section}
"""


def evaluation_prompt(context: Context, wp: wpfile.WorkPackage, tree: Path, ledger: RoundLedger, builder_spec: str, run_dir: Path, why: str) -> str:
    """Asks a strong model why a WP is not converging and what the next round should do differently."""
    config = context.project.config
    wp_path = wp_relative(context, wp)
    files = wp_files(context, wp, tree, config["builder_reading"])
    tiers = "; ".join(f"{name} (score floor {tier['floor']})" for name, tier in context.router.policy["tiers"].items())
    history = "\n\n".join(f"Round {entry['round']}, {entry['kind']} ({entry['at']}):\n{entry['feedback']}" for entry in ledger.history) or "none recorded"
    rejections = sum(1 for entry in ledger.history if entry["kind"] == "review requested changes")
    contract = (f"\nReviews rejected this WP {rejections} times in the history below. If two of them reject the same class of defect "
                "(the same kind of precision, invariant, matching rule or behaviour), another patch will not converge: decide 'revise' "
                "and write the structural direction and an explicit contract (a precision contract, an invariant, an exact rule) into "
                "the Context note, so the builder builds to it and the reviewer judges against it.\n") if rejections >= 2 else ""
    return f"""{files}

You are re-evaluating work package {wp.id} ({wp.title}), whose file is above: {why}.
So far: {ledger.rounds} builder rounds, {ledger.evaluations} earlier re-evaluations. Its builder runs on '{builder_spec}'.
A failed round is not a reason to give up or to cap effort. A WP that keeps failing was usually harder than planned, or
its file misleads the builder, or the reviews disagree with each other. Find out which, and change what the next round does.

Weigh its Goal, Context, Steps, Acceptance criteria, Progress log, Review and any earlier '{REEVALUATION_SECTION}' entries.
Look at the work so far with 'git diff {context.project.main_branch} --stat'
and the diffs that matter. Verify logs and session logs are in {run_dir.as_posix()} (attempt-N-verify.log and so on).
{SHELL_CHAINING}

Entries marked 'session interrupted' were sessions cut off by the harness or the machine, not failures of the work.{contract}
The latest failures, oldest first:
{history}

Reviews and builders work under the protocol below; a finding still disputed after two rejected vetoes is yours to settle.
Say which side the WP file supports; when the dispute comes from the WP file itself, decide 'revise' and make it unambiguous.
{findings.REVIEW_PROTOCOL}

{findings.ledger_text(run_dir)}

Decide exactly one:
- continue: the failures converge and the next builder can fix them from the feedback. Say what to focus on.
- revise: the WP file itself is the problem. Edit it: a Context note headed '**Re-evaluation note ({datetime.now():%Y-%m-%d}).**'
  that scopes a finding that over-reads the Goal, reorders the Steps into smaller ones, or puts a prototype or spike step
  first; a necessary path the WP must change may be added to 'owns' when no other WP owns it. Keep the Goal's intent and
  every acceptance criterion: never weaken a check so that failing work passes. Never edit the Progress log or Review.
- escalate <tier>: the work needs more capability than '{builder_spec}'. Tiers: {tiers}.
- split: the WP is several WPs. Describe the split (titles, owns, order, dependencies) for the orchestrator.
- needs-user <question>: only a decision the user must make blocks it.
You may edit only {wp_path}. Explain your reasoning briefly; it is recorded in the WP file for the next builder.
End with exactly one line: 'DECISION: continue', 'DECISION: revise', 'DECISION: escalate <tier>', 'DECISION: split'
or 'DECISION: needs-user <question>'.
"""


def stop_delegated_workers(context: Context, wp: wpfile.WorkPackage) -> None:
    """A worker still editing after its lead's session ended would race verification and review."""
    if context.machine.execution_mode == "t3":
        from .t3 import Bridge
        bridge = Bridge(context)
        bridge.cancel_wp(wp.id, "builder session ended; collect its remaining native workers", role="worker")
        # Verification must not race a native task after its local bridge worker is stopped.
        for path in (bridge.directory / "requests").glob("*.json"):
            request = json.loads(path.read_text(encoding="utf-8"))
            if request["payload"].get("wp") == wp.id and request["payload"].get("role") == "worker" and request["state"] not in ("done", "failed", "cancelled"):
                bridge.wait(request)
    delegate_dir = context.project.runs_dir / wp.id / "delegate"
    for state_path in delegate_dir.glob("*/state.json") if delegate_dir.exists() else ():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        spawn_pid = state_path.parent / "spawn.pid"
        pid = state.get("worker_pid") or (spawn_pid.read_text(encoding="utf-8").strip() if spawn_pid.exists() else None)
        if state.get("state") != "running" or not pid or not pid_alive(int(pid), ("python", "python3")):
            continue
        kill_tree(int(pid))
        wpfile.log_progress(wpfile.parse(wp.path), f"stopped worker '{state_path.parent.name}', still running when the builder session ended")


class WorkPackageRun:
    """One invocation of the runner for one WP in its tree."""

    def __init__(self, context: Context, wp_id: str, tree: Path, options: RunOptions) -> None:
        self.context = context
        self.wp_id = wp_id
        self.tree = tree
        self.options = options
        self.config = context.project.config
        self.run_dir = context.project.wp_run_dir(wp_id)
        self.env = session_env(context, wp_id, tree)
        wp = self.wp()
        self.lane = context.project.lane(options.lane or wp.lane)
        self.families = tuple(options.families or self.lane.get("families", ()))
        self.evaluate_after = int(self.config["evaluate_after_failures"])
        self.ledger = RoundLedger(self.run_dir / LEDGER_FILE)
        # Whether the session running now belongs to a counted builder round (uncounted on no route).
        self.counting_round = False
        (self.run_dir / NO_ROUTE_FILE).unlink(missing_ok=True)

    def wp(self) -> wpfile.WorkPackage:
        return self.context.wp_in_tree(self.wp_id, self.tree)

    def log(self, message: str) -> None:
        wpfile.log_progress(self.wp(), message)

    def emit(self, kind: str, message: str) -> None:
        self.context.journal.emit(kind, message, {"wp": self.wp_id})

    def session(self, spec: str, role: str, prompt: str, log_stem: Path, status_to_restore: str, **extra) -> object:
        access = "reviewer" if role == "reviewer" else "builder"
        # A builder session that never started wrote nothing: the writing record goes back to what it was.
        writing_before = extra.pop("writing_before", self.ledger.writing)
        if pause_requested(self.run_dir) is not None:
            wpfile.set_status(self.wp(), status_to_restore)
            raise RunPaused(None)
        # Every role, staged ones included, routes in the WP's mode (or the one its spec names), which
        # is recorded once a route is chosen, so the WP keeps it whatever happens to this runner.
        mode = spec_mode(spec) or self.mode
        spec = with_mode(spec, mode)
        try:
            result = run_session(self.context, spec, extra.pop("access", access), prompt, log_stem, self.tree, families=extra.pop("families", self.families),
                                 idle_minutes=self.options.idle_minutes, event_fields={"wp": self.wp_id, "role": role}, extra_env=self.env,
                                 inbox=inbox_of(self.run_dir), on_attempt=self.mode_recorder(mode), **extra)
        except NoRouteError as error:
            if role == "builder" and not error.failures:
                self.ledger.state["writing"] = writing_before
                self.ledger.save()
            wpfile.set_status(self.wp(), status_to_restore)
            self.log(f"no route for the {role} ({spec}): {error}; round not counted; status {status_to_restore}; run stopped")
            # The supervisor sleeps on this: the spec that failed may not be the one it pre-checked
            # (staged tiers, a busy local model).
            (self.run_dir / NO_ROUTE_FILE).write_text(json.dumps({"spec": spec, "role": role, "next_available_at": error.next_available_at,
                                                                  "detail": str(error)}), encoding="utf-8")
            if self.counting_round:
                self.ledger.uncount()
            raise RunStopped(f"{self.wp_id}: no route for the {role} ({spec})") from error
        except CompactionStop as stopped:
            # A staged session stopped at compaction may still have built the WP (an assembler
            # whose verify passed): it counts as the latest writer.
            self.record_writer(role, self.context.router.find_route(stopped.route_id))
            raise
        self.log(f"{role} session ran on {result.route['id']}{result.fallback_note}")
        self.record_writer(role, result.route)
        if pause_requested(self.run_dir) is not None:
            raise RunPaused(result)
        return result

    def record_writer(self, role: str, route: dict | Route) -> None:
        """Remembers who made the model of the latest session that built the WP (a builder, or a
        staged executor or assembler): an independent review leaves that family out. Planners,
        reviewers, re-evaluations and mechanical repairs build nothing of the WP's design."""
        if role in WRITER_ROLES:
            self.ledger.state["builder_model_family"] = model_family_of(route)
            self.ledger.save()

    def mode_recorder(self, mode: str):
        return lambda: record_wp_mode(self.context.project.runs_dir, self.wp_id, mode)

    @property
    def mode(self) -> str:
        """The routing mode the WP's first session ran in, else the project's (recorded by `session`)."""
        return wp_mode(self.context.project.runs_dir, self.wp_id)

    @property
    def builder_spec(self) -> str:
        # A re-evaluation's escalation outranks the lane's default (a pool lane's local pin).
        spec = self.options.builder_override or self.ledger.escalated_to or self.lane.get("builder_override") or self.wp().builder
        return with_mode(spec, self.mode)

    @property
    def reviewer_spec(self) -> str:
        return with_mode(self.options.reviewer_override or self.wp().reviewer, self.mode)

    @property
    def staged(self) -> bool:
        return self.wp().mode == "staged" or self.lane.get("mode") == "staged"

    def build(self, round_number: int, feedback: str) -> str:
        """Runs the builder (or the staged pipeline). Returns 'review'; 'failed' after recording a
        failed round in the ledger; 'interrupted' when the session was cut off before its RESULT
        line (the round is not counted and the next round continues that session); or 'handoff'
        when the session wrote a handoff and ended (the round goes on in a fresh session that starts
        from it). Raises RunStopped when no route can serve it."""
        wp = self.wp()
        status_before = wp.status
        failures_before = self.ledger.failures
        result: SessionResult | None = None
        wpfile.set_status(wp, "in_progress")
        self.log(f"round {round_number} started for builder {'staged mode' if self.staged else self.builder_spec}")
        if self.staged:
            from .staged import StagedBuild  # staged mode imports the runner's helpers
            StagedBuild(self, round_number, feedback, status_before).run()
        else:
            delegate_tier = self.available_delegate_tier()
            recovery = ""
            if self.ledger.writing is not None:
                # The previous session never ended on its own terms: what it left may be half written.
                recovery = self.recovery_report(round_number, self.ledger.writing["why"] or "the runner stopped while it was writing")
            resume = self.resume_target(round_number, feedback, status_before, delegate_tier, recovery)
            handoff = None if resume is not None else self.ledger.handoff
            prompt = builder_prompt(self.context, self.wp(), self.tree, round_number, feedback, delegate_tier, handoff, recovery)
            writing_before = self.ledger.writing
            self.ledger.writing_started(time.time(), [path.removesuffix(" (deleted)") for path in changed_paths(self.tree, self.context.project.main_branch)
                                                      if path.endswith(" (deleted)")])
            try:
                # A continued interrupted session and a session continuing a handoff get their own
                # logs, so no session's log is overwritten.
                stem = (f"attempt-{round_number}-builder" + (f"-continued-{self.ledger.interruptions}" if self.ledger.interruptions else "")
                        + (f"-handoff-{self.ledger.handoffs}" if self.ledger.handoffs else ""))
                result = self.session(self.builder_spec, "builder", prompt, self.run_dir / stem, status_before,
                                      lead=delegate_tier is not None, skills=builder_skills(self.context, self.wp().skills), resume=resume,
                                      handoff_message=handoff_request, recovery=lambda why: self.recovery_report(round_number, why),
                                      writing_before=writing_before)
            except RunPaused as paused:
                if paused.result is not None:
                    self.record_builder_session(paused.result)
                raise
            finally:
                stop_delegated_workers(self.context, self.wp())
            self.record_builder_session(result)
            if handoff is not None:
                self.ledger.handoff_delivered()
            # A session that comes back completed its last turn: a cut-off one fails on its route
            # and reaches the next route with a recovery report instead.
            self.ledger.wrote_cleanly()
        wp = self.wp()
        recorded = take_handoff(self.run_dir)
        if wp.status not in ("review", "blocked") and result is not None and recorded:
            why = result.handoff_reason or "it handed off on its own"
            self.ledger.handed_off(recorded, why)
            self.log(f"builder session handed off ({why}); round not counted; a fresh session continues from its handoff")
            self.emit("session-handoff", f"{self.wp_id} builder session on {result.route['id']} handed off ({why}); a fresh session continues")
            return "handoff"
        if wp.status == "blocked":
            # Builders often block on something that is not a real block (an over-read Goal, a
            # one-line fix outside owns); the re-evaluation decides whether it is.
            wpfile.set_status(wp, "changes_requested")
            self.log("builder reported blocked; a re-evaluation decides whether the block is real")
            self.ledger.fail("builder blocked", f"The builder set 'blocked'; its reason is the latest Progress log entry ({wp.latest_progress()})",
                             force_evaluation_after=self.evaluate_after)
        elif wp.status != "review":
            limit = int(self.config["interrupted_session_resumes"])
            if result is not None and self.ledger.interruptions < limit:
                self.ledger.interrupted_round("Your previous session ended before `wpo verify` passed and without setting 'blocked'. "
                                              "Continue where you stopped; the Progress log says how far you got.")
                self.log(f"builder session ended short of review (status {wp.status}); round not counted; its session is continued")
                self.emit("session-interrupted", f"{self.wp_id} builder session on {result.route['id']} ended short of review; continued without counting a round")
                return "interrupted"
            wpfile.set_status(wp, "changes_requested")
            self.log(f"builder did not reach review (status {wp.status})")
            self.ledger.fail("builder stopped short", f"The builder ended with status '{wp.status}' instead of 'review'. Finish the remaining steps.")
        if self.ledger.failures != failures_before:
            return "failed"
        self.ledger.finished_session()
        return "review"

    def head(self) -> str:
        return git(self.tree, "rev-parse", "HEAD").stdout.strip()

    def resume_target(self, round_number: int, feedback: str, status_to_restore: str, delegate_tier: str | None,
                      recovery: str = "") -> ResumeTarget | None:
        """The previous builder session to continue, or None (with the reason logged) for a fresh one.

        Continuing keeps everything the builder already read; a fresh session pays to read it
        again. Past the harness's handoff point every continued turn re-reads more than a fresh
        session would accumulate, so it starts over. A fresh session after a recorded one starts
        from that session's handoff (collect_handoff)."""
        last = self.ledger.last_builder
        if last is None:
            return None
        last_kind = self.ledger.history[-1]["kind"] if self.ledger.history else "unknown"
        reason = resume_refusal(last, last_kind, self.config["resume"], self.builder_spec, self.head(), handoff_limit(self.config, last["harness"]))
        if reason:
            self.log(f"fresh builder session: {reason}")
            self.collect_handoff(last, reason, round_number, feedback, status_to_restore, delegate_tier)
            return None
        self.log(f"continuing builder session {last['session_id']} on {last['route']} ({last['context_tokens']:,} tokens of context)")
        return ResumeTarget(last["route"], last["session_id"], resume_prompt(self.context, self.wp(), round_number, feedback, recovery))

    def collect_handoff(self, last: dict, why: str, round_number: int, feedback: str, status_to_restore: str, delegate_tier: str | None) -> None:
        """Resumes the previous builder session once, with the same tools and brief so its cached
        prefix still matches, for the handoff the fresh session starts from. Without it the fresh
        session has only the Progress log and the tree."""
        prompt = handoff_turn_prompt(self.wp(), why, feedback)
        try:
            result = self.session(last["spec"], "builder", prompt, self.run_dir / f"attempt-{round_number}-handoff", status_to_restore,
                                  lead=delegate_tier is not None, skills=builder_skills(self.context, self.wp().skills),
                                  resume=ResumeTarget(last["route"], last["session_id"], prompt), resume_only=True)
        except ResumeFailed as error:
            self.log(f"no handoff from the previous builder session: {error}")
            return
        finally:
            self.ledger.record_builder(None)
        text = take_handoff(self.run_dir)
        if not text:
            self.log(f"the previous builder session on {result.route['id']} wrote no handoff")
            return
        self.ledger.store_handoff(text, why)
        self.log(f"the previous builder session on {result.route['id']} wrote its handoff ({len(text):,} characters)")

    def recovery_report(self, round_number: int, why: str) -> str:
        """What a cut-off builder left, for the session that picks the WP up: every file it wrote or
        deleted with how long ago each was last edited, the diff of the most recent one (where a
        mid-write cut-off lands), and the Verify output on the tree as it stands. When the cut-off session(s) changed nothing
        (a route that refused at once), the tree is the one the round started from: Verify is not
        rerun on it."""
        self.ledger.cut_off(why)
        writing = self.ledger.writing
        since = float(writing["since"])
        changed = changed_paths(self.tree, self.context.project.main_branch)
        written = []
        for path in changed:
            if path.endswith(" (deleted)"):
                continue
            modified = (self.tree / path).stat().st_mtime
            if modified >= since:
                written.append((modified, path))
        deleted = [path.removesuffix(" (deleted)") for path in changed
                   if path.endswith(" (deleted)") and path.removesuffix(" (deleted)") not in writing["deleted_before"]]
        now = time.time()
        written.sort()
        files = [f"- {path} (last edited {elapsed_text(now - modified)} ago)" for modified, path in written]
        files += [f"- {path} (deleted)" for path in deleted]
        limit = int(self.config["repair_output_chars"])
        latest = ""
        if written:
            latest_path = written[-1][1]
            base = git(self.tree, "merge-base", "HEAD", self.context.project.main_branch).stdout.strip()
            tracked = git(self.tree, "ls-files", "--error-unmatch", "--", latest_path, check=False).returncode == 0
            # An untracked file has no base: its diff against an empty file is all of it.
            diff = (git(self.tree, "diff", base, "--", latest_path, check=False).stdout if tracked
                    else git(self.tree, "diff", "--no-index", "--", "/dev/null", latest_path, check=False).stdout)
            latest = f"\n\nDiff of the most recently edited file, {latest_path}:\n```diff\n{clipped(diff, limit)}\n```"
        if not written and not deleted:
            self.log(f"recovery report: {why}; nothing written or deleted since {datetime.fromtimestamp(since):%H:%M}")
            return (f"## Recovery: the previous builder session was cut off\n"
                    f"Why: {why}. It changed no file, so the tree is as this round found it.")
        commands, _ = scoped_checks(self.config, self.tree, self.wp().round_checks(self.config["verify_round"], self.config["verify_workspace"]),
                                    changed_paths(self.tree, self.context.project.main_branch))
        log_path = self.run_dir / f"attempt-{round_number}-recovery-verify-{datetime.now():%H%M%S}.log"
        with build_slot(int(self.config["build_slots"]), None, self.log):
            failures = run_commands_detailed(self.config["shell"], commands, self.tree, self.env, log_path, self.config["command_timeout_minutes"])
        failed = {failure.command for failure in failures}
        verify = [f"{'FAIL' if command in failed else 'pass'}: {command}" for command in commands]
        verify += [f"\n### {failure.command} (exit {failure.exit_code})\n{clipped(failure.output, limit)}" for failure in failures]
        self.log(f"recovery report: {why}; {len(written)} files written, {len(deleted)} deleted since {datetime.fromtimestamp(since):%H:%M}; "
                 f"{len(failures)} of {len(commands)} Verify commands fail")
        return (f"## Recovery: the previous builder session was cut off\n"
                f"Why: {why}. It may have stopped in the middle of an edit, and the Progress log may lag behind the files.\n\n"
                f"Files the cut-off session(s) wrote, starting {elapsed_text(now - since)} ago:\n"
                + ("\n".join(files) or "- none") + latest
                + "\n\nVerify on the tree as it was left (no fixers applied):\n" + "\n".join(verify))

    def available_delegate_tier(self) -> str | None:
        """The workers' tier when a worker route can serve it now; None keeps the WP whole, so a
        builder spends no turns writing task specs for workers that would end no-route."""
        tier = self.wp().delegate_tier(self.config["delegate_default"])
        if tier is None:
            return None
        try:
            selection = self.context.router.select(with_mode(tier, self.mode), self.config["worker_families"])
        except RoutingError as error:
            self.log(f"no delegation this round: {error}")
            return None
        if not any(candidate.get("not_dispatchable_reason") is None for candidate in selection.candidates):
            self.log(f"no delegation this round: no worker route serves {tier} now")
            return None
        return tier

    def record_builder_session(self, result: SessionResult) -> None:
        """Remembers the session a next round may continue (Claude-CLI harnesses keep conversations)."""
        if result.route["harness"] not in CLAUDE_CLI_HARNESSES:
            self.ledger.record_builder(None)
            return
        session_id, context_tokens = session_facts(result.log_path)
        # Without an id or any usage the session's size is unknown: never continue it blind.
        if session_id is None or context_tokens == 0:
            self.ledger.record_builder(None)
            return
        self.ledger.record_builder({"route": result.route["id"], "harness": result.route["harness"], "session_id": session_id,
                                    "context_tokens": context_tokens, "head": self.head(), "spec": self.builder_spec,
                                    "ended": datetime.now().isoformat(timespec="seconds")})

    def autofix(self) -> None:
        """Applies the project's fixers to the files this WP changed and owns (never shared files)."""
        wp = self.wp()
        wp_dir = self.context.project.wp_dir_relative
        files = [path for path in changed_paths(self.tree, self.context.project.main_branch)
                 if not path.endswith(" (deleted)") and wpfile.path_owned(path, wp, wp_dir, ())]
        for report in run_fixers(self.tree, files, configured_fixers(self.config), self.env, self.config["command_timeout_minutes"]):
            self.log(f"autofix: {report}")

    def check(self, round_number: int, log_suffix: str = "") -> CheckResult:
        """Fixers, then the Verify commands and the owns check."""
        self.autofix()
        wp = self.wp()
        changed = changed_paths(self.tree, self.context.project.main_branch)
        shared = [entry["path"] for entry in self.config["shared_files"]]
        wp_dir = self.context.project.wp_dir_relative
        # The orchestrator amends upcoming WP files in a worktree; those are not violations.
        outside = [path for path in changed if not wpfile.path_owned(path.removesuffix(" (deleted)"), wp, wp_dir, shared)
                   and not wpfile.is_wp_file(path, wp_dir) and not matches_main(self.tree, self.context.project.main_branch, path)]
        commands, scope_line = scoped_checks(self.config, self.tree, wp.round_checks(self.config["verify_round"], self.config["verify_workspace"]), changed)
        self.log(scope_line)
        with build_slot(int(self.config["build_slots"]), None, self.log):
            failures = run_commands_detailed(self.config["shell"], commands, self.tree, self.env,
                                             self.run_dir / f"attempt-{round_number}-verify{log_suffix}.log", self.config["command_timeout_minutes"])
        texts = [failure.text for failure in failures]
        if not commands:
            texts.append("Nothing verifies this WP: no Verify commands (a fenced block under '## Verify') and no round checks.")
        if not texts and not outside:
            return CheckResult(None, [], outside)
        problems = []
        if texts:
            problems.append("Verify failures:\n" + "\n\n".join(texts))
        if outside:
            problems.append("Files changed outside the WP's owns paths (revert or justify them): " + ", ".join(outside))
        wpfile.set_status(wp, "changes_requested")
        self.log(f"verification failed ({len(texts)} failing commands, {len(outside)} files outside owns{': ' + ', '.join(outside) if outside else ''})")
        return CheckResult("\n\n".join(problems), failures if commands else [], outside)

    def repair(self, round_number: int, failures: list[CommandFailure]) -> bool:
        """Hands mechanical failures to a fresh session on `repair_spec` with only the diagnostics;
        returns whether a repair session ran (the caller re-verifies)."""
        spec = with_mode(self.config["repair_spec"], self.mode)
        commands = ", ".join(failure.command for failure in failures)
        self.emit("repair-attempted", f"{self.wp_id}: only mechanical checks failed ({commands}); a {spec} repair session fixes them first")
        try:
            # The lane's families hold here too; a cheap tier's order already puts free and local routes first.
            result = run_session(self.context, spec, "worker", repair_prompt(self.context, self.wp(), failures),
                                 self.run_dir / f"attempt-{round_number}-repair", self.tree, write_paths=self.wp().owns, families=self.families,
                                 idle_minutes=self.options.idle_minutes, event_fields={"wp": self.wp_id, "role": "repair"}, extra_env=self.env,
                                 skills=builder_skills(self.context, self.wp().skills), on_attempt=self.mode_recorder(self.mode))
        except NoRouteError as error:
            self.log(f"no route for a repair session ({spec}): {error}; the builder gets the failure")
            return False
        self.log(f"repair session ran on {result.route['id']}{result.fallback_note} for {commands}")
        return True

    def verify_round(self, round_number: int) -> str | None:
        """Verification with one repair try for mechanical failures; returns the feedback for the
        builder, or None when the WP passed and is in review again. A missing Try it section fails
        before the (slow) Verify commands run."""
        if self.config["require_try_it"] and (problem := try_it_problem(self.wp())):
            wpfile.set_status(self.wp(), "changes_requested")
            self.log(f"the '## {TRY_IT_SECTION}' section has no commands; verification not run")
            return problem
        if self.session_verified():
            wpfile.set_status(self.wp(), "review")
            self.log("verification skipped: the builder's wpo verify passed on this tree (only the WP file changed since)")
            return None
        result = self.check(round_number)
        if result.feedback is not None and not result.outside and infrastructure_failure(result.failures, self.config["infrastructure_failure_markers"]):
            self.defer_verification(result.failures)
        if result.feedback is None or not is_mechanical(result, self.config["mechanical_checks"], self.config["mechanical_rejects"]):
            return result.feedback
        if not self.repair(round_number, result.failures):
            return result.feedback
        retried = self.check(round_number, "-after-repair")
        if retried.feedback is None:
            wpfile.set_status(self.wp(), "review")
            self.log("the repair session's fixes pass verification")
            return None
        return f"A repair session tried to fix the mechanical failures first; these remain:\n{retried.feedback}"

    def session_verified(self) -> bool:
        """Whether the builder's own `wpo verify` passed on the tree as it is now, apart from the WP
        file (the runner's Progress log lines): verifying it again would only repeat the same build."""
        record = self.run_dir / SESSION_VERIFIED_TREE_FILE
        if not record.exists():
            return False
        verified = record.read_text(encoding="utf-8").strip()
        changed = git(self.tree, "diff", "--name-only", verified, worktree_tree_id(self.tree), check=False)
        if changed.returncode != 0:
            return False
        wp_path = wp_relative(self.context, self.wp())
        return all(path == wp_path for path in changed.stdout.splitlines() if path)

    def defer_verification(self, failures: list[CommandFailure]) -> None:
        """Every failed check failed on the machine (killed, out of disk or memory), so the code was
        not judged: the WP stays in review, no failed round is recorded, and the supervisor retries
        after `infrastructure_retry_minutes`."""
        minutes = float(self.config["infrastructure_retry_minutes"])
        commands = ", ".join(failure.command for failure in failures)
        wpfile.set_status(self.wp(), "review")
        self.log(f"verification failed on the machine, not the code ({commands}); no failed round; retried in {minutes:g} min")
        self.emit("verify-deferred", f"{self.wp_id}: every failed check failed on the machine ({commands}); verification retried in {minutes:g} min")
        report = {"spec": "verification", "role": "verify", "next_available_at": datetime.now().timestamp() + minutes * 60,
                  "detail": f"infrastructure failure in {commands}"}
        (self.run_dir / NO_ROUTE_FILE).write_text(json.dumps(report), encoding="utf-8")
        raise RunStopped(f"{self.wp_id}: verification deferred (infrastructure)")

    def review(self, round_number: int) -> str:
        """Returns the findings when changes are requested; raises WorkPackageEnded when the WP
        leaves the loop otherwise (done, or no verdict)."""
        if self.reviewer_spec == "none":
            self.finish("verified; no review required; done", f"{self.wp_id} verified (no review required)")
        # A git or brief error escapes as a runner failure (runner-failed), which the orchestrator reads.
        brief = build_review_brief(self.tree, self.context.project.main_branch, self.config)
        (self.run_dir / f"attempt-{round_number}-review-brief.md").write_text(brief.full_text, encoding="utf-8")
        # The reviewer reads from its worktree; the gitignored tasks directory never counts as a change.
        brief_relative = TASKS_RELATIVE_PATH / self.wp_id / "review-brief.md"
        (self.tree / brief_relative).parent.mkdir(parents=True, exist_ok=True)
        (self.tree / brief_relative).write_text(brief.full_text, encoding="utf-8")
        review_number = findings.next_review_number(self.run_dir)
        prompt = review_prompt(self.context, self.wp(), self.tree, review_brief_section(self.context, brief, brief_relative), review_number, self.run_dir)
        result = self.session(self.reviewer_spec, "reviewer", prompt, self.run_dir / f"attempt-{round_number}-review", "review",
                              skills=reviewer_skills(self.context), exclude_model_families=self.independent_review_exclusion())
        body = review_text(result.result_text, result.log_path, review_number)
        wpfile.append_to_section(self.wp(), "Review", [f"### Review {review_number}, round {round_number} ({result.route['id']} for {self.reviewer_spec}, {datetime.now():%Y-%m-%d %H:%M})", body])
        verdict = last_verdict(body)
        parsed = findings.parse_review(body, review_number)
        for note in parsed.orchestrator_notes:
            self.emit("review-orchestrator-note", f"{self.wp_id} review {review_number}: {note}")
        if verdict is None:
            wpfile.set_status(self.wp(), "review")
            self.log("reviewer gave no verdict; left in review for a human")
            self.emit("wp-blocked", f"{self.wp_id} reviewer gave no verdict; left in review")
            raise WorkPackageEnded
        still_open = findings.record_review(self.run_dir, review_number, parsed, passed=verdict == "PASS")
        if verdict == "PASS":
            self.finish("verified and review passed; done", f"{self.wp_id} verified and review passed")
        if not still_open:
            # Under the protocol only a BLOCKING finding, or a STILL-OPEN or VETO-REJECTED ruling, requests changes:
            # a rejection with none of them leaves the builder nothing it owes.
            wpfile.set_status(self.wp(), "review")
            self.log(f"review {review_number} requested changes without an open BLOCKING finding in the protocol's format; left in review")
            self.emit("wp-blocked", f"{self.wp_id} review {review_number} requested changes but no BLOCKING finding is open in the ledger; "
                      "read its Review entry, then rerun the review or pass it")
            raise WorkPackageEnded
        identifiers = ", ".join(finding["id"] for finding in still_open)
        wpfile.set_status(self.wp(), "changes_requested")
        self.log(f"review {review_number} requested changes: {identifiers}")
        self.emit("wp-changes-requested", f"{self.wp_id} review {review_number} requested changes (round {round_number}; open: {identifiers})")
        return findings.builder_feedback(still_open, body)

    def independent_review_exclusion(self) -> list[str]:
        """With `routing.independent_reviews`, the model family that last built the WP, which its
        review may not run on; the review waits for another family's route rather than fall back. A
        ledger written before the family was recorded falls back to its last builder session's route;
        with neither, the review runs unrestricted and says so."""
        if not self.context.router.policy.get("independent_reviews"):
            return []
        family = self.ledger.state.get("builder_model_family")
        if not family and (last := self.ledger.last_builder):
            try:
                family = model_family_of(self.context.router.find_route(last["route"]))
            except RoutingError:
                family = None
        if not family:
            self.log("independent review: the WP's builder is not recorded, so no family is left out")
            return []
        self.log(f"independent review: routes of {family} models are left out (the builder's)")
        return [family]

    def finish(self, progress: str, event: str) -> None:
        """Marks the WP done and records the tree that was verified, so integration knows whether
        what it commits is exactly what passed."""
        wpfile.set_status(self.wp(), "done")
        self.log(progress)
        (self.run_dir / VERIFIED_TREE_FILE).write_text(worktree_tree_id(self.tree), encoding="utf-8")
        self.emit("wp-done", event)
        raise WorkPackageEnded

    def end_blocked(self, kind: str, progress: str, event: str, hand_over: bool = False) -> None:
        """Blocks the WP. The ledger forgets its failures (and, when handed to the orchestrator, its
        re-evaluations), so whoever unblocks it starts with a builder round, not a re-evaluation."""
        self.ledger.unblocked_start(clear_evaluations=hand_over)
        wpfile.set_status(self.wp(), "blocked")
        self.log(progress)
        self.emit(kind, event)
        raise WorkPackageEnded

    def known_spec(self, spec: str) -> bool:
        try:
            resolve_work_spec(spec, self.context.router.policy)
        except RoutingError:
            return False
        return True

    def reevaluate(self) -> None:
        """Asks a strong model why the WP is not converging and applies its decision; raises
        WorkPackageEnded when the decision takes the WP out of this run."""
        ledger = self.ledger
        if ledger.evaluations >= int(self.config["orchestrator_after_evaluations"]):
            self.end_blocked("wp-needs-orchestrator", f"{ledger.evaluations} re-evaluations have not made it converge; handed to the orchestrator",
                             f"{self.wp_id} still fails after {ledger.evaluations} re-evaluations ({ledger.rounds} rounds); the orchestrator decides how to proceed",
                             hand_over=True)
        last_kind = ledger.history[-1]["kind"] if ledger.history else "unknown"
        why = "its builder reported itself blocked" if last_kind == "builder blocked" else f"{ledger.failures} rounds failed since the last re-evaluation"
        wp = self.wp()
        wp_path = f"{self.context.project.wp_dir_relative}/{wp.file_name}"
        prompt = evaluation_prompt(self.context, wp, self.tree, ledger, self.builder_spec, self.run_dir, why)
        self.log(f"re-evaluating: {why}")
        # Any family: a lane restricted to free or local models still gets a strong evaluator.
        result = self.session(with_mode(self.config["evaluation_spec"], self.mode), "evaluator", prompt, self.run_dir / f"evaluation-{ledger.evaluations + 1}", wp.status,
                              access="evaluator", write_paths=[wp_path], families=())
        body = result.result_text.strip()
        decisions = DECISION.findall(body)
        decision, argument = (decisions[-1][0], decisions[-1][1].strip(DECISION_ARGUMENT_TRIM)) if decisions else ("none", "")
        escalated_to = argument if decision == "escalate" and self.known_spec(argument) else None
        after_round = ledger.rounds
        # The ledger records the evaluation before the file shows it: a crash in between costs a
        # section entry, never a repeated evaluation or a lost escalation.
        ledger.evaluated(decision, f"The re-evaluation after round {after_round} decided '{decision}'"
                         f"{' and revised the WP file' if decision == 'revise' else ''}; its reasoning is the latest '{REEVALUATION_SECTION}' entry:\n{body}",
                         escalated_to)
        wpfile.ensure_section(self.wp(), REEVALUATION_SECTION, before="Review")
        wpfile.append_to_section(self.wp(), REEVALUATION_SECTION, [f"### After round {after_round} ({result.route['id']}, {datetime.now():%Y-%m-%d %H:%M})", body])
        if escalated_to:
            wpfile.set_field(self.wp(), "builder", escalated_to)
        self.emit("wp-reevaluated", f"{self.wp_id} re-evaluated after round {after_round}: {decision} {argument}".strip())
        self.log(f"re-evaluation decided {decision} {argument}".rstrip())
        if decision == "none":
            self.end_blocked("wp-needs-orchestrator", "re-evaluation gave no decision; handed to the orchestrator",
                             f"{self.wp_id} re-evaluation gave no DECISION line; read its '{REEVALUATION_SECTION}' section", hand_over=True)
        if decision == "escalate" and not escalated_to:
            self.end_blocked("wp-needs-orchestrator", f"re-evaluation escalated to an unknown spec '{argument}'",
                             f"{self.wp_id} re-evaluation escalated to unknown spec '{argument}'", hand_over=True)
        if decision == "split":
            self.end_blocked("wp-needs-orchestrator", "re-evaluation proposes splitting the WP; the orchestrator drafts the new WPs",
                             f"{self.wp_id} should be split; the proposal is in its '{REEVALUATION_SECTION}' section", hand_over=True)
        if decision == "needs-user":
            self.end_blocked("wp-blocked", f"re-evaluation needs a user decision: {argument}", f"{self.wp_id} needs a user decision: {argument}")
        if escalated_to and self.lane.get("pool"):
            # A pool lane's model was not enough; the WP's own lane builds it on the stronger tier.
            wpfile.set_status(self.wp(), "changes_requested")
            self.log(f"escalated to {escalated_to}; left to the {self.wp().lane or 'milestone'} supervisor")
            raise WorkPackageEnded

    def run(self) -> None:
        wp = self.wp()
        if self.ledger.interrupted:
            self.log(f"the previous runner stopped mid-build; that round is not counted ({self.ledger.rounds} rounds so far)")
        if wp.status == "todo":
            self.ledger.reset()  # a new WP, or one the orchestrator reset
        skip_builder = wp.status == "review"
        if skip_builder:
            self.log("status is review; re-verifying and re-reviewing without a builder session")
        try:
            while True:
                if skip_builder:
                    round_number = max(self.ledger.rounds, 1)
                    skip_builder = False
                else:
                    if self.ledger.failures >= self.evaluate_after:
                        self.reevaluate()
                    round_number = self.ledger.count()
                    self.counting_round = True
                    outcome = self.build(round_number, self.ledger.builder_feedback)
                    self.ledger.close()
                    self.counting_round = False
                    if outcome != "review":
                        continue
                    if self.lane.get("pool"):
                        # The pool's dedicated model moves on; the WP's own lane verifies and reviews it.
                        self.log("built to review; verification and review are left to its own lane")
                        raise WorkPackageEnded
                problems = self.verify_round(round_number)
                if problems:
                    self.ledger.fail("verification failed", problems)
                else:
                    self.ledger.fail("review requested changes", self.review(round_number))
                if self.lane.get("builds_in_pool"):
                    # Only a pool lane builds this lane's WPs; the next round waits for it.
                    self.log("round failed; the next build is left to the pool")
                    raise WorkPackageEnded
        except WorkPackageEnded:
            return
        except RunPaused as paused:
            self.end_paused(paused)

    def end_paused(self, paused: RunPaused) -> None:
        """Ends the run at the orchestrator's request: the round is not counted and the WP keeps the
        status its last session left, so the next run continues from its Progress log."""
        if self.counting_round:
            self.ledger.uncount()
            self.counting_round = False
        reason = pause_requested(self.run_dir) or ""
        clear_pause(self.run_dir)
        status = self.wp().status
        ended = f"after its {paused.result.route['id']} session" if paused.result is not None else "before its next session"
        self.log(f"paused at the orchestrator's request {ended}; round not counted; status {status}")
        self.context.journal.emit("wp-paused", f"{self.wp_id} paused {ended} (status {status}){f': {reason}' if reason else ''}", {"wp": self.wp_id})


class WorkPackageEnded(Exception):
    """The WP left the build loop (done, blocked, handed on, or left in review); not an error."""


def run_wp(context: Context, wp_id: str, tree: Path, options: RunOptions) -> int:
    """Runs a WP; returns 0, or EXIT_NO_ROUTE when no route could serve a session."""
    from .artifacts import space_problem
    if problem := space_problem(context.project, tree):
        wpfile.set_status(context.wp_in_tree(wp_id, tree), "blocked")
        context.journal.emit("wp-blocked", f"{wp_id}: {problem}", {"wp": wp_id})
        print(problem)
        return 0
    try:
        WorkPackageRun(context, wp_id, tree, options).run()
    except RunStopped as stopped:
        print(f"run stopped: {stopped}")
        return EXIT_NO_ROUTE
    return 0
