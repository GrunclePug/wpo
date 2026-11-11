"""Scopes workspace-wide check commands to the packages a change can affect.

`verify_round` and `verify_workspace` name the whole workspace (`cargo clippy --workspace`, `cargo
test --workspace`). On a large workspace most of that work re-checks packages the change cannot
reach. With `check_scope` set, each command holding the scope's workspace flag runs instead for the
changed packages and every workspace package that depends on them, directly or not (normal, build
and dev dependencies), so a broken caller still fails. Main itself only ever moves to a verified
commit, so packages the change cannot reach already pass.

The whole workspace is still checked when a changed path belongs to no package and is not a
`verify_neutral_paths` path (a root manifest, the lock file, toolchain or lint configuration), and
when the package graph cannot be read. A change that reaches no package (plans, notes) drops the
workspace-wide commands: nothing they check changed.

Limits, accepted for the time saved: `-p` selections unify features over the selected packages only,
so a feature only an unselected package enables is off; a `mentioned_paths` file reaches only the
packages whose .rs or .toml sources contain its repository-relative path (a path built at run time, or
a directory listing, is not seen); a sibling crate reading another package's files without depending
on it is not reached. The merge into main of a milestone gate (`gate` config) still checks everything.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import wpfile


@dataclass(frozen=True)
class Scope:
    """The packages a change reaches; `packages` None means the whole workspace. `changed` is the
    packages the change itself touches, without their dependents (None: the same as `packages`)."""

    packages: tuple[str, ...] | None
    reason: str
    changed: tuple[str, ...] | None = None


def cargo_packages(tree: Path) -> tuple[dict[str, PurePosixPath], dict[str, set[str]]]:
    """Workspace members by name with their directories relative to the tree, and for each member
    the members that depend on it."""
    completed = subprocess.run(["cargo", "metadata", "--no-deps", "--format-version", "1", "--offline"], cwd=tree,
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        raise RuntimeError(f"cargo metadata failed: {completed.stderr.strip()[:400]}")
    metadata = json.loads(completed.stdout)
    root = Path(metadata["workspace_root"]).resolve()
    members = set(metadata["workspace_members"])
    directories: dict[str, PurePosixPath] = {}
    dependents: dict[str, set[str]] = {}
    packages = [package for package in metadata["packages"] if package["id"] in members]
    for package in packages:
        relative = Path(package["manifest_path"]).resolve().parent.relative_to(root)
        directories[package["name"]] = PurePosixPath(relative.as_posix())
        dependents.setdefault(package["name"], set())
    for package in packages:
        for dependency in package["dependencies"]:
            # Any dependency naming a member is an edge, path or not ([patch] and version-only
            # entries resolve to the member too): over-approximating only checks more.
            if dependency["name"] in directories:
                dependents[dependency["name"]].add(package["name"])
    return directories, dependents


def owning_package(path: str, directories: dict[str, PurePosixPath]) -> str | None:
    """The member whose directory holds the path (the deepest one: members can nest)."""
    candidate = PurePosixPath(path)
    owners = [(len(directory.parts), name) for name, directory in directories.items()
              if directory.parts and candidate.parts[:len(directory.parts)] == directory.parts]
    return max(owners)[1] if owners else None


def owned_packages(tree: Path, owns: list[str]) -> tuple[str, ...]:
    """The cargo packages whose directories an `owns` glob reaches into (its literal prefix lies in
    or above a package directory, `crates/x/src/**` -> x); () when the graph cannot be read."""
    try:
        directories, _ = cargo_packages(tree)
    except (RuntimeError, OSError, ValueError, KeyError):
        return ()
    names = set()
    for pattern in owns:
        literal = PurePosixPath(pattern.split("*", 1)[0].rstrip("/") or ".")
        for name, directory in directories.items():
            if directory.parts and (literal.parts[:len(directory.parts)] == directory.parts or directory.parts[:len(literal.parts)] == literal.parts) and literal.parts:
                names.add(name)
    return tuple(sorted(names))


def reached_packages(tree: Path, packages: tuple[str, ...]) -> tuple[str, ...]:
    """The packages and every workspace package that depends on them, directly or not."""
    try:
        _, dependents = cargo_packages(tree)
    except (RuntimeError, OSError, ValueError, KeyError):
        return ()
    return tuple(sorted(dependent_closure(set(packages), dependents)))


def dependent_closure(packages: set[str], dependents: dict[str, set[str]]) -> set[str]:
    reached = set(packages)
    frontier = list(packages)
    while frontier:
        for dependent in dependents.get(frontier.pop(), ()):
            if dependent not in reached:
                reached.add(dependent)
                frontier.append(dependent)
    return reached


# Package files searched for mentions of a `mentioned_paths` file.
SOURCE_SUFFIXES = (".rs", ".toml")


def mentioning_packages(tree: Path, path: str, directories: dict[str, PurePosixPath]) -> set[str]:
    """Packages whose sources or manifest name the path (an `include_str!`, a fixture path)."""
    found = set()
    for name, directory in directories.items():
        for source in (tree / directory).rglob("*"):
            if source.suffix in SOURCE_SUFFIXES and "target" not in source.relative_to(tree / directory).parts[:1] and source.is_file():
                if path in source.read_text(encoding="utf-8", errors="replace"):
                    found.add(name)
                    break
    return found


def cargo_scope(tree: Path, changed: list[str], neutral_patterns: list[str], mentioned_patterns: list[str] = ()) -> Scope:
    neutral = [wpfile.glob_to_regex(pattern) for pattern in neutral_patterns]
    mentioned = [wpfile.glob_to_regex(pattern) for pattern in mentioned_patterns]
    paths = [path.removesuffix(" (deleted)") for path in changed]
    try:
        directories, dependents = cargo_packages(tree)
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        return Scope(None, f"whole workspace: {error}")
    touched: set[str] = set()
    for path in paths:
        if any(pattern.match(path) for pattern in neutral):
            continue
        owner = owning_package(path, directories)
        if owner is None and any(pattern.match(path) for pattern in mentioned):
            touched |= mentioning_packages(tree, path, directories)
            continue
        if owner is None:
            return Scope(None, f"whole workspace: {path} belongs to no package")
        touched.add(owner)
    reached = dependent_closure(touched, dependents)
    names = tuple(sorted(reached))
    dependents_only = len(reached) - len(touched)
    return Scope(names, f"{len(touched)} changed package(s) and {dependents_only} dependent(s) of {len(directories)}: {', '.join(names) or 'none'}",
                 tuple(sorted(touched)))


def scope_for(config: dict, tree: Path, changed: list[str]) -> Scope:
    """The scope of a change under the project's `check_scope`; the whole workspace when unset."""
    setting = config.get("check_scope")
    if not setting:
        return Scope(None, "check_scope unset")
    if setting.get("kind") != "cargo":
        raise ValueError(f"check_scope kind {setting.get('kind')!r} is not supported (supported: cargo)")
    return cargo_scope(tree, changed, list(config["verify_neutral_paths"]), list(setting.get("mentioned_paths", [])))


