"""Staged mode: a stronger planner cuts a WP into small tasks a small-context model runs fresh.

Long sessions on small-context models churn in compaction, re-reading files they already read. In
staged mode one attempt is:

1. A planner session (planner tier) writes `plan.json`: ordered tasks, each naming the files it
   edits, the interface it provides and the interfaces it consumes, and a check command, sized so
   one task fits the configured context budget.
2. Each task runs in a fresh executor session (executor tier) limited to the task's files. A task
   whose session reaches compaction is stopped and sent back to the planner to split; a task whose
   check fails gets one retry with the failure, then goes back to the planner.
3. An assembler session runs the Verify commands, fixes small integration gaps, ticks the WP's steps,
   writes the Handoff and sets `review`. The normal verify and review follow.

Plans and task states persist (plan in the worktree, state in the run directory), so an interrupted
attempt resumes at the first unfinished task.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import wpfile
from .commands import run_commands
from .harness import SHELL_CHAINING
from .project import TASKS_RELATIVE_PATH
from .session import CompactionStop
from .session_brief import builder_skills, included_files


class PlanError(Exception):
    """A plan that does not satisfy the plan contract; the planner is asked to fix it."""


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    files: list[str]
    provides: str
    consumes: list[str]
    check: str
    instructions: str


def parse_plan(text: str, owns_check, allowed_prefixes: list[str]) -> list[Task]:
    """Validates a plan: unique ids, files inside owns, consumed tasks earlier in the list, check
    commands the executor is allowed to run."""
    try:
        raw_tasks = json.loads(text)["tasks"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise PlanError(f"plan.json is not an object with a 'tasks' list: {error}") from error
    tasks: list[Task] = []
    for index, raw in enumerate(raw_tasks):
        try:
            task = Task(str(raw["id"]), str(raw["title"]), list(raw["files"]), str(raw["provides"]), list(raw.get("consumes", [])),
                        str(raw["check"]), str(raw["instructions"]))
        except (KeyError, TypeError) as error:
            raise PlanError(f"task {index} lacks a field: {error}") from error
        if any(existing.id == task.id for existing in tasks):
            raise PlanError(f"duplicate task id '{task.id}'")
        if not task.files:
            raise PlanError(f"task '{task.id}' names no files")
        outside = [path for path in task.files if not owns_check(path)]
        if outside:
            raise PlanError(f"task '{task.id}' edits paths outside the WP's owns: {', '.join(outside)}")
        unknown = [dependency for dependency in task.consumes if dependency not in {existing.id for existing in tasks}]
        if unknown:
            raise PlanError(f"task '{task.id}' consumes tasks that do not come before it: {', '.join(unknown)}")
        if not any(task.check == prefix or task.check.startswith(prefix + " ") for prefix in allowed_prefixes):
            raise PlanError(f"task '{task.id}' check '{task.check}' does not start with an allowed command ({', '.join(allowed_prefixes)})")
        tasks.append(task)
    if not tasks:
        raise PlanError("the plan has no tasks")
    return tasks


PLAN_CONTRACT = """Write the plan as JSON to {plan_path} (create the directories if needed), exactly this shape:
{{"tasks": [{{"id": "t1", "title": "...", "files": ["repo/relative/path.rs"], "provides": "the public interface this task creates: types, signatures, behaviour",
  "consumes": ["ids of earlier tasks whose interfaces it uses"], "check": "one command proving the task works, e.g. cargo test -p <crate> <filter>",
  "instructions": "everything the executor needs: what to write, edge cases, tests to add"}}]}}
Rules for the plan:
- An executor knows only the task you give it, the interfaces of the tasks it consumes, AGENTS.md and the WP file. It has a context
  budget of about {budget} tokens for reading files and writing code, so a task must need at most a few files read and a few hundred
  lines written. Split anything larger.
