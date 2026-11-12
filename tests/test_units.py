"""Unit tests: `python -I -B -m unittest discover -s tests -t .` from the skill root."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wpo import harness, routing, wpfile  # noqa: E402
from wpo.events import Journal, UnknownEventKindError  # noqa: E402
from wpo.machine import MachineConfig, DEFAULTS  # noqa: E402
from wpo.staged import PlanError, parse_plan  # noqa: E402

WP_TEXT = """---
id: WP-0042
title: Example
milestone: M1
lane: core   # a comment
status: todo
depends_on: [WP-0001, WP-0002]
builder: code
reviewer: code-hard
owns: [src/core/**, docs/core.md]
---

## Goal
Do it.

## Steps
- [ ] One

## Verify
```
cargo test -p core
# a comment line
cargo fmt --all --check
```

## Progress log
- 2026-10-22 10:00 [runner] first

## Review

## Handoff
"""


def machine_in(directory: Path) -> MachineConfig:
    return MachineConfig({**DEFAULTS, "execution_mode": "standalone", "state_dir": str(directory / "state"), "usage": {**DEFAULTS["usage"], "cache": str(directory / "none.json"), "script": str(directory / "none.py"),
                                                                                                        "history": str(directory / "none.jsonl")}})


class WorkPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())
        self.path = self.directory / "WP-0042-example.md"
        self.path.write_bytes(WP_TEXT.replace("\n", "\r\n").encode())

    def test_front_matter_and_sections(self) -> None:
        wp = wpfile.parse(self.path)
        self.assertEqual((wp.id, wp.lane, wp.depends_on, wp.owns), ("WP-0042", "core", ["WP-0001", "WP-0002"], ["src/core/**", "docs/core.md"]))
        self.assertEqual(wp.verify_commands(), ["cargo test -p core", "cargo fmt --all --check"])
        self.assertEqual(wp.round_checks(["cargo fmt --all --check", "cargo test --workspace"]),
                         ["cargo test -p core", "cargo fmt --all --check", "cargo test --workspace"])
        self.assertEqual(wp.latest_progress(), "2026-10-22 10:00 [runner] first")
        self.assertEqual(wp.delegate_tier("code-simple"), "code-simple")

    def test_delegate_none_disables_workers(self) -> None:
        self.path.write_text(WP_TEXT.replace("owns:", "delegate: none\nowns:"), encoding="utf-8")
        self.assertIsNone(wpfile.parse(self.path).delegate_tier("code-simple"))

    def test_status_and_appends_keep_crlf(self) -> None:
        wp = wpfile.set_status(wpfile.parse(self.path), "review")
        wpfile.log_progress(wp, "second")
        wpfile.append_to_section(wp, "Review", ["### Attempt 1", "VERDICT: PASS"])
        raw = self.path.read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        wp = wpfile.parse(self.path)
        self.assertEqual(wp.status, "review")
        self.assertTrue(wp.latest_progress().endswith("[runner] second"))
        self.assertEqual([line for line in wp.section("Review") if line], ["### Attempt 1", "VERDICT: PASS"])
        self.assertIn("\r\n\r\n## Handoff", raw.decode())

    def test_missing_field_is_an_error(self) -> None:
        self.path.write_text(WP_TEXT.replace("reviewer: code-hard\n", ""), encoding="utf-8")
        with self.assertRaises(wpfile.WorkPackageError):
            wpfile.parse(self.path)

    def test_ownership(self) -> None:
        wp = wpfile.parse(self.path)
        owned = lambda path: wpfile.path_owned(path, wp, "docs/plan/wp", ["Cargo.toml"])  # noqa: E731
        self.assertTrue(owned("src/core/a/b.rs"))
        self.assertTrue(owned("docs/core.md"))
        self.assertTrue(owned("Cargo.toml"))
        self.assertTrue(owned("docs/plan/wp/WP-0042-example.md"))
        self.assertFalse(owned("src/other.rs"))
        self.assertFalse(owned("docs/core.md.bak"))
        self.assertTrue(wpfile.is_wp_file("docs/plan/wp/WP-0007-x.md", "docs/plan/wp"))
        self.assertFalse(wpfile.is_wp_file("docs/plan/wp/sub/WP-0007-x.md", "docs/plan/wp"))


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        # The ordering tests use their own free routes; agy's static routes are covered on their own.
        self.router = routing.Router(machine_in(Path(tempfile.mkdtemp())), {"legacy_keys": {"claude-opus-high": "frontier+taste"},
                                                                          "families": ["free", "local", "gpt", "claude"],
                                                                          "routes_extra": [{"id": "local-test", "family": "local", "harness": "local",
                                                                                            "model": "test-local-model", "effort": "max", "score": 39.5,
                                                                                            "writes_code": True}]})

    def free(self, route_id: str, score: float, dispatchable: bool = True) -> routing.Route:
        return routing.Route(route_id, "free", "opencode", f"opencode/{route_id}", None, score, True, "free", provider="zen",
                             dispatchable=dispatchable, not_dispatchable_reason=None if dispatchable else "test")

    def select(self, spec: str, free_routes=(), families=None, unavailable=(), free_last_resort=True, gate=False) -> routing.Selection:
        def family(oracle, name):
            return routing.Availability(False, "test cap", routing.now_utc() + timedelta(hours=1)) if name in unavailable else routing.Availability(True)
        with mock.patch.object(routing.Router, "catalog_routes", return_value=list(free_routes)), \
             mock.patch.object(routing.AvailabilityOracle, "family", family):
            return self.router.select(spec, families, free_last_resort=free_last_resort, gate=gate)

    def ids(self, selection: routing.Selection) -> list[str]:
        return [candidate["id"] for candidate in selection.candidates]

    def test_names_normalize(self) -> None:
        for name in ("zhipuai/GLM-5.3-Flash", "glm-5.3-flash-free", "z-ai/glm-5.3-flash:free", "glm-5.3-flash-20261019"):
            self.assertEqual(routing.normalize_model_name(name), "glm53flash")

    def test_specs(self) -> None:
        policy = self.router.policy
        self.assertEqual(routing.resolve_work_spec("frontier+taste", policy).taste, True)
        self.assertEqual(routing.resolve_work_spec("claude-opus-high", policy).tier, "frontier")
        self.assertEqual(routing.resolve_work_spec("pin:gpt-sol-high", policy).pinned_route, "gpt-sol-high")
        with self.assertRaises(routing.RoutingError):
            routing.resolve_work_spec("opus-please", policy)

    def test_free_first_best_first_then_paid_and_local_only_by_pin(self) -> None:
        ids = self.ids(self.select("research", [self.free("weak", 31), self.free("strong", 42)]))
        self.assertEqual(ids[:2], ["strong", "weak"])
        self.assertNotIn("local-test", ids)
        self.assertEqual(self.ids(self.select("pin:local-test")), ["local-test"])

    def test_paid_cheapest_first_and_taste(self) -> None:
        self.assertEqual(self.ids(self.select("code-hard"))[:3], ["gpt-sol-medium", "gpt-sol-high", "gpt-sol-xhigh"])
        # Code runs at low effort: Opus low is the cheapest Claude route at the code floor.
        self.assertEqual(self.ids(self.select("code+taste", [self.free("f", 49)]))[0], "claude-opus-low")
        self.assertEqual(self.ids(self.select("code-hard+taste"))[0], "claude-opus-medium")

    def test_try_it_needs_a_command_block_or_says_why_not(self) -> None:
        from types import SimpleNamespace

        from wpo import whats_new, wpfile

        def problem(try_it):
            # Only the sections matter: a stand-in WP whose file holds a Try it section.
            text = f"## Try it\n{try_it}\n\n## Handoff\n"
            return whats_new.try_it_problem(SimpleNamespace(section=lambda heading: wpfile.section_lines(text, heading)))
        self.assertIn("missing or empty", problem(""))
        self.assertIn("no commands", problem("Open the editor and look at the cheek."))
        self.assertIsNone(problem("```\n# the editor\ncargo run --example editor\n```\nLook at the cheek."))
        self.assertIsNone(problem(f"{whats_new.NOTHING_TO_TRY} a library change with no entry point."))

    def test_tiers_rank_each_familys_frontier_of_price_tokens_and_time(self) -> None:
        def route(route_id, score, cost, tokens, speed, family="claude"):
            return routing.Route(route_id, family, family, "m", "low", score, True, "subscription",
                                 cost_per_task=cost, index_tokens_m=tokens, output_speed=speed)
        cheap_slow = route("cheap-slow", 43, 0.21, 440, 240)
        dear_quick = route("dear-quick", 42.3, 0.55, 20, 82)
        dominated = route("dominated", 42, 0.60, 30, 80)
        other_family = route("other-family", 41, 0.90, 50, 50, family="gpt")
        unmeasured = routing.Route("free", "free", "free", "m", None, 40, True, "free")
        kept = routing.family_frontiers([cheap_slow, dear_quick, dominated, other_family, unmeasured])
        # Cheaper but twenty times wordier and slower does not dominate; worse on every measure does,
        # and only within its family.
        self.assertEqual([r.id for r in kept], ["cheap-slow", "dear-quick", "other-family", "free"])

    def test_over_the_weekly_pace_a_family_serves_one_step_down_before_it_waits(self) -> None:
        router = routing.Router(machine_in(Path(tempfile.mkdtemp())), {"weekly_pace": {"claude": {"budget_percent": 70, "slack_percent": 10}}})
        resets = (routing.now_utc() + timedelta(days=3.5)).timestamp()  # half the week gone: pace 45%

        def week(used):
            return [{"label": "7d", "used_percent": used, "window_seconds": 7 * 86400, "resets_at": resets}]
        self.assertEqual(router.subscription_state(week(40.0), "claude")[2], None)
        blocked_until, _, cheaper = router.subscription_state(week(50.0), "claude")
        self.assertLessEqual(blocked_until, routing.now_utc())
        self.assertIn("over the weekly pace", cheaper)
        self.assertGreater(router.subscription_state(week(60.0), "claude")[0], routing.now_utc())

        def family(oracle, name):
            return routing.Availability(True, cheaper_only="over pace") if name == "claude" else routing.Availability(False, "capped", routing.now_utc() + timedelta(hours=1))
        with mock.patch.object(routing.Router, "catalog_routes", return_value=[]), \
             mock.patch.object(routing.AvailabilityOracle, "family", family):
            selection = router.select("code-hard+taste")
        self.assertEqual([c["id"] for c in selection.candidates], ["claude-opus-low"])
        self.assertTrue(selection.candidates[0]["downgrade"])

    def test_downgrades_and_no_route(self) -> None:
        selection = self.select("frontier", unavailable=("claude",))
        self.assertEqual(self.ids(selection), ["gpt-sol-high", "gpt-sol-xhigh", "gpt-sol-medium"])
        self.assertEqual([candidate["downgrade"] for candidate in selection.candidates], [False, False, True])
        empty = self.select("frontier", families=["claude"], unavailable=("claude",))
        self.assertEqual(empty.candidates, [])
        self.assertIsNotNone(empty.next_available_at)

    def test_max_effort_only_by_pin_and_never_for_reviews(self) -> None:
        for spec in ("code", "code-hard", "frontier", "frontier+taste"):
            self.assertFalse([route for route in self.ids(self.select(spec)) if route.endswith("-max")], spec)
        self.assertEqual(self.ids(self.select("pin:claude-opus-max")), ["claude-opus-max"])
        with self.assertRaises(routing.RoutingError):
            self.select("pin:gpt-sol-max", gate=True)
        self.assertEqual(self.ids(self.select("pin:claude-opus-xhigh", gate=True)), ["claude-opus-xhigh"])

    def test_pin_only_routes_are_reached_only_by_a_pin(self) -> None:
        self.router.policy["pin_only_routes"] = ["claude-opus-high", "claude-opus-xhigh"]
        for spec in ("code-hard", "frontier", "frontier+taste"):
            chosen = self.ids(self.select(spec))
            self.assertFalse({"claude-opus-high", "claude-opus-xhigh"} & set(chosen), spec)
            self.assertIn("claude-opus-medium", chosen, spec)
        self.assertEqual(self.ids(self.select("pin:claude-opus-high")), ["claude-opus-high"])

    def test_free_last_resort_comes_after_paid_routes_and_not_for_reviews(self) -> None:
        free = [self.free("glm", 41.8), self.free("tiny", 30)]
        self.assertEqual(self.ids(self.select("code-hard", free))[-1:], ["glm"])
        self.assertEqual(self.ids(self.select("code-hard", free, unavailable=("claude", "gpt"))), ["glm"])
        self.assertEqual(self.select("code-hard", free, unavailable=("claude", "gpt"), free_last_resort=False).candidates, [])

    def test_undispatchable_route_is_skipped_with_reason(self) -> None:
        selection = self.select("research", [self.free("no-key", 45, dispatchable=False)])
        self.assertIn({"id": "no-key", "reason": "test"}, selection.skipped)

    def test_agy_routes_rank_as_free_and_share_quota_groups(self) -> None:
        router = routing.Router(machine_in(Path(tempfile.mkdtemp())))
        with mock.patch.object(routing.Router, "catalog_routes", return_value=[]), \
             mock.patch.object(routing.AvailabilityOracle, "family", lambda oracle, name: routing.Availability(True)):
            simple = [entry["id"] for entry in router.select("code-simple").candidates]
            research = [entry["id"] for entry in router.select("research").candidates]
        # Free first, smartest first: Grok 4.7 (46.4) ahead of Gemini Flash (40.9), both ahead of paid routes.
        self.assertEqual(simple[:2], ["xai-grok-4.7-xhigh", "xai-grok-4.7-high"])
        self.assertLess(simple.index("agy-gemini-flash-high"), simple.index("claude-haiku-xhigh"))
        key, _ = router.cooldown_for(router.find_route("xai-grok-4.6-high"), "rate_limited", "Too many requests")
        self.assertEqual(key, "provider:xai")
        spent = 'API Error: 402 {"code":"personal-team-blocked:spending-limit","error":"You have run out of credits"}'
        key, until = router.cooldown_for(router.find_route("xai-grok-4.7-high"), "api_error", spent)
        self.assertEqual(key, "provider:xai")
        self.assertGreater(until - routing.now_utc(), routing.timedelta(hours=23))
        refused = "hung while the proxy refused: All credentials are cooling down (last error: You have run out of credits or need a Grok subscription.)"
        key, until = router.cooldown_for(router.find_route("xai-grok-4.7-xhigh"), "rate_limited", refused)
        self.assertEqual(key, "provider:xai")
        self.assertGreater(until - routing.now_utc(), routing.timedelta(hours=23))
        # 39.8 is under code-simple's floor (40): a downgrade, after every qualified route.
        self.assertGreater(simple.index("agy-gemini-flash-medium"), simple.index("claude-haiku-xhigh"))
        self.assertIn("agy-claude-sonnet-4.6", research)
        self.assertNotIn("agy-claude-opus-4.6", simple)  # 31.9 is below code-simple's floors
        key, _ = router.cooldown_for(router.find_route("agy-gemini-flash-low"), "rate_limited", "RESOURCE_EXHAUSTED")
        self.assertEqual(key, "provider:agy-gemini")
        stated = "status ERROR, exit 3: Individual quota reached. Resets in 165h26m59s."
        self.assertEqual(routing.agy_quota_wait(stated), timedelta(hours=165, minutes=26, seconds=59))
        self.assertEqual(routing.agy_quota_wait("Resets in 45m."), timedelta(minutes=45))
        self.assertEqual(routing.agy_quota_wait("RESOURCE_EXHAUSTED"), routing.COOLDOWNS["agy_quota"])

    def test_policy_override(self) -> None:
        router = routing.Router(machine_in(Path(tempfile.mkdtemp())), {"ceilings": {"claude": {"weekly": 50}}, "routes_extra": [
            {"id": "extra", "family": "local", "harness": "command", "model": "x", "score": 60, "writes_code": True}]})
        self.assertEqual(router.policy["ceilings"]["claude"], {"five_hour": 85, "weekly": 50})
        self.assertIn("extra", [route.id for route in routing.static_routes(router.policy)])

    def test_cooldowns(self) -> None:
        route = routing.Route("openrouter/x:free", "free", "opencode", "openrouter/x:free", None, 40.0, True, "free", provider="openrouter")
        key, until = self.router.cooldown_for(route, "rate_limited", "Rate limit exceeded: free-models-per-day")
        self.assertEqual((key, until.hour, until.minute), ("provider:openrouter", 0, 0))
        key, until = self.router.cooldown_for(self.free("gone", 40), "api_error", "status 404: model not found")
        self.assertGreater(until - datetime.now(timezone.utc), timedelta(hours=23))
        # Zen's free usage limit covers every Zen model, so the provider cools down, not one route.
        key, _ = self.router.cooldown_for(self.free("glm", 41.8), "rate_limited", "AI_APICallError: Free usage limit reached. Subscribe")
        self.assertEqual(key, "provider:zen")

    def test_opencode_refusal_on_stderr_is_found(self) -> None:
        from wpo.session import _fatal_line
        stderr = Path(tempfile.mkdtemp()) / "x.stderr"
        stderr.write_bytes(b"level=WARN duplicate skill name\nlevel=ERROR error=\"AI_APICallError: Free usage limit reached. Subscribe\"\npartial")
        line, offset = _fatal_line(stderr, harness.OPENCODE_FATAL_STDERR, 0)
        self.assertIn("Free usage limit reached", line)
        self.assertEqual(offset, stderr.stat().st_size - len(b"partial"))
        self.assertEqual(_fatal_line(stderr, harness.OPENCODE_FATAL_STDERR, offset), (None, offset))

    def test_weekly_pace(self) -> None:
        now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
        week = 7 * 24 * 3600
        # One day into the week, a 50% budget with 5% slack allows 50 / 7 + 5 = 12.1%.
        window = {"window_seconds": week, "resets_at": (now + timedelta(days=6)).timestamp(), "used_percent": 20}
        ceiling, unblocks_at = routing.paced_weekly_ceiling(window, 92, {"budget_percent": 50, "slack_percent": 5}, now)
        self.assertAlmostEqual(ceiling, 50 / 7 + 5)
        # 20% is reached when 30% of the week has passed: 2.1 days from the start, 1.1 days from now.
        self.assertAlmostEqual((unblocks_at - now).total_seconds(), week * 0.3 - 24 * 3600, delta=1)
        spent = {**window, "used_percent": 55}
        self.assertEqual(routing.paced_weekly_ceiling(spent, 92, {"budget_percent": 50, "slack_percent": 5}, now)[1],
                         routing.from_timestamp(window["resets_at"]))

    def test_classify(self) -> None:
        ok = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "VERDICT: PASS"})
        self.assertEqual(routing.classify("claude", "noise\n" + ok, 0).result_text, "VERDICT: PASS")
        limited = json.dumps({"type": "result", "is_error": True, "api_error_status": 429, "result": "API Error: 429"})
        self.assertEqual(routing.classify("gpt", limited, 1).outcome, "rate_limited")
        self.assertEqual(routing.classify("claude", "Segmentation fault", 3).outcome, "session_error")
        texts = [{"type": "text", "part": {"messageID": "m1", "text": "thinking"}}, {"type": "text", "part": {"messageID": "m2", "text": "RESULT: "}},
                 {"type": "text", "part": {"messageID": "m2", "text": "review"}}]
        self.assertEqual(routing.classify("opencode", "\n".join(map(json.dumps, texts)), 0).result_text, "RESULT: review")


class TierRouteTests(unittest.TestCase):
    """Explicit per-tier route groups, quota headroom within a group, and routing modes."""

    ROUTING: ClassVar[dict] = {
        "routes_exclude": ["family:xai"],
        "tiers": {"review": {"floor": 54, "fallback_floor": 51, "writes_code": True, "use_for": "Reviews"}},
        "tier_routes": {
            "code-hard": {"routes": [["claude-opus-medium", "gpt-sol-high"]], "fallback": [["claude-opus-low", "gpt-sol-medium"]]},
            "review": {"routes": [["agy-gemini-flash-high"], ["gpt-sol-xhigh"], ["claude-opus-high"]]},
        },
        "modes": {"hard": {"tier_routes": {"code-hard": {"routes": [["claude-opus-high", "gpt-sol-xhigh"]], "free_last_resort": False}}}},
    }

    def router(self, routing_override: dict | None = None) -> routing.Router:
        machine = machine_in(Path(tempfile.mkdtemp()))
        machine.raw["routing"] = routing_override or self.ROUTING
        return routing.Router(machine)

    @staticmethod
    def windows(claude: float, gpt: float) -> dict:
        resets = (routing.now_utc() + timedelta(hours=2)).timestamp()
        return {family: [{"label": "5h", "used_percent": used, "resets_at": resets, "window_seconds": 18000}]
                for family, used in (("claude", claude), ("gpt", gpt))}

    def ranked(self, spec: str, claude: float = 10, gpt: float = 10, gate: bool = False, cheaper: str | None = None,
               free: list | None = None) -> list[tuple[str, bool]]:
        router = self.router()
        def state(router, windows, family, paced=True):
            return routing.now_utc(), None, cheaper if family == "claude" else None

        with mock.patch.object(routing.Router, "catalog_routes", return_value=free or []), \
             mock.patch.object(routing.Router, "usage_windows", return_value=self.windows(claude, gpt)), \
             mock.patch.object(routing.Router, "subscription_state", state):
            selection = router.select(spec, gate=gate, free_last_resort=not gate)
        return [(candidate["id"], candidate["downgrade"]) for candidate in selection.candidates]

    def test_the_family_with_more_headroom_goes_first_within_a_group(self) -> None:
        ranked = self.ranked("code-hard", claude=20, gpt=60)
        self.assertEqual(ranked[:4], [("claude-opus-medium", False), ("gpt-sol-high", False), ("claude-opus-low", True), ("gpt-sol-medium", True)])
        self.assertEqual({route for route, _ in ranked[4:]}, {"agy-gemini-flash-high", "agy-gemini-flash-medium"}, "then the free last resort")
        self.assertEqual([route for route, _ in self.ranked("code-hard", claude=60, gpt=20)][:2], ["gpt-sol-high", "claude-opus-medium"])
        # Headroom counts to each family's own ceiling: Claude's 5-hour one is 85, GPT's 92.
        self.assertEqual(self.ranked("code-hard", claude=10, gpt=15)[0], ("gpt-sol-high", False))

    def test_taste_puts_claude_first_whatever_the_headroom(self) -> None:
        self.assertEqual(self.ranked("code-hard+taste", claude=80, gpt=0)[0], ("claude-opus-medium", False))

    def test_groups_keep_their_order_and_only_listed_routes_run(self) -> None:
        self.assertEqual(self.ranked("review", claude=0, gpt=80, gate=True),
                         [("agy-gemini-flash-high", False), ("gpt-sol-xhigh", False), ("claude-opus-high", False)])

    def test_a_family_over_its_pace_serves_only_the_fallback(self) -> None:
        ranked = self.ranked("code-hard", claude=0, gpt=50, cheaper="weekly over pace")
        self.assertEqual(ranked[:3], [("gpt-sol-high", False), ("claude-opus-low", True), ("gpt-sol-medium", True)])

    def test_a_tier_without_a_list_still_ranks_by_floors(self) -> None:
        self.assertIn(("claude-opus-low", False), self.ranked("code"))

    def test_hard_mode_replaces_only_its_tiers_and_can_refuse_free_routes(self) -> None:
        free = [routing.Route(id="free-big", family="free", harness="opencode", model="m", effort=None, score=60, writes_code=True,
                              cost_class="free")]
        self.assertEqual(self.ranked("code-hard@hard", claude=10, gpt=50, free=free), [("claude-opus-high", False), ("gpt-sol-xhigh", False)])
        self.assertIn(("free-big", True), self.ranked("code-hard", free=free), "standard keeps the free last resort")
        self.assertEqual(self.ranked("review@hard", gate=True)[0], ("agy-gemini-flash-high", False), "no hard entry: the standard list")

    def test_a_mode_no_longer_configured_routes_as_standard(self) -> None:
        # A WP keeps the mode it first ran in; the configuration may drop that mode later.
        self.assertEqual(self.ranked("code-hard@turbo"), self.ranked("code-hard"))
        self.assertEqual(routing.resolve_work_spec("code-hard@turbo", self.router().policy).mode, "standard")

    def test_a_later_layer_replaces_a_tiers_entry_whole(self) -> None:
        machine = machine_in(Path(tempfile.mkdtemp()))
        machine.raw["routing"] = self.ROUTING
        router = routing.Router(machine, {"tier_routes": {"code-hard": {"routes": [["gpt-sol-high"]]}},
                                          "modes": {"hard": {"tier_routes": {"code-hard": {"routes": [["gpt-sol-xhigh"]]}}}}})
        self.assertEqual(router.tier_routes("code-hard"), {"routes": [["gpt-sol-high"]]}, "the machine's fallback does not survive")
        self.assertEqual(router.tier_routes("code-hard", "hard"), {"routes": [["gpt-sol-xhigh"]]})
        self.assertEqual(router.tier_routes("review"), self.ROUTING["tier_routes"]["review"], "other tiers keep the machine's entry")
        self.assertEqual(routing.Router(machine, {"tier_routes": {"code-hard": {}}}).tier_routes("code-hard"), {})

    def test_malformed_tier_lists_and_modes_fail_naming_the_setting(self) -> None:
        for override, message in (({"tier_routes": {"code-hard": {"routes": ["claude-opus-low"]}}}, r"tier_routes\.code-hard\.routes"),
                                  ({"tier_routes": {"code-hard": {"routes": [[]]}}}, r"tier_routes\.code-hard\.routes"),
                                  ({"tier_routes": {"code-hard": {"route": [["claude-opus-low"]]}}}, r"tier_routes\.code-hard must be"),
                                  ({"tier_routes": {"code-hard": {"free_last_resort": "no"}}}, "free_last_resort"),
                                  ({"tier_routes": {"nonsense": {}}}, "unknown tier"),
                                  ({"modes": {"a@b": {}}}, "cannot name a mode"),
                                  ({"modes": {"standard": {}}}, "cannot name a mode"),
                                  ({"modes": {"hard": {"routes": []}}}, r"modes\.hard must be"),
                                  ({"modes": {"hard": {"tier_routes": {"code-hard": {"fallback": "x"}}}}}, r"modes\.hard\.tier_routes\.code-hard\.fallback")):
            with self.subTest(override=override), self.assertRaisesRegex(routing.RoutingError, message):
                self.router({**self.ROUTING, **override})
        noted = self.router({**self.ROUTING, "tier_routes": {"$comment": "x", "code": {"$why": "x", "routes": [["claude-opus-low"]]}},
                             "modes": {"$comment": "x", "hard": {"$why": "x", "tier_routes": {}}}})
        self.assertEqual(routing.routing_modes(noted.policy), ["standard", "hard"], "$-keys are comments")
        with mock.patch.object(routing.Router, "load_catalog", return_value={"routes": []}):
            self.assertEqual(noted.unknown_listed_routes(), [])

    def test_the_supervisor_asks_for_the_wps_mode(self) -> None:
        from types import SimpleNamespace

        from wpo.supervisor import Supervisor
        runs = Path(tempfile.mkdtemp())
        supervisor = object.__new__(Supervisor)
        supervisor.project = SimpleNamespace(runs_dir=runs)
        supervisor.explicit_builder, supervisor.lane_builder, supervisor.escalation = None, None, lambda wp_id: None
        building = SimpleNamespace(id="WP-0001", status="todo", builder="code-hard", reviewer="review")
        reviewing = SimpleNamespace(id="WP-0002", status="review", builder="code", reviewer="review")
        routing.set_project_mode(runs, "hard")
        routing.record_wp_mode(runs, "WP-0002", "standard")
        self.assertEqual(supervisor.session_spec(building), "code-hard@hard")
        self.assertEqual(supervisor.session_spec(reviewing), "review", "a WP keeps the mode it started in")
        routing.record_wp_mode(runs, "WP-0003", "hard")
        self.assertEqual(supervisor.session_spec(SimpleNamespace(id="WP-0003", status="review", reviewer="pin:claude-opus-high")),
                         "pin:claude-opus-high", "pins carry no mode")

    def test_route_commands_outside_a_project(self) -> None:
        import contextlib
        import io
        from types import SimpleNamespace

        from wpo import cli
        from wpo.context import Context
        from wpo.machine import MachineConfig
        specs = []
        context = SimpleNamespace(router=SimpleNamespace(select=lambda spec, *arguments, **keywords: specs.append(spec) or routing.Selection(spec=spec, tier=None),
                                                         policy=self.router().policy), project=None, machine=None)
        with mock.patch.object(MachineConfig, "load", return_value=machine_in(Path(tempfile.mkdtemp()))), \
                mock.patch.object(Context, "load", return_value=context), mock.patch("wpo.dashboard.autostart"), mock.patch("sys.stdout"), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(cli.main(["route", "candidates", "--spec", "code-hard"]), 0)
            self.assertEqual(cli.main(["route", "mode"]), 2)
        self.assertEqual(specs, ["code-hard"], "no project, no mode")
        self.assertIn("belongs to a project", errors.getvalue())

    def test_route_mode_shows_sets_and_rejects_modes(self) -> None:
        import contextlib
        import io

        from wpo import cli
        project = DashboardProject()
        machine = machine_in(project.base)
        machine.raw["routing"] = self.ROUTING
        machine.raw["dashboard"] = {**machine.raw["dashboard"], "autostart": False}

        def run(*arguments: str) -> tuple[int, str]:
            output, errors = io.StringIO(), io.StringIO()
            with mock.patch("wpo.machine.MachineConfig.load", return_value=machine), contextlib.redirect_stdout(output), \
                    contextlib.redirect_stderr(errors), mock.patch("sys.stdout.reconfigure", create=True):
                code = cli.main(["--project", str(project.root), "route", "mode", *arguments])
            return code, output.getvalue() + errors.getvalue()

        self.assertEqual(run(), (0, "standard (modes: standard, hard)\n"))
        routing.set_project_mode(project.runs, "hard")
        with mock.patch("wpo.machine.MachineConfig.load", return_value=machine), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch("sys.stdout.reconfigure", create=True), \
                mock.patch.object(routing.Router, "select", return_value=routing.Selection(spec="", tier=None)) as select:
            self.assertEqual(cli.main(["--project", str(project.root), "route", "candidates", "--spec", "code-hard"]), 0)
        self.assertEqual(select.call_args.args[0], "code-hard@hard", "candidates rank as the project's next session would")
        routing.set_project_mode(project.runs, "standard")
        self.assertEqual(run("hard"), (0, "hard\n"))
        self.assertEqual(routing.project_mode(project.runs), "hard")
        code, text = run("turbo")
        self.assertEqual(code, 2)
        self.assertIn("unknown routing mode 'turbo'", text)
        routing.set_project_mode(project.runs, "gone")
        self.assertEqual(run(), (0, "gone (modes: standard, hard, not configured: routes as standard)\n"))

    def test_route_status_shows_how_reviews_rank_where_they_differ(self) -> None:
        import re
        router = self.router({**self.ROUTING, "gate_only_routes": ["agy-gemini-flash-high"]})
        def state(router, windows, family, paced=True):
            return routing.now_utc(), None, None

        catalog = {"refreshed_at": time.time(), "routes": [], "errors": [], "unrated": []}
        with mock.patch.object(routing.Router, "catalog_routes", return_value=[]), mock.patch.object(routing.Router, "load_catalog", return_value=catalog), \
             mock.patch.object(routing.Router, "usage_windows", return_value=self.windows(10, 50)), \
             mock.patch.object(routing.Router, "subscription_state", state):
            text = router.status_text()
        rows = {match.group(1): match for line in text.splitlines() if (match := re.match(r"  (\S.*?)\s{2,}(\S.*)$", line))}
        self.assertTrue(rows["review as a review"].group(2).startswith("agy-gemini-flash-high"), "reviews rank the gate-only route")
        self.assertNotIn("code-hard@hard as a review", rows, "hard code-hard ranks the same for reviews: no extra line")
        self.assertEqual(len({match.start(2) for match in rows.values()}), 1, "every row's choices start in one column")

    def test_route_candidates_gate_ranks_as_a_review(self) -> None:
        from types import SimpleNamespace

        from wpo import cli
        from wpo.context import Context
        from wpo.machine import MachineConfig
        calls = []

        def select(spec, families=None, force_refresh=False, **keywords):
            calls.append(keywords)
            return routing.Selection(spec=spec, tier="review")

        context = SimpleNamespace(router=SimpleNamespace(select=select), project=None, machine=None)
        with mock.patch.object(MachineConfig, "load", return_value=machine_in(Path(tempfile.mkdtemp()))), \
             mock.patch.object(Context, "load", return_value=context), mock.patch("wpo.dashboard.autostart"), mock.patch("sys.stdout"):
            cli.main(["route", "candidates", "--spec", "review", "--gate"])
            cli.main(["route", "candidates", "--spec", "review"])
        self.assertEqual(calls, [{"free_last_resort": False, "gate": True}, {"free_last_resort": True, "gate": False}])

    def test_unknown_listed_routes_are_reported(self) -> None:
        router = self.router({**self.ROUTING, "tier_routes": {"code": {"routes": [["claude-opus-low", "gpt-typo", "free-listed"]]}}})
        with mock.patch.object(routing.Router, "load_catalog", return_value={"routes": [{"id": "free-listed"}]}):
            self.assertEqual(router.unknown_listed_routes(), ["gpt-typo"], "catalog routes are known")

    def test_a_wp_keeps_the_mode_it_first_ran_in(self) -> None:
        runs = Path(tempfile.mkdtemp())
        self.assertEqual(routing.project_mode(runs), "standard")
        routing.set_project_mode(runs, "hard")
        self.assertEqual(routing.wp_mode(runs, "WP-0001"), "hard", "an unstarted WP follows the project")
        self.assertFalse((runs / "WP-0001" / routing.MODE_FILE).exists(), "and records nothing")
        routing.record_wp_mode(runs, "WP-0001", "hard")
        routing.set_project_mode(runs, "standard")
        self.assertEqual(routing.wp_mode(runs, "WP-0001"), "hard", "a started WP keeps its mode")
        routing.record_wp_mode(runs, "WP-0001", "standard")
        self.assertEqual(routing.wp_mode(runs, "WP-0001"), "hard", "and a second record changes nothing")
        self.assertEqual(routing.wp_mode(runs, "WP-0002"), "standard")

    def runner_for(self, runs: Path):
        from types import SimpleNamespace

        from wpo import runner
        run = object.__new__(runner.WorkPackageRun)
        run.context = SimpleNamespace(project=SimpleNamespace(runs_dir=runs))
        run.wp_id, run.run_dir, run.tree, run.families, run.env, run.counting_round = "WP-0001", runs / "WP-0001", runs, (), {}, False
        run.run_dir.mkdir(exist_ok=True)
        run.options = SimpleNamespace(idle_minutes=1)
        run.ledger = SimpleNamespace(writing=None, state={}, save=lambda: None)
        run.wp, run.log = (lambda: SimpleNamespace(owns=["src/**"], skills=[])), (lambda message: None)
        return run

    def test_the_runner_routes_every_role_in_the_mode_and_records_it_once_a_route_is_chosen(self) -> None:
        from types import SimpleNamespace

        from wpo import runner
        from wpo.session import NoRouteError
        runs = Path(tempfile.mkdtemp())
        run = self.runner_for(runs)
        specs = []

        def session(context, spec, *arguments, on_attempt=None, **keywords):
            specs.append(spec)
            if len(specs) == 1:
                raise NoRouteError("no route", None, [])  # no route was chosen, so nothing started
            on_attempt()
            routing.set_project_mode(runs, "standard")  # a switch while the session runs
            return SimpleNamespace(route={"id": "r", "family": "claude"}, fallback_note="")

        routing.set_project_mode(runs, "hard")
        with mock.patch.object(runner, "run_session", session), mock.patch.object(runner, "pause_requested", return_value=None), \
             mock.patch.object(runner, "inbox_of", return_value=None), mock.patch.object(runner.wpfile, "set_status"):
            with self.assertRaises(runner.RunStopped):
                run.session("code-hard", "planner", "p", run.run_dir / "plan", "todo", access="docs", write_paths=["x"])
            self.assertFalse((run.run_dir / routing.MODE_FILE).exists(), "no route chosen: nothing recorded")
            run.session("code-simple", "executor", "p", run.run_dir / "task", "todo")
            self.assertEqual(routing.wp_mode(runs, "WP-0001"), "hard", "recorded when the route was chosen, kept after the switch")
            run.session("code", "reviewer", "p", run.run_dir / "review", "review")
        self.assertEqual(specs, ["code-hard@hard", "code-simple@hard", "code@hard"], "staged and later roles route in the WP's mode")

    def test_a_spec_that_names_its_mode_records_that_mode(self) -> None:
        from types import SimpleNamespace

        from wpo import runner
        runs = Path(tempfile.mkdtemp())
        run = self.runner_for(runs)
        def session(context, spec, *arguments, on_attempt=None, **keywords):
            on_attempt()
            return SimpleNamespace(route={"id": "r", "family": "claude"}, fallback_note="")
        with mock.patch.object(runner, "run_session", session), mock.patch.object(runner, "pause_requested", return_value=None), \
             mock.patch.object(runner, "inbox_of", return_value=None):
            run.session("code-hard@hard", "builder", "p", run.run_dir / "build", "todo")
        self.assertEqual(routing.wp_mode(runs, "WP-0001"), "hard")

    def test_repairs_route_in_the_wp_mode(self) -> None:
        from types import SimpleNamespace

        from wpo import runner
        runs = Path(tempfile.mkdtemp())
        run = self.runner_for(runs)
        run.config = {"repair_spec": "code-simple"}
        run.emit = lambda *arguments: None
        specs = []
        def session(context, spec, *arguments, on_attempt=None, **keywords):
            specs.append(spec)
            on_attempt()
            return SimpleNamespace(route={"id": "r", "family": "claude"}, fallback_note="")
        routing.set_project_mode(runs, "hard")
        with mock.patch.object(runner, "run_session", session), mock.patch.object(runner, "repair_prompt", return_value="p"), \
             mock.patch.object(runner, "builder_skills", return_value=[]):
            self.assertTrue(run.repair(1, [SimpleNamespace(command="cargo fmt")]))
        self.assertEqual(specs, ["code-simple@hard"])
        self.assertEqual(routing.wp_mode(runs, "WP-0001"), "hard")

    def test_a_window_past_its_reset_counts_as_unused(self) -> None:
        router = self.router()
        past = (routing.now_utc() - timedelta(seconds=1)).timestamp()
        windows = {"gpt": [{"label": "5h", "used_percent": 100, "resets_at": past, "window_seconds": 18000}],
                   "claude": self.windows(70, 0)["claude"]}
        with mock.patch.object(routing.Router, "usage_windows", return_value=windows):
            oracle = router.oracle()
            self.assertEqual(oracle.headroom("gpt"), 92)
            self.assertEqual(oracle.headroom("claude"), 15)

    def test_an_empty_entry_lists_nothing_rather_than_falling_back_to_floors(self) -> None:
        router = self.router({**self.ROUTING, "tier_routes": {"code-hard": {}}})
        with mock.patch.object(routing.Router, "catalog_routes", return_value=[]), \
             mock.patch.object(routing.Router, "usage_windows", return_value=self.windows(0, 0)):
            ranked = [candidate["id"] for candidate in router.select("code-hard").candidates]
        self.assertFalse([route for route in ranked if not route.startswith("agy-")], "only the free last resort")

    def test_with_mode_marks_tiers_only(self) -> None:
        self.assertEqual(routing.with_mode("code-hard", "hard"), "code-hard@hard")
        self.assertEqual(routing.with_mode("code-hard", "standard"), "code-hard")
        for spec in ("none", "pin:claude-opus-high", "code@hard"):
            self.assertEqual(routing.with_mode(spec, "hard"), spec)


class QuotaGroupTests(unittest.TestCase):
    """Quota groups wait for their buckets to reset once a bucket passes its ceiling."""

    def router(self, machine_routing: dict, usage: dict | None = None) -> routing.Router:
        directory = Path(tempfile.mkdtemp())
        machine = machine_in(directory)
        machine.raw["routing"] = machine_routing
        if usage is not None:
            cache = directory / "usage.json"
            cache.write_text(json.dumps(usage), encoding="utf-8")
            machine.raw["usage"] = {**machine.raw["usage"], "cache": str(cache), "max_age_seconds": 10**9}
        return routing.Router(machine)

    def agy_usage(self, used: dict[str, float], resets_in_hours: float = 2.0) -> dict:
        resets = (routing.now_utc() + timedelta(hours=resets_in_hours)).timestamp()
        windows = [{"family": "agy", "label": label, "used_percent": percent, "resets_at": resets,
                    "window_seconds": 18000 if label.endswith("5h") else 7 * 86400} for label, percent in used.items()]
        return {"fetched_at": time.time(), "families": [{"family": "agy", "plan": None, "windows": windows, "notes": [], "error": None,
                                                       "fetched_at": time.time()}]}

    def test_a_quota_group_waits_until_its_buckets_reset(self) -> None:
        quota = {"groups": {"agy-gemini": {"family": "agy", "buckets": ["gemini-5h", "gemini-weekly"]}}, "ceilings": {"five_hour": 70, "weekly": 85}}
        flash = next(route for route in routing.static_routes(routing.load_policy()) if route.id == "agy-gemini-flash-high")
        cases = [
            ("under both ceilings", {"gemini-5h": 69.9, "gemini-weekly": 84.0}, True),
            ("5-hour bucket at its ceiling", {"gemini-5h": 70.0, "gemini-weekly": 10.0}, False),
            ("weekly bucket past its ceiling", {"gemini-5h": 0.0, "gemini-weekly": 90.0}, False),
            ("another group's bucket", {"3p-5h": 100.0}, True),
        ]
        for name, used, available in cases:
            router = self.router({"quota_groups": quota}, usage=self.agy_usage(used))
            availability = router.oracle().route(flash)
            self.assertEqual(availability.available, available, name)
            if not available:
                self.assertGreater(availability.available_at, routing.now_utc(), name)
                self.assertIn("agy gemini-", availability.reason, name)
        unknown = self.router({"quota_groups": quota})
        self.assertTrue(unknown.oracle().route(flash).available, "unknown usage blocks nothing")
        spent = self.agy_usage({"gemini-5h": 100.0})
        argon = dataclasses.replace(flash, id="agy-argon", provider="agy-argon")
        self.assertTrue(self.router({"quota_groups": quota}, usage=spent).oracle().route(argon).available, "a provider without a group")

    def test_any_family_can_hold_a_quota_group(self) -> None:
        quota = {"groups": {"team-key": {"family": "gpt", "buckets": ["5h"]}}, "ceilings": {"five_hour": 50, "weekly": 90}}
        resets = (routing.now_utc() + timedelta(hours=1)).timestamp()
        usage = {"fetched_at": time.time(), "families": [{"family": "gpt", "error": None,
                                                          "windows": [{"label": "5h", "used_percent": 60, "resets_at": resets, "window_seconds": 18000}]}]}
        sol = next(route for route in routing.static_routes(routing.load_policy()) if route.id == "gpt-sol-high")
        router = self.router({"quota_groups": quota}, usage=usage)
        with mock.patch.object(routing.AvailabilityOracle, "family", return_value=routing.Availability(True)):
            self.assertTrue(router.oracle().route(sol).available, "routes outside the group are not held")
            availability = router.oracle().route(dataclasses.replace(sol, provider="team-key"))
        self.assertFalse(availability.available)
        self.assertIn("gpt 5h 60% >= 50%", availability.reason)

    def test_malformed_quota_groups_fail_naming_the_setting(self) -> None:
        for settings, message in (({"groups": {"agy-gemini": ["gemini-5h"]}, "ceilings": {"five_hour": 70, "weekly": 85}}, r"groups\.agy-gemini"),
                                  ({"groups": {"agy-gemini": {"family": "agy", "buckets": []}}, "ceilings": {"five_hour": 70, "weekly": 85}}, r"groups\.agy-gemini"),
                                  ({"groups": {"agy-gemini": {"family": "agy", "buckets": ["gemini-5h"]}}}, "ceilings"),
                                  ({"groups": {"agy-gemini": {"family": "agy", "buckets": ["gemini-5h"]}}, "ceilings": {"five_hour": 70}}, "ceilings"),
                                  ({"group": {}}, "quota_groups must be")):
            with self.subTest(settings=settings), self.assertRaisesRegex(routing.RoutingError, message):
                self.router({"quota_groups": settings})
        self.router({"quota_groups": {"$comment": "x", "groups": {"agy-gemini": {"$why": "x", "family": "agy", "buckets": ["gemini-5h"]}},
                                      "ceilings": {"five_hour": 70, "weekly": 85}}})  # $-keys are comments

    def windows(self, buckets: dict[str, tuple[float, float]]) -> dict:
        """Buckets as {label: (used percent, hours until reset)}."""
        now = routing.now_utc()
        rows = [{"family": "agy", "label": label, "used_percent": used, "resets_at": (now + timedelta(hours=hours)).timestamp(),
                 "window_seconds": 18000 if label.endswith("5h") else 7 * 86400} for label, (used, hours) in buckets.items()]
        return {"fetched_at": time.time(), "families": [{"family": "agy", "plan": None, "windows": rows, "notes": [], "error": None,
                                                       "fetched_at": time.time()}]}

    def test_a_reset_bucket_unblocks_and_the_latest_reset_wins(self) -> None:
        quota = {"groups": {"agy-gemini": {"family": "agy", "buckets": ["gemini-5h", "gemini-weekly"]}}, "ceilings": {"five_hour": 70, "weekly": 85}}
        flash = next(route for route in routing.static_routes(routing.load_policy()) if route.id == "agy-gemini-flash-high")
        reset = self.router({"quota_groups": quota}, usage=self.windows({"gemini-5h": (95.0, -0.1), "gemini-weekly": (10.0, 50)}))
        self.assertTrue(reset.oracle().route(flash).available, "a reading from before its reset blocks nothing")
        both = self.router({"quota_groups": quota}, usage=self.windows({"gemini-5h": (95.0, 2), "gemini-weekly": (90.0, 50)}))
        availability = both.oracle().route(flash)
        self.assertFalse(availability.available)
        self.assertGreater(availability.available_at, routing.now_utc() + timedelta(hours=49), "waits for the later reset")

    def test_t3_routes_are_held_once_their_native_target_checks_out(self) -> None:
        from types import SimpleNamespace

        from wpo import t3
        quota = {"groups": {"agy-gemini": {"family": "agy", "buckets": ["gemini-5h", "gemini-weekly"]}}, "ceilings": {"five_hour": 70, "weekly": 85}}
        router = self.router({"quota_groups": quota}, usage=self.windows({"gemini-5h": (95.0, 2)}))
        router.machine.raw["execution_mode"] = "t3"
        router.project = SimpleNamespace(runs_dir=Path(tempfile.mkdtemp()))
        flash = next(route for route in routing.static_routes(routing.load_policy()) if route.id == "agy-gemini-flash-high")
        with mock.patch.object(t3.Bridge, "target", return_value={}) as target:
            oracle = router.oracle()
            availability = oracle.route(flash)
            oracle.route(flash)
        self.assertFalse(availability.available)
        self.assertIn("agy gemini-5h", availability.reason)
        self.assertEqual(target.call_count, 1, "one oracle checks a route's native target once")


class EventTests(unittest.TestCase):
    def test_emit_read_cursor_and_quiet_wait(self) -> None:
        journal = Journal(Path(tempfile.mkdtemp()))
        journal.emit("wp-done", "a", {"wp": "WP-1"})
        journal.emit("no-route", "b")
        self.assertEqual(journal.cursor(), 2)
        self.assertEqual([event["kind"] for event in journal.read(0)], ["wp-done", "no-route"])
        with mock.patch("builtins.print"):
            self.assertEqual(journal.wait(0, 0, quiet_kinds=["no-route"]), 3)
            self.assertEqual(journal.wait(0, 0), 0)
        with self.assertRaises(UnknownEventKindError):
            journal.emit("made-up", "x")

    def test_line_separators_and_partial_lines_do_not_break_reading(self) -> None:
        journal = Journal(Path(tempfile.mkdtemp()))
        journal.emit("wp-done", "aâ€¨bâ€©c\x85d")
        with journal.path.open("a", encoding="utf-8") as handle:
            handle.write('{"time": "half-writ')
        self.assertEqual(journal.cursor(), 1)
        self.assertEqual(journal.read(0)[0]["message"], "aâ€¨bâ€©c\x85d")


class RoundLedgerTests(unittest.TestCase):
    def test_a_round_interrupted_mid_build_is_not_counted(self) -> None:
        from wpo.runner import RoundLedger
        path = Path(tempfile.mkdtemp()) / "attempts.json"
        ledger = RoundLedger(path)
        ledger.count()
        ledger.close()
        ledger.count()  # this runner is then stopped mid-build
        restarted = RoundLedger(path)
        self.assertTrue(restarted.interrupted)
        self.assertEqual(restarted.rounds, 1)
        self.assertFalse(RoundLedger(path).interrupted)

    def test_failures_force_evaluation_and_reset_after_it(self) -> None:
        from wpo.runner import RoundLedger
        ledger = RoundLedger(Path(tempfile.mkdtemp()) / "attempts.json")
        ledger.count()
        ledger.fail("review requested changes", "fix x")
        self.assertEqual(ledger.failures, 1)
        ledger.fail("builder blocked", "blocked on y", force_evaluation_after=5)
        self.assertEqual(ledger.failures, 5)
        self.assertEqual([entry["kind"] for entry in ledger.history], ["review requested changes", "builder blocked"])
        ledger.evaluated("escalate", "guidance", "frontier")
        self.assertEqual((ledger.failures, ledger.evaluations, ledger.escalated_to), (0, 1, "frontier"))
        self.assertEqual(RoundLedger.escalation(ledger.path), "frontier")
        ledger.fail("verification failed", "fix z")
        self.assertEqual(ledger.builder_feedback, "fix z\n\nguidance")
        ledger.unblocked_start(clear_evaluations=True)
        self.assertEqual((ledger.failures, ledger.evaluations), (0, 0))

    def test_decision_line_tolerates_markdown(self) -> None:
        from wpo.runner import DECISION, DECISION_ARGUMENT_TRIM
        found = DECISION.findall("Reasoning.\n**DECISION: escalate `frontier`**")
        self.assertEqual((found[-1][0], found[-1][1].strip(DECISION_ARGUMENT_TRIM)), ("escalate", "frontier"))
        self.assertEqual(DECISION.findall("no decision here"), [])

    def test_reviews_leave_out_the_last_builders_model_family_only_when_independent(self) -> None:
        from types import SimpleNamespace

        from wpo.runner import RoundLedger, WorkPackageRun
        ledger = RoundLedger(Path(tempfile.mkdtemp()) / "attempts.json")
        policy: dict = {}
        logged: list[str] = []
        runner = SimpleNamespace(ledger=ledger, context=SimpleNamespace(router=SimpleNamespace(policy=policy)), log=logged.append)
        self.assertEqual(WorkPackageRun.independent_review_exclusion(runner), [])
        agy_opus = {"id": "agy-opus", "family": "agy", "model_family": "claude"}
        sol = {"id": "sol", "family": "gpt", "model_family": None}
        for role, route, family in (("builder", agy_opus, "claude"), ("executor", sol, "gpt"), ("assembler", agy_opus, "claude"),
                                    ("planner", sol, "claude"), ("reviewer", sol, "claude"), ("builder", sol, "gpt")):
            WorkPackageRun.record_writer(runner, role, route)
            self.assertEqual(RoundLedger(ledger.path).state["builder_model_family"], family, role)
        self.assertEqual(WorkPackageRun.independent_review_exclusion(runner), [])
        policy["independent_reviews"] = True
        self.assertEqual(WorkPackageRun.independent_review_exclusion(runner), ["gpt"])
        self.assertIn("gpt", logged[-1])
        ledger.reset()
        self.assertEqual(WorkPackageRun.independent_review_exclusion(runner), [])
        self.assertIn("not recorded", logged[-1])
        # A ledger written before the family was recorded: its last builder session's route tells.
        runner.context.router.find_route = lambda route_id: routing.Route(route_id, "claude", "claude", "opus", "high", 54, True, "subscription")
        ledger.record_builder({"route": "claude-opus-high", "harness": "claude", "session_id": "s", "context_tokens": 1, "head": "h", "spec": "code"})
        self.assertEqual(WorkPackageRun.independent_review_exclusion(runner), ["claude"])

    def test_staged_and_paused_sessions_record_who_built_the_wp(self) -> None:
        from types import SimpleNamespace

        from wpo import runner as runner_module
        from wpo.runner import RoundLedger, RunPaused, WorkPackageRun
        from wpo.session import CompactionStop
        directory = Path(tempfile.mkdtemp())
        ledger = RoundLedger(directory / "attempts.json")
        fake = SimpleNamespace(ledger=ledger, run_dir=directory, tree=directory, families=[], options=SimpleNamespace(idle_minutes=None),
                               wp_id="WP-0001", env={}, context=None, log=lambda message: None, mode="standard", mode_recorder=lambda mode: None)
        fake.record_writer = lambda role, route: WorkPackageRun.record_writer(fake, role, route)
        sol = {"id": "sol", "family": "gpt", "model_family": None}
        with mock.patch.object(runner_module, "run_session", return_value=SimpleNamespace(route=sol, fallback_note="")), \
                mock.patch.object(runner_module, "inbox_of", return_value=None), \
                mock.patch.object(runner_module, "pause_requested", side_effect=[None, None, None, "paused"]):
            WorkPackageRun.session(fake, "code", "executor", "prompt", directory / "executor", "todo")
            self.assertEqual(RoundLedger(ledger.path).state["builder_model_family"], "gpt")
            ledger.state["builder_model_family"] = None
            with self.assertRaises(RunPaused):
                WorkPackageRun.session(fake, "code", "assembler", "prompt", directory / "assembler", "todo")
        self.assertEqual(RoundLedger(ledger.path).state["builder_model_family"], "gpt")
        # An assembler stopped at compaction may have finished the WP: it is still the latest writer.
        fake.context = SimpleNamespace(router=SimpleNamespace(find_route=lambda route_id: routing.Route(
            route_id, "agy", "agy", "opus", "high", 54, True, "free", model_family="claude")))
        with mock.patch.object(runner_module, "run_session", side_effect=CompactionStop("agy-opus", directory / "log")), \
                mock.patch.object(runner_module, "inbox_of", return_value=None), \
                mock.patch.object(runner_module, "pause_requested", return_value=None), self.assertRaises(CompactionStop):
            WorkPackageRun.session(fake, "code", "assembler", "prompt", directory / "assembler", "todo")
        self.assertEqual(RoundLedger(ledger.path).state["builder_model_family"], "claude")

    def test_attempt_ledgers_migrate(self) -> None:
        from wpo.runner import RoundLedger
        path = Path(tempfile.mkdtemp()) / "attempts.json"
        path.write_text(json.dumps({"attempts": 3, "feedback": "Reviewer findings", "open": False}))
        ledger = RoundLedger(path)
        self.assertEqual((ledger.rounds, ledger.failures, ledger.feedback), (3, 3, "Reviewer findings"))


class EfficiencyReviewTests(unittest.TestCase):
    """The efficiency review's fixes (2026-10-26): interrupted sessions, machine failures, build
    slots, family cooldowns after a hang, lane caps and WP lint."""

    def test_an_interrupted_session_is_not_a_failed_round_and_is_continued(self) -> None:
        from wpo.runner import RESUMABLE_FAILURES, RoundLedger
        ledger = RoundLedger(Path(tempfile.mkdtemp()) / "attempts.json")
        ledger.count()
        ledger.interrupted_round("cut off")
        ledger.close()
        self.assertEqual((ledger.rounds, ledger.failures, ledger.interruptions), (0, 0, 1))
        self.assertIn(ledger.history[-1]["kind"], RESUMABLE_FAILURES)
        ledger.count()
        ledger.fail("verification failed", "fix it")
        self.assertEqual((ledger.rounds, ledger.failures, ledger.interruptions), (1, 1, 0))

    def test_only_failures_that_all_carry_a_machine_marker_are_infrastructure(self) -> None:
        from wpo.commands import CommandFailure, infrastructure_failure
        from wpo.project import DEFAULTS as PROJECT_DEFAULTS
        markers = PROJECT_DEFAULTS["infrastructure_failure_markers"]
        killed = CommandFailure("cargo test", -1, "running...\n(killed after 120 min)")
        dll = CommandFailure("cargo clippy", 1, "process didn't exit successfully (exit code: 0xc0000142, STATUS_DLL_INIT_FAILED)")
        real = CommandFailure("cargo test", 101, "test foo ... FAILED\nassertion failed")
        self.assertTrue(infrastructure_failure([killed, dll], markers))
        self.assertFalse(infrastructure_failure([killed, real], markers))
        self.assertFalse(infrastructure_failure([], markers))

    def test_build_slots_bound_concurrent_holders(self) -> None:
        import threading
        from wpo.commands import build_slot
        if sys.platform != "win32":
            self.skipTest("named mutexes are per process off Windows; covered by the Windows run")
        # The live lanes' verifications hold the real slots; this test has its own.
        isolated = mock.patch("wpo.commands.BUILD_SLOT_MUTEX_PREFIX", f"wpo-test-build-slot-{os.getpid()}-")
        isolated.start()
        self.addCleanup(isolated.stop)
        entered = threading.Event()
        release = threading.Event()
        messages: list[str] = []

        def hold() -> None:
            with build_slot(1, None, messages.append):
                entered.set()
                release.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(entered.wait(10))
        waiter_done = threading.Event()
        beat = Path(tempfile.mkdtemp()) / "heartbeat"

        def wait_for_slot() -> None:
            with mock.patch("wpo.commands.BUILD_SLOT_POLL_SECONDS", 0.05), build_slot(1, beat, messages.append):
                waiter_done.set()

        waiter = threading.Thread(target=wait_for_slot)
        waiter.start()
        self.assertFalse(waiter_done.wait(0.3))
        self.assertTrue(beat.exists())
        release.set()
        holder.join(10)
        waiter.join(10)
        self.assertTrue(waiter_done.is_set())
        self.assertIn("all 1 build slots are busy; waiting for one", messages)
        with build_slot(0, None, messages.append):
            pass

    def test_a_hung_subscription_session_cools_its_whole_family(self) -> None:
        router = routing.Router(machine_in(Path(tempfile.mkdtemp())))
        with mock.patch.object(routing.Router, "catalog_routes", return_value=[]):
            key, _ = router.cooldown_for(router.find_route("gpt-sol-high"), "hang", "no activity")
            self.assertEqual(key, "family:gpt")
            key, _ = router.cooldown_for(router.find_route("agy-gemini-flash-high"), "hang", "no activity")
            self.assertEqual(key, "route:agy-gemini-flash-high")

    def test_lanes_nearest_to_merging_rank_first(self) -> None:
        from wpo.lanes import merge_distance
        def wp(status: str) -> wpfile.WorkPackage:
            return wpfile.WorkPackage(Path("x.md"), {"id": "WP-X", "title": "x", "milestone": "M1", "status": status, "depends_on": [],
                                                     "builder": "code", "reviewer": "code", "owns": []})
        self.assertEqual(merge_distance([wp("todo"), wp("review")]), 0)
        self.assertEqual(merge_distance([wp("changes_requested")]), 1)
        self.assertEqual(merge_distance([wp("todo")]), 2)

    def test_owns_patterns_overlap(self) -> None:
        from wpo.lint import patterns_overlap
        self.assertTrue(patterns_overlap("crates/zm_ai/**", "crates/zm_ai/src/zombie.rs"))
        self.assertTrue(patterns_overlap("crates/zm_ai/src/zombie.rs", "crates/zm_ai/**"))
        self.assertTrue(patterns_overlap("crates/zm_customize/examples/**", "crates/zm_customize/examples/review/**"))
        self.assertFalse(patterns_overlap("crates/zm_ai/**", "crates/zm_audio/**"))
        self.assertFalse(patterns_overlap("crates/a/lib.rs", "crates/a/mod.rs"))


class PoolCandidateTests(unittest.TestCase):
    """Which WPs a pool lane may build: never the last blocker of an open WP, unless its own lane
    is sleeping for a route."""

    def setUp(self) -> None:
        from types import SimpleNamespace
        from wpo.supervisor import Supervisor
        runs = Path(tempfile.mkdtemp())
        project = SimpleNamespace(runs_dir=runs)
        self.runs = runs
        self.pool = SimpleNamespace(context=SimpleNamespace(project=project), project=project, handed_back=set(), escalation=lambda wp_id: None)
        self.pool.waiting_for_route = lambda wp_id: Supervisor.waiting_for_route(self.pool, wp_id)
        self.candidates = lambda wps: [wp.id for wp in Supervisor.pool_candidates(self.pool, wps, set())]

    def wp(self, wp_id: str, depends_on: list[str] = ()) -> wpfile.WorkPackage:
        return wpfile.WorkPackage(Path(f"{wp_id}.md"), {"id": wp_id, "title": wp_id, "milestone": "M1", "status": "todo",
                                                       "depends_on": list(depends_on), "builder": "code", "reviewer": "code",
                                                       "owns": [], "lane": "lane"})

    def test_leaves_first_and_sole_blockers_excluded(self) -> None:
        wps = [self.wp("WP-A"), self.wp("WP-B"), self.wp("WP-C", ["WP-A"]), self.wp("WP-D", ["WP-B", "WP-X"]), self.wp("WP-X", ["WP-Q"])]
        # WP-A alone holds up WP-C; WP-B shares WP-D with the unmerged WP-X.
        self.assertEqual(self.candidates(wps), ["WP-B"])

    def test_route_wait_lifts_the_sole_blocker_exception(self) -> None:
        (self.runs / "WP-A").mkdir()
        (self.runs / "WP-A" / "route-wait").write_text((datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        self.assertEqual(self.candidates([self.wp("WP-A"), self.wp("WP-C", ["WP-A"])]), ["WP-A"])
        (self.runs / "WP-A" / "route-wait").write_text((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
        self.assertEqual(self.candidates([self.wp("WP-A"), self.wp("WP-C", ["WP-A"])]), [])


class ResumeTests(unittest.TestCase):
    """`wpo lanes resume` targets, and lanes whose WPs only a pool lane builds."""

    def setUp(self) -> None:
        from types import SimpleNamespace
        lanes = {"local": {"pool": True}, "hygiene": {"builds_in_pool": True}, "plain": {}}
        project = SimpleNamespace(runs_dir=Path(tempfile.mkdtemp()), config={"lanes": lanes}, lane=lambda name: lanes.get(name, {}),
                                  mutex_name=lambda kind, wp_id: f"wpo-test-resume-{os.getpid()}-{kind}-{wp_id}")
        self.wps = [self.wp("WP-A", "hygiene", "todo"), self.wp("WP-B", "hygiene", "review"), self.wp("WP-C", "plain", "todo"),
                    self.wp("WP-D", "", "todo")]
        self.context = SimpleNamespace(project=project, effective_wps=lambda: self.wps)

    def wp(self, wp_id: str, lane: str, status: str) -> wpfile.WorkPackage:
        return wpfile.WorkPackage(Path(f"{wp_id}.md"), {"id": wp_id, "title": wp_id, "milestone": "M1", "status": status,
                                                       "depends_on": [], "builder": "code", "reviewer": "code", "owns": [], "lane": lane})

    def outcome(self, lane: str, wp_ids: list[str]) -> list[str] | None:
        from wpo.lanes import lane_outcome
        return lane_outcome(self.context, lane, [wp for wp in self.wps if wp.id in wp_ids], set(), {})[1]

    def test_a_pool_built_lane_starts_only_to_review(self) -> None:
        self.assertIsNone(self.outcome("hygiene", ["WP-A"]))
        self.assertEqual(self.outcome("hygiene", ["WP-A", "WP-B"]), ["WP-A", "WP-B"])
        self.assertEqual(self.outcome("plain", ["WP-C"]), ["WP-C"])

    def test_the_supervisor_of_a_pool_built_lane_skips_builds(self) -> None:
        from types import SimpleNamespace
        from wpo.supervisor import Supervisor
        lane = SimpleNamespace(context=SimpleNamespace(effective_wps=lambda: self.wps, merged_done_ids=set, parse_errors={}),
                               project=self.context.project, name="hygiene", is_pool=False, is_lane=True, wp_ids=[], finished={},
                               builds_in_pool=True, report_parse_errors=lambda: None,
                               claim=lambda wp_id: SimpleNamespace(release=lambda: None))
        lane.selected = lambda wp: Supervisor.selected(lane, wp)
        with mock.patch("wpo.lanes.read_holds", return_value={}):
            self.assertEqual(Supervisor.next_wp(lane)[0].id, "WP-B")

    def test_targets_name_lanes_or_the_lanes_of_wps(self) -> None:
        from wpo.lanes import resume_targets
        self.assertEqual(resume_targets(self.context, ["WP-A", "plain", "local"]), {"hygiene", "plain", "local"})
        with self.assertRaises(SystemExit):
            resume_targets(self.context, ["WP-D"])
        with self.assertRaises(SystemExit):
            resume_targets(self.context, ["nowhere"])


class CatchUpWithMainTests(unittest.TestCase):
    """An existing worktree takes main in before its WP's sessions, without touching uncommitted work."""

    def setUp(self) -> None:
        from types import SimpleNamespace
        from wpo.project import git
        self.git = git
        root = Path(tempfile.mkdtemp())
        self.main_tree, self.worktree = root / "main", root / "wt"
        self.main_tree.mkdir()
        for arguments in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            git(self.main_tree, *arguments)
        self.write(self.main_tree, "shared.txt", "base\n")
        self.write(self.main_tree, "edited.txt", "base\n")
        self.commit(self.main_tree, "base")
        git(self.main_tree, "worktree", "add", "-q", "-b", "wp/WP-X", str(self.worktree), "main")
        self.logs: list[str] = []
        self.supervisor = SimpleNamespace(project=SimpleNamespace(main_branch="main"), log=self.logs.append)

    def write(self, tree: Path, name: str, text: str) -> None:
        (tree / name).write_text(text, encoding="utf-8")

    def commit(self, tree: Path, message: str) -> None:
        self.git(tree, "add", "-A")
        self.git(tree, "commit", "-q", "-m", message)

    def catch_up(self) -> None:
        from wpo.supervisor import Supervisor
        Supervisor.catch_up_with_main(self.supervisor, self.worktree)

    def contains_main(self) -> bool:
        return self.git(self.worktree, "merge-base", "--is-ancestor", "main", "HEAD", check=False).returncode == 0

    def test_main_is_merged_and_uncommitted_edits_elsewhere_stay(self) -> None:
        self.write(self.worktree, "edited.txt", "builder work\n")
        self.write(self.main_tree, "dependency.txt", "new\n")
        self.commit(self.main_tree, "dependency merged")
        self.catch_up()
        self.assertTrue(self.contains_main())
        self.assertTrue((self.worktree / "dependency.txt").exists())
        self.assertEqual((self.worktree / "edited.txt").read_text(encoding="utf-8"), "builder work\n")

    def test_main_changing_an_uncommitted_file_leaves_the_tree_alone(self) -> None:
        self.write(self.worktree, "edited.txt", "builder work\n")
        self.write(self.main_tree, "edited.txt", "main change\n")
        self.commit(self.main_tree, "main edits the same file")
        self.catch_up()
        self.assertFalse(self.contains_main())
        self.assertEqual((self.worktree / "edited.txt").read_text(encoding="utf-8"), "builder work\n")
        self.assertIn("left for integration", self.logs[-1])

    def test_a_conflict_in_committed_work_is_aborted_and_keeps_uncommitted_edits(self) -> None:
        self.write(self.worktree, "shared.txt", "branch\n")
        self.commit(self.worktree, "branch commit")
        self.write(self.worktree, "edited.txt", "builder work\n")
        self.write(self.main_tree, "shared.txt", "main\n")
        self.commit(self.main_tree, "conflicting main commit")
        self.catch_up()
        self.assertFalse(self.contains_main())
        self.assertNotEqual(self.git(self.worktree, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode, 0)
        self.assertEqual((self.worktree / "shared.txt").read_text(encoding="utf-8"), "branch\n")
        self.assertEqual((self.worktree / "edited.txt").read_text(encoding="utf-8"), "builder work\n")


class RecoveryIntegrationTests(unittest.TestCase):
    """A supervisor's pass merges WPs done in their checkout but not on main, and never a WP main
    already holds as done, though under T3 its checkout outlives the merge; such a WP is read from main."""

    def setUp(self) -> None:
        from wpo.context import Context
        from wpo.machine import DEFAULTS as MACHINE_DEFAULTS
        from wpo.project import DEFAULTS as PROJECT_DEFAULTS
        from wpo.project import Project, git
        from wpo.routing import Router
        self.git = git
        scratch = tempfile.TemporaryDirectory(prefix="wpo-recovery-")
        self.addCleanup(scratch.cleanup)
        base = Path(scratch.name)
        self.root, self.checkout = base / "repo", base / "native"
        self.root.mkdir()
        for arguments in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            git(self.root, *arguments)
        self.project = Project(self.root, {**PROJECT_DEFAULTS, "name": "scratch", "autofix": {}}, "t3")
        machine = MachineConfig({**MACHINE_DEFAULTS, "state_dir": str(base / "state"), "dashboard": {"autostart": False}})
        self.context = Context(self.project, machine, Router(machine, project=self.project), Journal(self.project.runs_dir))
        (self.root / ".gitignore").write_text(".orchestration/\n", encoding="utf-8")

    def write_wp(self, tree: Path, status: str) -> None:
        directory = self.project.wp_dir(tree)
        directory.mkdir(parents=True, exist_ok=True)
        text = WP_TEXT.replace("lane: core   # a comment", "lane: life").replace("status: todo", f"status: {status}").replace("depends_on: [WP-0001, WP-0002]", "depends_on: []")
        (directory / "WP-0042-example.md").write_text(text, encoding="utf-8")

    def commit_main(self, status: str) -> None:
        self.write_wp(self.root, status)
        self.git(self.root, "add", "-A")
        self.git(self.root, "commit", "-q", "--allow-empty", "-m", f"WP-0042 {status}")

    def retain_checkout(self) -> None:
        """The WP's T3 checkout, on a branch at main's tip, recorded as T3 records it."""
        self.git(self.root, "worktree", "add", "-q", "-b", "t3/example", str(self.checkout), "main")
        record = self.project.wp_run_dir("WP-0042") / "t3-workspace.json"
        record.write_text(json.dumps({"path": str(self.checkout), "branch": "t3/example"}), encoding="utf-8")

    def supervisor(self):
        from wpo.supervisor import Supervisor
        supervisor = Supervisor(self.context, "life", True, [], {})
        supervisor.integrate = mock.Mock(return_value=True)
        return supervisor

    def test_a_wp_done_on_main_is_not_merged_again_though_its_checkout_remains(self) -> None:
        self.commit_main("done")
        self.retain_checkout()
        (self.checkout / "stray.txt").write_text("left in the checkout\n", encoding="utf-8")
        supervisor = self.supervisor()
        supervisor.loop()
        supervisor.integrate.assert_not_called()

    def test_a_wp_done_only_in_its_uncommitted_checkout_is_merged(self) -> None:
        # The branch holds nothing main lacks (a builder's work stays uncommitted until integration),
        # so a guard asking git whether main contains the branch would skip it.
        self.commit_main("review")
        self.retain_checkout()
        self.write_wp(self.checkout, "done")
        self.assertEqual(self.git(self.root, "merge-base", "--is-ancestor", "t3/example", "main", check=False).returncode, 0)
        supervisor = self.supervisor()
        supervisor.loop()
        supervisor.integrate.assert_called_once_with("WP-0042")

    def test_restarted_supervisors_leave_a_merged_wp_alone(self) -> None:
        self.commit_main("done")
        self.retain_checkout()
        for _ in range(3):
            supervisor = self.supervisor()
            supervisor.loop()
            supervisor.integrate.assert_not_called()

    def test_later_passes_of_one_supervisor_see_its_own_merge(self) -> None:
        from types import SimpleNamespace
        self.commit_main("review")
        self.retain_checkout()
        self.write_wp(self.checkout, "done")
        supervisor = self.supervisor()
        # The stub merges as integration does: main gets the WP as done, and the checkout stays.
        supervisor.integrate.side_effect = lambda wp_id: self.commit_main("done") or True
        pick = (SimpleNamespace(id="WP-OTHER"), SimpleNamespace(release=lambda: None))
        supervisor.next_wp = mock.Mock(side_effect=[pick, pick, None])
        supervisor.run_claimed = mock.Mock(return_value=None)
        supervisor.loop()
        self.assertEqual(supervisor.next_wp.call_count, 3, "three passes ran")
        supervisor.integrate.assert_called_once_with("WP-0042")

    def test_a_wp_done_on_main_is_read_from_main_though_its_checkout_is_open(self) -> None:
        # Settled on main (`wpo commit-main`) while its retained checkout still holds an open copy.
        from wpo.user_actions import effective_wp
        self.commit_main("done")
        self.retain_checkout()
        self.write_wp(self.checkout, "in_progress")
        (wp,) = self.context.effective_wps()
        self.assertEqual((wp.status, wp.path.parent), ("done", self.project.wp_dir()))
        wp, in_worktree = effective_wp(self.context, "WP-0042")
        self.assertEqual((wp.status, in_worktree), ("done", False))

    def test_a_wp_open_on_main_is_read_from_its_checkout(self) -> None:
        from wpo.user_actions import effective_wp
        self.commit_main("review")
        self.retain_checkout()
        self.write_wp(self.checkout, "done")
        (wp,) = self.context.effective_wps()
        self.assertEqual((wp.status, wp.path.parent), ("done", self.project.wp_dir(self.checkout)))
        wp, in_worktree = effective_wp(self.context, "WP-0042")
        self.assertEqual((wp.status, in_worktree), ("done", True))


class IntegrationChecksTests(unittest.TestCase):
    """The full checks run only on the merge, and a check that writes into the tree fails it."""

    def integration(self, tree: Path, round_checks: list[str]):
        from types import SimpleNamespace
        from wpo.integrate import Integration
        integration = Integration.__new__(Integration)
        integration.worktree = tree
        integration.wp_id = "WP-X"
        integration.context = mock.Mock()
        integration.log_path = tree.parent / "integrate.log"
        integration.main = "main"
        integration.project = SimpleNamespace(config={"shell": "bash", "verify_workspace": ["cargo test --workspace"], "check_scope": None,
                                                      "verify_neutral_paths": [], "verify_round": round_checks, "command_timeout_minutes": 1})
        return integration

    def test_a_check_writing_a_tracked_file_fails_the_merge(self) -> None:
        from wpo import integrate
        from wpo.project import git
        tree = Path(tempfile.mkdtemp()) / "repo"
        tree.mkdir()
        for arguments in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            git(tree, *arguments)
        (tree / "gate.log").write_text("recorded\n", encoding="utf-8")
        git(tree, "add", "-A")
        git(tree, "commit", "-q", "-m", "base")
        integration = self.integration(tree, ["cargo fmt --check"])

        def regenerate(*args, **kwargs) -> list:
            (tree / "gate.log").write_text("regenerated\n", encoding="utf-8")
            return []

        with mock.patch.object(integrate, "session_env"), mock.patch.object(integrate, "run_commands", side_effect=regenerate):
            self.assertFalse(integration.verify())
        self.assertIn("gate.log", integration.log_path.read_text(encoding="utf-8"))
        with mock.patch.object(integrate, "session_env"), mock.patch.object(integrate, "run_commands", return_value=[]):
            git(tree, "checkout", "--", "gate.log")
            self.assertTrue(integration.verify())

    def test_a_wps_own_merge_only_check_is_left_to_the_merge(self) -> None:
        from wpo.wpfile import WorkPackage
        wp = mock.Mock(verify_commands=lambda: ["cargo test -p zm_a", "cargo test --workspace"])
        checks = WorkPackage.round_checks(wp, ["cargo fmt --check"], ["cargo fmt --check", "cargo test --workspace"])
        self.assertEqual(checks, ["cargo test -p zm_a", "cargo fmt --check"])

    def test_rounds_without_the_full_checks_never_skip_them_on_the_merge(self) -> None:
        wp = mock.Mock(round_checks=lambda round_checks, merge_checks: ["cargo test -p zm_a", *round_checks])
        self.assertFalse(self.integration(Path(tempfile.mkdtemp()), ["cargo fmt --check"]).rounds_ran_the_full_checks(wp))
        self.assertTrue(self.integration(Path(tempfile.mkdtemp()), ["cargo test --workspace"]).rounds_ran_the_full_checks(wp))


class VerdictTests(unittest.TestCase):
    def test_last_verdict_wins(self) -> None:
        from wpo.runner import last_verdict
        self.assertEqual(last_verdict("Earlier draft said VERDICT: PASS.\nBLOCKING: x\nVERDICT: CHANGES_REQUESTED"), "CHANGES_REQUESTED")
        self.assertIsNone(last_verdict("no verdict"))


class HarnessTests(unittest.TestCase):
    config = {"builder_commands": ["cargo", "git diff"], "reviewer_commands": ["cargo test"]}

    def test_proxy_refusal_reports_only_rate_limits(self) -> None:
        import io
        import urllib.error
        env = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1", "ANTHROPIC_MODEL": "gpt-x(low)", "ANTHROPIC_API_KEY": "token"}

        def failing(code: int, body: str):
            return mock.patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("url", code, "msg", {}, io.BytesIO(body.encode())))

        with failing(429, '{"error": {"message": "usage_limit_reached"}}'):
            self.assertEqual(harness.proxy_refusal(env), "usage_limit_reached")
        with failing(500, "{}"):
            self.assertIsNone(harness.proxy_refusal(env))
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            self.assertIsNone(harness.proxy_refusal(env))

    def test_claude_sandbox(self) -> None:
        arguments = harness.claude_arguments("worker", ["src/a/**"], harness.command_prefixes("worker", self.config, lead=True))
        allowed = arguments[arguments.index("--allowedTools") + 1]
        self.assertIn("Bash(cargo:*)", allowed)
        self.assertNotIn("rtk", allowed)  # the user's hooks are off, so nothing rewrites commands to rtk
        self.assertEqual(json.loads(arguments[arguments.index("--settings") + 1]), {"disableAllHooks": True})
        self.assertIn("Edit(src/a/**)", allowed)
        self.assertNotIn("wpo delegate", allowed)
        self.assertNotIn("acceptEdits", arguments)
        self.assertIn("--strict-mcp-config", arguments)
        self.assertIn("--disable-slash-commands", arguments)
        self.assertNotIn("Skill", allowed.split())
        self.assertEqual(arguments[arguments.index("--input-format") + 1], "stream-json")
        builder = harness.command_prefixes("builder", self.config, lead=True)
        self.assertIn(harness.DELEGATE_COMMAND, builder)

    def test_session_workers_only_when_given(self) -> None:
        prefixes = harness.command_prefixes("builder", self.config, lead=True)
        with_workers = harness.claude_arguments("builder", [], prefixes, workers={"model": "haiku", "effort": "low"})
        definition = json.loads(with_workers[with_workers.index("--agents") + 1])
        self.assertEqual(definition[harness.SESSION_WORKER_AGENT]["model"], "haiku")
        self.assertEqual(definition[harness.SESSION_WORKER_AGENT]["effort"], "low")
        self.assertIn(f"Agent({harness.SESSION_WORKER_AGENT})", with_workers[with_workers.index("--allowedTools") + 1].split())
        self.assertNotIn("Agent", with_workers[with_workers.index("--disallowedTools") + 1].split())
        without = harness.claude_arguments("builder", [], prefixes)
        self.assertNotIn("--agents", without)
        self.assertIn("Agent", without[without.index("--disallowedTools") + 1].split())

    def test_compaction_limit_per_harness(self) -> None:
        handoff = {"context_tokens": {"default": 180000, "gpt": 140000}}
        config = {"autocompact_tokens": {"default": 275000, "gpt": 160000}, "handoff": handoff}
        self.assertEqual(harness.compaction_limit(config, "gpt"), 160000)
        self.assertEqual(harness.compaction_limit(config, "claude"), 275000)
        harness.validate_compaction_limits(config)
        with self.assertRaises(harness.HarnessError):
            harness.compaction_limit({"autocompact_tokens": 275000}, "claude")
        with self.assertRaises(harness.HarnessError):
            harness.validate_compaction_limits({"autocompact_tokens": {"default": 275000, "gpt": 50000}, "handoff": handoff})

    def test_the_handoff_point_must_come_before_compaction(self) -> None:
        config = {"autocompact_tokens": {"default": 275000, "gpt": 160000}, "handoff": {"context_tokens": {"default": 180000}}}
        self.assertEqual(harness.handoff_limit(config, "local"), 180000)
        with self.assertRaisesRegex(harness.HarnessError, "gpt handoff point"):
            harness.validate_compaction_limits(config)  # gpt compacts at 160k, before the 180k default handoff

    def test_autocompact_only_below_the_route_window(self) -> None:
        self.assertEqual(harness.autocompact_arguments(275000, None), ["--autocompact", "275k"])
        self.assertEqual(harness.autocompact_arguments(275000, 900000), ["--autocompact", "275k"])
        self.assertEqual(harness.autocompact_arguments(275000, 272000), [])
        for invalid in (50000, 275500, 2000000):
            with self.assertRaises(harness.HarnessError):
                harness.autocompact_arguments(invalid, None)

    def test_opencode_sandbox(self) -> None:
        config = json.loads(harness.opencode_config({"provider": "openrouter", "model": "openrouter/x:free"}, "worker", ["src/**"], ["cargo"]))
        self.assertEqual(config["permission"]["edit"], {"*": "deny", "src/*": "allow"})
        self.assertEqual(config["permission"]["bash"], {"*": "deny", "cargo": "allow", "cargo *": "allow"})
        self.assertEqual(config["permission"]["external_directory"], "deny")
        self.assertIn("x:free", config["provider"]["openrouter"]["models"])


class SessionBriefTests(unittest.TestCase):
    def test_agent_skills_from_front_matter(self) -> None:
        from wpo.session_brief import agent_skills
        with tempfile.TemporaryDirectory() as scratch:
            agent = Path(scratch) / "reviewer.md"
            agent.write_text("---\nname: reviewer\nskills: coding\n---\nBody\n")
            self.assertEqual(agent_skills(agent), ["coding"])
            agent.write_text("---\nname: reviewer\nskills: [coding, impeccable]\n---\nBody\n")
            self.assertEqual(agent_skills(agent), ["coding", "impeccable"])
            agent.write_text("---\nname: reviewer\n---\nBody\n")
            self.assertEqual(agent_skills(agent), [])
            self.assertEqual(agent_skills(Path(scratch) / "missing.md"), [])

    def test_wp_skills_field(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "WP-0042-x.md"
            path.write_text(WP_TEXT.replace("owns:", "skills: [impeccable]\nowns:"))
            self.assertEqual(wpfile.parse(path).skills, ["impeccable"])
            path.write_text(WP_TEXT)
            self.assertEqual(wpfile.parse(path).skills, [])


class NativeInstructionTests(unittest.TestCase):
    """A session whose harness loads instruction files itself gets a brief without them."""

    def setUp(self) -> None:
        from types import SimpleNamespace
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        (root / "code" / "rust" / "proj").mkdir(parents=True)
        (root / "code" / ".worktrees" / "proj" / "wt").mkdir(parents=True)
        (root / "other").mkdir()
        for path, text in (("global.md", "## Safety\nGLOBAL RULE\n"), ("session.md", "SESSION RULE\n"), ("code/CLAUDE.md", "CODE BASELINE\n"),
                           ("code/rust/CLAUDE.md", "RUST BASELINE\n"), ("code/rust/proj/CLAUDE.md", "PROJECT RULES\n"), ("other/SKILL.md", "EXTRA SKILL\n")):
            (root / path).write_text(text, encoding="utf-8")
        self.tree = root / "code" / ".worktrees" / "proj" / "wt"
        try:
            (root / "code" / ".worktrees" / "proj" / "CLAUDE.md").symlink_to(root / "code" / "rust" / "CLAUDE.md")
            (self.tree / "CLAUDE.md").symlink_to(root / "code" / "rust" / "proj" / "CLAUDE.md")
        except OSError as error:
            self.skipTest(f"symlink unavailable: {error}")
        self.native = {"families": ["claude"], "names": ["CLAUDE.md"], "files": [str(root / "global.md")]}
        raw = {"skills": {"dirs": [], "paths": {"code": str(root / "code" / "CLAUDE.md"), "rust": str(root / "code" / "rust" / "CLAUDE.md"),
                                                "extra": str(root / "other" / "SKILL.md")}},
               "user_rules": [{"file": str(root / "global.md"), "sections": ["Safety"]}, {"file": str(root / "session.md")}],
               "t3": {"native_instructions": {"families": [], "names": [], "files": []}}}
        self.context = SimpleNamespace(machine=SimpleNamespace(raw=raw), project=SimpleNamespace(
            root=root / "code" / "rust" / "proj", config={"instructions_file": "CLAUDE.md"}))

    def brief(self):
        from wpo.session_brief import build_brief
        return build_brief(self.context, self.tree, ["code", "rust", "extra"])

    def test_native_sessions_leave_out_what_they_load(self) -> None:
        from wpo.session_brief import loads_instructions_natively
        self.context.machine.raw["t3"]["native_instructions"] = self.native
        brief = self.brief()
        full, native = brief.system_text(), brief.system_text(native=True)
        for text in ("PROJECT RULES", "GLOBAL RULE", "CODE BASELINE", "RUST BASELINE", "SESSION RULE", "EXTRA SKILL", "## Working rules"):
            self.assertIn(text, full)
        for text in ("PROJECT RULES", "GLOBAL RULE", "CODE BASELINE", "RUST BASELINE"):
            self.assertNotIn(text, native)
        for text in ("SESSION RULE", "EXTRA SKILL", "## Working rules"):
            self.assertIn(text, native)
        self.assertTrue(loads_instructions_natively(self.context, "claude"))
        self.assertFalse(loads_instructions_natively(self.context, "agy"))

    def test_without_native_families_every_session_gets_the_whole_brief(self) -> None:
        brief = self.brief()
        self.assertIsNone(brief.native_text)
        self.assertEqual(brief.system_text(native=True), brief.system_text())

    def test_a_checkout_without_the_project_file_still_gets_it(self) -> None:
        (self.tree / "CLAUDE.md").unlink()
        self.context.machine.raw["t3"]["native_instructions"] = self.native
        native = self.brief().system_text(native=True)
        self.assertIn("PROJECT RULES", native)
        self.assertNotIn("RUST BASELINE", native)

    def test_a_checkout_copy_older_than_main_keeps_mains_instructions(self) -> None:
        (self.tree / "CLAUDE.md").unlink()
        (self.tree / "CLAUDE.md").write_text("OLD PROJECT RULES\n", encoding="utf-8")
        self.context.machine.raw["t3"]["native_instructions"] = self.native
        self.assertIn("PROJECT RULES\n", self.brief().system_text(native=True).replace("OLD PROJECT RULES", ""))
        (self.tree / "CLAUDE.md").write_text("PROJECT RULES\n", encoding="utf-8")
        self.assertNotIn("PROJECT RULES", self.brief().system_text(native=True))

    def test_malformed_settings_name_the_key(self) -> None:
        from wpo.session_brief import BriefError
        self.context.machine.raw["t3"]["native_instructions"] = {**self.native, "names": "CLAUDE.md"}
        with self.assertRaisesRegex(BriefError, "native_instructions.names"):
            self.brief()


class RoundRoutingTests(unittest.TestCase):
    def test_mechanical_only_when_every_failure_is_a_lint_check(self) -> None:
        from wpo.commands import CommandFailure
        from wpo.runner import CheckResult, is_mechanical
        prefixes = ["cargo fmt", "cargo clippy"]
        clippy = CommandFailure("cargo clippy --workspace -- -D warnings", 101, "warning: x")
        tests = CommandFailure("cargo test --workspace", 101, "panicked")
        rejects = ["error[E"]
        self.assertTrue(is_mechanical(CheckResult("f", [clippy], []), prefixes, rejects))
        self.assertFalse(is_mechanical(CheckResult("f", [clippy, tests], []), prefixes, rejects))
        self.assertFalse(is_mechanical(CheckResult("f", [clippy], ["other/file.rs"]), prefixes, rejects))
        self.assertFalse(is_mechanical(CheckResult("f", [], []), prefixes, rejects))  # e.g. no Verify block at all
        compile_error = CommandFailure("cargo clippy --workspace", 101, "error[E0425]: cannot find value `x`")
        self.assertFalse(is_mechanical(CheckResult("f", [compile_error], []), prefixes, rejects))
        compound = CommandFailure("cargo clippy --workspace; cargo test", 101, "panicked")
        self.assertFalse(is_mechanical(CheckResult("f", [compound], []), prefixes, rejects))
        lookalike = CommandFailure("cargo clippyish", 1, "x")  # words must match, not characters
        self.assertFalse(is_mechanical(CheckResult("f", [lookalike], []), prefixes, rejects))

    def test_resume_refusals(self) -> None:
        from wpo.runner import resume_refusal
        settings = {"harnesses": ["claude", "gpt"]}
        last = {"harness": "claude", "spec": "code-hard", "context_tokens": 150000, "head": "abc"}
        self.assertIsNone(resume_refusal(last, "review requested changes", settings, "code-hard", "abc", 180000))
        self.assertIn("builder blocked", resume_refusal(last, "builder blocked", settings, "code-hard", "abc", 180000))
        # A re-evaluation that decided 'continue' resumes even a builder that reported itself blocked.
        self.assertIsNone(resume_refusal({**last, "evaluated_continue": True}, "builder blocked", settings, "code-hard", "abc", 180000))
        self.assertIn("re-evaluation decided 'revise'",
                      resume_refusal({**last, "fresh_reason": "the re-evaluation decided 'revise'"}, "verification failed", settings, "code-hard", "abc", 180000))
        self.assertIn("reached the handoff point (180,000)",
                      resume_refusal({**last, "context_tokens": 180000}, "verification failed", settings, "code-hard", "abc", 180000))
        self.assertIn("tier changed", resume_refusal(last, "verification failed", settings, "frontier", "abc", 180000))
        self.assertIn("HEAD moved", resume_refusal(last, "verification failed", settings, "code-hard", "def", 180000))
        self.assertIn("not resumed", resume_refusal({**last, "harness": "opencode"}, "verification failed", settings, "code-hard", "abc", 180000))

    def test_a_bare_final_verdict_falls_back_to_the_message_with_findings(self) -> None:
        import json as json_module
        from wpo.runner import review_text
        log = Path(tempfile.mkdtemp()) / "review.log"
        review = "FINDING R1-F1 BLOCKING src/a.rs:3\n  Problem: wrong.\n  Fix: right.\n\nVERDICT: CHANGES_REQUESTED"
        bare = "The tests passed. The findings remain unchanged.\n\nVERDICT: CHANGES_REQUESTED"
        events = [{"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}} for text in (review, bare)]
        log.write_text("\n".join(json_module.dumps(event) for event in events), encoding="utf-8")
        self.assertEqual(review_text(bare, log, 1), review)
        self.assertEqual(review_text(review, log, 1), review)
        # A pass needs no findings, and a session with no verdict is left for a human.
        self.assertEqual(review_text("VERDICT: PASS", log, 1), "VERDICT: PASS")
        self.assertEqual(review_text("no verdict", log, 1), "no verdict")

    def test_a_large_session_past_its_prompt_cache_hands_off(self) -> None:
        from datetime import datetime
        from wpo.runner import resume_refusal
        settings = {"harnesses": ["claude"], "cold_after_minutes": 60, "cold_context_tokens": 100000}
        last = {"harness": "claude", "spec": "code-hard", "context_tokens": 150000, "head": "abc", "ended": "2026-11-01T03:00:00"}
        fresh = datetime.fromisoformat("2026-11-01T03:30:00")
        stale = datetime.fromisoformat("2026-11-05T04:00:00")
        self.assertIsNone(resume_refusal(last, "verification failed", settings, "code-hard", "abc", 420000, now=fresh))
        self.assertIn("past its prompt cache", resume_refusal(last, "verification failed", settings, "code-hard", "abc", 420000, now=stale))
        small = {**last, "context_tokens": 80000}
        self.assertIsNone(resume_refusal(small, "verification failed", settings, "code-hard", "abc", 420000, now=stale))

    def test_a_continue_keeps_the_session_and_other_decisions_hand_off(self) -> None:
        from wpo.runner import RoundLedger
        ledger = RoundLedger(Path(tempfile.mkdtemp()) / "attempts.json")
        session = {"route": "claude-opus-high", "harness": "claude", "session_id": "s-1", "context_tokens": 90000, "head": "abc", "spec": "code-hard"}
        ledger.record_builder(session)
        ledger.evaluated("continue", "focus on x", None)
        self.assertTrue(ledger.last_builder["evaluated_continue"])
        ledger.record_builder(session)
        ledger.evaluated("revise", "smaller steps", None)
        self.assertEqual(ledger.last_builder["fresh_reason"], "the re-evaluation decided 'revise'")

    def test_a_handoff_continues_the_round_without_counting_it(self) -> None:
        from wpo.runner import HANDOFF_FILE, RoundLedger, take_handoff
        run_dir = Path(tempfile.mkdtemp())
        ledger = RoundLedger(run_dir / "attempts.json")
        ledger.record_builder({"route": "r", "harness": "claude", "session_id": "s", "context_tokens": 1, "head": "h", "spec": "code"})
        self.assertEqual(ledger.count(), 1)
        (run_dir / HANDOFF_FILE).write_text("Done: a.\nNext: b.\n", encoding="utf-8")  # what `wpo handoff` records
        text = take_handoff(run_dir)
        self.assertEqual(text, "Done: a.\nNext: b.")
        self.assertIsNone(take_handoff(run_dir))  # taken once: no later session reuses it
        ledger.handed_off(text, "your context reached 180,000 tokens")
        self.assertEqual((ledger.rounds, ledger.handoffs, ledger.last_builder), (0, 1, None))
        self.assertEqual(ledger.handoff, {"text": text, "why": "your context reached 180,000 tokens"})
        self.assertEqual(ledger.count(), 1)  # the fresh session continues round 1
        ledger.handoff_delivered()
        ledger.finished_session()
        self.assertEqual((ledger.handoff, ledger.handoffs), (None, 0))

    def test_a_builder_prompt_carries_the_handoff(self) -> None:
        from wpo.runner import handoff_request, handoff_turn_prompt
        self.assertIn("wpo handoff", handoff_request("your context reached 180,000 tokens"))
        wp = mock.Mock(id="WP-1")
        self.assertIn("this feedback too", handoff_turn_prompt(wp, "the re-evaluation decided 'revise'", "fix x"))
        self.assertNotIn("this feedback", handoff_turn_prompt(wp, "its context reached the handoff point", ""))

    def test_a_stream_session_is_asked_for_its_handoff_once(self) -> None:
        from wpo.session import HandoffTrigger, HandoffWatch, StreamInput
        directory = Path(tempfile.mkdtemp())
        log = directory / "s.log"
        turn = {"type": "assistant", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 185000}}}
        subagent_turn = {**turn, "parent_tool_use_id": "t-1", "message": {"usage": {"input_tokens": 900000}}}
        log.write_text("\n".join(json.dumps(event) for event in (turn, subagent_turn)) + "\n", encoding="utf-8")
        process = mock.Mock()
        stream = StreamInput(process, log, None)
        stream.poll()
        self.assertEqual(stream.context_tokens, 185010)  # the subagent's turn is not the session's context
        sent: list[str] = []
        stream.send = sent.append
        watch = HandoffWatch(HandoffTrigger(180000, lambda: None, lambda reason: f"hand off: {reason}"))
        watch.poll(stream)
        watch.poll(stream)
        self.assertEqual(len(sent), 1)
        self.assertIn("185,010 tokens", sent[0])

    def test_a_quota_about_to_run_out_asks_for_the_handoff(self) -> None:
        from wpo.session import HandoffTrigger, HandoffWatch
        stream = mock.Mock(ready=True, context_tokens=1000)
        watch = HandoffWatch(HandoffTrigger(180000, lambda: "the claude 5h window is at 98%", lambda reason: reason))
        watch.poll(stream)
        stream.send.assert_called_once_with("the claude 5h window is at 98%")

    def test_quota_handoffs_go_one_at_a_time_per_family(self) -> None:
        from wpo.session import HandoffTrigger, HandoffWatch, QuotaHandoffSlot
        state = Path(tempfile.mkdtemp())
        first, second = QuotaHandoffSlot(state, "gpt", "a.log"), QuotaHandoffSlot(state, "gpt", "b.log")
        streams = [mock.Mock(ready=True, context_tokens=1000) for _ in range(2)]
        watches = [HandoffWatch(HandoffTrigger(180000, lambda: "the gpt 5h window is at 98%", lambda reason: reason, slot))
                   for slot in (first, second)]
        for watch, stream in zip(watches, streams):
            watch.poll(stream)
        streams[0].send.assert_called_once()
        streams[1].send.assert_not_called()  # the first session is writing its handoff
        self.assertTrue(QuotaHandoffSlot(state, "claude", "c.log").try_claim())  # another family's slot is its own
        first.release()
        watches[1].quota_checked_at = float("-inf")
        watches[1].poll(streams[1])
        streams[1].send.assert_called_once()
        # A holder that died frees the slot.
        second.release()
        (state / "quota-handoff-gpt.json").write_text(json.dumps({"holder": "dead.log", "pid": 999999, "since": time.time()}))
        self.assertTrue(first.try_claim())

    def test_a_pin_skips_the_weekly_pace_but_not_the_ceilings(self) -> None:
        router = routing.Router(machine_in(Path(tempfile.mkdtemp())), {"weekly_pace": {"claude": {"budget_percent": 70, "slack_percent": 10}}})
        def windows(weekly: float) -> dict:
            return {"claude": [{"label": "5h", "used_percent": 10, "window_seconds": 18000, "resets_at": time.time() + 3600},
                               {"label": "7d", "used_percent": weekly, "window_seconds": 604800, "resets_at": time.time() + 2 * 86400}],
                    "gpt": None}
        with mock.patch.object(routing.Router, "catalog_routes", return_value=[]), \
             mock.patch.object(routing.Router, "usage_windows", return_value=windows(80)):
            self.assertEqual([entry["id"] for entry in router.select("pin:claude-opus-high").candidates], ["claude-opus-high"])
            self.assertNotIn("claude-opus-high", [entry["id"] for entry in router.select("frontier+taste").candidates])
        with mock.patch.object(routing.Router, "catalog_routes", return_value=[]), \
             mock.patch.object(routing.Router, "usage_windows", return_value=windows(93)):
            self.assertEqual(router.select("pin:claude-opus-high").candidates, [])  # past the 92% weekly ceiling

    def burn_router(self, readings: list[tuple[float, float]], label: str = "5h") -> routing.Router:
        """A router whose usage history holds gpt `label` readings (minutes ago, used percent)."""
        directory = Path(tempfile.mkdtemp())
        machine = machine_in(directory)
        machine.raw["usage"]["history"] = str(directory / "history.jsonl")
        now, resets = time.time(), time.time() + 3 * 3600
        rows = [{"ts": now - minutes * 60, "family": "gpt", "label": label, "used_percent": used, "window_seconds": 18000, "resets_at": resets}
                for minutes, used in readings]
        (directory / "history.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return routing.Router(machine)

    def test_a_window_burning_to_its_cap_within_the_lead_hands_off_and_starts_nothing(self) -> None:
        # 60% to 80% in 10 minutes: 2%/min, about 10 min left from a reading 1 minute old.
        router = self.burn_router([(11, 60.0), (6, 70.0), (1, 80.0)])
        forecast = router.cap_forecast("gpt")
        self.assertAlmostEqual(forecast.percent_per_minute, 2.0)
        self.assertAlmostEqual(forecast.minutes_to_cap, 9.0, places=1)
        self.assertIsNotNone(router.burning_out("gpt"))
        blocked_until, reason = router.subscription_blocked_until([{"label": "5h", "used_percent": 80.0, "window_seconds": 18000,
                                                                    "resets_at": time.time() + 3 * 3600}], "gpt")
        self.assertGreater(blocked_until, routing.now_utc())
        self.assertIn("burning 2.00%/min", reason)
        from wpo.session import quota_running_out
        context = mock.Mock(router=router, project=mock.Mock(config={"handoff": {"quota_percent": 97}}))
        self.assertIn("which will cut this session off", quota_running_out(context, {"family": "gpt"}))

    def test_a_slow_burn_an_old_reading_or_a_reset_window_is_not_burning_out(self) -> None:
        self.assertIsNone(self.burn_router([(15, 60.0), (1, 62.0)]).burning_out("gpt"))  # 0.14%/min: hours left
        self.assertIsNone(self.burn_router([(40, 10.0), (1, 90.0)]).cap_forecast("gpt"))  # one reading in the lookback
        self.assertIsNone(self.burn_router([(3, 60.0), (1, 70.0)]).cap_forecast("gpt"))  # under the minimum span
        self.assertIsNone(self.burn_router([(11, 90.0), (1, 5.0)]).cap_forecast("gpt"))  # falling: not burning

    def test_sleeping_and_pool_lanes_take_no_lane_slot(self) -> None:
        from types import SimpleNamespace
        from wpo.lanes import active_lanes
        runs = Path(tempfile.mkdtemp())
        context = SimpleNamespace(project=SimpleNamespace(runs_dir=runs, config={"lanes": {"local": {"pool": True}}}))
        for lane, state in (("working", "running"), ("asleep", "sleeping"), ("local", "running"), ("gone", "running")):
            (runs / f"supervisor-{lane}.state").write_text(state, encoding="utf-8")
            (runs / f"supervisor-{lane}.pid").write_text(str(os.getpid() if lane != "gone" else 999999), encoding="utf-8")
        self.assertEqual(active_lanes(context, {"working", "asleep", "local", "gone"}), {"working"})

    def test_elapsed_text_reads_at_a_glance(self) -> None:
        from wpo.runner import elapsed_text
        self.assertEqual([elapsed_text(seconds) for seconds in (-3, 40, 720, 10800, 11100, 90000, 172800)],
                         ["0 s", "40 s", "12 min", "3 h", "3 h 5 min", "1 d 1 h", "2 d"])

    def test_a_cap_ends_every_session_of_the_family(self) -> None:
        router = routing.Router(machine_in(Path(tempfile.mkdtemp())))
        started = time.time()
        resets = (routing.now_utc() + timedelta(hours=2)).timestamp()
        with mock.patch.object(routing.Router, "usage_windows", return_value={"gpt": [{"label": "5h", "used_percent": 100, "resets_at": resets}]}):
            self.assertIn("5h usage window is at 100%", router.family_cut_off("gpt", started))
            self.assertIsNone(router.family_cut_off("local", started))
        with mock.patch.object(routing.Router, "usage_windows", return_value={"claude": [{"label": "5h", "used_percent": 60, "resets_at": resets}]}), \
             mock.patch.object(routing.Router, "subscription_blocked_until", return_value=(routing.now_utc() + timedelta(hours=1), None)):
            self.assertIsNone(router.family_cut_off("claude", started))
            router.mark("claude-opus-high", "rate_limited", "You've hit your limit")
            self.assertIn("another claude session hit its cap", router.family_cut_off("claude", started))
            # A cap recorded before the session started did not end it.
            self.assertIsNone(router.family_cut_off("claude", time.time() + 1))

    def test_a_cap_recorded_after_a_launch_ends_it_whatever_the_clocks(self) -> None:
        # Sessions take their launch time from time.time(); on Windows datetime.now() can lag it
        # by a clock tick, and a cap stamped from it then read as older than the launch.
        router = routing.Router(machine_in(Path(tempfile.mkdtemp())))
        launched = time.time()
        lagging = routing.from_timestamp(launched - 0.016)
        with mock.patch.object(routing, "now_utc", return_value=lagging), \
             mock.patch.object(routing.Router, "usage_windows", return_value={"claude": []}), \
             mock.patch.object(routing.Router, "subscription_blocked_until", return_value=(lagging + timedelta(hours=1), None)):
            router.mark("claude-opus-high", "rate_limited", "You've hit your limit")
            self.assertIn("another claude session hit its cap", router.family_cut_off("claude", launched))

    def test_a_session_whose_provider_is_cut_off_is_killed(self) -> None:
        from wpo import session
        directory = Path(tempfile.mkdtemp())
        with mock.patch.object(session, "LIVENESS_POLL_SECONDS", 0.1), mock.patch.object(session, "CUT_OFF_CHECK_SECONDS", 0):
            watched = session.watch_process([sys.executable, "-c", "import time; time.sleep(60)"], dict(os.environ), "", directory,
                                            directory / "s.log", 5, False, cut_off=lambda: "the gpt 5h usage window is at 100%")
        self.assertEqual(watched.cut_off, "the gpt 5h usage window is at 100%")

    def test_a_session_compacting_without_end_is_seen(self) -> None:
        from wpo.session import StreamInput
        log = Path(tempfile.mkdtemp()) / "s.log"
        compacting = {"type": "system", "subtype": "status", "status": "compacting"}
        log.write_text(json.dumps(compacting) + "\n", encoding="utf-8")
        stream = StreamInput(mock.Mock(), log, None)
        stream.poll()
        self.assertIsNotNone(stream.compacting_since)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "assistant", "message": {}}) + "\n")
        stream.poll()
        self.assertIsNone(stream.compacting_since)

    def test_a_background_task_ending_after_the_answer_keeps_the_inbox_open(self) -> None:
        from wpo.session import StreamInput
        log = Path(tempfile.mkdtemp()) / "s.log"
        events = [{"type": "system", "subtype": "task_started", "task_id": "replay"},
                  {"type": "result", "subtype": "success", "queued_turn_count": 0},
                  {"type": "system", "subtype": "task_notification", "task_id": "replay", "status": "completed"}]
        log.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        process = mock.Mock()
        stream = StreamInput(process, log, None)
        stream.poll()
        self.assertFalse(stream.closed)  # the notification starts the turn that finishes the work
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "result", "subtype": "success", "queued_turn_count": 0}) + "\n")
        stream.poll()
        self.assertTrue(stream.closed)

    def test_the_recovery_report_waits_for_a_next_route(self) -> None:
        from types import SimpleNamespace
        from wpo import session
        from wpo.routing import SessionOutcome
        route = {"id": "zen/glm", "downgrade": False}
        selections = iter([SimpleNamespace(candidates=[route], skipped=[], next_available_at=None),
                           SimpleNamespace(candidates=[route], skipped=[], next_available_at=None)])
        router = mock.Mock(select=lambda *args, **kwargs: next(selections))
        context = SimpleNamespace(router=router, journal=mock.Mock(),
                                  project=SimpleNamespace(config={"idle_minutes": 5}))
        recovery = mock.Mock(return_value="report")
        refused = SessionOutcome("rate_limited", "Free usage limit reached", "")
        with mock.patch.object(session, "validate_compaction_limits"), mock.patch.object(session, "build_brief"), \
                mock.patch.object(session, "attempt", return_value=refused):
            with self.assertRaises(session.NoRouteError):
                session.run_session(context, "code-hard", "builder", "prompt", Path(tempfile.mkdtemp()) / "attempt-1-builder",
                                    Path("."), recovery=recovery)
        recovery.assert_not_called()  # its Verify run is wasted when no route is left

    def test_failure_output_loses_colour_and_build_progress(self) -> None:
        from wpo.commands import condensed
        raw = "\x1b[0m\x1b[32m+added\x1b[0m\n    Checking zm_a v0.1.0\n   Compiling zm_b v0.1.0\nerror[E0308]: mismatched types\n    Finished dev"
        self.assertEqual(condensed(raw), "+added\nerror[E0308]: mismatched types")

    def test_session_dirs_reach_builders_and_path_prefixes_match_inside_words(self) -> None:
        builder = harness.claude_arguments("builder", [], ["cargo", "git -C E:/Tools/"], session_dirs=["E:/Tools"])
        self.assertEqual(builder[builder.index("--add-dir") + 1], "E:/Tools")
        self.assertNotIn("--add-dir", harness.claude_arguments("reviewer", [], ["cargo"], session_dirs=["E:/Tools"]))
        allowed = builder[builder.index("--allowedTools") + 1]
        self.assertIn("Bash(cargo:*)", allowed)
        self.assertIn("Bash(git -C E:/Tools/*)", allowed)  # ':*' would need a word boundary after the '/'

    def test_resume_flag_only_on_claude_cli_harnesses(self) -> None:
        arguments = harness.claude_arguments("builder", [], ["cargo"], resume_session="abc-123")
        self.assertEqual(arguments[arguments.index("--resume") + 1], "abc-123")
        self.assertNotIn("--resume", harness.claude_arguments("builder", [], ["cargo"]))

    def test_session_facts_take_each_message_at_its_largest(self) -> None:
        from wpo.session import session_facts
        events = [{"type": "system", "subtype": "init", "session_id": "s-1"},
                  {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 7720}}},
                  {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 100, "cache_read_input_tokens": 120000}}},
                  {"type": "assistant", "message": {"id": "m2", "usage": {"input_tokens": None}}},
                  # A delegated subagent's turn is not the session's own context.
                  {"type": "assistant", "parent_tool_use_id": "t1", "message": {"id": "m3", "usage": {"input_tokens": 900000}}}]
        with tempfile.TemporaryDirectory() as scratch:
            log = Path(scratch) / "s.log"
            log.write_text("\n".join(json.dumps(event) for event in events) + "\nnot json\n")
            self.assertEqual(session_facts(log), ("s-1", 120100))


class AgySandboxTests(unittest.TestCase):
    root = "C:/work/repo"

    def policy(self, access: str = "worker", write_paths: list[str] | None = None) -> dict:
        return harness.agy_policy(access, write_paths if write_paths is not None else ["src/**"], ["git status", "cargo"], Path(self.root),
                                  ["E:/Cache"], Path("C:/runs/x.agy-hook.log"))

    def decide(self, policy: dict, tool: str, **arguments) -> str:
        from wpo.agy_hook import decide
        return decide(policy, {"toolCall": {"name": tool, "args": arguments}})[0]

    def test_commands_by_whole_word_prefix_and_single(self) -> None:
        policy = self.policy()
        self.assertEqual(self.decide(policy, "run_command", CommandLine="git status --short"), "allow")
        self.assertEqual(self.decide(policy, "run_command", CommandLine="cargo test -p x --features install-tests -- --nocapture"), "allow")
        for denied in ("whoami", "git statusx", "git status && whoami", "cargo test > out.txt", "cargo $(whoami)", "git push",
                       "cargo test (whoami)", "cargo test @(whoami)", "cargo test ${x}", "cargo test $env:X", "cargo test %PATH%",
                       "cargo test ^& whoami", "cargo test\u2028whoami", "cargo test\twhoami", "cargo test & whoami",
                       "cargo test --target-dir C:/elsewhere", "cargo test --config build.rustc-wrapper=x", "git status -C ..",
                       "cargo test --manifest-path ../other/Cargo.toml", "cargo test C:/Windows/x", "cargo test ../outside"):
            self.assertEqual(self.decide(policy, "run_command", CommandLine=denied), "deny", repr(denied))
        self.assertEqual(self.decide(policy, "run_command", CommandLine="cargo test", Cwd="C:/elsewhere"), "deny")
        self.assertEqual(self.decide(policy, "run_command", CommandLine="cargo test E:/Cache/fixture.bin"), "allow")  # a readable dir

    def test_a_chain_of_allowed_commands_is_allowed(self) -> None:
        policy = self.policy()
        for allowed in ("git status --short && cargo test -p x", "cargo fmt --all; cargo clippy -p x"):
            self.assertEqual(self.decide(policy, "run_command", CommandLine=allowed), "allow", repr(allowed))
        for denied in ("cargo test && whoami", "cargo test && ", "cargo test || whoami", "cargo test && cargo test --target-dir C:/x"):
            self.assertEqual(self.decide(policy, "run_command", CommandLine=denied), "deny", repr(denied))

    def test_progress_log_messages_are_free_text_in_single_quotes(self) -> None:
        builder = harness.agy_policy("builder", ["src/**"], ["cargo", "wpo log"], Path(self.root), [], Path("C:/runs/x.agy-hook.log"))
        message = "wpo log --step 2 'parser done (fields: a; b); next `tests` $x'"
        self.assertEqual(self.decide(builder, "run_command", CommandLine=message), "allow")
        for denied in ("wpo log 'a' ; whoami", "wpo log \"$(whoami)\"", "wpo log 'a\nb'", "wpo verify 'x'"):
            self.assertEqual(self.decide(builder, "run_command", CommandLine=denied), "deny", repr(denied))
        self.assertEqual(self.decide(self.policy(), "run_command", CommandLine="wpo log 'x'"), "deny")  # not in a worker's commands

    def test_writes_by_glob_never_the_sandbox_itself(self) -> None:
        worker = self.policy()
        self.assertEqual(self.decide(worker, "write_to_file", TargetFile="C:/work/repo/src/a.rs"), "allow")
        self.assertEqual(self.decide(worker, "write_to_file", TargetFile="src\\deep\\b.rs"), "allow")
        self.assertEqual(self.decide(worker, "write_to_file", TargetFile="C:/work/repo/other/b.rs"), "deny")
        self.assertEqual(self.decide(worker, "write_to_file", TargetFile="C:/work/repo/src/../../escape.rs"), "deny")
        self.assertEqual(self.decide(worker, "replace_file_content"), "deny")  # no path to check
        builder = self.policy("builder")
        self.assertEqual(self.decide(builder, "write_file", TargetFile="C:/work/repo/anything/x.rs"), "allow")
        for protected in ("C:/work/repo/.agents/hooks.json", "C:/work/repo/.git/config", "C:/WORK/REPO/.agents/x", "C:/work/repo/.AGENTS/hooks.json",
                          "C:/work/repo/.GIT/config", "C:/work/repo/.git", "C:/work/repo/.agents./hooks.json", "C:/work/repo/.git /config",
                          "C:/work/repo/.git::$INDEX_ALLOCATION/config", "C:foo", "//server/share/x",
                          "file:///C:/work/repo/%2eagents/hooks.json", "C:/work/repo/src/sub/.git/config"):
            self.assertEqual(self.decide(builder, "write_file", TargetFile=protected), "deny", protected)
        self.assertEqual(self.decide(self.policy("reviewer"), "write_file", TargetFile="C:/work/repo/src/a.rs"), "deny")
        # Globs compare case-insensitively, as NTFS does.
        self.assertEqual(self.decide(self.policy(), "write_file", TargetFile="C:/work/repo/SRC/a.rs"), "allow")

    @unittest.skipUnless(sys.platform == "win32", "8.3 short names exist only on Windows")
    def test_short_name_alias_of_the_sandbox_is_resolved_and_denied(self) -> None:
        import ctypes
        with tempfile.TemporaryDirectory() as scratch:
            repo = Path(scratch) / "repo"
            (repo / ".agents").mkdir(parents=True)
            buffer = ctypes.create_unicode_buffer(1024)
            ctypes.windll.kernel32.GetShortPathNameW(str(repo / ".agents"), buffer, 1024)
            short = Path(buffer.value).name
            if short.lower() == ".agents":
                self.skipTest("8.3 names are disabled on this volume")
            policy = harness.agy_policy("builder", [], ["git status"], repo, [], Path(scratch) / "log")
            self.assertEqual(self.decide(policy, "write_file", TargetFile=str(repo / short / "hooks.json")), "deny")
            self.assertEqual(self.decide(policy, "write_file", TargetFile=str(repo / "NEWNAM~1.txt")), "allow")  # names nothing else

    def test_reads_inside_the_tree_and_listed_dirs_others_denied(self) -> None:
        policy = self.policy()
        self.assertEqual(self.decide(policy, "view_file", AbsolutePath="C:/work/repo/src/a.rs"), "allow")
        self.assertEqual(self.decide(policy, "view_file", AbsolutePath="E:/Cache/staging/x.json"), "allow")
        self.assertEqual(self.decide(policy, "view_file", AbsolutePath="C:/Users/me/.ssh/id_rsa"), "deny")
        for tool in ("search_web", "invoke_subagent", "open_browser_url", "call_mcp_tool"):
            self.assertEqual(self.decide(policy, tool, Url="https://x"), "deny", tool)
        self.assertEqual(self.decide(policy, "command_status", CommandId="1"), "allow")
        self.assertEqual(self.decide(policy, "view_file", Something="C:/Users/me/.ssh/id_rsa"), "deny")  # no checkable path

    def test_hook_fails_closed_and_logs(self) -> None:
        import subprocess as sp
        with tempfile.TemporaryDirectory() as scratch:
            log = Path(scratch) / "hook.log"
            policy = {**self.policy(), "log": log.as_posix()}
            policy_path = Path(scratch) / "policy.json"
            policy_path.write_text(json.dumps(policy))
            payload = json.dumps({"toolCall": {"name": "run_command", "args": {"CommandLine": "git status"}}})
            allowed = sp.run([sys.executable, "-I", str(harness.AGY_HOOK), str(policy_path)], input=payload, capture_output=True, text=True)
            self.assertEqual(json.loads(allowed.stdout)["decision"], "allow")
            missing = sp.run([sys.executable, "-I", str(harness.AGY_HOOK), str(Path(scratch) / "none.json")], input=payload, capture_output=True, text=True)
            self.assertEqual(json.loads(missing.stdout)["decision"], "deny")
            broken = sp.run([sys.executable, "-I", str(harness.AGY_HOOK), str(policy_path)], input="not json", capture_output=True, text=True)
            self.assertEqual(json.loads(broken.stdout)["decision"], "deny")
            self.assertEqual(len(log.read_text().splitlines()), 2)  # the missing policy has no log to write

    def test_sandbox_note_states_the_limits(self) -> None:
        note = harness.agy_sandbox_note(self.policy("reviewer"))
        self.assertIn("git status, cargo", note)
        self.assertIn("none: this session is read-only", note)
        self.assertIn("E:/Cache", note)

    def test_classify_agy_results(self) -> None:
        ok = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "RESULT: review"}})
        self.assertEqual(routing.classify("agy", ok, 0).outcome, "ok")
        self.assertEqual(routing.classify("agy", ok, 0).result_text, "RESULT: review")
        failed = json.dumps({"event": "result", "result": {"status": "ERROR", "error": "RESOURCE_EXHAUSTED: quota"}})
        self.assertEqual(routing.classify("agy", failed, 3).outcome, "rate_limited")
        self.assertEqual(routing.classify("agy", "", 1).outcome, "session_error")

    def test_tool_steps_counted_once_per_step(self) -> None:
        events = [{"event": "step_update", "step_update": {"step_index": 3, "step_type": "tool", "state": state}} for state in ("ACTIVE", "DONE")]
        events.append({"event": "step_update", "step_update": {"step_index": 4, "step_type": "agent_response"}})
        self.assertEqual(harness.agy_tool_steps("\n".join(json.dumps(event) for event in events)), 1)


class AutofixTests(unittest.TestCase):
    def test_builtins_keep_line_endings(self) -> None:
        from wpo.autofix import fix_end_of_file, strip_trailing_whitespace
        self.assertEqual(strip_trailing_whitespace(b"a  \r\nb\t\nc "), b"a\r\nb\nc")
        self.assertEqual(fix_end_of_file(b"a\r\nb\r\n\r\n\r\n"), b"a\r\nb\r\n")
        self.assertEqual(fix_end_of_file(b"a"), b"a\n")
        self.assertEqual(fix_end_of_file(b"\n\n"), b"")
        self.assertEqual(fix_end_of_file(b""), b"")

    def test_patterns_match_names_or_paths(self) -> None:
        from wpo.autofix import matches
        self.assertTrue(matches("src/deep/a.py", ["*.py"]))
        self.assertTrue(matches("crates/x/src/a.rs", ["crates/**/*.rs"]))
        self.assertFalse(matches("docs/a.md", ["*.py", "src/**"]))

    def test_project_overrides_one_fixer_by_name(self) -> None:
        from wpo.autofix import AutofixConfigError, configured_fixers
        from wpo.project import DEFAULTS, _merge
        config = _merge(DEFAULTS, {"autofix": {"rustfmt": {"globs": ["*.rs"], "command": ["rustfmt"]}, "ruff-check": None,
                                               "ruff-format": {"command": ["ruff", "format"]}}})
        fixers = {fixer.name: fixer for fixer in configured_fixers(config)}
        self.assertEqual(list(fixers), ["ruff-format", "trailing-whitespace", "end-of-file", "rustfmt"])
        self.assertEqual(fixers["ruff-format"].command, ("ruff", "format"))
        self.assertEqual(fixers["ruff-format"].globs, ("*.py", "*.pyi"))  # kept from the default
        for broken in ({"globs": ["*"]}, {"globs": ["*"], "builtin": "nope"}, {"globs": [], "builtin": "end-of-file"},
                       {"globs": ["*"], "command": "a string", "builtin": None}):
            with self.assertRaises(AutofixConfigError):
                configured_fixers({"autofix": {"x": broken}})

    def test_run_fixers_reports_changes_and_failures(self) -> None:
        from wpo.autofix import Fixer, run_fixers
        with tempfile.TemporaryDirectory() as scratch:
            tree = Path(scratch)
            (tree / "a.py").write_bytes(b"x = 1   \n")
            (tree / "b.bin").write_bytes(b"\0 trailing   ")
            (tree / "notes.md").write_bytes(b"keep  \n")
            upper = Fixer("upper", ("*.py",), (), (sys.executable, "-c", "import sys,pathlib\nfor f in sys.argv[1:]: p=pathlib.Path(f); p.write_bytes(p.read_bytes().upper())"), None)
            failing = Fixer("failing", ("*.py",), (), (sys.executable, "-c", "print('boom'); raise SystemExit(3)"), None)
            whitespace = Fixer("trailing-whitespace", ("*",), ("*.md",), None, "trailing-whitespace")
            reports = run_fixers(tree, ["a.py", "b.bin", "notes.md", "gone.py"], [whitespace, upper, failing], {}, 1)
            self.assertEqual((tree / "a.py").read_bytes(), b"X = 1\n")
            self.assertEqual((tree / "b.bin").read_bytes(), b"\0 trailing   ")
            self.assertEqual((tree / "notes.md").read_bytes(), b"keep  \n")
        self.assertEqual(reports, ["trailing-whitespace fixed 1 file(s): a.py", "upper fixed 1 file(s): a.py", "failing exited 3: boom"])


class ReviewBriefTests(unittest.TestCase):
    def test_symbols_and_line_numbers_from_a_rust_diff(self) -> None:
        from wpo.review_brief import DEFAULT_SYMBOL_PATTERNS, parse_diff, symbol_sites
        import re
        diff = [
            "diff --git a/src/a.rs b/src/a.rs",
            "index 1..2 100644",
            "--- a/src/a.rs",
            "+++ b/src/a.rs",
            "@@ -10,4 +10,5 @@ impl Shape {",
            "     fn keep(&self) {}",
            "-    pub fn old_name(&self) -> u32 {",
            "+    pub(crate) const fn new_name(&self) -> u32 {",
            "+        // -- not a header",
            "--- removed line that looks like a header",
            "@@ -40,2 +41,2 @@     pub fn area(&self) -> f32 {",
            "-        1.0",
            "+        2.0",
        ]
        changes = parse_diff(diff)
        self.assertEqual((changes[0].added, changes[0].removed), (3, 3))
        sites = symbol_sites(changes[0], re.compile(DEFAULT_SYMBOL_PATTERNS[".rs"]))
        self.assertEqual([(s.how, s.line) for s in sites["new_name"]], [("added", 11)])
        self.assertEqual([(s.how, s.line) for s in sites["old_name"]], [("removed", 11)])
        self.assertEqual([(s.how, s.line) for s in sites["area"]], [("changed near", 41)])
        self.assertNotIn("Shape", sites)  # an impl block is not a definition; git's rust driver names the fn instead

    def test_common_names_list_only_the_changed_files(self) -> None:
        from wpo.review_brief import describe_references
        many = {"src/a.rs": list(range(1, 30)), "src/b.rs": list(range(1, 30))}
        self.assertEqual(describe_references(many, {"src/a.rs"}, 40), "58 matches in 2 files, too common to list; in changed files: src/a.rs (29)")
        self.assertEqual(describe_references({"src/a.rs": [3, 9]}, {"src/a.rs"}, 40), "src/a.rs*: 3, 9")
        self.assertEqual(describe_references({}, set(), 40), "no references in the tree")

    def test_brief_of_a_worktree_with_untracked_files(self) -> None:
        import subprocess
        from wpo.review_brief import build_review_brief
        with tempfile.TemporaryDirectory() as scratch:
            tree = Path(scratch)
            run = lambda *arguments: subprocess.run(["git", "-C", str(tree), *arguments], check=True, capture_output=True)
            run("init", "-q", "-b", "main")
            run("config", "user.email", "t@example.com")
            run("config", "user.name", "t")
            (tree / "lib.py").write_text("def kept():\n    return 1\n")
            run("add", "-A")
            run("commit", "-q", "-m", "base")
            run("checkout", "-q", "-b", "wp/WP-1")
            # A lone carriage return inside a line must not split it.
            (tree / "new.py").write_bytes(b"def added_here():\n    return 'a\rb'\n\ndef caller():\n    return added_here()\n")
            (tree / "blob.bin").write_bytes(b"\0\1\2")
            (tree / "Cargo.lock").write_text("lock\n")
            (tree / "dir b").mkdir()
            (tree / "dir b" / "spaced b.py").write_text("def in_spaced_path():\n    pass\n")
            (tree / "empty.py").write_text("")
            (tree / "capture.txt").write_text("x" * 200)
            config = {"review_diff_exclude": ["Cargo.lock"], "review_symbol_patterns": {}, "review_reference_limit": 40,
                      "review_untracked_max_bytes": 100}
            brief = build_review_brief(tree, "main", config)
        self.assertIn("- `added_here`: added new.py:1. Used: new.py*: 1, 5", brief.full_text)
        self.assertIn("- `caller`: added new.py:4.", brief.full_text)
        self.assertIn("- blob.bin: binary", brief.full_text)
        self.assertIn("- Cargo.lock: +1 -0, excluded by review_diff_exclude", brief.full_text)
        self.assertIn("+    return 'a\rb'", brief.full_text)
        self.assertNotIn("+lock", brief.full_text)
        self.assertNotIn("## Diff", brief.index_text)
        self.assertIn("- `in_spaced_path`: added dir b/spaced b.py:1.", brief.full_text)
        self.assertIn("- empty.py (added, +0 -0)", brief.full_text)
        self.assertIn("- capture.txt: untracked, 200 bytes, over review_untracked_max_bytes", brief.full_text)

    def test_header_path_with_a_b_slash_inside(self) -> None:
        from wpo.review_brief import header_path
        self.assertEqual(header_path("diff --git a/dir b/x.rs b/dir b/x.rs"), "dir b/x.rs")
        with self.assertRaises(ValueError):
            header_path('diff --git "a/odd\\tname" "b/odd\\tname"')


class PlanTests(unittest.TestCase):
    def plan(self, tasks: list[dict]) -> list:
        return parse_plan(json.dumps({"tasks": tasks}), lambda path: path.startswith("src/"), ["cargo"])

    def task(self, task_id: str, **overrides) -> dict:
        return {"id": task_id, "title": "t", "files": ["src/a.rs"], "provides": "fn a()", "consumes": [], "check": "cargo test", "instructions": "do", **overrides}

    def test_valid_plan(self) -> None:
        tasks = self.plan([self.task("t1"), self.task("t2", consumes=["t1"], files=["src/b.rs"])])
        self.assertEqual([task.id for task in tasks], ["t1", "t2"])

    def test_invalid_plans(self) -> None:
        for tasks in ([self.task("t1", files=["other/x.rs"])], [self.task("t1", consumes=["t2"]), self.task("t2")],
                      [self.task("t1"), self.task("t1")], [self.task("t1", check="rm -rf /")], []):
            with self.assertRaises(PlanError):
                self.plan(tasks)


class SessionCommandTests(unittest.TestCase):
    def test_log_ticks_a_step_and_writes_a_dated_keyed_line(self) -> None:
        from types import SimpleNamespace
        from wpo import session_commands
        tree = Path(tempfile.mkdtemp())
        wp_dir = tree / "docs" / "plan" / "wp"
        wp_dir.mkdir(parents=True)
        (wp_dir / "WP-0001-x.md").write_text("---\nid: WP-0001\ntitle: x\nmilestone: M0\nstatus: in_progress\ndepends_on: []\nbuilder: code\nreviewer: code\nowns: [a/**]\n---\n\n## Steps\n- [x] one\n- [ ] two\n\n"
                                            "## Progress log\n- 2026-10-24 10:00 [runner] started\n\n## Review\n", encoding="utf-8")
        context = SimpleNamespace(project=SimpleNamespace(wp_dir=lambda root: root / "docs" / "plan" / "wp"))
        environment = {"WPO_WORK_PACKAGE": "WP-0001", "WPO_TREE": str(tree), "WPO_ROLE": "builder", "WPO_ROUTE": "local-test"}
        with mock.patch.dict(os.environ, environment):
            line = session_commands.log(context, "two done; next three", 2)
            with self.assertRaises(session_commands.SessionCommandError):
                session_commands.log(context, "x", 3)
        self.assertRegex(line, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} \[builder local-test\] two done; next three$")
        text = (wp_dir / "WP-0001-x.md").read_text(encoding="utf-8")
        self.assertIn("- [x] two", text)
        self.assertIn("[runner] started\n- ", text)  # entries stay consecutive

    def test_included_files_inline_whole_files_and_sections(self) -> None:
        from wpo.session_brief import BriefError, included_files
        tree = Path(tempfile.mkdtemp())
        (tree / "spec.md").write_text("# Spec\n\n## 1. Product\nx\n\n## 2. Non-negotiable rules\nrule\n\n## 3. Next\ny\n", encoding="utf-8")
        text = included_files(tree, ["spec.md#2. Non", "spec.md"])
        self.assertTrue(text.startswith("File spec.md (section 2. Non):\n## 2. Non-negotiable rules\nrule\n\nFile spec.md:\n# Spec"))
        # An @path in a prompt makes the Claude CLI attach the whole file.
        self.assertNotIn("@spec.md", text)
        with self.assertRaises(BriefError):
            included_files(tree, ["spec.md#9."])


class InboxTests(unittest.TestCase):
    ECHO_AGENT = (
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    content = json.loads(line)['message']['content']\n"
        "    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': content}), flush=True)\n")

    def test_stream_delivers_inbox_messages_then_closes(self) -> None:
        from wpo import session
        from wpo.inbox import inbox_of, post_message
        run_dir = Path(tempfile.mkdtemp())
        post_message(run_dir, "also do X")
        log = run_dir / "attempt-1-builder.claude-opus-high.log"
        with mock.patch.object(session, "LIVENESS_POLL_SECONDS", 0.2):
            watched = session.watch_process([sys.executable, "-c", self.ECHO_AGENT], dict(os.environ), "the prompt", run_dir, log, 5, False,
                                            stream_input=True, inbox=inbox_of(run_dir))
        self.assertEqual(watched.exit_code, 0)
        results = [json.loads(line)["result"] for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(results[0], "the prompt")
        self.assertTrue(results[1].endswith("also do X"))
        self.assertEqual(len(list((inbox_of(run_dir) / "delivered").glob("*.md"))), 1)
        self.assertFalse((inbox_of(run_dir) / "live.json").exists())

    def test_lane_running_wp_from_supervisor_log(self) -> None:
        from wpo.inbox import lane_running_wp
        runs = Path(tempfile.mkdtemp())
        (runs / "supervisor-ai.log").write_text("x running WP-0505 (todo) for 'code'; first route a\nx WP-0505 runner exit 0; status done\n"
                                                "x running WP-0506 (todo) for 'code'; first route a\n", encoding="utf-8")
        self.assertEqual(lane_running_wp(runs, "ai"), "WP-0506")
        (runs / "supervisor-ai.log").write_text("x running WP-0506 (todo) for 'code'\nx WP-0506 runner exit 0; status review\n", encoding="utf-8")
        self.assertIsNone(lane_running_wp(runs, "ai"))


class TranscriptTests(unittest.TestCase):
    def test_session_page_shows_calls_results_and_messages(self) -> None:
        from wpo.transcript import render_session
        directory = Path(tempfile.mkdtemp())
        log = directory / "attempt-1-builder.local-test.log"
        events = [
            {"type": "assistant", "timestamp": "2026-10-24T10:00:00Z", "message": {"id": "m1", "content": [
                {"type": "thinking", "thinking": "look at the <tests>"},
                {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "a.rs", "old_string": "x", "new_string": "y"}}]}},
            {"type": "user", "timestamp": "2026-10-24T10:00:05Z", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
            {"type": "user", "timestamp": "2026-10-24T10:00:06Z", "message": {"content": "Message from the orchestrator: wrap up"}},
            {"type": "result", "is_error": False, "result": "RESULT: paused", "usage": {}},
        ]
        log.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        (directory / (log.name + ".prompt.md")).write_text("You are the builder", encoding="utf-8")
        text = render_session(log)
        for expected in ("look at the &lt;tests&gt;", "<b>Edit</b> a.rs", "Edit result after 5 s", "wrap up", "RESULT: paused", "You are the builder"):
            self.assertIn(expected, text)


class UsageTests(unittest.TestCase):
    def test_session_log_names(self) -> None:
        from wpo.usage import SESSION_LOG
        resumed = SESSION_LOG.match("attempt-2-builder.claude-opus-high.resumed.log")
        self.assertEqual((resumed.group("stem"), resumed.group("route"), bool(resumed.group("resumed"))), ("attempt-2-builder", "claude-opus-high", True))
        self.assertEqual(SESSION_LOG.match("session.zen_glm-5.3-flash-free.log").group("route"), "zen_glm-5.3-flash-free")
        self.assertIsNone(SESSION_LOG.match("attempt-1-verify.log"))
        self.assertIsNone(SESSION_LOG.match("attempt-1-builder.claude-opus-high.log.stderr"))

    def test_claude_log_parse(self) -> None:
        from wpo.usage import read_session
        runs = Path(tempfile.mkdtemp())
        log = runs / "WP-0001" / "attempt-1-builder.claude-opus-high.log"
        log.parent.mkdir()
        usage = {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 5}
        events = [
            {"type": "user", "timestamp": "2026-10-24T10:00:00Z", "message": {"content": "go"}},
            {"type": "assistant", "timestamp": "2026-10-24T10:00:10Z", "message": {"id": "m1", "usage": usage, "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "rtk cargo test -p zm_core"}}]}},
            {"type": "user", "timestamp": "2026-10-24T10:01:10Z", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}},
            {"type": "assistant", "parent_tool_use_id": "a1", "timestamp": "2026-10-24T10:01:12Z", "message": {"id": "s1", "usage": usage}},
            {"type": "assistant", "timestamp": "2026-10-24T10:01:20Z", "message": {"id": "m2", "usage": usage, "content": []}},
            {"type": "result", "usage": {key: value * 2 for key, value in usage.items()}, "total_cost_usd": 1.5,
             "modelUsage": {"claude-opus-5-5": {"inputTokens": 20, "costUSD": 1.5, "canonicalModel": "claude-opus-5-5"}}},
        ]
        log.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        record = read_session(log, runs)
        self.assertEqual((record.wp, record.role, record.family, record.turns, record.subagent_turns), ("WP-0001", "builder", "claude", 2, 1))
        self.assertEqual(record.tokens["cache_read_input_tokens"], 2000)  # the result's total, not the streamed sum
        self.assertEqual(record.max_context, 1110)
        self.assertEqual((record.model_seconds, record.tool_seconds), (20.0, 60.0))
        self.assertEqual(record.tool_seconds_by_kind, {"cargo test": 60.0})
        self.assertEqual(record.model_usage, {"claude-opus-5-5": {"inputTokens": 20, "costUSD": 1.5}})

    def test_change_segments_merge_bursts(self) -> None:
        from wpo.usage import Change, change_segments
        changes = [Change(0, "skill", "a", "x"), Change(600, "project", "b", "y"), Change(100000, "skill", "c", "z")]
        self.assertEqual([[change.commit for change in group] for group in change_segments(changes, timedelta(minutes=30))], [["a", "b"], ["c"]])


class KillTreeTests(unittest.TestCase):
    def test_spared_subtree_can_grow_before_targets_are_enumerated(self) -> None:
        from wpo.platform import kill_tree

        root, spared, child = (mock.Mock(pid=pid) for pid in (1, 2, 3))
        spared.children.return_value = []  # The child starts after the earlier subtree snapshot.
        root.children.return_value = [spared, child]
        spared.parents.return_value = [root]
        child.parents.return_value = [spared, root]
        with mock.patch("wpo.platform.psutil.Process", side_effect={1: root, 2: spared}.__getitem__), \
                mock.patch("wpo.platform.psutil.wait_procs"):
            kill_tree(1, spared=[2])
        root.kill.assert_called_once()
        spared.kill.assert_not_called()
        child.kill.assert_not_called()

    def test_a_spared_descendant_survives_its_parent_being_killed(self) -> None:
        import subprocess
        import psutil
        from wpo.platform import kill_tree
        # The parent starts a long-lived child, as a pool supervisor starts a lane's supervisor, and prints its pid.
        parent_code = ("import subprocess, sys, time; child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                       "print(child.pid, flush=True); time.sleep(60)")
        parent = subprocess.Popen([sys.executable, "-c", parent_code], stdout=subprocess.PIPE, text=True)
        child_pid = int(parent.stdout.readline())
        try:
            kill_tree(parent.pid, spared=[child_pid])
            parent.wait(timeout=30)
            self.assertTrue(psutil.Process(child_pid).is_running())
        finally:
            kill_tree(child_pid)
            parent.stdout.close()


class ApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        from types import SimpleNamespace
        self.runs = Path(tempfile.mkdtemp())
        self.tree = Path(tempfile.mkdtemp())
        self.events: list[tuple[str, str]] = []
        project = SimpleNamespace(wp_run_dir=lambda wp_id: self.runs / wp_id, config={"command_timeout_minutes": 1})
        journal = SimpleNamespace(emit=lambda kind, message, fields=None: self.events.append((kind, message)))
        self.context = SimpleNamespace(project=project, journal=journal)
        self.wp = SimpleNamespace(id="WP-0001")

    def session(self):
        return mock.patch("wpo.approvals.session_wp", return_value=(self.wp, self.tree))

    def test_an_approved_command_runs_once_and_a_denied_one_never(self) -> None:
        from wpo.approvals import ApprovalError, decide, request, run_approved
        with self.session():
            first = request(self.context, "echo approved > out.txt", "the build needs it")
            second = request(self.context, "echo no", "curious")
        self.assertEqual((first["id"], second["id"]), ("WP-0001-A1", "WP-0001-A2"))
        self.assertEqual([kind for kind, _ in self.events], ["approval-requested", "approval-requested"])
        with self.session(), self.assertRaisesRegex(ApprovalError, "pending"):
            run_approved(self.context, "WP-0001-A1")
        decide(self.context, "WP-0001-A1", True, "")
        decide(self.context, "WP-0001-A2", False, "not needed")
        with self.session():
            self.assertEqual(run_approved(self.context, "WP-0001-A1"), 0)
            self.assertEqual((self.tree / "out.txt").read_text().strip(), "approved")
            with self.assertRaisesRegex(ApprovalError, "used"):
                run_approved(self.context, "WP-0001-A1")
            with self.assertRaisesRegex(ApprovalError, "denied"):
                run_approved(self.context, "WP-0001-A2")
        self.wp.id = "WP-0002"
        with self.session(), self.assertRaisesRegex(ApprovalError, "approved for WP-0001"):
            run_approved(self.context, "WP-0001-A1")


class FindingsLedgerTests(unittest.TestCase):
    REVIEW_ONE = """Intro text.
- **FINDING R1-F1 BLOCKING** src/a.rs:10
  Basis: criterion 2 "every garment is refit"
  Problem: the long sweater is skipped.
  Fix: include it.
FINDING R1-F2 NON-BLOCKING src/b.rs:4
  Basis: readability.
FINDING R1-F3 BLOCKING src/c.rs:1
  Basis: a future caller might pass NaN.
FINDING R0-F9 BLOCKING src/old.rs:1
ORCHESTRATOR: criterion 5 names a file WP-0002 owns; move it there.
VERDICT: CHANGES_REQUESTED"""

    def setUp(self) -> None:
        from types import SimpleNamespace
        self.runs = Path(tempfile.mkdtemp())
        self.tree = Path(tempfile.mkdtemp())
        self.context = SimpleNamespace(project=SimpleNamespace(wp_run_dir=lambda wp_id: self.runs / wp_id))
        self.wp = SimpleNamespace(id="WP-0001")
        self.run_dir = self.runs / "WP-0001"
        self.run_dir.mkdir()

    def session(self):
        return mock.patch("wpo.findings.session_wp", return_value=(self.wp, self.tree))

    def test_review_parsing_keeps_only_this_reviews_ids_and_collects_orchestrator_notes(self) -> None:
        from wpo.findings import parse_review
        parsed = parse_review(self.REVIEW_ONE, 1)
        self.assertEqual([(finding["id"], finding["severity"], finding["location"]) for finding in parsed.findings],
                         [("R1-F1", "BLOCKING", "src/a.rs:10"), ("R1-F2", "NON-BLOCKING", "src/b.rs:4"), ("R1-F3", "BLOCKING", "src/c.rs:1")])
        self.assertIn('criterion 2 "every garment is refit"', parsed.findings[0]["text"])
        self.assertNotIn("R1-F2", parsed.findings[0]["text"])
        self.assertEqual(parsed.orchestrator_notes, ["criterion 5 names a file WP-0002 owns; move it there."])

    def test_answers_rulings_and_vetoes_follow_the_protocol(self) -> None:
        from wpo.findings import answer, ledger_text, next_review_number, parse_review, record_review, unanswered
        from wpo.session_commands import SessionCommandError
        self.assertEqual(next_review_number(self.run_dir), 1)
        still_open = record_review(self.run_dir, 1, parse_review(self.REVIEW_ONE, 1), passed=False)
        self.assertEqual([finding["id"] for finding in still_open], ["R1-F1", "R1-F3"])
        self.assertEqual([finding["id"] for finding in unanswered(self.run_dir)], ["R1-F1", "R1-F3"])
        with self.session():
            with self.assertRaisesRegex(SessionCommandError, "needs no answer"):
                answer(self.context, "R1-F2", "fixed", None, "done")
            with self.assertRaisesRegex(SessionCommandError, "one ground"):
                answer(self.context, "R1-F3", "veto", "dislike", "no")
            answer(self.context, "R1-F1", "fixed", None, "src/a.rs:10 now refits the long sweater")
            self.assertIn("Every open finding is answered",
                          answer(self.context, "R1-F3", "veto", "no-basis", "a hypothetical caller; nothing passes NaN"))
        self.assertEqual(unanswered(self.run_dir), [])
        self.assertEqual(next_review_number(self.run_dir), 2)
        review_two = "RULING R1-F1 STILL-OPEN: the thin sweater is still skipped\nRULING R1-F3 VETO-ACCEPTED: hypothetical\nVERDICT: CHANGES_REQUESTED"
        still_open = record_review(self.run_dir, 2, parse_review(review_two, 2), passed=False)
        self.assertEqual([finding["id"] for finding in still_open], ["R1-F1"])
        self.assertEqual([finding["id"] for finding in unanswered(self.run_dir)], ["R1-F1"])
        self.assertIn("Review 2: STILL-OPEN: the thin sweater is still skipped", ledger_text(self.run_dir))
        for review in (3, 4):
            with self.session():
                answer(self.context, "R1-F1", "veto", "outside-wp", "the thin sweater is WP-0002's")
            record_review(self.run_dir, review, parse_review("RULING R1-F1 VETO-REJECTED: criterion 2 quoted\nVERDICT: CHANGES_REQUESTED", review), passed=False)
        with self.session(), self.assertRaisesRegex(SessionCommandError, "two vetoes"):
            answer(self.context, "R1-F1", "veto", "outside-wp", "again")
        self.assertEqual(record_review(self.run_dir, 5, parse_review("VERDICT: PASS", 5), passed=True), [])

    def test_the_agy_sandbox_allows_a_finding_answer_as_one_command(self) -> None:
        from wpo.agy_hook import decide
        policy = harness.agy_policy("builder", ["src/**"], ["wpo finding"], Path("C:/work/repo"), [], Path("C:/runs/x.agy-hook.log"))
        def call(line: str) -> str:
            return decide(policy, {"toolCall": {"name": "run_command", "args": {"CommandLine": line}}})[0]
        self.assertEqual(call("wpo finding veto R2-F1 no-basis 'quotes nothing; only a preference'"), "allow")
        self.assertEqual(call("wpo finding fixed R2-F1 'src/a.rs:3'; whoami"), "deny")



DASHBOARD_WP = """---
id: {id}
title: {title}
milestone: M1
lane: core
status: {status}
depends_on: [{depends}]
builder: code
reviewer: code-hard
owns: [src/**]
---

## Goal
Do it.

## Steps
- [x] One
- [ ] Two

## Progress log
- 2026-10-27 01:00 [builder claude-opus-high] {progress}

## Review

## Handoff
"""


def assistant_line(tokens: int, subagent: bool = False) -> str:
    return json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": tokens, "cache_read_input_tokens": 0,
                                                                  "cache_creation_input_tokens": 0}},
                       "parent_tool_use_id": "toolu_1" if subagent else None}, separators=(",", ":")) + "\n"


