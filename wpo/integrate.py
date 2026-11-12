"""Merges a finished WP's worktree into the main branch.

Commits the worktree on its branch, merges main into the branch, runs the full workspace checks
(`verify_workspace`, which rounds do not run) and refuses a merge whose checks changed any file,
fast-forwards main under a machine-wide mutex, removes the worktree, branch and per-WP build output,
and runs post-merge hooks whose paths changed. Main only ever moves by fast-forward to a verified
commit, so the user's checkout always builds. This is the only thing that commits for a WP.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from . import wpfile
from .commands import run_commands
from .context import Context
from .platform import NamedMutex
from .project import git
from .scope import scope_for, scoped_checks
from .runner import VERIFIED_TREE_FILE, session_env

EXIT_MERGED, EXIT_CONFLICT, EXIT_VERIFY_FAILED, EXIT_FAST_FORWARD_FAILED = 0, 5, 6, 7
# Outcomes of bringing main in again under the mutex (not exit codes).
MAIN_UNCHANGED, MAIN_MOVED = "main-unchanged", "main-moved"
# Verifications run unlocked before one runs under the mutex, so a busy main cannot starve a merge.
OPTIMISTIC_ATTEMPTS = 2
EXIT_MEANINGS = {
    EXIT_MERGED: "merged into main",
    EXIT_CONFLICT: "merge conflict with main",
    EXIT_VERIFY_FAILED: "verification failed after merging main",
    EXIT_FAST_FORWARD_FAILED: "main could not fast-forward",
    8: "the T3 backend or the boot fence failed",
}


class Integration:
    def __init__(self, context: Context, wp_id: str, force_verify: bool) -> None:
        self.context = context
        self.project = context.project
        self.wp_id = wp_id
        self.force_verify = force_verify
        self.worktree = self.project.worktree(wp_id)
        self.branch = self.project.branch(wp_id)
        self.main = self.project.main_branch
        self.log_path = self.project.wp_run_dir(wp_id) / "integrate.log"

    def log(self, message: str) -> None:
        from datetime import datetime
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")

    def commit(self, root: Path, subject: str) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as message:
            message.write(subject + ("\n\n" + self.project.commit_footer if self.project.commit_footer else "") + "\n")
        try:
            git(root, "commit", "--quiet", "-F", message.name)
        finally:
            Path(message.name).unlink()

    def save_worktree_changes(self, subject: str) -> None:
        git(self.worktree, "add", "-A")
        if git(self.worktree, "diff", "--cached", "--quiet", check=False).returncode == 0:
            return
        self.commit(self.worktree, subject)
        self.log(f"committed: {subject}")

    def merge_main(self, commit: str | None = None) -> bool | int:
        """Merges main (or `commit`, a pinned tip of main). Returns True when it brought new
        commits, False when the branch already contained it, or EXIT_CONFLICT after aborting a
        merge no reconciler can settle."""
        target = commit or self.main
        if git(self.worktree, "merge-base", "--is-ancestor", target, "HEAD", check=False).returncode == 0:
            return False
        if git(self.worktree, "merge", "--no-edit", target, check=False).returncode == 0:
            self.log("merged main")
            return True
        conflicts = [line for line in git(self.worktree, "diff", "--name-only", "--diff-filter=U").stdout.splitlines() if line]
        reconcilers = {entry["file"]: entry["command"] for entry in self.project.config["lockfile_reconcilers"]}
        if conflicts and all(path in reconcilers for path in conflicts):
            # Both sides changed a generated lock file: keep this branch's and let its tool add what
            # the merged manifests need. Any failure aborts the merge, so no half-merge is left.
            try:
                for path in conflicts:
                    git(self.worktree, "checkout", "--ours", "--", path)
                    failures = run_commands(self.project.config["shell"], [reconcilers[path]], self.worktree,
                                            session_env(self.context, self.wp_id, self.worktree), self.log_path, self.project.config["command_timeout_minutes"])
                    if failures:
                        raise RuntimeError(f"reconciling {path} failed: {failures[0][:400]}")
                    git(self.worktree, "add", "--", path)
                git(self.worktree, "commit", "--quiet", "--no-edit")
            except Exception as error:
                git(self.worktree, "merge", "--abort", check=False)
                self.log(f"lock-file reconciliation failed, merge aborted: {error}")
                return EXIT_CONFLICT
            self.log(f"merged main; {', '.join(conflicts)} reconciled")
            return True
        git(self.worktree, "merge", "--abort", check=False)
        self.log(f"merge conflict with main in: {', '.join(conflicts)}; merge aborted")
        return EXIT_CONFLICT

    def paths_between(self, start: str, end: str) -> list[str]:
        return [line for line in git(self.worktree, "-c", "core.quotepath=off", "diff", "--name-only", "--no-renames", start, end).stdout.splitlines() if line]

    def verify(self) -> bool:
        commands, scope_line = scoped_checks(self.project.config, self.worktree, self.project.config["verify_workspace"], self.paths_between(self.main, "HEAD"))
        self.log(scope_line)
        failures = run_commands(self.project.config["shell"], commands, self.worktree,
                                session_env(self.context, self.wp_id, self.worktree), self.log_path, self.project.config["command_timeout_minutes"])
        if failures:
            return False
        # Everything was committed before the checks ran, so any change is a check writing into the
        # tree (a test regenerating tracked evidence): main must not receive it, and no check may make it.
        written = git(self.worktree, "status", "--porcelain", "--untracked-files=all").stdout.splitlines()
        if written:
            self.log("the workspace checks changed files in the tree: " + "; ".join(line.strip() for line in written[:20])
                     + "; checks must not write files (restore them, and make the check stop writing)")
            return False
        return True

    def bring_main_in_again(self, verify_under_mutex: bool) -> int:
        """Called under the integration mutex after verifying unlocked. MAIN_UNCHANGED when the
        branch holds main's tip and stands verified (main did not move, or its new commits reach
        no package this WP reaches: each was verified on its own merge, so only packages both
        changes reach can break together); MAIN_MOVED when the new merge needs verifying again;
        else an exit code."""
        verified_main = git(self.worktree, "merge-base", self.main, "HEAD").stdout.strip()
        main_tip = git(self.worktree, "rev-parse", self.main).stdout.strip()
        if main_tip == verified_main:
            return MAIN_UNCHANGED
        if self.merge_main(main_tip) == EXIT_CONFLICT:
            return EXIT_CONFLICT
        # Both reaches are read from the merged tree's package graph: an edge either side added
        # (a manifest naming the other's package) is in it.
        ours = scope_for(self.project.config, self.worktree, self.paths_between(main_tip, "HEAD"))
        theirs = scope_for(self.project.config, self.worktree, self.paths_between(verified_main, main_tip))
        if ours.packages is not None and theirs.packages is not None and not set(ours.packages) & set(theirs.packages):
            self.log("main moved during verification; its changes reach no package this WP reaches, so the merge stands verified")
            return MAIN_UNCHANGED
        if not verify_under_mutex:
            self.log("main moved during verification into packages this WP reaches; verifying the new merge")
            return MAIN_MOVED
        self.log("main moved during verification again; verifying under the mutex")
        if self.verify():
            return MAIN_UNCHANGED
        self.log("workspace verification failed; main not moved")
        return EXIT_VERIFY_FAILED

    def rounds_ran_the_full_checks(self, wp: wpfile.WorkPackage) -> bool:
        """Whether the runner's verification already included every integration check."""
        round_checks = wp.round_checks(self.project.config["verify_round"], self.project.config["verify_workspace"])
        return all(command in round_checks for command in self.project.config["verify_workspace"])

    def unfinished_merge(self) -> bool:
        merging = git(self.worktree, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0
        unmerged = bool(git(self.worktree, "diff", "--name-only", "--diff-filter=U").stdout.strip())
        return merging or unmerged

    def verified_tree_matches(self) -> bool:
        """Whether the tip differs from the tree the runner verified only in `verify_neutral_paths`
        (plans and orchestration state no build or test reads): main's plan edits or the WP file's
        Handoff since verification cannot change the verified result."""
        record = self.project.wp_run_dir(self.wp_id) / VERIFIED_TREE_FILE
        if not record.exists():
            return False
        verified = record.read_text(encoding="utf-8").strip()
        if git(self.worktree, "rev-parse", "HEAD^{tree}").stdout.strip() == verified:
            return True
        patterns = [wpfile.glob_to_regex(pattern) for pattern in self.project.config["verify_neutral_paths"]]
        changed = git(self.worktree, "diff", "--name-only", verified, "HEAD", check=False)
        if not patterns or changed.returncode != 0:
            return False
        return all(any(pattern.match(path) for pattern in patterns) for path in changed.stdout.splitlines() if path)

    def post_merge(self, before: str) -> None:
        changed = git(self.project.root, "diff", "--name-only", before, "HEAD").stdout.splitlines()
        for hook in self.project.config["post_merge_hooks"]:
            patterns = [wpfile.glob_to_regex(pattern) for pattern in hook["when_changed"]]
            if not any(pattern.match(path) for path in changed for pattern in patterns):
                continue
            failures = run_commands(self.project.config["shell"], [hook["command"]], self.project.root, self.project.env_for(None), self.log_path,
                                    self.project.config["command_timeout_minutes"])
            self.log(f"post-merge hook '{hook['command']}': {'failed' if failures else 'ok'}")
            if failures:
                self.context.journal.emit("integration-failed", f"{self.wp_id} merged, but post-merge hook '{hook['command']}' failed (log {self.log_path})", {"wp": self.wp_id})

    def cleanup(self) -> None:
        """Removes the worktree, branch and build output after main moved; a failure here (files
        locked on Windows) does not undo the merge, so it is journaled for the orchestrator."""
        from .artifacts import remove_output
        from .project import ProjectError
        try:
            output_paths = self.project.paths_removed_after_merge(self.wp_id)
        except (ProjectError, OSError, subprocess.CalledProcessError) as error:
            self.log(f"build cleanup refused: {error}")
            self.context.journal.emit("cleanup-failed", f"{self.wp_id} merged, but build cleanup was refused: {error}", {"wp": self.wp_id})
            output_paths = []
        if self.context.machine.execution_mode == "t3":
            self.log("retained T3-owned worktree and branch; their lifecycle belongs to T3")
        else:
            self.cleanup_standalone_worktree()
        for path in output_paths:
            if path.exists():
                try:
                    remove_output(path)
                except (OSError, ProjectError) as error:
                    self.log(f"build cleanup failed for {path}: {error}")
                    self.context.journal.emit("cleanup-failed", f"{self.wp_id} merged, but build cleanup failed for {path}: {error}", {"wp": self.wp_id})
                else:
                    self.log(f"removed {path}")

    def cleanup_standalone_worktree(self) -> None:
        try:
            # --force because ignored files (build output, task specs) remain; everything tracked is committed.
            git(self.project.root, "worktree", "remove", "--force", str(self.worktree))
            git(self.project.root, "branch", "--quiet", "-d", self.branch)
        except Exception as error:
            self.log(f"cleanup failed: {error}")
            self.context.journal.emit("cleanup-failed", f"{self.wp_id} merged into main, but removing its worktree or branch failed: {error}", {"wp": self.wp_id})

    def run(self) -> int:
        if self.context.machine.execution_mode == "t3":
            from .t3 import Bridge
            Bridge(self.context).validate_workspace(self.wp_id)
        if not self.worktree.exists():
            raise FileNotFoundError(f"no worktree for {self.wp_id} at {self.worktree}")
        current = git(self.project.root, "symbolic-ref", "--short", "HEAD", check=False).stdout.strip()
        if current != self.main:
            raise RuntimeError(f"the main checkout is on '{current or 'a detached HEAD'}', not {self.main}; integration refused")
        if self.unfinished_merge():
            self.log("the worktree has an unfinished merge (MERGE_HEAD or unmerged paths); finish or abort it first")
            return EXIT_CONFLICT
        wp = self.context.wp_in_tree(self.wp_id, self.worktree)
        self.log(f"integrating {self.wp_id} ({wp.title})")
        self.save_worktree_changes(f"{self.wp_id}: {wp.title}")
        # Verification runs outside the integration mutex, so merges of other WPs proceed meanwhile;
        # the mutex covers only bringing main in again and the fast-forward.
        merged = self.merge_main()
        if merged == EXIT_CONFLICT:
            return EXIT_CONFLICT
        # Verify unless the runner already ran the full checks on this tree, give or take
        # verify-neutral paths.
        must_verify = self.force_verify or not self.rounds_ran_the_full_checks(wp) or not self.verified_tree_matches()
        if must_verify and not self.verify():
            self.log("workspace verification failed; main not moved")
            return EXIT_VERIFY_FAILED
        for attempt in range(OPTIMISTIC_ATTEMPTS + 1):
            # Main cannot move between deciding the branch stands verified and the fast-forward.
            with NamedMutex(self.project.integration_mutex):
                outcome = self.bring_main_in_again(verify_under_mutex=attempt == OPTIMISTIC_ATTEMPTS)
                if outcome in (EXIT_CONFLICT, EXIT_VERIFY_FAILED):
                    return outcome
                if outcome == MAIN_UNCHANGED:
                    before = git(self.project.root, "rev-parse", "HEAD").stdout.strip()
                    tip = git(self.worktree, "rev-parse", "HEAD").stdout.strip()
                    fast_forward = git(self.project.root, "merge", "--ff-only", "--quiet", tip, check=False)
                    if fast_forward.returncode != 0:
                        self.log(f"main could not fast-forward to {tip}: {(fast_forward.stderr or fast_forward.stdout).strip()}")
                        return EXIT_FAST_FORWARD_FAILED
                    self.log(f"main fast-forwarded to {self.branch} ({tip[:10]}){' after verification' if must_verify else ''}")
                    break
            # Main brought changes that reach this WP's packages: verify the new merge, unlocked.
            must_verify = True
            if not self.verify():
                self.log("workspace verification failed; main not moved")
                return EXIT_VERIFY_FAILED
        else:
            raise RuntimeError("integration loop ended undecided; its last attempt verifies under the mutex")
        self.post_merge(before)
        self.cleanup()
        return EXIT_MERGED


def integrate(context: Context, wp_id: str, force_verify: bool = False) -> int:
    return Integration(context, wp_id, force_verify).run()
