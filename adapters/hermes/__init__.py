"""Optional Hermes adapter. No Hermes imports, network calls, or configuration writes."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import threading


def _load_core():
    path = Path(__file__).resolve().parents[2] / "scripts" / "window_shopper.py"
    if not path.is_file():
        raise RuntimeError("Keep the Hermes adapter inside the Window Shopper checkout; install it by symlink.")
    name = "_window_shopper_" + hashlib.sha256(str(path).encode()).hexdigest()[:12]
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


def _strings(value, name, *, nonempty=False):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError(f"{name} must be a list of nonempty strings")
    if nonempty and not value:
        raise ValueError(f"Configure plugins.entries.window-shopper.settings.{name} first")
    return list(dict.fromkeys(value))


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


class WindowShopper:
    """Session accounting covers returned SKILL.md bodies, not the entire model prompt."""

    def __init__(self, roots, budget=2000, max_skills=3, required=None, guarded_tools=None,
                 block_native_skill_tools=False, core=None):
        self.roots = _strings(roots, "roots", nonempty=True)
        self.budget = _positive(budget, "budget")
        self.max_skills = _positive(max_skills, "max_skills")
        self.required = _strings(required if required is not None else [], "required")
        self.guarded_tools = set(_strings(guarded_tools if guarded_tools is not None else [], "guarded_tools"))
        if self.guarded_tools & {"window_shop", "window_load"}:
            raise ValueError("guarded_tools must not include Window Shopper's own tools")
        if not isinstance(block_native_skill_tools, bool):
            raise ValueError("block_native_skill_tools must be a boolean")
        self.block_native_skill_tools = block_native_skill_tools
        self.core = core or _load_core()
        self.catalog = self.core.build_catalog(self.roots)
        self.sessions = {}
        self.lock = threading.RLock()

    def _session(self, session_id):
        # task_id can change each turn: never use it as a substitute for session_id.
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("Window Shopper requires a host-provided session_id")
        if session_id not in self.sessions:
            if len(self.sessions) >= 1024:
                raise ValueError("Session ledger full; start a fresh agent process and conversation")
            self.sessions[session_id] = {"spent": 0, "selection": None, "loaded": set(), "required": []}
        return self.sessions[session_id]

    @staticmethod
    def _reply(**payload):
        return json.dumps(payload, ensure_ascii=False)

    def shop(self, args, *, session_id="", **_kwargs):
        with self.lock:
            try:
                state = self._session(session_id)
                state["selection"] = None
                task = args.get("task") if isinstance(args, dict) else None
                if not isinstance(task, str) or not task.strip() or len(task) > 20000:
                    raise ValueError("task must be a nonempty string of at most 20000 characters")
                remaining = self.budget - state["spent"]
                # Refresh metadata and hashes for deliberate reroutes, without resetting receipts.
                self.catalog = self.core.build_catalog(self.roots)
                by_name = {entry["name"]: entry for entry in self.catalog["skills"]}
                missing = [name for name in self.required if name not in by_name]
                if missing:
                    raise ValueError("Required skills missing from catalog: " + ", ".join(missing))
                required_entries = [by_name[name] for name in self.required]
                known = state["loaded"]
                unseen_catalog = {**self.catalog, "skills": [
                    entry for entry in self.catalog["skills"]
                    if (entry["name"], entry["sha256"]) not in known
                ]}
                pending_required = [entry["name"] for entry in required_entries
                                    if (entry["name"], entry["sha256"]) not in known]
                selected = self.core.select_skills(
                    unseen_catalog, task, budget=remaining, max_skills=self.max_skills,
                    required=pending_required,
                )
                state["selection"] = selected
                state["required"] = required_entries
                return self._reply(
                    success=True, status=selected["status"], remaining_estimated_tokens=remaining,
                    already_loaded=sorted({e["name"] for e in self.catalog["skills"]
                                           if (e["name"], e["sha256"]) in known}),
                    selected=[{k: entry[k] for k in ("name", "description", "estimated_tokens", "reason")}
                              for entry in selected["selected"]],
                    warnings=selected.get("warnings", []),
                )
            except (ValueError, OSError) as exc:
                return self._reply(success=False, error=str(exc))

    def load(self, args, *, session_id="", **_kwargs):
        with self.lock:
            try:
                state = self._session(session_id)
                selection = state["selection"]
                if selection is None:
                    raise ValueError("Call window_shop with the current task before loading a skill")
                name = args.get("name") if isinstance(args, dict) else None
                entry = next((e for e in selection["selected"] if e["name"] == name), None)
                if entry is None:
                    raise ValueError("Skill is not in the current selection; reroute with window_shop")
                cost = entry["estimated_tokens"]
                if state["spent"] + cost > self.budget:
                    raise ValueError("Session body budget exceeded; repeated reads also consume budget")
                # Successful validation and read precede accounting and the loaded receipt.
                content = self.core.load_skill(selection, name)
                state["spent"] += cost
                state["loaded"].add((name, entry["sha256"]))
                return self._reply(
                    success=True, name=name, content=content, estimated_tokens=cost,
                    spent_estimated_tokens=state["spent"],
                    remaining_estimated_tokens=self.budget - state["spent"],
                )
            except (ValueError, OSError) as exc:
                return self._reply(success=False, error=str(exc))

    def pre_tool_call(self, tool_name="", args=None, session_id="", **_kwargs):
        if self.block_native_skill_tools and tool_name in {"skill_view", "skills_list"}:
            return {"action": "block", "message": "Use window_shop(task), then window_load(name), for budgeted skill access."}
        if tool_name not in self.guarded_tools:
            return None
        with self.lock:
            try:
                state = self._session(session_id)
                selection = state["selection"]
                if selection is None:
                    raise ValueError("Call window_shop before this configured guarded tool")
                missing = [e["name"] for e in state["required"]
                           if (e["name"], e["sha256"]) not in state["loaded"]]
                if missing:
                    raise ValueError("Load required skills successfully first: " + ", ".join(missing))
            except ValueError as exc:
                return {"action": "block", "message": str(exc)}
        return None


def register(ctx):
    """Hermes directory-plugin entry point; settings live in this plugin's namespace."""
    shopper = WindowShopper(
        roots=ctx.get_config("roots", []), budget=ctx.get_config("budget", 2000),
        max_skills=ctx.get_config("max_skills", 3), required=ctx.get_config("required", []),
        guarded_tools=ctx.get_config("guarded_tools", []),
        block_native_skill_tools=ctx.get_config("block_native_skill_tools", False),
    )
    for name, field, description, handler in (
        ("window_shop", "task", "Select a small local skill pack for a task within the remaining session body budget.", shopper.shop),
        ("window_load", "name", "Read a selected SKILL.md. Each successful read consumes the session body budget.", shopper.load),
    ):
        ctx.register_tool(
            name=name, toolset="window_shopper", handler=handler,
            schema={"name": name, "description": description, "parameters": {
                "type": "object", "properties": {field: {"type": "string"}},
                "required": [field], "additionalProperties": False,
            }},
        )
    ctx.register_hook("pre_tool_call", shopper.pre_tool_call)
