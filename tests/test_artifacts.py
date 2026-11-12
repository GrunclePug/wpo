"""Build cleanup operates only on settled, unclaimed, configured disposable output."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wpo.artifacts import GIB, clean_merged, cleanup_paths, remove_output, space_problem
from wpo.context import Context
from wpo.events import Journal
from wpo.integrate import Integration
from wpo.project import DEFAULTS, Project, ProjectError, git
from wpo.runner import RunOptions, run_wp


class BuildArtifactTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="wpo-build-cleanup-")
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name).resolve()
        self.root, self.tree = self.base / "repo", self.base / "native"
        self.root.mkdir()
        for args in (["init", "-q", "-b", "main"], ["config", "user.name", "Test"], ["config", "user.email", "test@example.invalid"]):
            git(self.root, *args)
        (self.root / ".gitignore").write_text("target/\n.orchestration/\n", encoding="utf-8")
        directory = self.root / "docs/plan/wp"
        directory.mkdir(parents=True)
        self.wp_path = directory / "WP-0001-example.md"
        self.wp_path.write_text("---\nid: WP-0001\ntitle: Example\nmilestone: M1\ndepends_on: []\nstatus: done\nlane: test\nbuilder: code\nreviewer: code\nowns: [src/**]\n---\n\n## Goal\nExample.\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "Fixture")
        git(self.root, "worktree", "add", "-qb", "t3/example", str(self.tree), "main")
        self.project = Project(self.root, {**DEFAULTS, "name": "scratch", "remove_after_merge": ["{worktree}/target"]}, "t3")
        (self.project.wp_run_dir("WP-0001") / "t3-workspace.json").write_text(json.dumps({"path": str(self.tree), "branch": "t3/example"}), encoding="utf-8")
        self.context = Context(self.project, SimpleNamespace(execution_mode="t3", raw={}), None, Journal(self.project.runs_dir))
        self.output = self.tree / "target"
        self.output.mkdir()
        (self.output / "compiled.bin").write_bytes(b"rebuildable")

    def test_native_checkout_token_uses_registered_tree_and_external_legacy_path_still_resolves(self):
        self.project.config["remove_after_merge"].append(str(self.base / "build-{wp}"))
        self.assertEqual(cleanup_paths(self.project, "WP-0001"), [self.output, self.base / "build-WP-0001"])

    def test_cleanup_after_merge_removes_output_but_keeps_native_checkout_branch_and_source(self):
        integration = Integration(self.context, "WP-0001", False)
        integration.cleanup()
        self.assertFalse(self.output.exists())
        self.assertTrue((self.tree / "docs/plan/wp/WP-0001-example.md").exists())
        self.assertEqual(git(self.tree, "branch", "--show-current").stdout.strip(), "t3/example")

    def test_dry_run_and_apply_for_already_merged_wp_preserve_source_and_run_history(self):
        marker = self.project.wp_run_dir("WP-0001") / "history.json"
        marker.write_text("retained", encoding="utf-8")
        self.assertEqual(clean_merged(self.context, ["WP-0001"], False)[0]["state"], "planned")
        self.assertTrue(self.output.exists())
        self.assertEqual(clean_merged(self.context, ["WP-0001"], True)[0]["state"], "removed")
        self.assertEqual(marker.read_text(encoding="utf-8"), "retained")
        self.assertTrue(self.tree.exists())

    def test_open_and_held_work_is_protected_even_if_worktree_status_claims_done(self):
        self.wp_path.write_text(self.wp_path.read_text().replace("status: done", "status: in_progress"))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "Pending work")
        (self.project.runs_dir / "lane-holds.json").write_text('{"test":"waiting"}', encoding="utf-8")
        self.assertEqual(clean_merged(self.context, ["WP-0001"], True)[0]["state"], "protected")
        self.assertTrue(self.output.exists())

    def test_stalled_native_session_is_protected(self):
        directory = self.project.runs_dir / "t3/requests"
        directory.mkdir(parents=True)
        (directory / "request.json").write_text(json.dumps({"kind": "session", "state": "stalled", "payload": {"wp": "WP-0001"}}), encoding="utf-8")
        self.assertEqual(clean_merged(self.context, ["WP-0001"], True)[0]["state"], "protected")
        self.assertTrue(self.output.exists())

    def test_claimed_wp_is_skipped_without_waiting(self):
        with mock.patch("wpo.artifacts.NamedMutex") as mutex:
            mutex.return_value.acquire.return_value = False
            self.assertEqual(clean_merged(self.context, ["WP-0001"], True)[0]["state"], "protected")
            mutex.return_value.acquire.assert_called_once_with(blocking=False)
        self.assertTrue(self.output.exists())

    def test_checkout_roots_escapes_git_data_and_shared_paths_are_rejected(self):
        for template in ("{worktree}", "{worktree}/../native", "{worktree}/../elsewhere", "{worktree}/.git", str(self.root / ".git" / "objects-{wp}"), str(self.base / "shared")):
            with self.subTest(template=template):
                self.project.config["remove_after_merge"] = [template]
                with self.assertRaises(ProjectError):
                    cleanup_paths(self.project, "WP-0001")
        self.assertTrue(self.output.exists())

    def test_tracked_content_is_rejected(self):
        git(self.tree, "add", "-f", "target/compiled.bin")
        with self.assertRaisesRegex(ProjectError, "tracked"):
            cleanup_paths(self.project, "WP-0001")

    def test_untracked_source_and_ignored_orchestration_history_are_rejected(self):
        for name in ("src/newcrate", ".orchestration"):
            with self.subTest(name=name):
                source = self.tree / name
                source.mkdir(parents=True)
                marker = source / "keep.txt"
                marker.write_text("preserve", encoding="utf-8")
                self.project.config["remove_after_merge"] = ["{worktree}/" + name]
                with self.assertRaises(ProjectError):
                    clean_merged(self.context, ["WP-0001"], True)
                self.assertEqual(marker.read_text(), "preserve")

    def test_external_scope_cannot_be_cancelled_by_parent_traversal(self):
        shared = self.base / "shared"
        shared.mkdir()
        marker = shared / "keep.bin"
        marker.write_bytes(b"preserve")
        for template in (str(shared / "{wp}" / ".."), str(shared / "{wp}" / ".." / "other")):
            with self.subTest(template=template):
                self.project.config["remove_after_merge"] = [template]
                with self.assertRaisesRegex(ProjectError, "work-package directory"):
                    clean_merged(self.context, ["WP-0001"], True)
        self.assertTrue(marker.exists())
        self.project.config["remove_after_merge"] = ["../build/{wp}/sub/.."]
        self.assertEqual(cleanup_paths(self.project, "WP-0001"), [self.base / "build/WP-0001"])

    def test_configured_external_history_and_its_parents_are_preserved(self):
        history = self.base / "runtime-WP-0001/history"
        self.project.config["runs_dir"] = str(history)
        record = self.project.wp_run_dir("WP-0001") / "t3-workspace.json"
        record.write_text(json.dumps({"path": str(self.tree), "branch": "t3/example"}), encoding="utf-8")
        marker = history / "history.json"
        marker.write_text("preserve", encoding="utf-8")
        for template in (str(self.base / "runtime-{wp}"), str(self.base / "runtime-{wp}/history"), str(self.base / "runtime-{wp}/history/WP-0001")):
            with self.subTest(template=template):
                self.project.config["remove_after_merge"] = [template]
                with self.assertRaisesRegex(ProjectError, "orchestration history"):
                    clean_merged(self.context, ["WP-0001"], True)
                self.assertEqual(marker.read_text(), "preserve")
                self.assertTrue(record.exists())

    def test_foreign_repositories_and_nested_git_data_are_preserved(self):
        foreign = self.base / "other-WP-0001"
        foreign.mkdir()
        git(foreign, "init", "-q")
        source = foreign / "src"
        source.mkdir()
        marker = source / "source.py"
        marker.write_text("preserve", encoding="utf-8")
        git(foreign, "add", "src/source.py")
        for template in (str(self.base / "other-{wp}"), str(self.base / "other-{wp}/src")):
            with self.subTest(template=template):
                self.project.config["remove_after_merge"] = [template]
                with self.assertRaisesRegex(ProjectError, "foreign Git"):
                    clean_merged(self.context, ["WP-0001"], True)
                self.assertTrue(marker.exists())
        self.project.config["remove_after_merge"] = ["{worktree}/target"]
        git(self.output, "init", "-q")
        with self.assertRaisesRegex(ProjectError, "foreign Git|contains Git"):
            clean_merged(self.context, ["WP-0001"], True)
        self.assertTrue((self.output / "compiled.bin").exists())

    def test_bare_nested_repository_is_preserved(self):
        nested = self.output / "dependency"
        git(self.output, "init", "--bare", "-q", str(nested))
        with self.assertRaisesRegex(ProjectError, "Git data"):
            clean_merged(self.context, ["WP-0001"], True)
        self.assertTrue((nested / "HEAD").exists())

    def test_another_checkouts_output_is_rejected(self):
        self.project.config["remove_after_merge"] = [str(self.root / "target-{wp}")]
        with self.assertRaisesRegex(ProjectError, "another checkout"):
            cleanup_paths(self.project, "WP-0001")

    def test_linked_cleanup_root_is_refused_and_external_data_survives(self):
        remove_output(self.output)
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep").write_text("keep", encoding="utf-8")
        if os.name == "nt":
            import subprocess
            subprocess.run(["cmd", "/c", "mklink", "/J", str(self.output), str(outside)], check=True, capture_output=True)
        else:
            self.output.symlink_to(outside, target_is_directory=True)
        self.addCleanup(lambda: os.rmdir(self.output) if os.name == "nt" else self.output.unlink())
        with self.assertRaises(ProjectError):
            cleanup_paths(self.project, "WP-0001")
        with self.assertRaises(ProjectError):
            remove_output(self.output)
        self.assertEqual((outside / "keep").read_text(), "keep")

    def test_failed_removal_emits_attention_without_undoing_the_merge(self):
        integration = Integration(self.context, "WP-0001", False)
        with mock.patch("wpo.artifacts.shutil.rmtree", side_effect=PermissionError("locked")):
            integration.cleanup()
        self.assertTrue(self.output.exists())
        self.assertIn("cleanup-failed", (self.project.runs_dir / "events.jsonl").read_text(encoding="utf-8"))
        self.assertNotIn(f"removed {self.output}", integration.log_path.read_text(encoding="utf-8"))

    def test_inspection_errors_emit_attention_without_undoing_the_merge(self):
        for error in (PermissionError("inspection denied"), subprocess.CalledProcessError(1, "git")):
            with self.subTest(error=error), mock.patch.object(Project, "paths_removed_after_merge", side_effect=error):
                Integration(self.context, "WP-0001", False).cleanup()
                self.assertTrue(self.output.exists())
                self.assertIn("cleanup-failed", (self.project.runs_dir / "events.jsonl").read_text(encoding="utf-8"))

    def test_space_floor_is_disabled_by_default_and_rejects_invalid_values(self):
        with mock.patch("wpo.artifacts.shutil.disk_usage") as usage:
            self.assertIsNone(space_problem(self.project, self.tree))
            usage.assert_not_called()
        for invalid in (True, -1, float("nan"), float("inf"), "20"):
            self.project.config["minimum_build_free_gib"] = invalid
            with self.assertRaises(ProjectError):
                space_problem(self.project, self.tree)

    def test_low_space_blocks_before_runner_or_provider_launch_without_resetting_history(self):
        self.project.config["minimum_build_free_gib"] = 20
        history = self.project.wp_run_dir("WP-0001") / "attempts.json"
        history.write_text('{"rounds":42}', encoding="utf-8")
        with mock.patch("wpo.artifacts.shutil.disk_usage", return_value=SimpleNamespace(free=19 * GIB)), mock.patch("wpo.runner.WorkPackageRun") as runner:
            self.assertEqual(run_wp(self.context, "WP-0001", self.tree, RunOptions()), 0)
            runner.assert_not_called()
        self.assertEqual(history.read_text(), '{"rounds":42}')
        self.assertEqual(self.context.wp_in_tree("WP-0001", self.tree).status, "blocked")

    def test_space_check_queries_the_checkout_volume_and_allows_exact_floor(self):
        self.project.config["minimum_build_free_gib"] = 20
        with mock.patch("wpo.artifacts.shutil.disk_usage", return_value=SimpleNamespace(free=20 * GIB)) as usage:
            self.assertIsNone(space_problem(self.project, self.tree))
            usage.assert_called_once_with(self.tree.resolve())


if __name__ == "__main__":
    unittest.main()