class DashboardProject:
    """A scratch repository with a done WP, a running one and a waiting one, plus the run state a
    supervisor, a runner and a session leave behind."""

    def __init__(self) -> None:
        from wpo.project import git
        base = Path(tempfile.mkdtemp(prefix="wpo-dashboard-"))
        self.base = base
        self.root = base / "repo"
        self.root.mkdir()
        for arguments in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            git(self.root, *arguments)
        (self.root / ".orchestration").mkdir()
        (self.root / ".orchestration" / "config.json").write_text(json.dumps({"name": "scratch"}), encoding="utf-8")
        (self.root / ".gitignore").write_text(".orchestration/runs/\n", encoding="utf-8")
        wp_dir = self.root / "docs" / "plan" / "wp"
        wp_dir.mkdir(parents=True)
        for wp_id, status, depends in (("WP-0001", "done", ""), ("WP-0002", "in_progress", "WP-0001"), ("WP-0003", "todo", "WP-0002")):
            (wp_dir / f"{wp_id}-x.md").write_text(DASHBOARD_WP.format(id=wp_id, title=f"Title {wp_id}", status=status, depends=depends,
                                                                       progress=f"progress of {wp_id}"), encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "WP-0001: merged work")
        self.machine = machine_in(base)
        self.runs = self.root / ".orchestration" / "runs"
        self.runs.mkdir()

    def write_run_state(self) -> Path:
        runs = self.runs
        (runs / "supervisor-core.state").write_text("running", encoding="utf-8")
        (runs / "supervisor-core.pid").write_text("999999", encoding="utf-8")  # no such process: the supervisor died
        (runs / "supervisor-core.log").write_text("2026-10-27 01:00:00 running WP-0002 (in_progress) for 'code'; first route claude-opus-high\n",
                                                  encoding="utf-8")
        run_dir = runs / "WP-0002"
        (run_dir / "inbox").mkdir(parents=True)
        (run_dir / "attempts.json").write_text(json.dumps({"rounds": 3, "failures": 1, "evaluations": 1, "history": [
            {"round": 2, "kind": "review requested changes", "at": "2026-10-27 00:30", "feedback": "fix it"}]}), encoding="utf-8")
        (run_dir / "findings.json").write_text(json.dumps({"reviews": 2, "findings": [
            {"id": "R2-F1", "severity": "BLOCKING", "location": "src/a.rs:3", "text": "Basis: criterion 1", "review": 2, "status": "open",
             "status_review": 2, "answer": None, "rulings": []},
            {"id": "R1-F1", "severity": "BLOCKING", "location": "src/b.rs:1", "text": "Basis: criterion 2", "review": 1, "status": "RESOLVED",
             "status_review": 2, "answer": None, "rulings": []}]}), encoding="utf-8")
        log = run_dir / "attempt-3-builder.claude-opus-high.log"
        log.write_text(assistant_line(50000) + assistant_line(300000, subagent=True) + assistant_line(120000), encoding="utf-8")
        (run_dir / "inbox" / "live.json").write_text(json.dumps({"log": str(log), "pid": os.getpid(), "started": time.time()}), encoding="utf-8")
        return log


