"""End-to-end: a scratch repository, three WPs in two lanes, scripted agents.

Covers worktree creation, a dependency that waits for a merge, a delegated worker, a review that
requests changes once, a lock-file conflict settled by the reconciler, verification after merging
main, fast-forwards into main and the event journal. Run from the skill root:
`python -I -B tests/test_end_to_end.py` (about a minute).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
ENTRY_POINT = SKILL_ROOT / "bin" / "wpo.py"
FAKE_AGENT = Path(__file__).resolve().parent / "fake_agent.py"
SHELL = ["cmd", "/c"] if sys.platform == "win32" else ["sh", "-c"]
TIMEOUT_SECONDS = 600

REGEN = "import pathlib; pathlib.Path('deps.lock').write_text(''.join(sorted(p.read_text() for p in pathlib.Path('deps').glob('*.dep'))))"
CHECK = "import pathlib, sys; want = ''.join(sorted(p.read_text() for p in pathlib.Path('deps').glob('*.dep'))); sys.exit(pathlib.Path('deps.lock').read_text() != want)"
# A mechanical check (configured in mechanical_checks): no top-level text file may carry the marker.
LINT = "import pathlib, sys; sys.exit(any('LINT' in p.read_text() for p in pathlib.Path('.').glob('*.txt')))"

WP = """---
id: {id}
title: {title}
milestone: M1
lane: {lane}
status: todo
depends_on: [{depends}]
builder: code
reviewer: code-hard
owns: [{owns}]
---

## Goal
Test.

## Steps
- [ ] Do it

## Verify
```
python check.py
python lint.py
```

## Progress log

## Review

