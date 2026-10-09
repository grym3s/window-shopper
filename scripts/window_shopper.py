#!/usr/bin/env python3
"""Offline, metadata-only skill selection with bounded, verified loading.

Python 3.10+, standard library only. Budget estimates use UTF-8 bytes divided by
four, rounded up per file; they are not model-tokenizer measurements. Selection
files are editable local policy records, not cryptographic authorization tokens.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any

CATALOG_SCHEMA = "window-shopper.catalog.v1"
SELECTION_SCHEMA = "window-shopper.selection.v1"
ESTIMATOR = "ceil(utf8_bytes/4)"
MAX_FILE_BYTES = 262_144
MAX_JSON_BYTES = 8_388_608
MAX_SCAN_ENTRIES = 20_000
MAX_SKILLS = 4096
MAX_DEPTH = 8
MAX_ROOTS = 64
MAX_SELECTION = 64
IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".local",
})
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
STOPWORDS = frozenset("""
    a an and are as at be been before but by can could do does for from get have
    how i if in into is it its me my of on or our please should so some that the
    their them then these they this to use used using us want we when which with
    would you your task tasks skill skills agent agents help work working need
    needs make create file files project projects code coding run new existing
""".split())
PROJECT_MARKERS = {
    "pyproject.toml": "python", "requirements.txt": "python",
    "setup.py": "python", "package.json": "javascript typescript node",
    "Cargo.toml": "rust", "go.mod": "golang", "Gemfile": "ruby",
    "composer.json": "php", "CMakeLists.txt": "cmake cpp",
    "pom.xml": "java maven", "build.gradle": "java gradle",
    "Dockerfile": "docker containers",
}


class ShopperError(ValueError):
    """A malformed input, stale record, unavailable skill, or budget failure."""


def estimated_tokens(size: int) -> int:
    return (size + 3) // 4


def _integer(value: Any, label: str, minimum: int = 0,
             maximum: int = 16_777_216) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ShopperError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _scalar(value: str, field: str) -> str:
    """Read the safe YAML scalar subset we advertise; never execute YAML tags."""
    value = value.strip()
    if not value:
        raise ShopperError(f"empty {field} frontmatter")
    if value.startswith('"'):
        try:
            result, end = json.JSONDecoder().raw_decode(value)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ShopperError(f"unsupported quoted {field} frontmatter") from exc
        if not isinstance(result, str) or (
            value[end:].strip() and not value[end:].lstrip().startswith("#")
        ):
            raise ShopperError(f"unsupported quoted {field} frontmatter")
        return result
    if value.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", value)
        if not match:
            raise ShopperError(f"unsupported quoted {field} frontmatter")
        return match.group(1).replace("''", "'")
    if value[0] in "[{&*!>|" or ": " in value:
        raise ShopperError(f"unsupported YAML syntax in {field}; use a quoted scalar")
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


def parse_frontmatter(text: str) -> dict[str, str]:
    """Extract name/description from plain, quoted, or block YAML scalars.

    Other fields are ignored as data. YAML anchors, tags, collections, duplicate
    routing keys, multiline quoted scalars, and implicit multiline plain scalars
    are not supported for the two routing fields.
    """
    lines = text.splitlines()
    if not lines or lines[0].lstrip("\ufeff") != "---":
        raise ShopperError("missing YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ShopperError("unterminated YAML frontmatter") from exc
    values: dict[str, str] = {}
    i = 1
    while i < end:
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^(name|description):(?:[ \t]+(.*)|[ \t]*)$", line)
        if not match:
            continue
        key, value = match.group(1), match.group(2) or ""
        if key in values:
            raise ShopperError(f"duplicate {key} frontmatter")
        block = re.fullmatch(r"([>|])([-+]?)(?:\s+#.*)?", value.strip())
        if block:
            continuation: list[str] = []
            while i < end and (not lines[i].strip() or lines[i].startswith(" ")):
                continuation.append(lines[i])
                i += 1
            populated = [item for item in continuation if item.strip()]
            if not populated:
                raise ShopperError(f"empty {key} block scalar")
            indent = min(len(item) - len(item.lstrip(" ")) for item in populated)
            if indent == 0:
                raise ShopperError(f"invalid {key} block indentation")
            stripped = [item[indent:] if item.strip() else "" for item in continuation]
            if block.group(1) == ">":
                # Paragraph boundaries matter only as whitespace for routing.
                result = "\n".join(" ".join(p.splitlines())
                                   for p in "\n".join(stripped).split("\n\n"))
            else:
                result = "\n".join(stripped)
            values[key] = result.strip()
        else:
            values[key] = _scalar(value, key)
            if i < end and lines[i].startswith((" ", "\t")) and lines[i].strip():
                raise ShopperError(f"unsupported multiline {key}; use a block scalar")
    name = values.get("name", "")
    description = values.get("description", "")
    if not NAME_RE.fullmatch(name):
        raise ShopperError("missing or invalid skill name (letters, digits, dots, hyphens, underscores)")
    if not description.strip() or len(description) > 8192:
        raise ShopperError("missing or oversized description (maximum 8192 characters)")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in description):
        raise ShopperError("control characters in description")
    return {"name": name, "description": description}


def _read_skill(path: Path) -> bytes:
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise ShopperError(f"skill is not a regular file: {path}")
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise ShopperError(f"cannot read skill {path}: {exc.strerror or exc}") from exc
    if len(data) > MAX_FILE_BYTES:
        raise ShopperError(f"skill exceeds {MAX_FILE_BYTES} bytes: {path}")
    return data


def _entry_from_file(path: Path) -> dict[str, Any]:
    data = _read_skill(path)
    try:
        metadata = parse_frontmatter(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ShopperError(f"skill is not UTF-8: {path}") from exc
    return {
        **metadata, "path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data), "estimated_tokens": estimated_tokens(len(data)),
    }


def _resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ShopperError(f"cannot resolve path {path}: {exc}") from exc


def build_catalog(roots: list[str | Path]) -> dict[str, Any]:
    """Index skill metadata and source digests; never run skill code.

    Scan at most eight directory levels and 20,000 directory entries. Symlinked
    installed skill directories are supported, with resolved-path deduplication.
    A directory containing SKILL.md is a skill boundary; its references are not
    recursively indexed. Invalid individual skills become explicit warnings.
    """
    if not isinstance(roots, (list, tuple)) or not 1 <= len(roots) <= MAX_ROOTS:
        raise ShopperError(f"provide 1 to {MAX_ROOTS} skill roots")
    resolved = sorted({_resolve(Path(root)) for root in roots}, key=str)
    pending = [(root, 0) for root in reversed(resolved)]
    seen_dirs: set[Path] = set()
    seen_files: set[Path] = set()
    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    scanned = 0
    while pending:
        path, depth = pending.pop()
        path = _resolve(path)
        if path.is_file():
            if path.name != "SKILL.md":
                raise ShopperError(f"root file must be named SKILL.md: {path}")
            files = [path]
        elif path.is_dir():
            if path in seen_dirs:
                continue
            seen_dirs.add(path)
            try:
                children: list[Path] = []
                with os.scandir(path) as iterator:
                    for child in iterator:
                        scanned += 1
                        if scanned > MAX_SCAN_ENTRIES:
                            raise ShopperError(f"scan exceeds {MAX_SCAN_ENTRIES} entries; choose narrower roots")
                        children.append(Path(child.path))
            except OSError as exc:
                warnings.append(f"cannot scan {path}: {exc.strerror or exc}")
                continue
            children.sort(key=lambda item: item.name)
            files = [child for child in children if child.name == "SKILL.md"]
            if not files:
                for child in reversed(children):
                    if child.name in IGNORED_DIRS:
                        continue
                    try:
                        if child.is_dir():
                            if depth >= MAX_DEPTH:
                                warnings.append(f"depth limit skipped directory: {child}")
                            else:
                                pending.append((child, depth + 1))
                    except OSError as exc:
                        warnings.append(f"cannot inspect {child}: {exc.strerror or exc}")
        else:
            raise ShopperError(f"root must be a directory or SKILL.md: {path}")
        for file in files:
            try:
                canonical = _resolve(file)
                if canonical in seen_files:
                    continue
                seen_files.add(canonical)
                entries.append(_entry_from_file(canonical))
            except ShopperError as exc:
                warnings.append(f"skipped {file}: {exc}")
            if len(entries) > MAX_SKILLS:
                raise ShopperError(f"catalog exceeds {MAX_SKILLS} skills; choose narrower roots")
    entries.sort(key=lambda item: (item["name"].casefold(), item["path"]))
    catalog = {
        "schema": CATALOG_SCHEMA, "estimator": ESTIMATOR,
        "roots": [str(root) for root in resolved], "skills": entries,
        "warnings": warnings,
    }
    _validate_catalog(catalog)
    return catalog


def _validate_entry(entry: Any) -> None:
    if not isinstance(entry, dict):
        raise ShopperError("skill entry must be an object")
    name, description, path = (entry.get(key) for key in ("name", "description", "path"))
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ShopperError("invalid skill name in record")
    if not isinstance(description, str) or not description.strip() or len(description) > 8192:
        raise ShopperError(f"invalid description for {name}")
    if not isinstance(path, str) or not Path(path).is_absolute() or "\x00" in path:
        raise ShopperError(f"skill path must be absolute for {name}")
    if not isinstance(entry.get("sha256"), str) or not SHA_RE.fullmatch(entry["sha256"]):
        raise ShopperError(f"invalid SHA-256 for {name}")
    size = _integer(entry.get("bytes"), f"bytes for {name}", 1, MAX_FILE_BYTES)
    tokens = _integer(entry.get("estimated_tokens"), f"estimated_tokens for {name}")
    if tokens != estimated_tokens(size):
        raise ShopperError(f"incorrect token estimate for {name}")


def _validate_unique(entries: list[dict[str, Any]]) -> None:
    names: dict[str, str] = {}
    paths: set[str] = set()
    for entry in entries:
        _validate_entry(entry)
        key = entry["name"].casefold()
        if key in names:
            raise ShopperError(f"ambiguous duplicate skill name {entry['name']!r}; use narrower roots")
        if entry["path"] in paths:
            raise ShopperError(f"duplicate skill path: {entry['path']}")
        names[key] = entry["path"]
        paths.add(entry["path"])


def _validate_warnings(value: Any) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ShopperError("warnings must be an array of strings")


def _validate_record_text(value: Any, depth: int = 0) -> None:
    """Reject strings JSON can escape but UTF-8 files and tool output cannot emit."""
    if depth > 64:
        raise ShopperError("JSON record nesting exceeds 64 levels")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ShopperError("record contains text that is not valid UTF-8") from exc
    elif isinstance(value, dict):
        for key, item in value.items():
            _validate_record_text(key, depth + 1)
            _validate_record_text(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_record_text(item, depth + 1)


def _validate_catalog(catalog: Any) -> None:
    if not isinstance(catalog, dict) or catalog.get("schema") != CATALOG_SCHEMA:
        raise ShopperError("unsupported or malformed catalog schema")
    _validate_record_text(catalog)
    if catalog.get("estimator") != ESTIMATOR:
        raise ShopperError("unsupported catalog token estimator")
    entries = catalog.get("skills")
    if not isinstance(entries, list) or len(entries) > MAX_SKILLS:
        raise ShopperError("invalid catalog skills array")
    roots = catalog.get("roots")
    if not isinstance(roots, list) or not 1 <= len(roots) <= MAX_ROOTS or any(
        not isinstance(root, str) or not Path(root).is_absolute() for root in roots
    ):
        raise ShopperError("invalid catalog roots")
    _validate_warnings(catalog.get("warnings"))
    _validate_unique(entries)


def _words(text: str) -> set[str]:
    return {word for word in WORD_RE.findall(text.casefold())
            if len(word) > 1 and word not in STOPWORDS}


def _project_terms(project: str | Path | None) -> set[str]:
    if project is None:
        return set()
    root = _resolve(Path(project))
    if not root.is_dir():
        raise ShopperError("project must be a directory")
    # Fixed marker names, no source reads or recursive traversal.
    terms: set[str] = set()
    for marker, aliases in PROJECT_MARKERS.items():
        if "*" not in marker and (root / marker).is_file():
            terms.update(_words(aliases))
    return terms


def _totals(entries: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "bytes": sum(entry["bytes"] for entry in entries),
        "estimated_tokens": sum(entry["estimated_tokens"] for entry in entries),
    }


def select_skills(catalog: dict[str, Any], task: str, budget: int = 2000,
                  max_skills: int = 3, required: list[str] | None = None,
                  project: str | Path | None = None) -> dict[str, Any]:
    """Select by local metadata overlap; never read skill bodies or call a model."""
    _validate_catalog(catalog)
    _integer(budget, "budget", 0, 1_000_000)
    _integer(max_skills, "max_skills", 0, MAX_SELECTION)
    if not isinstance(task, str) or not task.strip() or len(task) > 65_536:
        raise ShopperError("task must be nonempty text of at most 65536 characters")
    if required is None:
        required = []
    if not isinstance(required, (list, tuple)) or any(not isinstance(name, str) for name in required):
        raise ShopperError("required must be an array of skill names")
    wanted = list(dict.fromkeys(required))
    by_name = {entry["name"]: entry for entry in catalog["skills"]}
    missing = [name for name in wanted if name not in by_name]
    if missing:
        raise ShopperError("required skills missing from catalog: " + ", ".join(missing))
    if len(wanted) > max_skills:
        raise ShopperError("required skills exceed max_skills; increase the limit explicitly")
    selected = [{**by_name[name], "reason": "required", "matched_terms": [], "score": 0}
                for name in wanted]
    totals = _totals(selected)
    if totals["bytes"] > budget * 4 or totals["estimated_tokens"] > budget:
        raise ShopperError("required skills exceed the budget; increase it explicitly or reduce requirements")
    task_words, project_words = _words(task), _project_terms(project)
    ranked: list[tuple[int, dict[str, Any], list[str]]] = []
    for entry in catalog["skills"]:
        if entry["name"] in wanted or entry["name"].casefold() == "window-shopper":
            continue
        name_words, desc_words = _words(entry["name"]), _words(entry["description"])
        matched = sorted((task_words | project_words) & (name_words | desc_words))
        score = (6 * len(task_words & name_words) + 2 * len(task_words & desc_words)
                 + len(project_words & (name_words | desc_words)))
        if score:
            ranked.append((score, entry, matched))
    ranked.sort(key=lambda item: (-item[0], item[1]["name"].casefold(), item[1]["path"]))
    skipped_budget = 0
    for score, entry, matched in ranked:
        if len(selected) >= max_skills:
            break
        if (totals["bytes"] + entry["bytes"] > budget * 4 or
                totals["estimated_tokens"] + entry["estimated_tokens"] > budget):
            skipped_budget += 1
            continue
        selected.append({**entry, "reason": "metadata-match", "matched_terms": matched, "score": score})
        totals = _totals(selected)
    warnings = list(catalog["warnings"])
    if skipped_budget:
        warnings.append(f"{skipped_budget} matching skill(s) did not fit the budget")
    if not selected:
        warnings.append("No skills selected. Refine task wording, inspect the catalog, or explicitly require a skill and adjust limits.")
    status = ("selected" if selected else "limit_reached" if ranked and max_skills == 0
              else "budget_exhausted" if ranked else "no_match")
    selection = {
        "schema": SELECTION_SCHEMA, "estimator": ESTIMATOR,
        "budget": budget, "max_skills": max_skills, "selected": selected,
        "totals": totals,
        "catalog_baseline": {"skills": len(catalog["skills"]), **_totals(catalog["skills"])},
        "status": status, "warnings": warnings,
    }
    _validate_selection_record(selection)
    return selection


def _validate_selection_record(selection: Any) -> None:
    if not isinstance(selection, dict) or selection.get("schema") != SELECTION_SCHEMA:
        raise ShopperError("unsupported or malformed selection schema")
    _validate_record_text(selection)
    if selection.get("estimator") != ESTIMATOR:
        raise ShopperError("unsupported selection token estimator")
    budget = _integer(selection.get("budget"), "budget", 0, 1_000_000)
    limit = _integer(selection.get("max_skills"), "max_skills", 0, MAX_SELECTION)
    selected = selection.get("selected")
    if not isinstance(selected, list) or len(selected) > limit:
        raise ShopperError("selection exceeds max_skills or has an invalid selected array")
    _validate_unique(selected)
    for entry in selected:
        if not isinstance(entry.get("reason"), str) or entry["reason"] not in {"required", "metadata-match"}:
            raise ShopperError("invalid selection reason")
        terms = entry.get("matched_terms")
        if not isinstance(terms, list) or any(not isinstance(term, str) for term in terms):
            raise ShopperError("invalid matched_terms")
        _integer(entry.get("score"), "score")
    totals = _totals(selected)
    reported = selection.get("totals")
    if not isinstance(reported, dict):
        raise ShopperError("missing selection totals")
    for key, value in totals.items():
        _integer(reported.get(key), f"totals.{key}")
        if reported[key] != value:
            raise ShopperError("selection totals do not match its entries")
    if totals["bytes"] > budget * 4 or totals["estimated_tokens"] > budget:
        raise ShopperError("selection exceeds its declared budget")
    baseline = selection.get("catalog_baseline")
    if not isinstance(baseline, dict):
        raise ShopperError("missing catalog baseline")
    for key in ("skills", "bytes", "estimated_tokens"):
        _integer(baseline.get(key), f"catalog_baseline.{key}", 0, 1_073_741_824)
    if (baseline["skills"] < len(selected) or baseline["bytes"] < totals["bytes"]
            or baseline["estimated_tokens"] < totals["estimated_tokens"]):
        raise ShopperError("catalog baseline is smaller than the selection")
    status = selection.get("status")
    if not isinstance(status, str) or (selected and status != "selected") or (
        not selected and status not in {"no_match", "budget_exhausted", "limit_reached"}
    ):
        raise ShopperError("selection status disagrees with selected entries")
    _validate_warnings(selection.get("warnings"))


def _verified_sources(selection: dict[str, Any]) -> dict[str, str]:
    _validate_selection_record(selection)
    texts: dict[str, str] = {}
    for entry in selection["selected"]:
        path = Path(entry["path"])
        if str(_resolve(path)) != entry["path"]:
            raise ShopperError(f"skill path changed since indexing: {entry['name']}; rebuild catalog")
        data = _read_skill(path)
        if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ShopperError(f"stale or altered skill: {entry['name']}; rebuild catalog and selection")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ShopperError(f"skill is not UTF-8: {entry['name']}") from exc
        metadata = parse_frontmatter(text)
        if metadata["name"] != entry["name"] or metadata["description"] != entry["description"]:
            raise ShopperError(f"catalog metadata does not match source: {entry['name']}")
        texts[entry["name"]] = text
    return texts


def validate_selection(selection: dict[str, Any]) -> None:
    """Validate the full record and all selected source files, without emitting them."""
    _verified_sources(selection)


def load_skill(selection: dict[str, Any], name: str) -> str:
    """Return an allowlisted skill only after verifying the whole selected set."""
    texts = _verified_sources(selection)
    if name not in texts:
        raise ShopperError(f"skill {name!r} is not selected; rerun select with an explicit requirement if appropriate")
    return texts[name]


def report_selection(selection: dict[str, Any]) -> dict[str, Any]:
    """Report file-payload estimates, not savings against an agent's actual baseline."""
    validate_selection(selection)
    return {
        "status": selection["status"], "estimator": ESTIMATOR,
        "budget": selection["budget"], "max_skills": selection["max_skills"],
        "selected_skills": [entry["name"] for entry in selection["selected"]],
        "selected_payload": dict(selection["totals"]),
        "catalog_baseline": dict(selection["catalog_baseline"]),
        "remaining_estimated_tokens": selection["budget"] - selection["totals"]["estimated_tokens"],
        "note": "UTF-8 byte heuristic for complete SKILL.md files only. Catalog baseline assumes loading every indexed file; actual agent savings are unmeasured. Other prompt, tool, reference, and tokenizer overhead is excluded.",
        "warnings": list(selection["warnings"]),
    }