class DashboardDataTests(unittest.TestCase):
    """The board assembled from run state, read without disturbing it."""

    def test_default_machine_loads_existing_state_with_t3_backend(self) -> None:
        from unittest import mock
        from wpo.dashboard_data import DashboardData

        scratch = DashboardProject()
        machine = MachineConfig({**scratch.machine.raw, "execution_mode": "t3"})
        with mock.patch.object(MachineConfig, "load", return_value=machine):
            snapshot = DashboardData(scratch.root).snapshot()
        self.assertTrue(snapshot["wps"])
        self.assertEqual(snapshot["native_backend"]["pending"], 0)
        self.assertIsNone(snapshot["native_backend"]["coordinator_thread_id"])

    def test_snapshot_lanes_wps_merges_and_milestones(self) -> None:
        from wpo.dashboard_data import DashboardData
        scratch = DashboardProject()
        scratch.write_run_state()
        snapshot = DashboardData(scratch.root, scratch.machine).snapshot()
        lane = next(lane for lane in snapshot["lanes"] if lane["name"] == "core")
        self.assertEqual((lane["state"], lane["recorded_state"], lane["live"], lane["current_wp"], lane["last_wp"]),
                         ("died", "running", False, None, "WP-0002"))
        self.assertEqual(lane["open_wps"], ["WP-0002", "WP-0003"])
        rows = {row["id"]: row for row in snapshot["wps"]}
        self.assertEqual(set(rows), {"WP-0002", "WP-0003"})
        running = rows["WP-0002"]
        self.assertEqual((running["steps_done"], running["steps_total"], running["rounds"], running["failures"], running["evaluations"]),
                         (1, 2, 3, 1, 1))
        self.assertEqual(running["latest_progress"], "2026-10-27 01:00 [builder claude-opus-high] progress of WP-0002")
        self.assertEqual([finding["id"] for finding in running["open_findings"]], ["R2-F1"])
        self.assertTrue(running["active"])
        session = running["session"]
        # The latest own turn counts; the subagent's larger turn does not.
        self.assertEqual((session["role"], session["route"], session["live"], session["context_tokens"], session["handoff_tokens"]),
                         ("builder", "claude-opus-high", True, 120000, 180000))
        self.assertEqual(rows["WP-0003"]["unmerged_deps"], ["WP-0002"])
        self.assertIsNone(rows["WP-0003"]["session"])
        self.assertEqual([merge["id"] for merge in snapshot["merged"]], ["WP-0001"])
        self.assertEqual(snapshot["milestones"], [{"name": "M1", "counts": {"todo": 1, "in_progress": 1, "review": 0, "changes_requested": 0,
                                                                              "blocked": 0, "done": 1}, "total": 3}])
        self.assertEqual([family["windows"] for family in snapshot["usage"]["families"]], [[], []])

    def test_detail_and_run_files_stay_inside_the_runs_directory(self) -> None:
        from wpo.dashboard_data import DashboardData, DashboardDataError
        scratch = DashboardProject()
        scratch.write_run_state()
        data = DashboardData(scratch.root, scratch.machine)
        detail = data.wp_detail("WP-0002")
        self.assertEqual([step["done"] for step in detail["steps"]], [True, False])
        self.assertEqual(detail["progress"][0]["actor"], "builder claude-opus-high")
        self.assertEqual(detail["ledger"]["history"][0]["kind"], "review requested changes")
        self.assertIn("WP-0002/attempt-3-builder.claude-opus-high.log", [log["path"] for log in detail["logs"]])
        self.assertTrue(data.run_file("WP-0002/attempts.json").is_file())
        for outside in ("../config.json", "../../docs/plan/wp/WP-0001-x.md", "WP-0002/inbox"):
            with self.assertRaises(DashboardDataError):
                data.run_file(outside)
        with self.assertRaises(DashboardDataError):
            data.wp_detail("WP-0404")

    def test_usage_windows_carry_their_ceilings(self) -> None:
        from wpo.dashboard_data import DashboardData
        scratch = DashboardProject()
        now = time.time()
        windows = [{"family": "claude", "label": "5h", "used_percent": 90.0, "window_seconds": 18000, "resets_at": now + 3600},
                   {"family": "claude", "label": "7d", "used_percent": 40.0, "window_seconds": 604800, "resets_at": now + 86400}]
        Path(scratch.machine.usage_cache).write_text(json.dumps({"fetched_at": now, "families": [
            {"family": "claude", "windows": windows, "error": None, "notes": []}]}), encoding="utf-8")
        usage = DashboardData(scratch.root, scratch.machine).snapshot()["usage"]
        claude = next(family for family in usage["families"] if family["family"] == "claude")
        self.assertEqual([(window["label"], window["used_percent"], window["ceiling"]) for window in claude["windows"]],
                         [("5h", 90.0, 85.0), ("7d", 40.0, 92.0)])
        self.assertEqual(usage["fetched_at"], now)

    def test_a_torn_ledger_keeps_its_last_whole_value(self) -> None:
        from wpo.dashboard_data import StampedCache, read_json_file
        path = Path(tempfile.mkdtemp()) / "attempts.json"
        cache = StampedCache()
        path.write_text('{"rounds": 2}', encoding="utf-8")
        self.assertEqual(cache.get(path, read_json_file, None), {"rounds": 2})
        path.write_text('{"rounds": 3, "fail', encoding="utf-8")  # caught mid-write
        self.assertEqual(cache.get(path, read_json_file, None), {"rounds": 2})
        path.write_text('{"rounds": 3}', encoding="utf-8")
        self.assertEqual(cache.get(path, read_json_file, None), {"rounds": 3})
        path.unlink()
        self.assertIsNone(cache.get(path, read_json_file, None))

    def test_torn_ledger_retries_when_repair_reuses_the_previous_file_stamp(self) -> None:
        from wpo.dashboard_data import StampedCache, read_json_file

        path = Path(tempfile.mkdtemp()) / "attempts.json"
        cache = StampedCache()
        with mock.patch("wpo.dashboard_data.file_stamp", side_effect=[(1, 13), (2, 18), (1, 13)]):
            path.write_text('{"rounds": 2}', encoding="utf-8")
            self.assertEqual(cache.get(path, read_json_file, None), {"rounds": 2})
            path.write_text('{"rounds": 3, "fail', encoding="utf-8")
            self.assertEqual(cache.get(path, read_json_file, None), {"rounds": 2})
            path.write_text('{"rounds": 3}', encoding="utf-8")
            self.assertEqual(cache.get(path, read_json_file, None), {"rounds": 3})

    def test_session_logs_and_the_journal_are_followed_by_whole_lines(self) -> None:
        from wpo.dashboard_data import SessionLogTracker
        from wpo.events import JournalReader
        directory = Path(tempfile.mkdtemp())
        log = directory / "attempt-1-builder.claude-opus-high.log"
        log.write_text(assistant_line(1000) + assistant_line(2000)[:20], encoding="utf-8")  # the second turn is half written
        tracker = SessionLogTracker()
        self.assertEqual(tracker.follow(log).context_tokens, 1000)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(assistant_line(2000)[20:] + '{"type":"system","subtype":"compact_boundary"}\n' + assistant_line(900))
        state = tracker.follow(log)
        self.assertEqual((state.context_tokens, state.largest_context, state.compactions), (900, 2000, 1))

        journal = Journal(directory)
        journal.emit("wp-done", "first")
        reader = JournalReader(journal)
        with journal.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write('{"time": "x", "kind": "wp-blocked"')  # being appended
        self.assertEqual([(event["index"], event["message"]) for event in reader.read_new()], [(0, "first")])
        with journal.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(', "severity": "attention", "message": "second"}\n')
        self.assertEqual([(event["index"], event["kind"]) for event in reader.read_new()], [(1, "wp-blocked")])
        self.assertEqual(reader.read_new(), [])


