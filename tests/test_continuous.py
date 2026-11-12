"""Continuous scheduling uses scratch state and never starts a real worker."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wpo import continuous
from wpo.events import Journal
from wpo.project import DEFAULTS, Project


class ContinuousTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = Project(self.root, copy.deepcopy(DEFAULTS), "standalone")
        self.wps = [SimpleNamespace(id="WP-0001", lane="first", status="in_progress", depends_on=[], owns=["first/**"], builder="pin:test", reviewer="pin:review"),
                    SimpleNamespace(id="WP-0002", lane="second", status="todo", depends_on=[], owns=["second/**"], builder="pin:test", reviewer="pin:review")]
        self.context = SimpleNamespace(project=self.project, machine=SimpleNamespace(execution_mode="standalone"),
                                       effective_wps=lambda: self.wps, merged_done_ids=lambda: set(),
                                       journal=Journal(self.project.runs_dir),
                                       router=SimpleNamespace(select=Mock(return_value=SimpleNamespace(candidates=[{"id": "test"}], skipped=[]))))
        self.launches = []
        self.stack = []
        for name, value in [("claimed_elsewhere", lambda *_: False), ("supervisor_pid", lambda *_: None),
                            ("lane_active", lambda *_: False), ("resume", lambda _c, _d, ids: self.launches.extend(ids))]:
            item = patch("wpo.lanes." + name, side_effect=value)
            item.start()
            self.addCleanup(item.stop)

    def enable(self):
        continuous.enable(self.context, ["first", "second"])

    def test_default_is_idle(self):
        self.assertFalse(continuous.tick(self.context, now=100)["enabled"])
        self.assertEqual(self.launches, [])

    def test_human_wait_fills_an_independent_slot_and_survives_restart(self):
        self.enable()
        continuous.wait(self.context, "WP-0001", "Choose design")
        continuous.tick(self.context, now=100)
        self.assertEqual(self.launches, ["WP-0002"])
        self.assertEqual(continuous.block_reason(self.context, "WP-0001"), "Choose design")
        self.assertEqual(continuous.status(self.context)["records"]["WP-0001"]["state"], "human_wait")

    def test_manual_hold_is_preserved(self):
        from wpo.lanes import read_holds, write_holds
        self.enable()
        write_holds(self.context, {"first": "Deliberate user hold"})
        continuous.tick(self.context, now=100)
        self.assertEqual(self.launches, ["WP-0002"])
        self.assertEqual(read_holds(self.context), {"first": "Deliberate user hold"})

    def test_quota_unavailable_does_not_start_or_start_clock(self):
        self.enable()
        self.context.router.select.return_value = SimpleNamespace(candidates=[], skipped=[{"reason": "quota exhausted"}])
        state = continuous.tick(self.context, now=100)
        self.assertEqual(self.launches, [])
        self.assertEqual(state["waiting"]["WP-0001"], "quota exhausted")
        self.context.router.select.return_value = SimpleNamespace(candidates=[{}], skipped=[])
        continuous.tick(self.context, now=200)
        self.assertEqual(self.launches, ["WP-0001", "WP-0002"])

    def test_milestone_expiry_coalesces_and_does_not_repeat_work(self):
        self.enable()
        continuous.tick(self.context, now=100)
        self.launches.clear()
        continuous.tick(self.context, now=3701)
        continuous.tick(self.context, now=3800)
        self.assertEqual(self.launches, [])
        self.assertIn("checkpoint", continuous.block_reason(self.context, "WP-0001"))
        events = self.context.journal.path.read_text().splitlines()
        self.assertEqual(sum('"kind": "progress-intervention"' in line for line in events), 2)

    def test_explicit_stop_prevents_launch_and_requires_start(self):
        self.enable()
        continuous.disable(self.context, "User stopped project")
        continuous.tick(self.context, now=100)
        self.assertEqual(self.launches, [])
        self.assertEqual(continuous.block_reason(self.context, "WP-0002"), "User stopped project")

    def test_repeated_failures_intervene_before_another_round(self):
        self.enable()
        continuous.tick(self.context, now=100)
        directory = self.project.runs_dir / "WP-0001"
        directory.mkdir(exist_ok=True)
        (directory / "attempts.json").write_text('{"failures": 3}')
        continuous.tick(self.context, now=110)
        self.assertIn("failed", continuous.block_reason(self.context, "WP-0001"))

    def test_fresh_plan_cannot_overwrite_an_extant_native_task(self):
        self.enable()
        continuous.wait(self.context, "WP-0001", "Need decision")
        with patch.object(continuous, "native_busy", return_value={"WP-0001"}), self.assertRaisesRegex(ValueError, "native"):
            continuous.continue_work(self.context, "WP-0001", "Smaller task", "New evidence")

    def test_continue_clears_only_its_own_pause_and_keeps_history(self):
        from wpo.inbox import pause_requested, request_pause
        self.enable()
        continuous.wait(self.context, "WP-0001", "Need decision")
        continuous.continue_work(self.context, "WP-0001", "Small next result", "Decision made")
        self.assertIsNone(pause_requested(self.project.runs_dir / "WP-0001"))
        self.assertEqual(len(continuous.status(self.context)["records"]["WP-0001"]["history"]), 1)
        continuous.wait(self.context, "WP-0001", "Another choice")
        request_pause(self.project.runs_dir / "WP-0001", "Manual pause after intervention")
        continuous.continue_work(self.context, "WP-0001", "Plan", "Routine decision")
        self.assertEqual(pause_requested(self.project.runs_dir / "WP-0001"), "Manual pause after intervention")

    def test_progress_does_not_extend_the_checkpoint(self):
        self.enable()
        continuous.tick(self.context, now=100)
        with patch("wpo.continuous.time.time", return_value=3600):
            continuous.progress(self.context, "WP-0001", "Saved a useful recording")
        continuous.tick(self.context, now=3701)
        self.assertIn("checkpoint", continuous.block_reason(self.context, "WP-0001"))

    def test_overlapping_active_work_blocks_only_that_candidate(self):
        self.enable()
        self.wps[1].owns = ["first/module.py"]
        with patch.object(continuous, "native_busy", return_value={"WP-0001"}):
            state = continuous.tick(self.context, now=100)
        self.assertEqual(self.launches, [])
        self.assertIn("overlap", state["waiting"]["WP-0002"])

    def test_guard_checks_failure_before_another_session(self):
        self.enable()
        continuous.tick(self.context, now=100)
        (self.project.runs_dir / "WP-0001").mkdir(exist_ok=True)
        (self.project.runs_dir / "WP-0001/attempts.json").write_text('{"failures": 3}')
        with patch("wpo.continuous.time.time", return_value=110):
            self.assertIn("failed", continuous.checkpoint(self.context, "WP-0001"))

    def test_unsafe_numeric_policy_is_rejected(self):
        for key, value in [("interval_seconds", 0), ("checkpoint_seconds", float("nan")), ("failed_rounds", True)]:
            self.project.config["continuous"] = {key: value}
            with self.assertRaises(ValueError):
                self.enable()

    def test_same_lane_transition_requires_individual_admission(self):
        from wpo.supervisor import Supervisor
        self.wps[1].lane = "first"
        continuous.enable(self.context, ["first"])
        with patch("wpo.continuous.time.time", return_value=100):
            continuous.tick(self.context, now=100)
            # Simulate one admitted WP then completion, leaving a same-lane WP unadmitted.
            state = continuous.status(self.context)
            state["records"].pop("WP-0002", None)
            continuous.save(self.context, state)
            self.wps[0].status = "done"
            supervisor = Supervisor(self.context, "first", True, ["WP-0001"], {})
            self.assertIsNone(supervisor.claim("WP-0002"))

    def test_stop_survives_failed_pause_delivery(self):
        self.enable()
        with patch.object(continuous, "_pause", side_effect=RuntimeError("coordinator offline")), self.assertRaises(RuntimeError):
            continuous.disable(self.context, "Explicit stop")
        state = continuous.status(self.context)
        self.assertFalse(state["enabled"])
        self.assertTrue(state["stopped"])
        self.assertEqual(state["records"]["WP-0001"]["pause_pending"], "Explicit stop")

    def test_stop_start_rearms_quiescent_work_without_counting_stopped_time(self):
        self.enable()
        continuous.tick(self.context, now=100)
        continuous.disable(self.context, "Stop")
        self.launches.clear()
        self.enable()
        continuous.tick(self.context, now=10000)
        self.assertEqual(self.launches, ["WP-0001", "WP-0002"])
        self.assertEqual(continuous.status(self.context)["records"]["WP-0001"]["started_at"], 10000)

    def test_repeated_wait_has_one_owned_wrapup_message(self):
        self.enable()
        with patch("wpo.continuous.live_session", return_value={"pid": 123}):
            continuous.wait(self.context, "WP-0001", "Choose")
            continuous.wait(self.context, "WP-0001", "Choose")
        inbox = self.project.runs_dir / "WP-0001/inbox"
        self.assertEqual(len(list(inbox.glob("*.md"))), 1)
        continuous.continue_work(self.context, "WP-0001", "Small next task", "User selected")
        self.assertEqual(list(inbox.glob("*.md")), [])

    def test_failure_total_survives_reevaluation_and_reset(self):
        from wpo.runner import RoundLedger
        self.enable()
        path = self.project.runs_dir / "WP-0001/attempts.json"
        path.parent.mkdir(exist_ok=True)
        ledger = RoundLedger(path)
        ledger.fail("verify", "First")
        ledger.fail("verify", "Second")
        continuous.tick(self.context, now=100)
        for index in range(3):
            ledger.evaluated("continue", "Re-evaluate", None)
            ledger.fail("verify", str(index))
        ledger.reset()
        continuous.tick(self.context, now=110)
        self.assertIn("failed", continuous.block_reason(self.context, "WP-0001"))

    def test_late_cancelled_task_remains_busy_until_actual_terminal_callback(self):
        self.context.machine.execution_mode = "t3"
        request = {"id": "1" * 32, "kind": "session", "state": "cancelled", "task_id": "late-task",
                   "payload": {"wp": "WP-0001"}}
        with patch("wpo.t3.Bridge") as bridge:
            bridge.return_value.requests.return_value = [request]
            self.assertEqual(continuous.native_busy(self.context), {"WP-0001"})
            request["result"] = {"state": "cancelled", "task_id": "late-task", "detail": "cancel requested"}
            self.assertEqual(continuous.native_busy(self.context), {"WP-0001"})
            request["late_result"] = {"state": "failed", "task_id": "late-task", "outcome": "cancelled", "text": "Native task terminal interrupted"}
            self.assertEqual(continuous.native_busy(self.context), set())

    def test_scheduler_failure_is_exposed_without_replaying_a_launch(self):
        from wpo.t3 import read_json
        self.enable()
        with patch("wpo.context.Context.load", return_value=self.context), patch.object(continuous, "tick", side_effect=ValueError("invalid durable state")), self.assertRaises(ValueError):
            continuous.run(self.context)
        runtime = read_json(continuous.directory(self.context) / "runtime.json")
        self.assertEqual(runtime["state"], "failed")
        self.assertIn("invalid durable state", runtime["detail"])
        self.assertEqual(self.launches, [])

    def test_main_done_is_not_reintegrated_or_launched(self):
        self.enable()
        self.wps[0].status = "done"
        self.context.merged_done_ids = lambda: {"WP-0001"}
        continuous.tick(self.context, now=100)
        self.assertEqual(self.launches, ["WP-0002"])

    def test_pending_native_receipt_blocks_duplicate_launch_across_ticks(self):
        self.enable()
        with patch.object(continuous, "native_busy", return_value={"WP-0001"}):
            continuous.tick(self.context, now=100)
            continuous.tick(self.context, now=110)
        self.assertNotIn("WP-0001", self.launches)


if __name__ == "__main__":
    unittest.main()