def read_json(path: str | Path) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ShopperError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ShopperError(f"invalid JSON constant: {value}")

    try:
        with Path(path).expanduser().open("rb") as handle:
            data = handle.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            raise ShopperError(f"JSON input exceeds {MAX_JSON_BYTES} bytes")
        result = json.loads(data, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ShopperError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(result, dict):
        raise ShopperError("JSON record must be an object")
    return result


def write_json(path: str | Path, value: dict[str, Any]) -> None:
    """Atomic explicit write; parent must already exist, with no implicit home path."""
    target = Path(path).expanduser()
    temporary: str | None = None
    try:
        encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                              allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ShopperError(f"cannot serialize JSON: {exc}") from exc
    if len(encoded) > MAX_JSON_BYTES:
        raise ShopperError(f"JSON output exceeds {MAX_JSON_BYTES} bytes; choose narrower roots")
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent,
                                         prefix=f".{target.name}.", suffix=".tmp",
                                         delete=False) as handle:
            temporary = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    except OSError as exc:
        raise ShopperError(f"cannot write JSON {target}: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="Build an offline catalog from installed skill roots")
    index.add_argument("--root", action="append", required=True)
    index.add_argument("--output", required=True)
    select = commands.add_parser("select", help="Choose skills using metadata only")
    select.add_argument("--catalog", required=True)
    select.add_argument("--task", required=True)
    select.add_argument("--budget", type=int, default=2000)
    select.add_argument("--max-skills", type=int, default=3)
    select.add_argument("--require", action="append", default=[])
    select.add_argument("--project")
    select.add_argument("--output")
    load = commands.add_parser("load", help="Verify selection and emit exactly one selected SKILL.md")
    load.add_argument("--selection", required=True)
    load.add_argument("--skill", required=True)
    report = commands.add_parser("report", help="Verify selection and report file payload estimates")
    report.add_argument("--selection", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "index":
            value = build_catalog(args.root)
            write_json(args.output, value)
            print(json.dumps({"catalog": str(Path(args.output).expanduser().absolute()),
                              "skills": len(value["skills"]), "warnings": value["warnings"]},
                             ensure_ascii=False, separators=(",", ":")))
        elif args.command == "select":
            value = select_skills(read_json(args.catalog), args.task, args.budget,
                                  args.max_skills, args.require, args.project)
            if args.output:
                write_json(args.output, value)
            print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        elif args.command == "load":
            sys.stdout.write(load_skill(read_json(args.selection), args.skill))
        else:
            print(json.dumps(report_selection(read_json(args.selection)),
                             ensure_ascii=False, separators=(",", ":")))
    except (ShopperError, TypeError) as exc:
        print(f"window-shopper: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