- Every file a task edits must be inside the WP's owns. Two tasks never edit the same file unless the later one consumes the earlier.
- Order tasks so every consumed task comes first. Interfaces in 'provides' are exact: names, signatures, field types, error cases.
- 'check' must start with one of: {prefixes}.
- Leave integration across tasks, the WP's Steps checkboxes and the Handoff to the assembly session that follows.
Do not write any code yourself; write only the plan file."""


class StagedBuild:
    """The staged builder pipeline for one attempt of a WorkPackageRun."""

    def __init__(self, run, attempt: int, feedback: str, status_before: str) -> None:
        self.wp_run = run
        self.attempt = attempt
        self.feedback = feedback
        self.status_before = status_before
        project = run.context.project
        self.settings = {**project.config["staged"], **run.lane.get("staged", {})}
        self.plan_relative = f"{TASKS_RELATIVE_PATH.as_posix()}/{run.wp_id}/plan.json"
        self.plan_path = run.tree / self.plan_relative
        self.state_dir = run.run_dir / "staged"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "state.json"
        self.executor_prefixes = list(project.config["builder_commands"])

    # ------------------------------------------------------------------------------------- state

    def load_state(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"replans": 0, "tasks": {}}

    def save_state(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    def owns_check(self, path: str) -> bool:
        project = self.wp_run.context.project
        return wpfile.path_owned(path, self.wp_run.wp(), project.wp_dir_relative, ()) and not wpfile.is_wp_file(path, project.wp_dir_relative)

    def emit(self, kind: str, message: str) -> None:
        self.wp_run.context.journal.emit(kind, message, {"wp": self.wp_run.wp_id})

    # ---------------------------------------------------------------------------------- planning

    def wp_file(self) -> str:
        """The WP file, inlined at the top of every staged prompt."""
        wp = self.wp_run.wp()
        return included_files(self.wp_run.tree, [f"{self.wp_run.context.project.wp_dir_relative}/{wp.file_name}"])

    def plan(self, reason: str, state: dict) -> list[Task]:
        """Runs the planner until it writes a valid plan (two tries per call)."""
        wp = self.wp_run.wp()
        done = [task_id for task_id, entry in state["tasks"].items() if entry["state"] == "done"]
        contract = PLAN_CONTRACT.format(plan_path=self.plan_relative, budget=self.settings["context_tokens"], prefixes=", ".join(self.executor_prefixes))
        problem = ""
        for planner_try in (1, 2):
            existing = self.plan_path.read_text(encoding="utf-8") if self.plan_path.exists() else ""
            prompt = f"""{self.wp_file()}

You are the planner for work package {wp.id}, whose file is above. Read the code under its owns that the plan depends on.
Small-context executors will build the WP one task at a time, each in a fresh session. Cut the remaining work into tasks.
{contract}
{f"Why you are planning: {reason}" if reason else ""}
{f"Tasks already done (keep their ids and interfaces; plan only the rest): {', '.join(done)}" if done else ""}
{f"The current plan, to revise:{chr(10)}{existing}" if existing else ""}
{f"Your last plan was rejected: {problem}" if problem else ""}
{f"The previous attempt was rejected for:{chr(10)}{self.feedback}" if self.feedback else ""}
End with one line: 'PLAN: written'."""
            self.wp_run.session(self.settings["planner"], "planner", prompt, self.state_dir / f"attempt-{self.attempt}-plan-{state['replans']}-{planner_try}",
                             self.status_before, access="docs", write_paths=[self.plan_relative])
            try:
                tasks = parse_plan(self.plan_path.read_text(encoding="utf-8") if self.plan_path.exists() else "", self.owns_check, self.executor_prefixes)
            except PlanError as error:
                problem = str(error)
                continue
            (self.state_dir / f"plan-{state['replans']}.json").write_text(self.plan_path.read_text(encoding="utf-8"), encoding="utf-8")
            self.emit("staged-plan", f"{wp.id}: plan with {len(tasks)} tasks ({', '.join(task.id for task in tasks)})")
            return tasks
        raise PlanError(problem)

    # --------------------------------------------------------------------------------- execution

    def execute(self, task: Task, tasks: list[Task], retry_feedback: str) -> bool:
        """Runs one task in a fresh executor session; returns whether its check passes. Raises
        CompactionStop when the task does not fit the context budget."""
        wp = self.wp_run.wp()
        consumed = "\n".join(f"- {other.id} ({other.title}): {other.provides}" for other in tasks if other.id in task.consumes)
        prompt = f"""{self.wp_file()}

You are an executor on work package {wp.id}, whose file is above (for context only: never edit it).
You may change only these files: {', '.join(task.files)}
Shell commands run from the repository root. {SHELL_CHAINING}
Read only the files you need: your context is small, and running out of it ends your session.

# Task {task.id}: {task.title}
{task.instructions}