def scoped_commands(config: dict, commands: list[str], scope: Scope) -> list[str]:
    """Commands with the workspace flag replaced by one `-p` per reached package; commands without
    the flag are kept as they are; a scope that reaches nothing drops the flagged commands.

    A flagged command matching a `check_scope.changed_only` regex selects only the changed packages,
    not their dependents: slow suites such as install tests re-run every dependent's simulations,
    which the merge gate checks anyway."""
    if scope.packages is None:
        return list(commands)
    flag = re.escape(config["check_scope"].get("workspace_flag", "--workspace"))
    pattern = re.compile(rf"(?<!\S){flag}(?!\S)")
    changed_only = [re.compile(expression) for expression in config["check_scope"].get("changed_only", [])]
    changed = scope.packages if scope.changed is None else scope.changed
    scoped = []
    for command in commands:
        if not pattern.search(command):
            scoped.append(command)
            continue
        selected = changed if any(expression.search(command) for expression in changed_only) else scope.packages
        if selected:
            scoped.append(pattern.sub(" ".join(f"-p {name}" for name in selected), command))
    return scoped


def scoped_checks(config: dict, tree: Path, commands: list[str], changed: list[str]) -> tuple[list[str], str]:
    """The commands to run for a change, and a line saying how they were scoped."""
    scope = scope_for(config, tree, changed)
    return scoped_commands(config, commands, scope), f"check scope: {scope.reason}"