class DashboardLifecycleTests(unittest.TestCase):
    """The cheap check every wpo command runs: one server per project, started once, replaced when
    its code is stale, never started against the operator's stop or in a launch loop."""

    def setUp(self) -> None:
        from wpo.project import Project
        self.scratch = DashboardProject()
        self.project = Project.locate(self.scratch.root)
        self.machine = MachineConfig({**self.scratch.machine.raw, "dashboard": {**DEFAULTS["dashboard"], "autostart": True}})
        self.spawned: list[list[str]] = []
        self.terminated: list[int] = []
        # The spawned "server" is this test process: alive, a python, and not to be killed.
        spawn = mock.patch("wpo.dashboard.spawn_detached", side_effect=lambda arguments, cwd, **_: self.spawned.append(list(arguments)) or os.getpid())
        terminate = mock.patch("wpo.dashboard.terminate", side_effect=self.terminated.append)
        version = mock.patch("wpo.dashboard.code_version", side_effect=lambda: self.version)
        self.version = "v1"
        for patcher in (spawn, terminate, version):
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = mock.patch.dict(os.environ, {"WPO_DASHBOARD_AUTOSTART": "1"})
        environment.start()
        self.addCleanup(environment.stop)

    def ensure(self, explicit: bool = False) -> str:
        from wpo.dashboard import ensure_running
        return ensure_running(self.project, self.machine, explicit)

    def test_one_launch_then_running_until_the_code_changes(self) -> None:
        from wpo.dashboard import read_state, write_state
        self.assertEqual(self.ensure(), "started")
        self.assertEqual(self.spawned[0][-2:], ["dashboard", "--detach-server"])
        self.assertEqual(read_state(self.project)["phase"], "launched")
        self.assertEqual(self.ensure(), "running")
        self.assertEqual(len(self.spawned), 1)
        # The intermediate recorded its server (this process); then the skill's code changes.
        write_state(self.project, {**read_state(self.project), "phase": "serving"})
        self.version = "v2"
        self.assertEqual(self.ensure(), "started")
        self.assertEqual((self.terminated, len(self.spawned)), ([os.getpid()], 2))

    def test_autostart_off_and_the_operators_stop_are_respected(self) -> None:
        self.machine = MachineConfig({**self.machine.raw, "dashboard": {**self.machine.raw["dashboard"], "autostart": False}})
        self.assertEqual(self.ensure(), "autostart is off")
        self.machine = MachineConfig({**self.machine.raw, "dashboard": {**self.machine.raw["dashboard"], "autostart": True}})
        (self.project.runs_dir / "dashboard.stopped").write_text("stopped", encoding="utf-8")
        self.assertIn("stopped by the operator", self.ensure())
        with mock.patch.dict(os.environ, {"WPO_DASHBOARD_AUTOSTART": "0"}):
            (self.project.runs_dir / "dashboard.stopped").unlink()
            self.assertEqual(self.ensure(), "autostart is off")
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.ensure(explicit=True), "started")

    def test_a_dead_or_failed_launch_waits_before_trying_again(self) -> None:
        from wpo.dashboard import write_state
        write_state(self.project, {"phase": "starting", "pid": 999999, "created": 0, "launched_at": time.time(), "code": "v1"})
        self.assertEqual(self.ensure(), "a launch is under way")
        write_state(self.project, {"phase": "failed", "pid": 999999, "failed_at": time.time(), "error": "port in use", "code": "v1"})
        self.assertIn("port in use", self.ensure())
        self.assertEqual(self.spawned, [])
        write_state(self.project, {"phase": "failed", "pid": 999999, "failed_at": time.time() - 3600, "error": "port in use", "code": "v1"})
        self.assertEqual(self.ensure(), "started")

    def test_a_reused_pid_is_not_taken_for_the_server(self) -> None:
        from wpo.dashboard import write_state
        # This process is alive and a python, but it was not created when the state says.
        write_state(self.project, {"phase": "serving", "pid": os.getpid(), "created": 12345.0, "code": "v1"})
        self.assertEqual(self.ensure(), "started")
        self.assertEqual(self.terminated, [])

    def test_a_concurrent_launch_is_left_to_its_launcher(self) -> None:
        import threading
        from wpo.platform import NamedMutex
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            mutex = NamedMutex(self.project.mutex_name("dashboard-start"))
            mutex.acquire()
            held.set()
            release.wait(10)
            mutex.release()

        holder = threading.Thread(target=hold)
        holder.start()
        held.wait(10)
        try:
            self.assertEqual(self.ensure(), "another process is starting it")
        finally:
            release.set()
            holder.join()
        self.assertEqual(self.spawned, [])

    def test_autostart_logs_failures_instead_of_failing_the_command(self) -> None:
        from wpo.dashboard import LOG_FILE, autostart
        with mock.patch("wpo.dashboard.ensure_running", side_effect=OSError("disk gone")):
            autostart(self.project, self.machine)
        self.assertIn("autostart failed: OSError: disk gone", (self.project.runs_dir / LOG_FILE).read_text(encoding="utf-8"))