It must provide: {task.provides}
{f"It uses these interfaces from earlier tasks (already in the tree):{chr(10)}{consumed}" if consumed else ""}
Done means this command passes: {task.check}
{f"Your previous try failed its check:{chr(10)}{retry_feedback}" if retry_feedback else ""}
End with a short report: the files you changed and whether the check passed."""
        self.wp_run.session(self.settings["executor"], "executor", prompt, self.state_dir / f"attempt-{self.attempt}-{task.id}", self.status_before,
                         access="worker", write_paths=task.files, stop_on_compaction=True, skills=builder_skills(self.wp_run.context, wp.skills))
        failures = run_commands(self.wp_run.config["shell"], [task.check], self.wp_run.tree, self.wp_run.env, self.state_dir / f"attempt-{self.attempt}-{task.id}-check.log",
                                self.wp_run.config["command_timeout_minutes"])
        self.last_check_failure = "\n".join(failures)
        return not failures

    def assemble(self, tasks: list[Task]) -> None:
        wp = self.wp_run.wp()
        summary = "\n".join(f"- {task.id} ({task.title}): {task.provides}" for task in tasks)
        prompt = f"""{self.wp_file()}

You are the assembler for work package {wp.id}, whose file is above. Executors built these tasks one at a time:
{summary}
Tick each completed step and record it with `wpo log --step N '<message>'` (single quotes, no apostrophe in the message), fill
the '## Try it' section (the exact commands the user runs from the main checkout once this is merged, and what to look for) and
write the Handoff. Then run `wpo verify` from the repository root: it checks
the Try it section, runs the WP's Verify commands and owns check, and sets the status to review when everything passes. Fix what
fails when the fix is small and inside the WP's owns, and run it again. If something large is missing, record what with `wpo log`.
A passing `wpo verify` is the only way to review."""
        try:
            self.wp_run.session(self.settings["assembler"] or self.settings["executor"], "assembler", prompt,
                             self.state_dir / f"attempt-{self.attempt}-assemble", self.status_before, stop_on_compaction=True,
                             skills=builder_skills(self.wp_run.context, wp.skills))
        except CompactionStop as stopped:
            wpfile.log_progress(self.wp_run.wp(), f"assembler reached compaction and was stopped ({stopped.log_path}); the next attempt resumes")

    def run(self) -> None:
        """Plans, executes and assembles; a WP that does not reach review returns to the runner,
        which counts the attempt."""
        try:
            self._run()
        except PlanError as error:
            wpfile.log_progress(self.wp_run.wp(), f"the planner could not write a valid plan: {error}")

    def _run(self) -> None:
        state = self.load_state()
        tasks: list[Task] | None = None
        if self.plan_path.exists():
            try:
                tasks = parse_plan(self.plan_path.read_text(encoding="utf-8"), self.owns_check, self.executor_prefixes)
            except PlanError:
                tasks = None
        if tasks is None:
            tasks = self.plan("" if not self.feedback else "the previous attempt was rejected", state)
        retry_feedback: dict[str, str] = {}
        index = 0
        while index < len(tasks):
            task = tasks[index]
            entry = state["tasks"].setdefault(task.id, {"state": "todo", "tries": 0})
            if entry["state"] == "done":
                index += 1
                continue
            entry["tries"] += 1
            reason = ""
            try:
                passed = self.execute(task, tasks, retry_feedback.get(task.id, ""))
            except CompactionStop:
                passed, reason = False, f"task '{task.id}' did not fit an executor's context (its session reached compaction); split it"
            if passed:
                entry["state"] = "done"
                self.save_state(state)
                self.emit("staged-task", f"{self.wp_run.wp_id}: task {task.id} done")
                wpfile.log_progress(self.wp_run.wp(), f"staged task {task.id} ({task.title}) done", "runner")
                index += 1
                continue
            if not reason and entry["tries"] < 2:
                retry_feedback[task.id] = self.last_check_failure
                self.save_state(state)
                continue
            reason = reason or f"task '{task.id}' failed its check twice:\n{self.last_check_failure}"
            if state["replans"] >= int(self.settings["max_replans"]):
                wpfile.log_progress(self.wp_run.wp(), f"staged mode gave up after {state['replans']} replans: {reason[:300]}")
                self.save_state(state)
                return
            state["replans"] += 1
            entry["tries"] = 0
            self.save_state(state)
            tasks = self.plan(reason, state)
            index = 0
        self.assemble(tasks)
