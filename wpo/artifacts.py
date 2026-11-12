"""Configured build-output cleanup and free-space checks, independent of checkout retention."""

from __future__ import annotations

import math
import os
import re
import shutil
import stat
from pathlib import Path

from .context import Context
from .platform import NamedMutex
from .project import Project, ProjectError, git

GIB = 1 << 30


def linked(path: Path) -> bool:
    """Whether a path redirects filesystem access, including a Windows junction."""
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def walk_error(error: OSError) -> None:
    """Refuse cleanup when a directory cannot be inspected completely."""
    raise error


def cleanup_paths(project: Project, wp_id: str) -> list[Path]:
    """Resolve scoped build paths; reject checkout roots, Git data, links and tracked content."""
    if not project.config["remove_after_merge"]:
        return []
    if not re.fullmatch(r"WP-[A-Za-z0-9_.-]+", wp_id):
        raise ProjectError(f"Invalid work-package ID for build cleanup: {wp_id}")
    worktree = project.worktree(wp_id).absolute()
    checkouts = [Path(line.removeprefix("worktree ")).resolve()
                 for line in git(project.root, "worktree", "list", "--porcelain").stdout.splitlines()
                 if line.startswith("worktree ")]
    git_dir = Path(git(project.root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()).resolve()
    paths = []
    for template in project.config["remove_after_merge"]:
        if not isinstance(template, str) or not template or not any(token in template for token in ("{wp}", "{worktree}")):
            raise ProjectError("remove_after_merge paths must contain {wp} or {worktree}; shared cleanup is refused")
        expanded = template.replace("{wp}", wp_id).replace("{worktree}", str(worktree))
        path = Path(expanded)
        path = path if path.is_absolute() else project.root / path
        for ancestor in (path, *path.parents):
            if (ancestor.exists() or ancestor.is_symlink()) and linked(ancestor):
                raise ProjectError(f"build cleanup refuses linked path: {ancestor}")
        path = path.resolve()
        if "{worktree}" in template and (path == worktree.resolve() or not path.is_relative_to(worktree.resolve())):
            raise ProjectError(f"build cleanup escapes its checkout: {path}")
        if "{worktree}" not in template:
            components = Path(template).parts
            boundary = next(index for index, component in enumerate(components) if "{wp}" in component)
            anchor = Path(*components[:boundary + 1])
            anchor = Path(str(anchor).replace("{wp}", wp_id))
            anchor = (anchor if anchor.is_absolute() else project.root / anchor).resolve()
            if not path.is_relative_to(anchor):
                raise ProjectError(f"build cleanup escapes its work-package directory: {path}")
        if path == Path(path.anchor) or path.is_relative_to(git_dir) or git_dir.is_relative_to(path):
            raise ProjectError(f"build cleanup refuses filesystem root or Git data: {path}")
        history = project.runs_dir.resolve()
        if path.is_relative_to(history) or history.is_relative_to(path):
            raise ProjectError(f"build cleanup refuses orchestration history: {path}")
        for checkout in checkouts:
            checkout = checkout.resolve()
            if checkout == path or checkout.is_relative_to(path):
                raise ProjectError(f"build cleanup would remove a checkout: {path}")
            if path.is_relative_to(checkout):
                if checkout != worktree.resolve():
                    raise ProjectError(f"build cleanup belongs to another checkout: {path}")
                relative = path.relative_to(checkout).as_posix()
                if ".git" in path.relative_to(checkout).parts:
                    raise ProjectError(f"build cleanup refuses Git data: {path}")
                if git(checkout, "ls-files", "-z", "--", relative).stdout:
                    raise ProjectError(f"build cleanup contains tracked files: {path}")
                if ".orchestration" in path.relative_to(checkout).parts:
                    raise ProjectError(f"build cleanup refuses orchestration history: {path}")
                if git(checkout, "check-ignore", "-q", "--", relative + "/", check=False).returncode:
                    raise ProjectError(f"build cleanup is not ignored output: {path}")
        for ancestor in (path, *path.parents):
            if ((ancestor / ".git").exists() or (ancestor / ".git").is_symlink()) and ancestor not in checkouts:
                raise ProjectError(f"build cleanup belongs to a foreign Git repository: {path}")
            if (ancestor / "HEAD").is_file() and (ancestor / "objects").is_dir() and (ancestor / "config").is_file():
                raise ProjectError(f"build cleanup refuses bare Git repository: {ancestor}")
        if path.is_dir():
            for directory, folders, files in os.walk(path, onerror=walk_error):
                if ".git" in folders or ".git" in files or ({"HEAD", "config"}.issubset(files) and "objects" in folders):
                    raise ProjectError(f"build cleanup contains Git data: {directory}")
                for folder in folders:
                    if linked(Path(directory) / folder):
                        raise ProjectError(f"build cleanup refuses linked path: {Path(directory) / folder}")
        if path not in paths:
            paths.append(path)
    return paths


def remove_output(path: Path) -> None:
    """Remove build output without following directory links or hiding failed removals."""
    if not path.exists():
        return
    for ancestor in (path, *path.parents):
        if (ancestor.exists() or ancestor.is_symlink()) and linked(ancestor):
            raise ProjectError(f"build cleanup refuses linked path: {ancestor}")
    if not path.is_dir():
        raise ProjectError(f"build cleanup expected a directory: {path}")
    shutil.rmtree(path)


def space_problem(project: Project, tree: Path) -> str | None:
    """Describe insufficient checkout-volume space, or None when disabled or sufficient."""
    minimum = project.config.get("minimum_build_free_gib")
    if minimum is None:
        return None
    if isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not math.isfinite(minimum) or minimum < 0:
        raise ProjectError("minimum_build_free_gib must be a finite nonnegative number or null")
    volume_path = tree.resolve()
    while not volume_path.exists():
        if volume_path.parent == volume_path:
            raise ProjectError(f"Build volume does not exist: {tree}")
        volume_path = volume_path.parent
    free = shutil.disk_usage(volume_path).free / GIB
    if free >= minimum:
        return None
    return (f"Build space too low on {volume_path}: {free:.2f} GiB free, {minimum:g} GiB required. "
            "Remove retired build output or choose another build volume before resuming; no model was launched.")


def clean_merged(context: Context, wp_ids: list[str], apply: bool) -> list[dict]:
    """Plan or remove configured output only for settled WPs whose claims are idle."""
    from .inbox import live_session
    from .t3 import read_json

    by_id = {wp.id: wp for wp in context.main_wps()}
    if unknown := set(wp_ids) - by_id.keys():
        raise ProjectError(f"Unknown work packages: {', '.join(sorted(unknown))}")
    reports = []
    for wp_id in wp_ids or sorted(by_id):
        if by_id[wp_id].status != "done":
            reports.append({"wp": wp_id, "state": "protected", "reason": "not done on main"})
            continue
        claim = NamedMutex(context.project.mutex_name("wp", wp_id))
        if not claim.acquire(blocking=False):
            reports.append({"wp": wp_id, "state": "protected", "reason": "claimed by a runner or supervisor"})
            continue
        try:
            requests = context.project.runs_dir / "t3/requests"
            native_active = any((request := read_json(path, {})).get("payload", {}).get("wp") == wp_id
                                and request.get("kind") == "session" and request.get("state") not in ("done", "failed", "cancelled")
                                for path in requests.glob("*.json"))
            if native_active or live_session(context.project.wp_run_dir(wp_id)) is not None:
                reports.append({"wp": wp_id, "state": "protected", "reason": "session still outstanding"})
                continue
            tree = context.project.worktree(wp_id)
            if context.machine.execution_mode == "t3" and tree.exists():
                from .t3 import Bridge
                Bridge(context).validate_workspace(wp_id)
            for path in cleanup_paths(context.project, wp_id):
                exists = path.exists()
                if apply:
                    remove_output(path)
                reports.append({"wp": wp_id, "path": str(path), "state": "removed" if apply and exists else "planned" if exists else "absent"})
        finally:
            claim.release()
    return reports
