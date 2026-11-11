"""Routing policy and carried-rule contracts, without providers or live state."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wpo import routing
from wpo.machine import DEFAULTS, MachineConfig
from wpo.session_brief import BriefError, carried_user_rules


def route_entry(name: str, score: int = 60, family: str = "claude", **extra) -> dict:
    return {"id": name, "family": family, "harness": family, "model": name,
            "score": score, "writes_code": True, "effort": "high", **extra}


class RouteRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.machine = MachineConfig({**DEFAULTS, "state_dir": str(self.root), "execution_mode": "standalone"})
        self.router = routing.Router(self.machine, {"routes": [route_entry("builder"), route_entry("review")]})
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(routing.Router, "catalog_routes", return_value=[]).start()
        mock.patch.object(routing.Router, "oracle", return_value=SimpleNamespace(route=lambda _: routing.Availability(True))).start()

    def ids(self, spec: str = "code", **kwargs) -> list[str]:
        return [route["id"] for route in self.router.select(spec, **kwargs).candidates]

    def test_machine_then_project_policy_precedence(self) -> None:
        machine = MachineConfig({**self.machine.raw, "routing": {
            "routes": [route_entry("base")], "routes_extra": [route_entry("machine")],
            "routes_exclude": ["base"], "ceilings": {"claude": {"weekly": 50}},
        }})
        router = routing.Router(machine, {"routes_extra": [route_entry("project")],
                                          "routes_exclude": ["machine"], "ceilings": {"claude": {"weekly": 60}}})
        self.assertEqual([route.id for route in router.all_routes([])], ["base", "project"])
        self.assertEqual(router.policy["ceilings"]["claude"], {"five_hour": 85, "weekly": 60})

    def test_same_layer_route_replacement_and_append_ignore_key_order(self) -> None:
        for override in ({"routes": [route_entry("replacement")], "routes_extra": [route_entry("extra")]},
                         {"routes_extra": [route_entry("extra")], "routes": [route_entry("replacement")]}):
            with self.subTest(override=override):
                router = routing.Router(self.machine, override)
                self.assertEqual([route.id for route in router.all_routes([])], ["replacement", "extra"])

    def test_excluded_dominator_does_not_remove_remaining_route(self) -> None:
        measured = {"cost_per_task": 1, "index_tokens_m": 1, "output_speed": 100}
        self.router.policy["routes"] = [route_entry("excluded", 70, **measured), route_entry("builder", 60, **measured)]
        self.router.policy["routes_exclude"] = ["excluded"]
        self.assertEqual(self.ids(), ["builder"])

    def test_static_exclusions_apply_to_candidates_and_pins(self) -> None:
        self.router.policy["routes_exclude"] = ["review"]
        self.assertEqual(self.ids(), ["builder"])
        with self.assertRaisesRegex(routing.RoutingError, "excluded by routing.routes_exclude"):
            self.ids("pin:review", gate=True)

    def test_family_exclusion_applies_to_static_pins(self) -> None:
        self.router.policy["routes_exclude"] = ["family:claude"]
        self.assertEqual(self.ids(), [])
        with self.assertRaisesRegex(routing.RoutingError, "excluded"):
            self.ids("pin:builder")

    def test_dynamic_exclusions_apply_to_candidates_and_pins(self) -> None:
        free = routing.Route("free-test", "free", "opencode", "m", None, 70, True, "free")
        self.router.catalog_path.write_text(json.dumps({"routes": [routing.asdict(free)]}), encoding="utf-8")
        with mock.patch.object(self.router, "catalog_routes", return_value=[free]):
            for excluded in (["free-test"], ["family:free"]):
                with self.subTest(excluded=excluded):
                    self.router.policy["routes_exclude"] = excluded
                    self.assertNotIn("free-test", self.ids())
                    with self.assertRaisesRegex(routing.RoutingError, "excluded"):
                        self.ids("pin:free-test")

    def test_metadata_lookup_retains_excluded_routes_for_history(self) -> None:
        self.router.policy["routes_exclude"] = ["review"]
        self.assertEqual(self.router.find_route("review").id, "review")
        self.assertNotIn("review", [route.id for route in self.router.all_routes([])])
        with self.assertRaisesRegex(routing.RoutingError, "unknown route"):
            self.router.find_route("nonexistent")

    def test_gate_only_routes_cannot_be_used_by_builders_even_pinned(self) -> None:
        self.router.policy["gate_only_routes"] = ["review"]
        self.assertEqual(self.ids(), ["builder"])
        with self.assertRaisesRegex(routing.RoutingError, "reserved for reviews"):
            self.ids("pin:review")
        self.assertEqual(self.ids("pin:review", gate=True), ["review"])
        self.assertEqual(set(self.ids(gate=True)), {"builder", "review"})

    def test_gate_only_dominator_does_not_hide_builder(self) -> None:
        measured = {"cost_per_task": 1, "index_tokens_m": 1, "output_speed": 100}
        self.router.policy["routes"] = [route_entry("review", 70, **measured), route_entry("builder", 60, **measured)]
        self.router.policy["gate_only_routes"] = ["review"]
        self.assertEqual(self.ids(), ["builder"])
        self.assertEqual(self.ids(gate=True), ["review"])

    def test_gate_only_does_not_override_pin_only_or_effort_rules(self) -> None:
        self.router.policy.update({"gate_only_routes": ["review"], "pin_only_routes": ["review"]})
        self.assertEqual(self.ids(gate=True), ["builder"])
        self.assertEqual(self.ids("pin:review", gate=True), ["review"])
        self.router.policy["pin_only_efforts"] = ["high"]
        self.assertEqual(self.ids(gate=True), [])
        with self.assertRaisesRegex(routing.RoutingError, "never run at high effort"):
            self.ids("pin:review", gate=True)

    def test_dynamic_gate_only_routes(self) -> None:
        free = routing.Route("free-review", "free", "opencode", "m", None, 70, True, "free")
        self.router.policy["gate_only_routes"] = [free.id]
        with mock.patch.object(self.router, "catalog_routes", return_value=[free]):
            self.assertNotIn(free.id, self.ids())
            self.assertIn(free.id, self.ids(gate=True))
            with self.assertRaisesRegex(routing.RoutingError, "reserved"):
                self.ids(f"pin:{free.id}")

    def test_excluded_model_families_leave_reviews_but_not_pins(self) -> None:
        self.router.policy["routes"] = [route_entry("opus"), route_entry("sol", 59, family="gpt"),
                                        route_entry("agy-opus", 58, family="agy", model_family="claude"), route_entry("flash", 57, family="agy")]
        self.assertCountEqual(self.ids(gate=True, exclude_model_families=["claude"]), ["sol", "flash"])
        self.assertCountEqual(self.ids(gate=True, exclude_model_families=["gpt"]), ["opus", "agy-opus", "flash"])
        self.assertEqual(self.ids("pin:agy-opus", gate=True, exclude_model_families=["claude"]), ["agy-opus"])

    def test_excluded_model_family_dominator_does_not_hide_its_family_peer(self) -> None:
        measured = {"cost_per_task": 1, "index_tokens_m": 1, "output_speed": 100}
        self.router.policy["routes"] = [route_entry("agy-opus", 70, family="agy", model_family="claude", **measured),
                                        route_entry("flash", 60, family="agy", **measured)]
        self.assertEqual(self.ids(gate=True), ["agy-opus"])
        self.assertEqual(self.ids(gate=True, exclude_model_families=["claude"]), ["flash"])

    def test_excluded_model_families_leave_listed_tiers_too(self) -> None:
        self.router.policy["routes"] = [route_entry("opus"), route_entry("sol", 59, family="gpt"),
                                        route_entry("agy-opus", 58, family="agy", model_family="claude")]
        self.router.policy["tier_routes"] = {"code": {"routes": [["agy-opus"], ["sol"], ["opus"]], "free_last_resort": False}}
        oracle = SimpleNamespace(route=lambda _: routing.Availability(True), headroom=lambda family: 50.0)
        with mock.patch.object(routing.Router, "oracle", return_value=oracle):
            self.assertEqual(self.ids(gate=True), ["agy-opus", "sol", "opus"])
            self.assertEqual(self.ids(gate=True, exclude_model_families=["gpt"]), ["agy-opus", "opus"])
            self.assertEqual(self.ids(gate=True, exclude_model_families=["claude"]), ["sol"])

    def test_route_rule_shapes_fail_with_named_setting(self) -> None:
        for key in ("routes_exclude", "gate_only_routes", "pin_only_routes", "pin_only_efforts"):
            for value in ("builder", None, {}, [1], [""]):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(routing.RoutingError, f"routing.{key}"):
                    routing.Router(self.machine, {key: value})
        for selector in ("family:", "family:   "):
            with self.subTest(selector=selector), self.assertRaisesRegex(routing.RoutingError, "family selectors require a name"):
                routing.Router(self.machine, {"routes_exclude": [selector]})


class CarriedRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.first = self.root / "first.md"
        self.first.write_text("Preamble\n## First\nOne\n### Detail\nDetails\n## Second\nTwo\n", encoding="utf-8")
        self.second = self.root / "second.md"
        self.second.write_text("Another complete file\n", encoding="utf-8")

    def carry(self, settings) -> str:
        return carried_user_rules(SimpleNamespace(machine=SimpleNamespace(raw={"user_rules": settings})))

    def test_legacy_sections_keep_exact_heading_and_requested_order(self) -> None:
        self.assertEqual(self.carry({"file": str(self.first), "sections": ["Second", "First"]}),
                         "## Second\nTwo\n\n## First\nOne\n### Detail\nDetails")

    def test_legacy_empty_sections_disable_including_missing_file(self) -> None:
        self.assertEqual(self.carry({"file": str(self.root / "absent"), "sections": []}), "")
        self.assertEqual(self.carry({"file": str(self.root / "absent"), "sections": None}), "")
        self.assertEqual(self.carry({}), "")

    def test_legacy_extra_fields_remain_ignored(self) -> None:
        self.assertEqual(self.carry({"file": str(self.first), "sections": ["Second"], "note": "legacy metadata"}),
                         "## Second\nTwo")

    def test_multiple_files_preserve_file_and_section_order(self) -> None:
        settings = [{"file": str(self.first), "sections": ["Second", "First"]}, {"file": str(self.second)}]
        self.assertEqual(self.carry(settings), "## Second\nTwo\n\n## First\nOne\n### Detail\nDetails\n\nAnother complete file")

    def test_list_omitted_or_null_sections_include_whole_file(self) -> None:
        for entry in ({"file": str(self.first)}, {"file": str(self.first), "sections": None}):
            with self.subTest(entry=entry):
                self.assertEqual(self.carry([entry]), self.first.read_text(encoding="utf-8").strip())

    def test_list_empty_sections_disable_entry_and_empty_list_disables_all(self) -> None:
        self.assertEqual(self.carry([{ "file": str(self.root / "absent"), "sections": []}]), "")
        self.assertEqual(self.carry([]), "")

    def test_missing_file_and_heading_report_configuration_location(self) -> None:
        with self.assertRaisesRegex(BriefError, r"does not exist.*user_rules\[0\].file"):
            self.carry([{"file": str(self.root / "absent")}])
        with self.assertRaisesRegex(BriefError, r"has no '## Missing'.*user_rules\[0\].sections"):
            self.carry([{"file": str(self.first), "sections": ["Missing"]}])
        with self.assertRaisesRegex(BriefError, "has no '## Fir' section"):
            self.carry({"file": str(self.first), "sections": ["Fir"]})

    def test_invalid_shapes_raise_brief_error_instead_of_iterating_strings(self) -> None:
        for settings in (None, "path", ["path"], [{"file": 1}], [{"sections": []}],
                         [{"file": str(self.first), "sections": "First"}],
                         [{"file": str(self.first), "sections": [1]}],
                         [{"file": str(self.first), "sections": [""]}],
                         [{"file": str(self.first), "section": ["First"]}]):
            with self.subTest(settings=settings), self.assertRaisesRegex(BriefError, "user_rules"):
                self.carry(settings)


if __name__ == "__main__":
    unittest.main()
