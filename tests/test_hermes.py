"""Hermes adapter tests using real local skill fixtures and its registration contract."""

import importlib.util
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("window_shopper_hermes_test", ROOT / "adapters/hermes/__init__.py")
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


class HermesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="window-shopper-hermes-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, desc in (("alpha", "Orchestration operators."), ("beta", "Photogrammetry reconstruction."),
                           ("gamma", "Typography kerning.")):
            directory = self.root / name
            directory.mkdir()
            (directory / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\nUse the {name} procedure.\n", encoding="utf-8")
        self.core = adapter._load_core()
        self.costs = {s["name"]: s["estimated_tokens"] for s in self.core.build_catalog([str(self.root)])["skills"]}

    def make(self, **kwargs):
        return adapter.WindowShopper([str(self.root)], max_skills=1, **kwargs)

    def shop(self, gate, task="orchestration", sid="session-a"):
        return json.loads(gate.shop({"task": task}, session_id=sid))

    def load(self, gate, name="alpha", sid="session-a"):
        return json.loads(gate.load({"name": name}, session_id=sid))

    def test_successful_read_receipt_controls_required_gate(self):
        gate = self.make(required=["alpha"], guarded_tools=["write_file"])
        self.assertEqual(gate.pre_tool_call("write_file", session_id="session-a")["action"], "block")
        self.assertTrue(self.shop(gate)["success"])
        self.assertEqual(gate.pre_tool_call("write_file", session_id="session-a")["action"], "block")
        body = self.load(gate)
        self.assertTrue(body["success"])
        self.assertIn("# alpha", body["content"])
        self.assertIsNone(gate.pre_tool_call("write_file", session_id="session-a"))

    def test_optional_selection_does_not_block_guarded_action(self):
        gate = self.make(guarded_tools=["write_file"])
        self.assertTrue(self.shop(gate)["selected"])
        self.assertIsNone(gate.pre_tool_call("write_file", session_id="session-a"))

    def test_failed_changed_file_read_never_counts_as_loaded(self):
        gate = self.make(required=["alpha"], guarded_tools=["write_file"])
        self.shop(gate)
        with (self.root / "alpha/SKILL.md").open("a") as f:
            f.write("Changed after selection.\n")
        self.assertFalse(self.load(gate)["success"])
        self.assertEqual(gate.sessions["session-a"]["spent"], 0)
        self.assertFalse(gate.sessions["session-a"]["loaded"])
        self.assertEqual(gate.pre_tool_call("write_file", session_id="session-a")["action"], "block")

    def test_not_selected_is_rejected(self):
        gate = self.make()
        self.shop(gate)
        self.assertFalse(self.load(gate, "beta")["success"])
        self.assertEqual(gate.sessions["session-a"]["spent"], 0)

    def test_repeated_bodies_consume_budget(self):
        gate = self.make(budget=self.costs["alpha"] * 2 - 1)
        self.shop(gate)
        self.assertTrue(self.load(gate)["success"])
        self.assertFalse(self.load(gate)["success"])
        self.assertEqual(gate.sessions["session-a"]["spent"], self.costs["alpha"])

    def test_reroute_keeps_cumulative_budget(self):
        gate = self.make(budget=self.costs["alpha"] + self.costs["beta"])
        self.shop(gate)
        self.load(gate)
        route = self.shop(gate, "photogrammetry")
        self.assertEqual(route["already_loaded"], ["alpha"])
        self.assertEqual(route["remaining_estimated_tokens"], self.costs["beta"])
        self.assertTrue(self.load(gate, "beta")["success"])
        self.assertEqual(self.shop(gate, "typography")["status"], "budget_exhausted")
        self.assertEqual(gate.sessions["session-a"]["spent"], gate.budget)

    def test_concurrent_reads_cannot_overspend(self):
        gate = self.make(budget=self.costs["alpha"])
        self.shop(gate)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.load(gate), range(8)))
        self.assertEqual(sum(r["success"] for r in results), 1)
        self.assertEqual(gate.sessions["session-a"]["spent"], gate.budget)

    def test_unchanged_required_receipt_survives_reroute_without_reserving_again(self):
        gate = self.make(budget=self.costs["alpha"] + self.costs["beta"], required=["alpha"],
                         guarded_tools=["write_file"])
        self.shop(gate)
        self.load(gate)
        route = self.shop(gate, "photogrammetry")
        self.assertTrue(route["success"])
        self.assertEqual([s["name"] for s in route["selected"]], ["beta"])
        self.assertIsNone(gate.pre_tool_call("write_file", session_id="session-a"))

    def test_changed_required_needs_fresh_receipt_on_reroute(self):
        gate = self.make(required=["alpha"], guarded_tools=["write_file"])
        self.shop(gate)
        self.load(gate)
        with (self.root / "alpha/SKILL.md").open("a") as f:
            f.write("New rule.\n")
        self.assertTrue(self.shop(gate)["success"])
        self.assertEqual(gate.pre_tool_call("write_file", session_id="session-a")["action"], "block")
        self.assertTrue(self.load(gate)["success"])
        self.assertIsNone(gate.pre_tool_call("write_file", session_id="session-a"))

    def test_missing_required_cannot_silently_allow_actions(self):
        gate = self.make(required=["absent"], guarded_tools=["write_file"])
        self.assertFalse(self.shop(gate)["success"])
        self.assertEqual(gate.pre_tool_call("write_file", session_id="session-a")["action"], "block")

    def test_sessions_do_not_share_receipts_or_budget(self):
        gate = self.make(required=["alpha"], guarded_tools=["write_file"])
        self.shop(gate)
        self.load(gate)
        self.shop(gate, sid="session-b")
        self.assertEqual(gate.sessions["session-b"]["spent"], 0)
        self.assertEqual(gate.pre_tool_call("write_file", session_id="session-b")["action"], "block")

    def test_absent_session_fails_instead_of_using_task_id(self):
        gate = self.make(guarded_tools=["write_file"])
        self.assertFalse(json.loads(gate.shop({"task": "alpha"}, task_id="turn-1"))["success"])
        self.assertFalse(json.loads(gate.load({"name": "alpha"}, task_id="turn-1"))["success"])
        self.assertEqual(gate.pre_tool_call("write_file", task_id="turn-1")["action"], "block")
        self.assertFalse(gate.sessions)

    def test_native_interception_is_explicit(self):
        gate = self.make()
        self.assertIsNone(gate.pre_tool_call("skill_view"))
        guarded = self.make(block_native_skill_tools=True)
        for name in ("skill_view", "skills_list"):
            self.assertEqual(guarded.pre_tool_call(name)["action"], "block")
        self.assertIsNone(guarded.pre_tool_call("read_file"))

    def test_invalid_configuration_fails_before_registration(self):
        for overrides in ({"budget": True}, {"budget": 0}, {"required": "alpha"},
                          {"guarded_tools": ["window_load"]}, {"block_native_skill_tools": "true"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.make(**overrides)

    def test_register_uses_documented_hermes_contract(self):
        class Context:
            def __init__(self):
                self.tools, self.hooks = {}, {}

            def get_config(ctx, key, default):
                return [str(self.root)] if key == "roots" else default

            def register_tool(ctx, name, toolset, schema, handler):
                self.assertEqual(toolset, "window_shopper")
                self.assertEqual(schema["name"], name)
                ctx.tools[name] = handler

            def register_hook(ctx, name, handler):
                ctx.hooks[name] = handler

        ctx = Context()
        adapter.register(ctx)
        self.assertEqual(set(ctx.tools), {"window_shop", "window_load"})
        self.assertEqual(set(ctx.hooks), {"pre_tool_call"})
        result = json.loads(ctx.tools["window_shop"]({"task": "alpha"}, session_id="real-host-id"))
        self.assertTrue(result["success"])


if __name__ == "__main__":
    unittest.main()
