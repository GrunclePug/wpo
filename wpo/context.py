"""The objects every command needs: the project, the machine settings, the router and the journal."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import wpfile
from .boot import ensure_boot
from .events import Journal
from .machine import MachineConfig
from .project import Project, git
from .routing import Router


@dataclass
class Context:
    project: Project
    machine: MachineConfig
    router: Router
    journal: Journal
    # WP files that failed to parse in the last listing: path -> error. Callers report them.
    parse_errors: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, start: Path | None = None, machine: MachineConfig | None = None) -> Context:
        machine = machine or MachineConfig.load()
        project = Project.locate(start, machine.raw.get("project_defaults"), machine.execution_mode)
        journal = Journal(project.runs_dir)
        ensure_boot(project.runs_dir, journal)
        router = Router(machine, project.config["routing"], lambda kind, message: journal.emit(kind, message), project=project)
        context = cls(project, machine, router, journal)
        if machine.execution_mode == "t3":
            from .t3 import Bridge
            journal.on_attention = Bridge(context).attention
        return context

    # ------------------------------------------------------------------------------ work packages

    def main_wps(self) -> list[wpfile.WorkPackage]:
        """The WP files committed on the main branch. Drafts in the main checkout's working tree do
        not count: a worktree starts from the branch, so an uncommitted WP cannot run there yet."""
        project = self.project
        listing = git(project.root, "ls-tree", "-z", project.main_branch, "--", f"{project.wp_dir_relative}/").stdout
        blobs = []
        for entry in filter(None, listing.split("\0")):
            meta, _, path = entry.partition("\t")
            name = path.rsplit("/", 1)[-1]
            if meta.split()[1] == "blob" and name.startswith("WP-") and name.endswith(".md") and name != wpfile.TEMPLATE_NAME:
                blobs.append((meta.split()[2], path))
        blobs.sort(key=lambda blob: blob[1])
        batch = subprocess.run(["git", "-C", str(project.root), "cat-file", "--batch"], input="".join(f"{sha}\n" for sha, _ in blobs).encode(),
                               capture_output=True, check=True).stdout
        wps, offset = [], 0
        for _sha, path in blobs:
            header_end = batch.index(b"\n", offset)
            size = int(batch[offset:header_end].split()[2])
            text = batch[header_end + 1:header_end + 1 + size].decode("utf-8-sig")
            offset = header_end + 1 + size + 1
            try:
                wps.append(wpfile.parse_text(text, project.root / path))
            except wpfile.WorkPackageError as error:
                self.parse_errors[path] = str(error)
        return wps

    def effective_wps(self, main_copies: list[wpfile.WorkPackage] | None = None) -> list[wpfile.WorkPackage]:
        """Main's WPs, except that a WP with a worktree is read from it: its live status and progress
        stay there until it merges. A worktree copy that does not parse falls back to main's.
        `main_copies` is a listing of main the caller already holds (the dashboard caches it until
        main moves); None reads main now."""
        effective = []
        for main_copy in self.main_wps() if main_copies is None else main_copies:
            worktree_copy = self.project.wp_dir(self.project.worktree(main_copy.id)) / main_copy.file_name
            if not worktree_copy.exists():
                effective.append(main_copy)
                continue
            try:
                effective.append(wpfile.parse(worktree_copy))
            except wpfile.WorkPackageError as error:
                self.parse_errors[str(worktree_copy)] = str(error)
                effective.append(main_copy)
        return effective

    def wp_in_tree(self, wp_id: str, tree: Path) -> wpfile.WorkPackage:
        return wpfile.find(self.project.wp_dir(tree), wp_id)

    def merged_done_ids(self, main_copies: list[wpfile.WorkPackage] | None = None) -> set[str]:
        """WPs done on the main branch; dependencies count only once merged, since worktrees start from main."""
        return {wp.id for wp in (self.main_wps() if main_copies is None else main_copies) if wp.status == "done"}
