"""Keeps a lane's (or a milestone's) work packages moving unattended.

Runs one WP at a time in its own worktree (branch <prefix><id>, created from main), each through a
`wpo run` subprocess, and integrates WPs that end done. Dependencies count only once merged into
main. Before each WP it asks the router whether any route can serve the session the WP needs next;
when none can, it journals `no-route` and sleeps until the earliest route frees up. Several
supervisors run at once; each claims the WP it runs with a machine-wide mutex, and a lane has at
most one supervisor.

A pool lane (`"pool": true` in its lane settings) has no WPs of its own and never finishes: it keeps
one dedicated route (the local model, pinned by the lane's builder_override) busy around the clock
on runnable WPs from any lane that is not held: WPs nothing depends on first, then WPs whose
dependents also wait on other work, preferring lanes with no live supervisor. A WP that is the last
thing an open WP waits for is left to its hosted lane, unless that lane is sleeping for a route.
A supervisor waiting for a route releases its WP's claim while it sleeps, so the pool can take it. A WP it builds to review, or that a re-evaluation escalated, goes back to its own
lane, whose supervisor is started when it is not running.

A lane asleep until a route frees up gives up its `max_active_lanes` slot (state "sleeping"), so
lanes that can work start meanwhile; when it wakes it waits for a free slot before it works again.

State: `<runs>/supervisor-<name>.state` reads running, sleeping, finished, needs-orchestrator,
crashed or stopped; `.pid` and `.log` sit beside it. Every outcome is also journaled.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import wpfile
from .commands import run_commands
from .context import Context
from .integrate import EXIT_MEANINGS, EXIT_MERGED
from .platform import NamedMutex, spawn_detached
from .scope import owned_packages, reached_packages
from .project import git
from .routing import with_mode, wp_mode
from .runner import EXIT_NO_ROUTE, LEDGER_FILE, NO_ROUTE_FILE, RoundLedger, session_env
from .session import describe_next

RESUMABLE_STATUSES = ("todo", "changes_requested", "in_progress", "review")
UNKNOWN_ROUTE_RECHECK = timedelta(minutes=15)
LONGEST_SLEEP_SECONDS = 1800
POOL_IDLE_RECHECK = timedelta(minutes=10)
# How often a lane that woke up asks again for one of the `max_active_lanes` slots.
LANE_SLOT_RECHECK = timedelta(minutes=2)
# Statuses a pool lane builds; reviews and re-reviews stay with the WP's own lane.
POOL_STATUSES = ("todo", "changes_requested", "in_progress")
# Beside a WP's run state while its supervisor waits for a route, holding the wake time; the WP's
# claim is released meanwhile so a pool lane may build it.
ROUTE_WAIT_FILE = "route-wait"


class Supervisor:
    def __init__(self, context: Context, name: str, lane: bool, wp_ids: list[str], options: dict) -> None:
        self.context = context
        self.project = context.project
        self.name = name
        self.is_lane = lane
        self.wp_ids = wp_ids
        lane_settings = self.project.lane(name) if lane else {}
        self.families = options.get("families") or lane_settings.get("families", [])
        self.is_pool = bool(lane_settings.get("pool"))
        # Only an explicit override is passed to the runner; it reads the lane's own settings itself.
        self.explicit_builder = options.get("builder_override")
        self.lane_builder = lane_settings.get("builder_override")
        # Pool lanes build this lane's WPs; it only reviews them (project.py, lanes).
        self.builds_in_pool = bool(lane_settings.get("builds_in_pool"))
        self.idle_minutes = options.get("idle_minutes")
        runs = self.project.runs_dir
        self.log_path = runs / f"supervisor-{name}.log"
        self.state_path = runs / f"supervisor-{name}.state"
        self.pid_path = runs / f"supervisor-{name}.pid"
        self.reported_parse_errors: set[str] = set()
        # WP id -> status its last run ended with; skipped while it keeps that status, so a WP the
        # orchestrator unblocks or resets becomes runnable again.
        self.finished: dict[str, str] = {}
        self.integration_failed: set[str] = set()
        self.no_route_reported_for: str | None = None
        # WPs a pool lane gave back to their own lane; it does not pick them again.
        self.handed_back: set[str] = set()

    # --------------------------------------------------------------------------------- utilities

    def log(self, message: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")

    def set_state(self, state: str) -> None:
        self.state_path.write_text(state, encoding="utf-8")

    def emit(self, kind: str, message: str, **fields: str) -> None:
        self.context.journal.emit(kind, message, {"supervisor": self.name, **fields})

    def selected(self, wp: wpfile.WorkPackage) -> bool:
        # A milestone run leaves lane WPs to their lane's supervisor.
        if self.is_lane:
            return wp.id in self.wp_ids or wp.lane == self.name
        return wp.milestone == self.name and not wp.lane

    def claim(self, wp_id: str) -> NamedMutex | None:
        """Claims a WP machine-wide for this supervisor; None when another supervisor runs it."""
        mutex = NamedMutex(self.project.mutex_name("wp", wp_id))
        return mutex if mutex.acquire(blocking=False) else None

    def report_parse_errors(self) -> None:
        for path, error in self.context.parse_errors.items():
            if path not in self.reported_parse_errors:
                self.reported_parse_errors.add(path)
                self.emit("wp-blocked", f"a WP file does not parse and is skipped or read from main: {error} ({path})")

    def sleep_until(self, wake_at: datetime, reason: str) -> None:
        self.log(f"sleeping until {wake_at.astimezone():%Y-%m-%d %H:%M}: {reason}")
        while (remaining := (wake_at - datetime.now(timezone.utc)).total_seconds()) > 0:
            time.sleep(max(1, min(LONGEST_SLEEP_SECONDS, remaining)))

    def wpo(self, *arguments: str, log_name: str) -> int:
        with (self.project.runs_dir / log_name).open("ab") as output:
            return subprocess.run([*self.context.machine.wpo_command(), *arguments], cwd=str(self.project.root), stdout=output, stderr=subprocess.STDOUT).returncode

    # ---------------------------------------------------------------------------------- selection

    def escalation(self, wp_id: str) -> str | None:
        return RoundLedger.escalation(self.project.runs_dir / wp_id / LEDGER_FILE)

    def pool_candidates(self, effective: list[wpfile.WorkPackage], merged: set[str]) -> list[wpfile.WorkPackage]:
        """WPs a pool lane may build, best first: runnable, their lane not held, not escalated by a
        re-evaluation, and not the last thing an open WP waits for (a slower dedicated model must
        not hold up work that could otherwise start). WPs nothing depends on come first, then WPs
        whose dependents also wait on other unmerged work, each preferring lanes without a live
        supervisor."""
        from .lanes import read_holds, supervisor_pid
        holds = read_holds(self.context)
        open_wps = [wp for wp in effective if wp.status != "done"]
        depended_on = {dependency for wp in open_wps for dependency in wp.depends_on}
        sole_blockers = {missing.pop() for wp in open_wps if len(missing := set(wp.depends_on) - merged) == 1}
        # While its own lane waits for a hosted route, the pool's model is the faster way forward.
        sole_blockers -= {wp_id for wp_id in sole_blockers if self.waiting_for_route(wp_id)}
        eligible = [wp for wp in effective if wp.status in POOL_STATUSES and wp.id not in sole_blockers and wp.lane not in holds
                    and wp.id not in self.handed_back and set(wp.depends_on) <= merged and not self.escalation(wp.id)]
        live = {lane for lane in {wp.lane for wp in eligible if wp.lane} if supervisor_pid(self.context, lane)}
        return sorted(eligible, key=lambda wp: (wp.id in depended_on, wp.lane in live, wp.milestone, wp.id))

    def waiting_for_route(self, wp_id: str) -> bool:
        """Whether a supervisor is sleeping until a route frees up for this WP."""
        marker = self.project.runs_dir / wp_id / ROUTE_WAIT_FILE
        if not marker.exists():
            return False
        return datetime.fromisoformat(marker.read_text(encoding="utf-8").strip()) > datetime.now(timezone.utc)

    def next_wp(self) -> tuple[wpfile.WorkPackage, NamedMutex] | None:
        """The next runnable WP, claimed; release the claim when its run ends."""
        merged = self.context.merged_done_ids()
        effective = self.context.effective_wps()
        self.report_parse_errors()
        from .lanes import read_holds
        if self.name in read_holds(self.context):
            # A held lane takes no new work (`wpo lanes hold`, `wpo wrap-up --lane`); what runs finishes.
            return None
        if self.is_pool:
            for wp in self.pool_candidates(effective, merged):
                if self.finished.get(wp.id) != wp.status and (claim := self.claim(wp.id)) is not None:
                    return wp, claim
            return None
        if self.is_lane:
            listed = [wp for wp_id in self.wp_ids for wp in effective if wp.id == wp_id]
            candidates = listed + [wp for wp in effective if wp.lane == self.name and wp.id not in self.wp_ids]
        else:
            candidates = effective
        for wp in candidates:
            if not self.selected(wp) or self.finished.get(wp.id) == wp.status or wp.status not in RESUMABLE_STATUSES:
                continue
            if not set(wp.depends_on) <= merged:
                continue
            if self.builds_in_pool and wp.status in POOL_STATUSES:
                continue
            claim = self.claim(wp.id)
            if claim is not None:
                return wp, claim
        return None

    def session_spec(self, wp: wpfile.WorkPackage) -> str:
        """The spec the WP's next session needs; mirrors the runner's builder_spec."""
        mode = wp_mode(self.project.runs_dir, wp.id)
        if wp.status == "review":
            return with_mode(wp.reviewer, mode)
        return with_mode(self.explicit_builder or self.escalation(wp.id) or self.lane_builder or wp.builder, mode)

    # ---------------------------------------------------------------------------------- worktrees

    def catch_up_with_main(self, worktree: Path) -> None:
        """Merges main into an existing worktree before its WP's sessions start, so a dependency
        added after the worktree was made (and merged since) is in the tree its builder sees, and
        semantic conflicts with main surface in a round rather than at integration.

        Builders' work is uncommitted, so the merge runs on a dirty tree: git refuses before
        starting when main changes a file with uncommitted edits, and an aborted conflicted merge
        (`reset --merge`) keeps the uncommitted edits of files the merge did not touch. Either way
        the tree is left as it was and integration merges main later."""
        main = self.project.main_branch
        if git(worktree, "merge-base", "--is-ancestor", main, "HEAD", check=False).returncode == 0:
            return
        if git(worktree, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
            self.log(f"{worktree} has an unfinished merge; not merging main")
            return
        merged = git(worktree, "merge", "--no-edit", "--quiet", main, check=False)
        if merged.returncode == 0:
            self.log(f"merged main into {worktree}")
            return
        if git(worktree, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
            git(worktree, "merge", "--abort", check=False)
        reason = (merged.stderr or merged.stdout).strip().splitlines()
        self.log(f"main not merged into {worktree} (left for integration): {reason[0] if reason else 'merge failed'}")

    def prewarm(self, wp: wpfile.WorkPackage, worktree: Path) -> None:
        """Starts the project's `worktree_prewarm` commands detached in a new worktree."""
        commands = self.project.config["worktree_prewarm"]
        if not commands:
            return
        owned = owned_packages(worktree, wp.owns)
        selections = {"{packages}": owned, "{reach}": reached_packages(worktree, owned)}
        chosen = []
        for command in commands:
            used = [placeholder for placeholder in selections if placeholder in command]
            if any(not selections[placeholder] for placeholder in used):
                continue
            for placeholder in used:
                command = command.replace(placeholder, " ".join(f"-p {name}" for name in selections[placeholder]))
            chosen.append(command)
        if not chosen:
            return
        log_path = self.project.runs_dir / f"supervisor-{self.name}-{wp.id}-prewarm.log"
        # A warm-up, not a check: it takes no build slot (the builder's own builds wait on cargo's
        # build-directory lock instead), and a launch failure only loses the head start.
        try:
            spawn_detached([*self.project.config["shell"], "; ".join(chosen)], worktree, env={**os.environ, **session_env(self.context, wp.id, worktree)},
                           log_path=log_path)
        except OSError as error:
            self.log(f"prewarm of {wp.id} not started: {error}")
            return
        self.log(f"prewarming {wp.id}: {'; '.join(chosen)}")

    def ensure_worktree(self, wp_id: str) -> Path:
        """The WP's worktree, created from main on first use (a branch left by a removed worktree is
        checked out again), with the project's setup commands run in it."""
        if self.context.machine.execution_mode == "t3":
            from .t3 import Bridge
            worktree = Bridge(self.context).ensure_workspace(wp_id)
            self.catch_up_with_main(worktree)
            return worktree
        worktree = self.project.worktree(wp_id)
        if worktree.exists():
            if not list(self.project.wp_dir(worktree).glob(f"{wp_id}-*.md")):
                # Created before its WP file reached main (a draft committed later): catch up.
                git(worktree, "merge", "--ff-only", "--quiet", self.project.main_branch)
                self.log(f"fast-forwarded {worktree} to main for its WP file")
            self.catch_up_with_main(worktree)
            return worktree
        branch = self.project.branch(wp_id)
        exists = git(self.project.root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0
        worktree.parent.mkdir(parents=True, exist_ok=True)
        arguments = ["worktree", "add", "--quiet", str(worktree), branch] if exists else ["worktree", "add", "--quiet", "-b", branch, str(worktree), self.project.main_branch]
        git(self.project.root, *arguments)
        setup = self.project.config["worktree_setup"]
        if setup:
            failures = run_commands(self.project.config["shell"], setup, worktree, session_env(self.context, wp_id, worktree),
                                    self.project.runs_dir / f"supervisor-{self.name}-{wp_id}-setup.log", self.project.config["command_timeout_minutes"])
            if failures:
                raise RuntimeError(f"worktree setup failed in {worktree}: {failures[0][:300]}")
        self.log(f"created worktree {worktree} on {branch}")
        return worktree

    def integrate(self, wp_id: str) -> bool:
        code = self.wpo("integrate", "--wp", wp_id, log_name=f"supervisor-{self.name}-{wp_id}-integrate.log")
        meaning = EXIT_MEANINGS.get(code, "wpo integrate failed")
        self.log(f"{wp_id} integration exit {code}: {meaning}")
        if code == EXIT_MERGED:
            self.emit("wp-merged", f"{wp_id} merged into main", wp=wp_id)
            return True
        self.emit("integration-failed", f"{wp_id} could not merge into main: {meaning} (exit {code}); its worktree is left for the orchestrator", wp=wp_id)
        self.integration_failed.add(wp_id)
        return False

    def run_wp(self, wp: wpfile.WorkPackage) -> int:
        created = not self.project.worktree(wp.id).exists()
        worktree = self.ensure_worktree(wp.id)
        if created:
            self.prewarm(wp, worktree)
        arguments = ["run", "--wp", wp.id, "--tree", str(worktree)]
        if self.is_lane:
            arguments += ["--lane", self.name]
        if self.families:
            arguments += ["--families", ",".join(self.families)]
        if self.explicit_builder:
            arguments += ["--builder", self.explicit_builder]
        if self.idle_minutes:
            arguments += ["--idle-minutes", str(self.idle_minutes)]
        return self.wpo(*arguments, log_name=f"supervisor-{self.name}-{wp.id}.log")

    def status_of(self, wp_id: str) -> str:
        return next(wp.status for wp in self.context.effective_wps() if wp.id == wp_id)

    def route_wait(self, wp_id: str, spec: str, next_available_at: float | None, unavailable: str) -> datetime:
        """Journals no-route once per WP; returns when the earliest route frees up."""
        wake_at = (datetime.fromtimestamp(next_available_at, timezone.utc) + timedelta(minutes=2)) if next_available_at \
            else datetime.now(timezone.utc) + UNKNOWN_ROUTE_RECHECK
        if self.no_route_reported_for != wp_id:
            self.emit("no-route", f"{wp_id} needs '{spec}' and no route can serve it; {self.name} sleeps until {describe_next(wake_at.timestamp())}. Unavailable: {unavailable}", wp=wp_id)
            self.no_route_reported_for = wp_id
        return wake_at

    def sleep_unclaimed(self, wp_id: str, wake_at: datetime) -> None:
        """Sleeps for a route with the WP's claim and the lane's slot released. A WP's own lane marks
        the wait for pool lanes; a pool lane waiting for its model leaves the marker, which is the
        own lane's, alone."""
        if self.is_pool:
            self.sleep_until(wake_at, f"no route for {wp_id}")
            return
        marker = self.project.runs_dir / wp_id / ROUTE_WAIT_FILE
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(wake_at.isoformat(), encoding="utf-8")
        self.set_state("sleeping")
        try:
            self.sleep_until(wake_at, f"no route for {wp_id}")
        finally:
            marker.unlink(missing_ok=True)
        self.take_lane_slot()

    def take_lane_slot(self) -> None:
        """Waits until fewer than `max_active_lanes` other lanes are working, then works again."""
        from .lanes import active_lanes, known_lanes, slots_lock
        limit = int(self.project.config["max_active_lanes"])
        reported = False
        while True:
            with slots_lock(self.context):
                working = active_lanes(self.context, known_lanes(self.context) - {self.name})
                if not limit or len(working) < limit:
                    self.set_state("running")
                    return
            if not reported:
                self.emit("lane-deferred", f"lane {self.name} woke but {len(working)} of {limit} lanes are working; it waits for a slot")
                reported = True
            self.sleep_until(datetime.now(timezone.utc) + LANE_SLOT_RECHECK, "waiting for a lane slot")

    def run_claimed(self, wp: wpfile.WorkPackage) -> datetime | None:
        """Runs one claimed WP; returns when to retry it if no route could serve it."""
        spec = self.session_spec(wp)
        first = "none (verify only)"
        if spec != "none":
            selection = self.context.router.select(spec, self.families or None, free_last_resort=wp.status != "review", gate=wp.status == "review")
            if not selection.candidates:
                unavailable = "; ".join(f"{entry['id']}: {entry['reason']}" for entry in selection.skipped) or "no route reaches the tier"
                return self.route_wait(wp.id, spec, selection.next_available_at, unavailable)
            first = selection.candidates[0]["id"]
        self.log(f"running {wp.id} ({wp.status}) for '{spec}'; first route {first}")
        code = self.run_wp(wp)
        status = self.status_of(wp.id)
        self.log(f"{wp.id} runner exit {code}; status {status}")
        if code == EXIT_NO_ROUTE:
            # The session that found no route may need another spec than the pre-check (staged
            # tiers, a busy local model); the runner says which and when it frees up.
            report_path = self.project.runs_dir / wp.id / NO_ROUTE_FILE
            report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
            if report.get("role") == "verify":
                # The machine failed the checks (the runner journaled verify-deferred): wait, then re-verify.
                return datetime.fromtimestamp(report["next_available_at"], timezone.utc)
            if self.is_pool and report.get("role") != "builder":
                # A review or re-evaluation waits for hosted routes; the pool's model moves on.
                self.hand_back(wp.id, f"no route for its {report.get('role', 'next')} session")
                return None
            return self.route_wait(wp.id, report.get("spec", spec), report.get("next_available_at"), report.get("detail", "see the runner log"))
        self.no_route_reported_for = None
        self.finished[wp.id] = status
        if code != 0:
            self.emit("runner-failed", f"{wp.id}: wpo run exited {code} (status {status}); read {self.project.runs_dir / f'supervisor-{self.name}-{wp.id}.log'}", wp=wp.id)
            return None
        if status == "done":
            self.integrate(wp.id)
        elif self.is_pool and status in wpfile.OPEN_STATUSES and (status == "review" or self.escalation(wp.id)):
            self.hand_back(wp.id, "reached review" if status == "review" else f"escalated to {self.escalation(wp.id)}")
        return None

    def hand_back(self, wp_id: str, reason: str) -> None:
        """Gives a WP back to its own lane, starting that lane's supervisor when none is live."""
        from .lanes import start_if_idle
        self.handed_back.add(wp_id)
        wp = next(wp for wp in self.context.effective_wps() if wp.id == wp_id)
        outcome = start_if_idle(self.context, wp.lane, frozenset({wp_id})) if wp.lane else "it has no lane; the orchestrator resumes it"
        self.log(f"{wp_id} handed back ({reason}): {outcome}")
        self.emit("wp-handed-back", f"{wp_id} left the {self.name} pool ({reason}); lane {wp.lane or '-'}: {outcome}", wp=wp_id)

    # -------------------------------------------------------------------------------------- loop

    def loop(self) -> None:
        while True:
            # A WP that finished but did not merge (a conflict since resolved, an interrupted
            # supervisor) is merged before new work starts. A WP committed as done on main is
            # settled (by its integration, or by the orchestrator through `wpo commit-main`):
            # under T3 its checkout outlives the merge, so the checkout alone does not mean unmerged.
            # Main is read again each pass, since this lane's own merges move it.
            main_copies = self.context.main_wps()
            merged = self.context.merged_done_ids(main_copies)
            for wp in self.context.effective_wps(main_copies):
                if (wp.status == "done" and wp.id not in merged and self.selected(wp) and wp.id not in self.integration_failed
                        and self.project.worktree(wp.id).exists()):
                    self.integrate(wp.id)
            picked = self.next_wp()
            if picked is None:
                if not self.is_pool:
                    return
                self.sleep_until(datetime.now(timezone.utc) + POOL_IDLE_RECHECK, "no WP the pool may build")
                continue
            wp, claim = picked
            try:
                wake_at = self.run_claimed(wp)
            finally:
                claim.release()
            if wake_at is not None:
                self.sleep_unclaimed(wp.id, wake_at)

    def work_appeared(self) -> bool:
        """Whether a WP became runnable (one a pool lane handed back), without keeping its claim."""
        picked = self.next_wp()
        if picked is None:
            return False
        picked[1].release()
        return True

    def wait_for_orchestrator_room(self) -> bool:
        """"finished" wakes the orchestrator, which runs on Claude; wait until Claude has room.
        Returns False when runnable work appeared meanwhile, so the lane resumes instead."""
        threshold = self.project.config["orchestrator_room_percent"]
        while threshold is not None:
            windows = self.context.router.usage_windows().get("claude")
            five_hour = next((window for window in windows or [] if window["window_seconds"] <= 18000), None)
            if five_hour and five_hour["used_percent"] < threshold:
                return True
            wake_at = datetime.fromtimestamp(float(five_hour["resets_at"]), timezone.utc) + timedelta(minutes=2) if five_hour \
                else datetime.now(timezone.utc) + UNKNOWN_ROUTE_RECHECK
            self.set_state("sleeping")
            self.sleep_until(min(wake_at, datetime.now(timezone.utc) + POOL_IDLE_RECHECK), "waiting for Claude room before handing back to the orchestrator")
            if self.work_appeared():
                self.take_lane_slot()
                return False
        return True

    def run(self) -> None:
        # One supervisor per lane machine-wide, held for the supervisor's lifetime.
        lane_mutex = NamedMutex(self.project.mutex_name("lane", self.name))
        if not lane_mutex.acquire(blocking=False):
            print(f"a supervisor for {self.name} is already running")
            return
        try:
            self.supervise()
        finally:
            lane_mutex.release()

    def supervise(self) -> None:
        # The pid first: the event waiter reads state "running" with this pid to detect a death.
        self.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        self.set_state("running")
        details = "".join([f", families {','.join(self.families)}" if self.families else "", f", builder {self.explicit_builder or self.lane_builder}" if self.explicit_builder or self.lane_builder else "", " (pool)" if self.is_pool else ""])
        self.log(f"supervisor started for {self.name} (pid {os.getpid()}){details}")
        try:
            while True:
                self.loop()
                if self.integration_failed:
                    break
                self.log(f"no runnable {self.name} work package left")
                if self.wait_for_orchestrator_room():
                    break
                self.log("a work package became runnable while waiting; resuming")
        except Exception as error:
            self.set_state("crashed")
            self.log(f"crashed: {type(error).__name__}: {error}")
            self.emit("supervisor-crashed", f"supervisor {self.name} crashed: {type(error).__name__}: {error}")
            raise
        if self.integration_failed:
            self.set_state("needs-orchestrator")
            failed = ", ".join(sorted(self.integration_failed))
            self.log(f"no runnable {self.name} work package left; not merged into main: {failed}")
            self.emit("supervisor-finished", f"supervisor {self.name} has nothing left to run; needs-orchestrator: not merged {failed}")
            return
        self.set_state("finished")
        self.log("finished")
        left = [f"{wp.id} {wp.status}" for wp in self.context.effective_wps() if self.selected(wp) and wp.status != "done"]
        self.emit("supervisor-finished", f"supervisor {self.name} finished; not done: {', '.join(left) or 'none'}")