class DashboardServerTests(unittest.TestCase):
    """The HTTP surface: the board as JSON, the page, and nothing for other hosts or outside the runs."""

    def setUp(self) -> None:
        import threading
        from wpo.dashboard_data import DashboardData
        from wpo.dashboard_server import DashboardServer, Hub
        from wpo.project import Project
        scratch = DashboardProject()
        scratch.write_run_state()
        project = Project.locate(scratch.root)
        self.server = DashboardServer(("127.0.0.1", 0), Hub(DashboardData(scratch.root, scratch.machine), project))
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path: str, host: str | None = None) -> tuple[int, bytes]:
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=30)
        connection.request("GET", path, headers={"Host": host} if host else {})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def test_state_page_and_refusals(self) -> None:
        status, body = self.get("/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual([row["id"] for row in state["snapshot"]["wps"]], ["WP-0002", "WP-0003"])
        self.assertIsNone(state["error"])
        status, body = self.get("/")
        self.assertEqual((status, b"/dashboard.js" in body), (200, True))
        self.assertEqual(self.get("/api/wp/WP-0002")[0], 200)
        self.assertEqual(self.get("/raw/WP-0002/attempts.json")[0], 200)
        self.assertEqual(self.get("/raw/..%2F..%2F.gitignore")[0], 404)
        self.assertEqual(self.get("/raw/WP-0002/%00.log")[0], 404)
        self.assertEqual(self.get("/api/state", host="attacker.example")[0], 403)


class DashboardRefusalTransportTests(unittest.TestCase):
    def handler(self, length: int, payload: bytes):
        import io
        from email.message import Message
        from wpo.dashboard_server import DashboardHandler

        handler = object.__new__(DashboardHandler)
        handler.headers = Message()
        handler.headers["Content-Length"] = str(length)
        handler.rfile = io.BytesIO(payload)
        handler.connection = mock.Mock()
        handler.connection.gettimeout.return_value = 60
        handler.action_refusal = mock.Mock(return_value=(403, "refused"))
        handler.send_action_result = mock.Mock()
        handler.run_action = mock.Mock()
        return handler

    def test_small_refused_body_is_discarded_without_parsing_or_action(self) -> None:
        handler = self.handler(9, b"{not-json")
        handler.do_POST()
        self.assertEqual(handler.rfile.tell(), 9)
        handler.send_action_result.assert_called_once_with(403, "refused")
        handler.run_action.assert_not_called()

    def test_oversized_or_chunked_refused_body_is_not_drained(self) -> None:
        from wpo.dashboard_server import MAX_ACTION_BYTES

        for length, chunked in ((MAX_ACTION_BYTES + 1, False), (9, True)):
            handler = self.handler(length, b"{not-json")
            if chunked:
                handler.headers["Transfer-Encoding"] = "chunked"
            handler.do_POST()
            self.assertEqual(handler.rfile.tell(), 0)
            handler.send_action_result.assert_called_once_with(403, "refused")
            handler.run_action.assert_not_called()

    def test_refused_body_timeout_still_returns_refusal(self) -> None:
        handler = self.handler(9, b"")
        handler.rfile = mock.Mock()
        handler.rfile.read1.side_effect = TimeoutError
        handler.do_POST()
        handler.send_action_result.assert_called_once_with(403, "refused")
        timeouts = handler.connection.settimeout.call_args_list
        self.assertTrue(0 < timeouts[0].args[0] <= 1)
        self.assertEqual(timeouts[-1], mock.call(60))
        handler.run_action.assert_not_called()

    def test_slow_drip_cannot_extend_the_refusal_drain_deadline(self) -> None:
        handler = self.handler(9, b"")
        handler.rfile = mock.Mock()
        handler.rfile.read1.return_value = b"x"
        with mock.patch("wpo.dashboard_server.time.monotonic", side_effect=[100, 100, 100.4, 101.1]):
            handler.do_POST()
        self.assertEqual(handler.rfile.read1.call_count, 2)
        handler.send_action_result.assert_called_once_with(403, "refused")
        self.assertEqual(handler.connection.settimeout.call_args_list[-1], mock.call(60))
        handler.run_action.assert_not_called()

    def test_refused_method_discards_body_and_closes_connection(self) -> None:
        import io

        handler = self.handler(9, b"{not-json")
        handler.path = "/api/action/message"
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = io.BytesIO()
        handler.refuse_method()
        self.assertTrue(handler.close_connection)
        self.assertEqual(handler.rfile.tell(), 9)
        handler.send_response.assert_called_once_with(405)
        handler.run_action.assert_not_called()


class DashboardActionTests(unittest.TestCase):
    """The page's actions: POST only, from this server's own page (token, origin, host), each routed
    to the user_actions function `wpo` itself would use."""

    def setUp(self) -> None:
        import threading
        from wpo.dashboard_data import DashboardData
        from wpo.dashboard_server import DashboardServer, Hub
        from wpo.project import Project
        scratch = DashboardProject()
        scratch.write_run_state()
        self.hub = Hub(DashboardData(scratch.root, scratch.machine), Project.locate(scratch.root))
        self.server = DashboardServer(("127.0.0.1", 0), self.hub)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.calls: list[tuple[str, tuple]] = []
        for name in ("unblock", "answer_approval", "send_message"):
            patcher = mock.patch(f"wpo.user_actions.{name}", side_effect=lambda *arguments, name=name: self.calls.append((name, arguments[1:])) or f"{name} done")
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(self, method: str, path: str, body: object = None, headers: dict | None = None, raw: bytes | None = None) -> tuple[int, dict | bytes]:
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=30)
        sent = {"Origin": self.origin, "X-WPO-Token": self.hub.action_token, "Content-Type": "application/json", **(headers or {})}
        payload = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
        connection.request(method, path, body=payload, headers={key: value for key, value in sent.items() if value is not None})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        try:
            return response.status, json.loads(data)
        except ValueError:
            return response.status, data

    def test_the_page_carries_the_token(self) -> None:
        import http.client
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=30)
        connection.request("GET", "/")
        page = connection.getresponse().read()
        connection.close()
        self.assertIn(self.hub.action_token.encode("ascii"), page)
        self.assertNotIn(b"__WPO_ACTION_TOKEN__", page)

    def test_cross_site_and_tokenless_requests_are_refused(self) -> None:
        body = {"wp": "WP-0002", "text": "hi"}
        refusals = [
            {"X-WPO-Token": None},
            {"X-WPO-Token": "wrong"},
            {"Origin": None},
            {"Origin": "http://attacker.example"},
            {"Origin": "null"},
            {"Host": "attacker.example"},
        ]
        for headers in refusals:
            with self.subTest(headers=headers):
                status, answer = self.request("POST", "/api/action/message", body, headers)
                self.assertEqual(status, 403)
                self.assertFalse(answer["ok"])
        self.assertEqual(self.calls, [])

    def test_actions_are_post_only(self) -> None:
        for method in ("GET", "PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                status, _ = self.request(method, "/api/action/unblock", {"wp": "WP-0003"})
                self.assertEqual(status, 405)
        self.assertEqual(self.calls, [])

    def test_each_action_reaches_its_operation(self) -> None:
        cases = [
            ("unblock", {"wp": "WP-0003", "note": "use the new API", "status": "todo"}, ("unblock", ("WP-0003", "use the new API", "todo"))),
            ("unblock", {"wp": "WP-0003"}, ("unblock", ("WP-0003", "", None))),
            ("approve", {"id": "WP-0002-A1", "note": "once"}, ("answer_approval", ("WP-0002-A1", True, "once"))),
            ("deny", {"id": "WP-0002-A1", "reason": "not needed"}, ("answer_approval", ("WP-0002-A1", False, "not needed"))),
            ("message", {"wp": "WP-0002", "text": "also do X"}, ("send_message", ("WP-0002", "also do X"))),
            ("message", {"wp": None, "text": "a new tool"}, ("send_message", (None, "a new tool"))),
        ]
        for action, body, expected in cases:
            with self.subTest(action=action, body=body):
                self.calls.clear()
                status, answer = self.request("POST", f"/api/action/{action}", body)
                self.assertEqual((status, answer), (200, {"ok": True, "message": f"{expected[0]} done"}))
                self.assertEqual(self.calls, [expected])

    def test_bad_requests_and_failed_actions_report_their_error(self) -> None:
        from wpo import user_actions
        self.assertEqual(self.request("POST", "/api/action/launch", {"x": 1})[0], 404)
        self.assertEqual(self.request("POST", "/api/action/message", raw=b"{not json")[0], 400)
        self.assertEqual(self.request("POST", "/api/action/message", ["a"])[0], 400)
        self.assertEqual(self.request("POST", "/api/action/message", {"wp": "WP-0002"}, {"Content-Type": "text/plain"})[0], 400)
        self.assertEqual(self.request("POST", "/api/action/deny", {"id": "WP-0002-A1"})[0], 400)  # no reason
        self.assertEqual(self.request("POST", "/api/action/message", {"wp": 7, "text": "x"})[0], 400)
        self.assertEqual(self.calls, [])
        for error, status in ((user_actions.UnknownTargetError("no WP-9"), 404), (user_actions.ActionConflictError("running"), 409),
                              (user_actions.UserActionError("WP-0002 is in_progress"), 400)):
            with self.subTest(error=error), mock.patch("wpo.user_actions.unblock", side_effect=error):
                self.assertEqual(self.request("POST", "/api/action/unblock", {"wp": "WP-0002"}), (status, {"ok": False, "error": str(error)}))


class UserActionTests(unittest.TestCase):
    """The operations behind the page's actions, on a scratch repository with lanes mocked."""

    def setUp(self) -> None:
        from wpo.context import Context
        from wpo.project import git
        self.scratch = DashboardProject()
        wp_file = self.scratch.root / "docs" / "plan" / "wp" / "WP-0003-x.md"
        wp_file.write_text(wp_file.read_text(encoding="utf-8").replace("status: todo", "status: blocked")
                           .replace("## Steps", "## Context\nBuilt on WP-0002.\n\n## Steps"), encoding="utf-8")
        git(self.scratch.root, "commit", "-q", "-am", "WP-0003 blocked")
        self.context = Context.load(self.scratch.root, self.scratch.machine)
        self.resumed: list[list[str]] = []
        resume = mock.patch("wpo.lanes.resume", side_effect=lambda context, dry_run, targets: self.resumed.append(list(targets)) or print("core: started"))
        resume.start()
        self.addCleanup(resume.stop)
        self.spawned: list[str] = []

    def events(self) -> list[dict]:
        return self.context.journal.read(0)

    def head_subject(self) -> str:
        from wpo.project import git
        return git(self.scratch.root, "log", "-1", "--format=%s").stdout.strip()

    def spawn(self, context, placement_id: str) -> int:
        self.spawned.append(placement_id)
        return 4242

    def test_unblock_notes_resets_commits_releases_and_resumes(self) -> None:
        from wpo import lanes, wpfile
        lanes.hold(self.context, "core", "waiting on the user")
        report = user_actions_module().unblock(self.context, "WP-0003", "Use the v2 format.\nIt is in docs/v2.md.")
        wp = wpfile.find(self.context.project.wp_dir(), "WP-0003")
        self.assertEqual(wp.status, "todo")
        context_lines = wp.section("Context")
        self.assertRegex(context_lines[1], r"^- \d{4}-\d{2}-\d{2} \d{2}:\d{2} User note: Use the v2 format\.$")
        self.assertEqual(context_lines[2], "  It is in docs/v2.md.")
        self.assertEqual(self.head_subject(), "WP-0003: unblocked by the user (blocked -> todo)")
        self.assertNotIn("core", lanes.read_holds(self.context))
        self.assertEqual(self.resumed, [["core"]])
        self.assertIn("core: started", report)
        unblocked = [event for event in self.events() if event["kind"] == "wp-unblocked"]
        self.assertEqual((unblocked[0]["wp"], unblocked[0]["text"]), ("WP-0003", "Use the v2 format.\nIt is in docs/v2.md."))

    def test_unblock_in_a_worktree_edits_the_worktree_copy_only(self) -> None:
        from wpo import wpfile
        worktree_copy = self.context.project.wp_dir(self.context.project.worktree("WP-0003")) / "WP-0003-x.md"
        worktree_copy.parent.mkdir(parents=True)
        worktree_copy.write_text((self.context.project.wp_dir() / "WP-0003-x.md").read_text(encoding="utf-8").replace("status: blocked", "status: review"),
                                 encoding="utf-8")
        before = self.head_subject()
        user_actions_module().unblock(self.context, "WP-0003", "rerun the review")
        self.assertEqual(wpfile.parse(worktree_copy).status, "review")  # blocked in review stays in review
        self.assertIn("User note: rerun the review", worktree_copy.read_text(encoding="utf-8"))
        self.assertEqual(wpfile.find(self.context.project.wp_dir(), "WP-0003").status, "blocked")
        self.assertEqual(self.head_subject(), before)

    def test_unblock_refuses_work_that_is_not_waiting(self) -> None:
        actions = user_actions_module()
        with self.assertRaisesRegex(actions.UserActionError, "in_progress"):
            actions.unblock(self.context, "WP-0002", "")
        with self.assertRaises(actions.UnknownTargetError):
            actions.unblock(self.context, "WP-0404", "")
        self.assertEqual(self.resumed, [])

    def test_a_live_session_gets_the_message_and_nothing_is_placed(self) -> None:
        self.scratch.write_run_state()  # WP-0002 has a live session (this process)
        outcome = user_actions_module().send_message(self.context, "WP-0002", "also cover the empty case", spawn=self.spawn)
        self.assertIn("delivered", outcome)
        inbox = self.context.project.runs_dir / "WP-0002" / "inbox"
        self.assertEqual([path.read_text(encoding="utf-8") for path in inbox.glob("*.md")], ["also cover the empty case"])
        self.assertEqual(self.spawned, [])
        message = [event for event in self.events() if event["kind"] == "user-message"]
        self.assertEqual((message[0]["wp"], message[0]["text"]), ("WP-0002", "also cover the empty case"))

    def test_without_a_live_session_a_placement_agent_is_started(self) -> None:
        actions = user_actions_module()
        for wp_id in ("WP-0001", "WP-0003", None):  # done, open without a session, no WP
            with self.subTest(wp=wp_id):
                self.spawned.clear()
                actions.send_message(self.context, wp_id, f"request about {wp_id}", spawn=self.spawn)
                self.assertEqual(len(self.spawned), 1)
                request = json.loads((actions.placement_dir(self.context, self.spawned[0]) / actions.REQUEST_FILE).read_text(encoding="utf-8"))
                self.assertEqual((request["wp"], request["text"]), (wp_id, f"request about {wp_id}"))
                queued = [event for event in self.events() if event["kind"] == "user-message-queued"][-1]
                self.assertEqual((queued.get("wp"), queued["placement"], queued["text"]), (wp_id, self.spawned[0], f"request about {wp_id}"))
        with self.assertRaises(actions.UserActionError):
            actions.send_message(self.context, "WP-0003", "   ", spawn=self.spawn)

    def test_placement_runs_on_code_hard_without_free_or_local_routes(self) -> None:
        actions = user_actions_module()
        families = actions.placement_families(self.context)
        self.assertTrue(families)
        cost_classes = self.context.router.policy["family_cost_class"]
        self.assertTrue(all(cost_classes[family] not in ("free", "local") for family in families))
        self.assertEqual(actions.PLACEMENT_SPEC, "code-hard")

    def placement(self, wp_id: str | None, answer: str, write: dict[str, str] | None = None) -> tuple[int, str]:
        actions = user_actions_module()
        actions.send_message(self.context, wp_id, "add a replay viewer", spawn=self.spawn)
        placement_id = self.spawned[-1]
        seen: dict = {}

        def session(context, spec, prompt, access, write_paths, families, cwd, run_dir, event_fields):
            seen.update(spec=spec, access=access, write_paths=write_paths, families=families, prompt=prompt)
            for relative, text in (write or {}).items():
                (self.scratch.root / relative).write_text(text, encoding="utf-8")
            return SimpleNamespaceResult(answer)

        with mock.patch("wpo.user_actions.dispatch_session", side_effect=session):
            code = actions.place_request(self.context, placement_id)
        self.assertEqual((seen["spec"], seen["access"], seen["write_paths"]), ("code-hard", "docs", ["docs/plan/wp/**"]))
        self.assertIn("add a replay viewer", seen["prompt"])
        return code, placement_id

    def test_a_request_about_a_done_wp_placed_in_a_blocked_open_wp_unblocks_it(self) -> None:
        from wpo import wpfile
        answer = 'Weighed WP-0003.\n```json\n{"decision": "existing", "wp": "WP-0003", "why": "it builds the viewer", "pointer": "see src/view.rs"}\n```'
        code, placement_id = self.placement("WP-0001", answer)
        self.assertEqual(code, 0)
        wp = wpfile.find(self.context.project.wp_dir(), "WP-0003")
        self.assertEqual(wp.status, "todo")
        text = "\n".join(wp.section("Context"))
        for expected in ("User note: add a replay viewer", "(Asked about WP-0001; placed here: it builds the viewer)", "(Placement pointer: see src/view.rs)"):
            self.assertIn(expected, text)
        self.assertEqual(self.resumed, [["core"]])
        placed = [event for event in self.events() if event["kind"] == "user-message-placed"]
        self.assertEqual((placed[0]["wp"], placed[0]["asked_about"], placed[0]["placement"], placed[0]["severity"]),
                         ("WP-0003", "WP-0001", placement_id, "attention"))

    def test_a_drafted_wp_is_committed_with_the_request(self) -> None:
        from wpo import wpfile
        draft = DASHBOARD_WP.format(id="WP-0004", title="Replay viewer", status="todo", depends="", progress="drafted").replace(
            "## Steps", "## Context\nFrom a user request.\n\n## Steps")
        answer = '```json\n{"decision": "new", "wp": "WP-0004", "file": "docs/plan/wp/WP-0004-replay.md", "why": "no open WP fits"}\n```'
        code, _ = self.placement(None, answer, {"docs/plan/wp/WP-0004-replay.md": draft})
        self.assertEqual(code, 0)
        self.assertEqual(self.head_subject(), f"WP-0004: drafted from a user request (placement {self.spawned[-1]})")
        self.assertIn("User note: add a replay viewer", "\n".join(wpfile.find(self.context.project.wp_dir(), "WP-0004").section("Context")))
        self.assertIn("WP-0004", {wp.id for wp in self.context.main_wps()})
        self.assertEqual([event["wp"] for event in self.events() if event["kind"] == "user-message-placed"], ["WP-0004"])

    def test_an_unusable_decision_is_journaled_with_the_text(self) -> None:
        for answer in ("I could not decide.", '```json\n{"decision": "existing", "wp": "WP-0001", "why": "done one"}\n```'):
            with self.subTest(answer=answer):
                code, placement_id = self.placement(None, answer)
                self.assertEqual(code, 1)
                unplaced = [event for event in self.events() if event["kind"] == "user-message-unplaced"][-1]
                self.assertEqual((unplaced["placement"], unplaced["text"], unplaced["severity"]), (placement_id, "add a replay viewer", "attention"))


class SimpleNamespaceResult:
    """What dispatch_session returns, as far as a placement reads it."""

    def __init__(self, text: str) -> None:
        self.result_text = text
        self.route = {"id": "claude-opus-high"}
        self.fallback_note = ""


def user_actions_module():
    from wpo import user_actions
    return user_actions


class BringMainInAgainTests(unittest.TestCase):
    """Under the mutex, a branch verified on an older main fast-forwards only when main's new
    commits reach none of its packages (read from the merged tree)."""

    def setUp(self) -> None:
        from wpo.project import git
        self.git = git
        base = Path(tempfile.mkdtemp())
        self.root, self.tree = base / "repo", base / "wp"
        self.root.mkdir()
        for arguments in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            git(self.root, *arguments)
        (self.root / "a.rs").write_text("a\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        git(self.root, "worktree", "add", "-q", "-b", "wp/WP-X", str(self.tree), "main")
        (self.tree / "a.rs").write_text("a changed\n", encoding="utf-8")
        git(self.tree, "commit", "-q", "-am", "wp")

    def integration(self):
        from types import SimpleNamespace
        from wpo.integrate import Integration
        integration = Integration.__new__(Integration)
        integration.worktree, integration.wp_id, integration.main = self.tree, "WP-X", "main"
        integration.context = mock.Mock()
        integration.log_path = self.tree.parent / "integrate.log"
        integration.project = SimpleNamespace(config={"check_scope": {"kind": "cargo"}, "verify_neutral_paths": [], "lockfile_reconcilers": []})
        return integration

    def main_changes(self, name: str) -> None:
        (self.root / name).write_text("main\n", encoding="utf-8")
        self.git(self.root, "add", "-A")
        self.git(self.root, "commit", "-q", "-m", f"main changes {name}")

    @staticmethod
    def scope_by_file(config, tree, paths):
        from wpo.scope import Scope
        # Each file is its own package; package a depends on every shared*.rs.
        reached = set()
        for path in paths:
            reached |= {"a", "shared"} if path.startswith("shared") else {path.removesuffix(".rs")}
        return Scope(tuple(sorted(reached)), "")

    def test_main_unmoved_stands(self) -> None:
        from wpo import integrate
        self.assertEqual(self.integration().bring_main_in_again(verify_under_mutex=False), integrate.MAIN_UNCHANGED)

    def test_disjoint_main_changes_merge_without_verifying(self) -> None:
        from wpo import integrate
        self.main_changes("b.rs")
        with mock.patch.object(integrate, "scope_for", side_effect=self.scope_by_file):
            self.assertEqual(self.integration().bring_main_in_again(verify_under_mutex=False), integrate.MAIN_UNCHANGED)
        self.assertEqual(self.git(self.tree, "merge-base", "--is-ancestor", "main", "HEAD", check=False).returncode, 0)

    def test_overlapping_main_changes_need_verifying(self) -> None:
        from wpo import integrate
        self.main_changes("shared.rs")
        with mock.patch.object(integrate, "scope_for", side_effect=self.scope_by_file):
            self.assertEqual(self.integration().bring_main_in_again(verify_under_mutex=False), integrate.MAIN_MOVED)
            integration = self.integration()
            with mock.patch.object(integration, "verify", return_value=False):
                self.main_changes("shared2.rs")
                self.assertEqual(integration.bring_main_in_again(verify_under_mutex=True), integrate.EXIT_VERIFY_FAILED)


class MatchesMainTests(unittest.TestCase):
    def test_only_paths_equal_to_mains_current_version_match(self) -> None:
        from wpo.commands import changed_paths, matches_main
        from wpo.project import git
        tree = Path(tempfile.mkdtemp()) / "repo"
        tree.mkdir()
        for arguments in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            git(tree, *arguments)
        (tree / "shared.toml").write_text("old\n", encoding="utf-8")
        (tree / "gone.txt").write_text("x\n", encoding="utf-8")
        git(tree, "add", "-A")
        git(tree, "commit", "-q", "-m", "base")
        git(tree, "checkout", "-q", "-b", "wp")
        git(tree, "checkout", "-q", "main")
        (tree / "shared.toml").write_text("new\n", encoding="utf-8")
        (tree / "gone.txt").unlink()
        git(tree, "add", "-A")
        git(tree, "commit", "-q", "-m", "main moves")
        git(tree, "checkout", "-q", "wp")
        (tree / "shared.toml").write_text("new\n", encoding="utf-8")
        (tree / "gone.txt").unlink()
        (tree / "own.rs").write_text("fn main() {}\n", encoding="utf-8")
        changed = changed_paths(tree, "main")
        self.assertEqual(changed, ["gone.txt (deleted)", "own.rs", "shared.toml"])
        self.assertEqual([path for path in changed if not matches_main(tree, "main", path)], ["own.rs"])


class CheckScopeTests(unittest.TestCase):
    CONFIG = {"check_scope": {"kind": "cargo"}, "verify_neutral_paths": ["docs/**"]}
    DIRECTORIES = {"core": PurePosixPath("crates/core"), "app": PurePosixPath("crates/app"), "nested": PurePosixPath("crates/app/nested")}
    DEPENDENTS = {"core": {"app"}, "app": set(), "nested": set()}

    def scope(self, changed: list[str]):
        from wpo import scope
        with mock.patch.object(scope, "cargo_packages", return_value=(self.DIRECTORIES, self.DEPENDENTS)):
            return scope.cargo_scope(Path("."), changed, self.CONFIG["verify_neutral_paths"])

    def test_a_changed_package_reaches_its_dependents(self) -> None:
        self.assertEqual(self.scope(["crates/core/src/lib.rs"]).packages, ("app", "core"))

    def test_the_deepest_member_owns_a_path(self) -> None:
        self.assertEqual(self.scope(["crates/app/nested/src/lib.rs (deleted)"]).packages, ("nested",))

    def test_a_path_outside_every_package_checks_the_whole_workspace(self) -> None:
        self.assertIsNone(self.scope(["Cargo.toml", "crates/core/src/lib.rs"]).packages)

    def test_neutral_paths_reach_nothing_and_drop_workspace_commands(self) -> None:
        from wpo.scope import scoped_commands
        scope = self.scope(["docs/plan.md"])
        self.assertEqual(scope.packages, ())
        self.assertEqual(scoped_commands(self.CONFIG, ["cargo fmt --all --check", "cargo test --workspace"], scope), ["cargo fmt --all --check"])

    def test_the_workspace_flag_becomes_one_selection_per_package(self) -> None:
        from wpo.scope import Scope, scoped_commands
        commands = scoped_commands(self.CONFIG, ["cargo clippy --workspace --all-targets -- -D warnings", "cargo test --workspace-ish"], Scope(("app", "core"), ""))
        self.assertEqual(commands, ["cargo clippy -p app -p core --all-targets -- -D warnings", "cargo test --workspace-ish"])

    def test_changed_only_commands_select_the_changed_packages_without_dependents(self) -> None:
        from wpo.scope import scoped_commands
        config = {**self.CONFIG, "check_scope": {**self.CONFIG["check_scope"], "changed_only": [r"--features install-tests"]}}
        scope = self.scope(["crates/core/src/lib.rs"])
        commands = scoped_commands(config, ["cargo test --workspace", "cargo test --workspace --features install-tests"], scope)
        self.assertEqual(commands, ["cargo test -p app -p core", "cargo test -p core --features install-tests"])

    def test_owns_globs_select_the_packages_they_reach_into(self) -> None:
        from wpo import scope
        with mock.patch.object(scope, "cargo_packages", return_value=(self.DIRECTORIES, self.DEPENDENTS)):
            self.assertEqual(scope.owned_packages(Path("."), ["crates/core/src/**", "docs/x.md"]), ("core",))
            self.assertEqual(scope.owned_packages(Path("."), ["crates/**"]), ("app", "core", "nested"))
            self.assertEqual(scope.reached_packages(Path("."), ("core",)), ("app", "core"))

    def test_a_mentioned_path_reaches_only_the_packages_naming_it(self) -> None:
        from wpo import scope
        tree = Path(tempfile.mkdtemp())
        for name in ("core", "app"):
            (tree / "crates" / name / "src").mkdir(parents=True)
        (tree / "crates" / "core" / "src" / "lib.rs").write_text('const X: &str = include_str!("../../../docs/gates/x.ron");', encoding="utf-8")
        (tree / "crates" / "app" / "src" / "lib.rs").write_text("fn main() {}", encoding="utf-8")
        directories = {"core": PurePosixPath("crates/core"), "app": PurePosixPath("crates/app")}
        with mock.patch.object(scope, "cargo_packages", return_value=(directories, {"core": {"app"}, "app": set()})):
            self.assertEqual(scope.cargo_scope(tree, ["docs/gates/x.ron"], [], ["docs/**"]).packages, ("app", "core"))
            self.assertEqual(scope.cargo_scope(tree, ["docs/gates/y.md"], [], ["docs/**"]).packages, ())
            self.assertIsNone(scope.cargo_scope(tree, ["docs/gates/y.md"], [], []).packages)

    def test_an_unset_scope_keeps_every_command(self) -> None:
        from wpo.scope import scoped_checks
        commands, line = scoped_checks({"check_scope": None}, Path("."), ["cargo test --workspace"], ["crates/core/src/lib.rs"])
        self.assertEqual(commands, ["cargo test --workspace"])
        self.assertIn("unset", line)


LINT_WP = """---
id: {id}
title: {title}
milestone: {milestone}
lane: {lane}
status: {status}
depends_on: [{depends}]
builder: code
reviewer: code-hard
owns: [{owns}]
---

## Goal
{goal}

## Context
{context}

## Deliverables
- {deliverable}

## Steps
- [ ] One

## Acceptance criteria
1. {criterion}

## Verify
```
{verify}
```

## Progress log

## Review

## Handoff
"""


def lint_settings(**overrides) -> object:
    from wpo.lint import LintSettings
    from wpo.project import DEFAULTS as PROJECT_DEFAULTS
    lint_config = {**PROJECT_DEFAULTS["lint"], **overrides.pop("lint", {})}
    return LintSettings.from_config({**PROJECT_DEFAULTS, "lint": lint_config, **overrides})


class LintChecksTests(unittest.TestCase):
    """`wpo lint`'s checks on WP files in a scratch directory (planning audit, 2026-11-01)."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="wpo-lint-"))
        self.wp_dir = self.root / "docs" / "plan" / "wp"
        self.wp_dir.mkdir(parents=True)
        self.wps: dict[str, wpfile.WorkPackage] = {}

    def wp(self, wp_id: str, status: str = "todo", lane: str = "", depends: str = "", owns: str = "src/other/**", milestone: str = "M1",
           title: str | None = None, goal: str = "Do it.", context: str = "None.", deliverable: str = "A thing.",
           criterion: str = "It works.", verify: str = "cargo test -p thing") -> wpfile.WorkPackage:
        path = self.wp_dir / f"{wp_id}-x.md"
        path.write_text(LINT_WP.format(id=wp_id, title=title or f"Title {wp_id}", milestone=milestone, lane=lane, status=status,
                                       depends=depends, owns=owns, goal=goal, context=context, deliverable=deliverable,
                                       criterion=criterion, verify=verify), encoding="utf-8")
        self.wps[wp_id] = wpfile.parse(path)
        return self.wps[wp_id]

    def lint(self, wp: wpfile.WorkPackage, settings=None, similarity=None, reported=None) -> list:
        from wpo.lint import lint_wp
        return lint_wp(wp, self.wps, self.root, settings or lint_settings(), similarity, reported)

    def messages(self, findings: list, check: str) -> list[tuple[str, str]]:
        return [(finding.severity, finding.message) for finding in findings if finding.check == check]

    def test_a_clean_wp_has_no_findings(self) -> None:
        (self.root / "src" / "other").mkdir(parents=True)
        self.assertEqual(self.lint(self.wp("WP-0001")), [])

    def test_an_empty_verify_block_is_an_error_even_with_round_checks(self) -> None:
        findings = self.lint(self.wp("WP-0001", verify=""), lint_settings(verify_round=["cargo fmt --all --check"]))
        self.assertEqual([severity for severity, _ in self.messages(findings, "verify")], ["error"])
        self.assertIn("no commands", self.messages(findings, "verify")[0][1])

    def test_forbidden_verify_commands_are_reported_with_their_message_and_severity(self) -> None:
        rules = [{"pattern": r"^cargo test --workspace\b", "message": "whole suite runs only at merge", "severity": "warning"},
                 {"pattern": r"--features install-tests\s*(--(\s+-\S+)*)?$", "message": "unfiltered install tests"}]
        settings = lint_settings(lint={"forbidden_verify": rules})
        wp = self.wp("WP-0001", verify="cargo test --workspace\ncargo test -p a --features install-tests -- --nocapture\n"
                                       "cargo test -p a --features install-tests wp0001_ -- --nocapture")
        found = self.messages(self.lint(wp, settings), "verify")
        self.assertEqual(found, [("warning", "Verify runs `cargo test --workspace`: whole suite runs only at merge"),
                                 ("error", "Verify runs `cargo test -p a --features install-tests -- --nocapture`: unfiltered install tests")])

    def test_malformed_lint_settings_are_refused(self) -> None:
        from wpo.lint import LintConfigError
        for lint_config in ({"forbidden_verify": [{"pattern": "(", "message": "x"}]},
                            {"forbidden_verify": [{"pattern": "x"}]},
                            {"forbidden_verify": [{"pattern": "x", "message": "y", "severity": "fatal"}]},
                            {"similarity_warn": 1.5}, {"similarity_top": 0}, {"unknown": 1}):
            with self.subTest(lint=lint_config), self.assertRaises(LintConfigError):
                lint_settings(lint=lint_config)
        self.assertIsNone(lint_settings(lint={"similarity_warn": None}).similarity_warn)

    def test_dependency_cycles_unknown_ids_blocked_chains_and_later_milestones(self) -> None:
        self.wp("WP-0001", depends="WP-0003", milestone="M2")
        self.wp("WP-0002", depends="WP-0001")
        third = self.wp("WP-0003", depends="WP-0002, WP-0404")
        found = self.messages(self.lint(third), "dependencies")
        self.assertIn(("error", "depends on WP-0404, which has no WP file"), found)
        self.assertTrue(any(severity == "error" and "WP-0003 -> WP-0002 -> WP-0001 -> WP-0003" in message for severity, message in found))
        self.wp("WP-0010", status="blocked", milestone="M5")
        waiting = self.wp("WP-0011", depends="WP-0010", milestone="M3")
        found = [message for _, message in self.messages(self.lint(waiting), "dependencies")]
        self.assertTrue(any("WP-0010, which is blocked" in message for message in found))
        self.assertTrue(any("is in M3 but depends on WP-0010 in the later M5" in message for message in found))
        # A blocked WP behind a blocked one is the same stalled chain; milestones that are not numbered do not order.
        blocked_too = self.wp("WP-0012", status="blocked", depends="WP-0010", milestone="M?")
        self.assertEqual(self.messages(self.lint(blocked_too), "dependencies"), [])

    def test_self_dependency_is_a_cycle(self) -> None:
        from wpo.lint import dependency_cycle
        self.assertEqual(dependency_cycle(self.wp("WP-0001", depends="WP-0001"), self.wps), ["WP-0001", "WP-0001"])
        self.assertIsNone(dependency_cycle(self.wp("WP-0002", depends="WP-0001"), self.wps))

    def test_overlaps_cover_in_flight_and_blocked_wps_and_are_reported_once_per_pair(self) -> None:
        mine = self.wp("WP-0001", lane="a", owns="crates/x/**")
        self.wp("WP-0002", status="in_progress", lane="b", owns="crates/x/src/lib.rs")
        self.wp("WP-0003", status="blocked", lane="c", owns="crates/x/Cargo.toml")
        self.wp("WP-0004", status="todo", lane="a", owns="crates/x/**")  # same lane: never concurrent
        self.wp("WP-0005", status="todo", lane="d", owns="crates/x/**", depends="WP-0001")  # ordered
        self.wp("WP-0006", status="done", lane="e", owns="crates/x/**")
        reported: set = set()
        found = self.messages(self.lint(mine, reported=reported), "overlap")
        self.assertEqual([message.split(" (")[0] for _, message in found], ["shares owned paths with open WP-0002", "shares owned paths with open WP-0003"])
        self.assertIn("in_progress", found[0][1])
        def partners(findings: list) -> list[str]:
            return [message.split(" (")[0].rsplit(" ", 1)[-1] for _, message in self.messages(findings, "overlap")]
        # WP-0001's pair was reported on WP-0001; linted alone, WP-0002 reports it too.
        self.assertEqual(partners(self.lint(self.wps["WP-0002"], reported=reported)), ["WP-0004", "WP-0005"])
        self.assertEqual(partners(self.lint(self.wps["WP-0002"])), ["WP-0001", "WP-0004", "WP-0005"])

    def test_a_new_module_needs_its_declaring_file_owned_or_declaring_it(self) -> None:
        src = self.root / "crates" / "x" / "src"
        (src / "parent").mkdir(parents=True)
        (src / "lib.rs").write_text("pub mod parent;\nmod declared;\n", encoding="utf-8")
        (src / "parent.rs").write_text("mod existing;\n", encoding="utf-8")
        cases = {
            "crates/x/src/fresh/**": "`mod fresh;` in crates/x/src/lib.rs or crates/x/src/main.rs",
            "crates/x/src/parent/child.rs": "`mod child;` in crates/x/src/parent/mod.rs or crates/x/src/parent.rs",
            "crates/x/src/fresh/**, crates/x/src/lib.rs": None,  # owns the crate root
            "crates/x/src/declared/**": None,  # the crate root declares it already
            "crates/x/src/bin/tool.rs": None,  # its own crate root
            "crates/x/src/parent.rs": None,  # exists
            "crates/x/tests/fresh.rs": None,  # not under src/
        }
        for owns, expected in cases.items():
            with self.subTest(owns=owns):
                found = self.messages(self.lint(self.wp("WP-0001", owns=owns)), "module-declaration")
                if expected is None:
                    self.assertEqual(found, [])
                else:
                    self.assertEqual(len(found), 1)
                    self.assertIn(expected, found[0][1])

    def test_a_module_a_dependency_creates_is_not_reported(self) -> None:
        (self.root / "crates" / "x" / "src").mkdir(parents=True)
        self.wp("WP-0001", owns="crates/x/src/fresh/**, crates/x/src/lib.rs")
        later = self.wp("WP-0002", depends="WP-0001", owns="crates/x/src/fresh/**")
        self.assertEqual(self.messages(self.lint(later), "module-declaration"), [])

    def test_stale_wp_references(self) -> None:
        self.wp("WP-0002")
        wp = self.wp("WP-0001", depends="WP-0777", context="Builds on WP-0002 and WP-0999; see WP-0001.")
        found = self.messages(self.lint(wp), "stale-reference")
        self.assertEqual(found, [("warning", "names WP-0999, which has no WP file (renumbered, merged into another, or a typo?)")])

    def test_similar_wps_are_named_with_status_score_and_citation(self) -> None:
        from wpo.similarity import SimilarityIndex
        tablet = "The tablet in play shows the map app with teammates, markers and the overview bake."
        self.wp("WP-0001", status="done", title="Tablet map app in play", goal=tablet, criterion="The map app shows teammates and markers.")
        self.wp("WP-0002", status="todo", title="Garment refit cache", goal="Refit every garment to every body into the cache.",
                criterion="The refit cache is warm on the second run.")
        draft = self.wp("WP-0003", title="Tablet map app with markers", goal=tablet, criterion="The map app shows markers placed by teammates.")
        index = SimilarityIndex.of_wps(self.wps.values())
        self.assertGreater(index.cosine("WP-0003", "WP-0001"), 0.5)
        self.assertLess(index.cosine("WP-0003", "WP-0002"), 0.1)
        found = self.messages(self.lint(draft, similarity=index), "similarity")
        self.assertEqual(len(found), 1)
        self.assertRegex(found[0][1], r"^reads like WP-0001 'Tablet map app in play' \(done, 0\.\d\d, not cited here\): ")
        self.assertNotIn("WP-0002", found[0][1])
        cited = self.wp("WP-0003", title="Tablet map app with markers", goal=tablet + " WP-0001 did the single-player part.")
        index = SimilarityIndex.of_wps(self.wps.values())
        self.assertIn("cited here", self.messages(self.lint(cited, similarity=index), "similarity")[0][1])
        self.assertEqual(self.messages(self.lint(draft, lint_settings(lint={"similarity_warn": None}), similarity=index), "similarity"), [])

    def test_tokens_drop_stopwords_ids_and_format_words_and_fold_plurals(self) -> None:
        from wpo.similarity import tokens
        self.assertEqual(tokens("The garments of WP-0904 and wp0904_ tests in crates/zm_wardrobe: bodies, caches!"),
                         ["garment", "zm_wardrobe", "body", "cache"])

    def test_summary_counts_by_check(self) -> None:
        from wpo.lint import Finding, findings_json, summary_lines
        findings = [Finding("WP-1", "error", "verify", "a"), Finding("WP-1", "warning", "overlap", "b"), Finding("WP-2", "warning", "overlap", "c")]
        self.assertEqual(summary_lines(findings), ["by check: overlap 2 warnings, verify 1 error", "1 errors, 2 warnings"])
        self.assertEqual(summary_lines([]), ["0 errors, 0 warnings"])
        self.assertEqual(findings_json(findings)["counts"], {"overlap": {"error": 0, "warning": 2}, "verify": {"error": 1, "warning": 0}})


class LintIntegrationTests(unittest.TestCase):
    """lint's default scope, commit-main's report, drafting sessions and `wpo audit`, on a scratch repository."""

    def setUp(self) -> None:
        from wpo.context import Context
        from wpo.project import git
        self.scratch = DashboardProject()
        wp_dir = self.scratch.root / "docs" / "plan" / "wp"
        third = wp_dir / "WP-0003-x.md"
        third.write_text(third.read_text(encoding="utf-8").replace("status: todo", "status: blocked"), encoding="utf-8")
        git(self.scratch.root, "commit", "-q", "-am", "WP-0003 blocked")
        self.git = git
        self.context = Context.load(self.scratch.root, self.scratch.machine)

    def test_default_scope_is_every_open_wp_blocked_included(self) -> None:
        from wpo.lint import lint
        self.assertEqual(sorted({finding.wp_id for finding in lint(self.context, [])}), ["WP-0002", "WP-0003"])
        self.assertIn("WP-0001", {finding.wp_id for finding in lint(self.context, [], include_done=True)})
        self.assertEqual({finding.wp_id for finding in lint(self.context, ["WP-0001"])}, {"WP-0001"})

    def test_commit_main_prints_the_committed_wps_lint_and_never_blocks(self) -> None:
        import contextlib
        import io
        from wpo.orchestrator import commit_main
        draft = self.scratch.root / "docs" / "plan" / "wp" / "WP-0004-x.md"
        draft.write_text(DASHBOARD_WP.format(id="WP-0004", title="New", status="todo", depends="WP-0009", progress="x"), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = commit_main(self.context, "WP-0004: drafted", ["docs/plan/wp/WP-0004-x.md"])
        self.assertEqual(code, 0)
        self.assertEqual(self.git(self.scratch.root, "log", "-1", "--format=%s").stdout.strip(), "WP-0004: drafted")
        printed = output.getvalue()
        self.assertIn("wpo lint WP-0004:", printed)
        self.assertIn("WP-0004 error: depends on WP-0009, which has no WP file", printed)
        self.assertIn("!! ", printed)
        self.assertNotIn("WP-0002", printed.split("wpo lint WP-0004:")[1])

    def test_commit_main_without_wp_files_prints_no_lint(self) -> None:
        import contextlib
        import io
        from wpo.orchestrator import commit_main
        (self.scratch.root / "notes.md").write_text("x", encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            commit_main(self.context, "notes", ["notes.md"])
        self.assertNotIn("wpo lint", output.getvalue())

    def test_drafting_sessions_get_the_lint_rule_and_command(self) -> None:
        from wpo.harness import command_prefixes
        from wpo.orchestrator import DRAFTING_RULE, dispatch_session, writes_wp_files
        self.assertTrue(writes_wp_files(self.context, ["docs/plan/wp/**"]))
        self.assertTrue(writes_wp_files(self.context, ["docs/**"]))
        self.assertFalse(writes_wp_files(self.context, ["docs/design/**"]))
        self.assertEqual(command_prefixes("docs", self.context.project.config, False), ["wpo lint"])
        self.assertEqual(command_prefixes("read", self.context.project.config, False), [])
        prompts: list[str] = []

        def session(context, spec, access, prompt, *arguments, **keywords):
            prompts.append(prompt)
            return SimpleNamespaceResult("done")

        with mock.patch("wpo.orchestrator.run_session", side_effect=session):
            dispatch_session(self.context, "research", "draft it", "docs", ["docs/plan/wp/**"], [], self.scratch.root,
                             self.scratch.base / "d1", {})
            dispatch_session(self.context, "research", "research it", "read", [], [], self.scratch.root, self.scratch.base / "d2", {})
        self.assertIn(DRAFTING_RULE, prompts[0])
        self.assertNotIn(DRAFTING_RULE, prompts[1])

    def test_ad_hoc_sessions_route_in_the_projects_mode(self) -> None:
        from wpo import routing
        from wpo.orchestrator import dispatch_session
        specs: list[str] = []

        def session(context, spec, *arguments, **keywords):
            specs.append(spec)
            return SimpleNamespaceResult("done")

        routing.set_project_mode(self.context.project.runs_dir, "hard")
        with mock.patch("wpo.orchestrator.run_session", side_effect=session):
            for spec in ("code-hard", "code-hard@standard", "pin:claude-opus-high"):
                dispatch_session(self.context, spec, "task", "read", [], [], self.scratch.root, self.scratch.base / spec.replace(":", "-"), {})
        self.assertEqual(specs, ["code-hard@hard", "code-hard@standard", "pin:claude-opus-high"], "an explicit mode or pin stays")

    def test_audit_writes_a_dated_report_runs_the_review_and_journals_audit_done(self) -> None:
        import contextlib
        import io
        from datetime import date
        from wpo import audit
        seen: dict = {}

        def session(context, spec, prompt, access, write_paths, families, cwd, run_dir, event_fields):
            seen.update(spec=spec, access=access, write_paths=write_paths, families=families, prompt=prompt)
            return SimpleNamespaceResult("### WP-0002\n- 1: duplicates WP-0001.")

        with mock.patch("wpo.audit.dispatch_session", side_effect=session), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(audit.run_audit(self.context, mechanical_only=True, today=date(2026, 11, 2)), 0)
            self.assertEqual(audit.run_audit(self.context, mechanical_only=False, today=date(2026, 11, 2)), 0)
        audits = self.context.project.runs_dir / "audits"
        first, second = audits / "2026-11-02.md", audits / "2026-11-02-2.md"
        self.assertNotIn("## Judgment review", first.read_text(encoding="utf-8"))
        report = second.read_text(encoding="utf-8")
        self.assertIn("## Lint findings", report)
        self.assertIn("- WP-0003 [blocked, lane core, M1]", report)
        self.assertTrue(report.rstrip().endswith("- 1: duplicates WP-0001."))
        self.assertEqual((seen["spec"], seen["access"], seen["write_paths"]), ("code-hard", "read", []))
        cost_classes = self.context.router.policy["family_cost_class"]
        self.assertTrue(seen["families"] and all(cost_classes[family] not in ("free", "local") for family in seen["families"]))
        self.assertIn("read-only", seen["prompt"])
        done = [event for event in self.context.journal.read(0) if event["kind"] == "audit-done"]
        self.assertEqual(len(done), 1)  # the mechanical-only run journals nothing
        self.assertEqual((done[0]["severity"], done[0]["report"]), ("attention", str(second)))

    def test_audit_without_a_route_keeps_the_lint_part_and_says_so(self) -> None:
        import contextlib
        import io
        from wpo import audit
        from wpo.session import NoRouteError
        with mock.patch("wpo.audit.dispatch_session", side_effect=NoRouteError("all capped", None)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(audit.run_audit(self.context, mechanical_only=False), audit.EXIT_NO_ROUTE)
        event = [event for event in self.context.journal.read(0) if event["kind"] == "audit-done"][-1]
        self.assertEqual(event["review"], "no-route")
        self.assertIn("Not run: no route", Path(event["report"]).read_text(encoding="utf-8"))

    def test_schedule_registers_a_weekly_task_for_this_project(self) -> None:
        from wpo import audit
        arguments = audit.schedule_arguments(self.context)
        self.assertEqual(arguments[:11], ["schtasks", "/Create", "/TN", audit.task_name(self.context), "/SC", "WEEKLY", "/D", "MON", "/ST", "09:00", "/TR"])
        self.assertIn(f"--project {self.context.project.root}", arguments[11].replace('"', ""))
        self.assertTrue(arguments[11].endswith(" audit --from-os-schedule"))
        self.assertEqual(audit.remove_arguments(self.context), ["schtasks", "/Delete", "/TN", audit.task_name(self.context), "/F"])
        self.context.project.config["audit"]["schedule"] = {"day": "MONDAY", "time": "09:00"}
        with self.assertRaises(audit.AuditError):
            audit.schedule_arguments(self.context)
        self.context.project.config["audit"]["schedule"] = {"day": "TUE", "time": "9am"}
        with self.assertRaises(audit.AuditError):
            audit.schedule_arguments(self.context)


if __name__ == "__main__":
    unittest.main()