## Handoff
"""


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True).stdout


class EndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="wpo-e2e-"))
        self.root = base / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "test")
        (self.root / "deps").mkdir()
        (self.root / "deps" / "base.dep").write_text("base\n")
        (self.root / "deps.lock").write_text("base\n")
        (self.root / "regen.py").write_text(REGEN)
        (self.root / "check.py").write_text(CHECK)
        (self.root / "lint.py").write_text(LINT)
        (self.root / ".gitignore").write_text(".orchestration/runs/\n.orchestration/tasks/\n")
        (self.root / "AGENTS.md").write_text("# Scratch rules\nChange only what the WP owns.\n")
        wp_dir = self.root / "docs" / "plan" / "wp"
        wp_dir.mkdir(parents=True)
        for wp_id, title, lane, depends, owns in (
            ("WP-0001", "Add a", "alpha", "", "a.txt, deps/a.dep"),
            ("WP-0002", "Add b with a worker", "beta", "", "b.txt, b2.txt, deps/b.dep"),
            ("WP-0003", "Add c after a", "alpha", "WP-0001", "c.txt"),
            ("WP-0004", "Add d in staged mode", "gamma", "", "d1.txt, d2.txt"),
        ):
            (wp_dir / f"{wp_id}-x.md").write_text(WP.format(id=wp_id, title=title, lane=lane, depends=depends, owns=owns))
        agent = f"{Path(sys.executable).as_posix()} -I -B {FAKE_AGENT.as_posix()}"
        config = {
            "name": "scratch",
            "builder_skills": ["coding"],
            "verify_workspace": ["python check.py"],
            "builder_commands": ["python"],
            "reviewer_commands": ["python"],
            "shared_files": [{"path": "deps.lock"}],
            "lockfile_reconcilers": [{"file": "deps.lock", "command": "python regen.py"}],
            "worker_families": ["script"],
            "lanes": {"gamma": {"mode": "staged"}},
            "orchestrator_room_percent": None,
            "shell": SHELL,
            "mechanical_checks": ["python lint.py"],
            "routing": {"families": ["script"], "family_cost_class": {"script": "local"}, "free_routes": {"enabled": False},
                        "routes": [{"id": "fake", "family": "script", "harness": "command", "model": agent, "score": 60, "writes_code": True},
                                   # Where a session cut off on 'fake' continues, as a cut-off subscription session fails over.
                                   {"id": "fake-backup", "family": "script", "harness": "command", "model": agent, "score": 59, "writes_code": True}]},
        }
        (self.root / ".orchestration").mkdir()
        (self.root / ".orchestration" / "config.json").write_text(json.dumps(config, indent=2))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "scratch")
        # An uncommitted draft in the main checkout must not be scheduled: its worktree would lack it.
        (wp_dir / "WP-0005-x.md").write_text(WP.format(id="WP-0005", title="Draft", lane="alpha", depends="", owns="e.txt"))

        scenario = {
            "builders": {
                # The builder leaves a lint marker; the repair session, not a builder round, removes it.
                "WP-0001": {"files": {"a.txt": "a LINT\n", "deps/a.dep": "a\n", "deps.lock": "a\nbase\n"}},
                "WP-0002": {"files": {"b.txt": "b\n", "deps/b.dep": "b\n", "deps.lock": "b\nbase\n"},
                            "delegate": {"name": "second-file", "task": "Write b2.txt.", "write_paths": "b2.txt"}, "cut_off_first": True},
                "WP-0003": {"files": {"c.txt": "c\n"}, "handoff_first": True},
            },
            "workers": {"WP-0002": {"files": {"b2.txt": "from the worker\n"}}},
            "repairs": {"WP-0001": {"files": {"a.txt": "a\n"}}},
            # Two rejections trigger a re-evaluation before the third round.
            "reviews": {"WP-0003": ["CHANGES_REQUESTED", "CHANGES_REQUESTED", "PASS"]},
            # The first plan's single task does not fit an executor's context; the replan splits it.
            "plans": {"WP-0004": [
                {"tasks": [{"id": "big", "title": "both files", "files": ["d1.txt", "d2.txt"], "provides": "d1 and d2", "check": "python check.py", "instructions": "write both"}]},
                {"tasks": [{"id": "t1", "title": "d1", "files": ["d1.txt"], "provides": "d1.txt", "check": "python check.py", "instructions": "write d1"},
                           {"id": "t2", "title": "d2", "files": ["d2.txt"], "provides": "d2.txt", "consumes": ["t1"], "check": "python check.py", "instructions": "write d2"}]},
            ]},
            "executors": {"big": {"compact": True}, "t1": {"files": {"d1.txt": "d1\n"}}, "t2": {"files": {"d2.txt": "d2\n"}}},
        }
        state = base / "fake-state"
        state.mkdir()
        (base / "scenario.json").write_text(json.dumps(scenario))
        skills = base / "skills"
        (skills / "coding").mkdir(parents=True)
        (skills / "coding" / "SKILL.md").write_text("---\nname: coding\ndescription: test\n---\n\n# Coding\nGuard clauses first.\n")
        (base / "user-rules.md").write_text("# Global\n\n## Model selection\nNot carried.\n\n## Correctness\nVerify, never guess.\n")
        machine = {"execution_mode": "standalone", "python": sys.executable, "state_dir": str(base / "machine-state"), "reviewer_agent": str(base / "no-reviewer.md"),
                   "usage": {"script": str(base / "no-usage.py"), "cache": str(base / "no-usage.json"), "max_age_seconds": 300},
                   "skills": {"dirs": [str(skills)], "paths": {}},
                   "user_rules": {"file": str(base / "user-rules.md"), "sections": ["Correctness"]},
                   # Scratch projects get no dashboard server; the unit tests cover its lifecycle.
                   "dashboard": {"autostart": False}}
        (base / "machine.json").write_text(json.dumps(machine))
        self.env = {**os.environ, "WPO_MACHINE_CONFIG": str(base / "machine.json"), "FAKE_SCENARIO": str(base / "scenario.json"), "FAKE_STATE": str(state)}

    def one(self, pattern: str) -> str:
        """The one run file matching `pattern` (under the runs directory), whichever route wrote it."""
        matches = list((self.root / ".orchestration/runs").glob(pattern))
        self.assertEqual(len(matches), 1, f"{pattern}: {matches}")
        return matches[0].read_text()

    def wpo(self, *arguments: str) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-I", "-B", str(ENTRY_POINT), *arguments], cwd=self.root, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def test_two_lanes_to_main(self) -> None:
        # Both worktrees start from the same main, so whichever of WP-0001 and WP-0002 merges second
        # conflicts on deps.lock and is reconciled.
        alpha = self.wpo("supervise", "--lane", "alpha", "--wps", "WP-0001,WP-0003")
        beta = self.wpo("supervise", "--lane", "beta", "--wps", "WP-0002")
        for process in (alpha, beta):
            output, _ = process.communicate(timeout=TIMEOUT_SECONDS)
            self.assertEqual(process.returncode, 0, output)

        events = self.events_text()
        for wp_id in ("WP-0001", "WP-0002", "WP-0003"):
            self.assertIn(f"{wp_id} merged into main", events)
        self.assertIn("WP-0003 review 1 requested changes (round 1; open: R1-F1)", events)
        self.assertIn("WP-0003 review 2 requested changes (round 2; open: R1-F1, R2-F1)", events)
        # The builder answered each finding through `wpo finding`; the passing review closed them.
        ledger = json.loads((self.root / ".orchestration/runs/WP-0003/findings.json").read_text())
        self.assertEqual({finding["id"]: (finding["status"], finding["answer"]["kind"]) for finding in ledger["findings"]},
                         {"R1-F1": ("closed-by-pass", "fixed"), "R2-F1": ("closed-by-pass", "fixed")})
        self.assertIn("WP-0003 re-evaluated after round 2: continue", events)
        # Its first builder session handed off; a fresh session continued round 1 from the handoff.
        self.assertRegex(events, r"WP-0003 builder session on fake(-backup)? handed off")
        continued = self.one("WP-0003/attempt-1-builder-handoff-1.*.log.prompt.md")
        self.assertIn("<handoff>\nDone: half of the files.\nNext: the rest.\n</handoff>", continued)
        wp3 = next((self.root / "docs/plan/wp").glob("WP-0003-*.md")).read_text()
        self.assertIn("## Re-evaluation", wp3)
        for name, content in (("a.txt", "a\n"), ("b.txt", "b\n"), ("b2.txt", "from the worker\n"), ("c.txt", "c\n"), ("deps.lock", "a\nb\nbase\n")):
            self.assertEqual((self.root / name).read_text(), content, name)
        statuses = [line for path in sorted((self.root / "docs/plan/wp").glob("*.md")) for line in path.read_text().splitlines() if line.startswith("status:")]
        self.assertEqual(statuses[:3], ["status: done"] * 3)
        self.assertEqual(git(self.root, "worktree", "list").count("\n"), 1)
        self.assertFalse((self.root / "e.txt").exists())
        self.assertFalse((self.root / ".orchestration/runs/dashboard.json").exists())
        self.assertNotIn("WP-0005", events)
        integrate_logs = "".join((self.root / f".orchestration/runs/{wp_id}/integrate.log").read_text() for wp_id in ("WP-0001", "WP-0002"))
        self.assertIn("deps.lock reconciled", integrate_logs)
        # WP-0002's first session was cut off mid-write: the next route's session got what it wrote and the Verify output.
        prompts = [path.read_text() for path in (self.root / ".orchestration/runs/WP-0002").glob("attempt-1-builder.*.prompt.md")]
        self.assertEqual(len(prompts), 2)
        continued = next((text for text in prompts if "## Recovery: the previous builder session was cut off" in text), "")
        self.assertTrue(continued, "the second route's prompt carries the recovery report")
        self.assertRegex(continued, r"- b\.txt \(last edited \d+ (s|min) ago\)")
        self.assertRegex(continued, r"Diff of the most recently edited file, \S+:\n```diff\n(.|\n)*\+")
        self.assertRegex(continued, r"(pass|FAIL): ")
        self.assertIsNone(json.loads((self.root / ".orchestration/runs/WP-0002/attempts.json").read_text())["writing"])
        delegate_state = json.loads((self.root / ".orchestration/runs/WP-0002/delegate/second-file/state.json").read_text())
        self.assertEqual(delegate_state["state"], "done")
        news, _ = self.wpo("whats-new", "--last", "5").communicate(timeout=60)
        for wp_id in ("WP-0001", "WP-0002", "WP-0003"):
            self.assertIn(f"## {wp_id}: ", news)
            self.assertIn(f"type {wp_id}.txt", news)
        # The mechanical failure went to a repair session and WP-0001 passed without a second builder round.
        self.assertIn("WP-0001: only mechanical checks failed (python lint.py)", events)
        self.assertFalse(list((self.root / ".orchestration/runs/WP-0001").glob("attempt-2-builder.*.log")))
        self.assertTrue((self.root / ".orchestration/runs/WP-0001/attempt-1-verify-after-repair.log").exists())

        # Builders and workers load the coding skill and the carried user rules, nothing more.
        builder_prompt = self.one("WP-0001/attempt-1-builder.*.log.prompt.md")
        worker_prompt = self.one("WP-0002/delegate/second-file/session.*.log.prompt.md")
        for prompt in (builder_prompt, worker_prompt):
            self.assertIn("## Working rules", prompt)
            self.assertIn("coding/SKILL.md:", prompt)
            self.assertIn("Verify, never guess.", prompt)
            self.assertNotIn("Not carried.", prompt)
        # The WP file is inlined under its path, and progress goes through wpo log.
        self.assertIn("File docs/plan/wp/WP-0001-", builder_prompt)
        self.assertIn("wpo log", builder_prompt)
        self.assertNotIn("already in your context", builder_prompt)
        # Reviews start from the brief: WP-0003 added c.txt, which has no symbol patterns.
        review_prompt = self.one("WP-0003/attempt-1-review.*.log.prompt.md")
        self.assertIn("# Review brief", review_prompt)
        self.assertIn("+c", review_prompt)
        self.assertNotIn("SKILL.md:", review_prompt)  # the scratch machine's reviewer agent declares none

    def test_staged_lane_replans_a_task_that_compacts(self) -> None:
        gamma = self.wpo("supervise", "--lane", "gamma", "--wps", "WP-0004")
        output, _ = gamma.communicate(timeout=TIMEOUT_SECONDS)
        self.assertEqual(gamma.returncode, 0, output)
        events = self.events_text()
        self.assertIn("WP-0004 merged into main", events)
        self.assertIn("session-compacted", events)
        self.assertEqual(events.count('"kind": "staged-plan"'), 2)
        self.assertEqual(((self.root / "d1.txt").read_text(), (self.root / "d2.txt").read_text()), ("d1\n", "d2\n"))

    def events_text(self) -> str:
        path = self.root / ".orchestration" / "runs" / "events.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""


if __name__ == "__main__":
    unittest.main()
