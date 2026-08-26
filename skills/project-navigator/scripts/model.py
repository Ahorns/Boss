"""Schema, loading, validation and atomic writing of .project/roadmap.yaml.

roadmap.yaml is the single source of truth for a project's state. Everything
else under .project/ is generated from it or is an append-only record of how
that current plan came to be.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import yaml

# ---------------------------------------------------------------- constants

STATUSES = ("todo", "in_progress", "done", "failed", "blocked", "deferred")
KINDS = ("task", "experiment", "gate", "milestone", "stop")
OUTCOMES = (
    "active", "pending", "passed", "failed", "inconclusive",
    "superseded", "deferred", "abandoned", "not_needed",
)
EDGE_CONDITIONS = ("next", "pass", "fail", "always")

GLYPH = {
    "done": "✅",
    "in_progress": "\U0001f504",
    "todo": "⬜",
    "failed": "❌",
    "blocked": "\U0001f6a7",
    "deferred": "\U0001f4a4",
}

# Canonical key order used when the CLI rewrites the file.
NODE_KEYS = (
    "id",
    "name",
    "kind",
    "status",
    "outcome",
    "weight",
    "question",
    "experiment",
    "goal",
    "success_criteria",
    "evidence",
    "next_action",
    "blocked_by",
    "note",
    "children",
)

DOC_KEYS = ("project", "plan_version", "current", "nodes", "edges")
EDGE_KEYS = ("from", "to", "when", "label")

LIST_KEYS = ("success_criteria", "evidence", "blocked_by")

ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

PROJECT_DIR = ".project"
ROADMAP = "roadmap.yaml"
STATE = "state.json"
EVENTS = "events.jsonl"
BACKUP = "roadmap.bak"
STRUCTURE = "structure.json"
DECISIONS = "decisions.md"
HISTORY = "history"
CHANGES = "changes.jsonl"
PROPOSALS = "proposals.jsonl"


class PnavError(Exception):
    """Fatal, user-facing error. Printed without a traceback."""


# ------------------------------------------------------------ root discovery


def find_root(explicit: str | None = None, start: Path | None = None) -> Path:
    """Locate the project root: the dir containing .project/roadmap.yaml.

    With --project the path is used as given (it need not exist yet, so that
    `pnav init --project X` works). Otherwise walk up from cwd.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()

    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / PROJECT_DIR / ROADMAP).is_file():
            return candidate
    raise PnavError(
        "no .project/roadmap.yaml found in this directory or any parent.\n"
        "  Run `pnav init` here, or pass --project PATH."
    )


def paths(root: Path) -> dict[str, Path]:
    d = root / PROJECT_DIR
    return {
        "dir": d,
        "roadmap": d / ROADMAP,
        "state": d / STATE,
        "events": d / EVENTS,
        "backup": d / BACKUP,
        "structure": d / STRUCTURE,
        "decisions": d / DECISIONS,
        "history": d / HISTORY,
        "changes": d / CHANGES,
        "proposals": d / PROPOSALS,
    }


# ------------------------------------------------------------------- loading


def load(root: Path) -> dict:
    """Read and normalise roadmap.yaml. Raises PnavError on unusable input."""
    p = paths(root)["roadmap"]
    if not p.is_file():
        raise PnavError(f"{p} does not exist. Run `pnav init` first.")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PnavError(f"{p} is not valid YAML:\n  {exc}") from exc
    if raw is None:
        raise PnavError(f"{p} is empty.")
    if not isinstance(raw, dict):
        raise PnavError(f"{p} must be a mapping at the top level, got {type(raw).__name__}.")
    return normalize(raw)


def normalize(doc: dict) -> dict:
    """Coerce shorthand into the canonical shape. Does not validate."""
    doc.setdefault("project", "unnamed")
    doc.setdefault("plan_version", 1)
    nodes = doc.get("nodes")
    if nodes is None:
        # Tolerate the shorthand `phases:` used in early sketches.
        nodes = doc.pop("phases", None) or []
        doc["nodes"] = nodes
    if not isinstance(doc["nodes"], list):
        raise PnavError("top-level `nodes:` must be a list.")
    for node in doc["nodes"]:
        _normalize_node(node)
    edges = doc.get("edges")
    if edges is None:
        doc["edges"] = []
    elif not isinstance(edges, list):
        raise PnavError("top-level `edges:` must be a list.")
    else:
        for edge in edges:
            if not isinstance(edge, dict):
                raise PnavError(f"every edge must be a mapping, got {type(edge).__name__}: {edge!r}")
            if isinstance(edge.get("when"), str):
                edge["when"] = edge["when"].strip().lower().replace(" ", "_")
            edge.setdefault("when", "next")
    return doc


def _normalize_node(node) -> None:
    if not isinstance(node, dict):
        raise PnavError(f"every node must be a mapping, got {type(node).__name__}: {node!r}")

    if isinstance(node.get("status"), str):
        node["status"] = node["status"].strip().lower().replace(" ", "_").replace("-", "_")
    node.setdefault("status", "todo")
    if isinstance(node.get("kind"), str):
        node["kind"] = node["kind"].strip().lower().replace(" ", "_")
    node.setdefault("kind", "task")
    if isinstance(node.get("outcome"), str):
        node["outcome"] = node["outcome"].strip().lower().replace(" ", "_").replace("-", "_")
    node.setdefault("outcome", "active")

    # A bare string where a list belongs is a common hand-edit; accept it.
    for key in LIST_KEYS:
        if key in node:
            val = node[key]
            if val is None:
                node[key] = []
            elif isinstance(val, str):
                node[key] = [val]
            elif not isinstance(val, list):
                node[key] = [str(val)]
            else:
                node[key] = [str(v) for v in val]

    # `tasks:` is the shorthand from the original sketch.
    if "children" not in node and "tasks" in node:
        node["children"] = node.pop("tasks")
    if node.get("children") is None:
        node.pop("children", None)
    for child in node.get("children", []) or []:
        _normalize_node(child)


def walk(doc: dict):
    """Yield (node, depth, parent) depth-first in document order."""

    def rec(nodes, depth, parent):
        for node in nodes:
            yield node, depth, parent
            yield from rec(node.get("children") or [], depth + 1, node)

    yield from rec(doc.get("nodes") or [], 0, None)


def index(doc: dict) -> dict[str, dict]:
    return {n["id"]: n for n, _, _ in walk(doc) if isinstance(n.get("id"), str)}


def graph_edges(doc: dict, include_contains: bool = True) -> list[dict]:
    """Return canonical directed edges, including legacy tree containment.

    `children:` remains a compact authoring form and becomes a `contains` edge
    in the dashboard graph. Explicit top-level `edges:` carry research flow
    such as PASS/FAIL and may form a DAG with merges.
    """
    out: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()

    if include_contains:
        for node, _depth, parent in walk(doc):
            if parent and isinstance(parent.get("id"), str) and isinstance(node.get("id"), str):
                edge = {"from": parent["id"], "to": node["id"], "when": "contains"}
                key = (edge["from"], edge["to"], edge["when"], "")
                if key not in seen:
                    seen.add(key)
                    out.append(edge)

    for raw in doc.get("edges") or []:
        if not isinstance(raw, dict):
            continue
        edge = {
            "from": raw.get("from"),
            "to": raw.get("to"),
            "when": raw.get("when", "next"),
        }
        if raw.get("label"):
            edge["label"] = raw["label"]
        key = (str(edge["from"]), str(edge["to"]), str(edge["when"]), str(edge.get("label", "")))
        if key not in seen:
            seen.add(key)
            out.append(edge)
    return out


def get_node(doc: dict, node_id: str) -> dict:
    node = index(doc).get(node_id)
    if node is None:
        known = ", ".join(sorted(index(doc))) or "(none)"
        raise PnavError(f"no node with id {node_id!r}.\n  Known ids: {known}")
    return node


# ---------------------------------------------------------------- validation


def validate(doc: dict, raw_text: str | None = None) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors mean the file must not be written."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(doc.get("project"), str) or not doc["project"].strip():
        errors.append("`project:` must be a non-empty string.")

    version = doc.get("plan_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append("`plan_version:` must be a positive integer.")

    for key in doc:
        if key not in DOC_KEYS:
            warnings.append(f"unknown top-level key `{key}:` (ignored).")

    seen: dict[str, int] = {}
    for node, _depth, _parent in walk(doc):
        nid = node.get("id")
        label = f"node {nid!r}" if nid else f"node {node.get('name', '?')!r}"

        if not isinstance(nid, str) or not nid:
            errors.append(f"{label}: missing `id:`.")
        elif not ID_RE.match(nid):
            errors.append(f"{label}: id must match [A-Za-z0-9._-] (no spaces).")
        else:
            seen[nid] = seen.get(nid, 0) + 1

        if not isinstance(node.get("name"), str) or not node["name"].strip():
            errors.append(f"{label}: missing `name:`.")

        status = node.get("status")
        if status not in STATUSES:
            errors.append(
                f"{label}: invalid status {status!r}. "
                f"Allowed: {', '.join(STATUSES)}."
            )

        kind = node.get("kind", "task")
        if kind not in KINDS:
            errors.append(f"{label}: invalid kind {kind!r}. Allowed: {', '.join(KINDS)}.")

        outcome = node.get("outcome", "active")
        if outcome not in OUTCOMES:
            errors.append(
                f"{label}: invalid outcome {outcome!r}. Allowed: {', '.join(OUTCOMES)}."
            )

        weight = node.get("weight", 1)
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0:
            errors.append(f"{label}: `weight:` must be a positive number, got {weight!r}.")

        for key in node:
            if key not in NODE_KEYS:
                warnings.append(f"{label}: unknown key `{key}:` (ignored) - typo?")

        children = node.get("children")
        if children is not None and not isinstance(children, list):
            errors.append(f"{label}: `children:` must be a list.")

        if status == "blocked" and not node.get("blocked_by"):
            errors.append(f"{label}: status is `blocked` but `blocked_by:` is empty.")

        if status == "done" and not node.get("evidence") and not children:
            warnings.append(f"{label}: status is `done` with no `evidence:`.")

    for nid, count in seen.items():
        if count > 1:
            errors.append(f"duplicate id {nid!r} appears {count} times.")

    graph_errors, graph_warnings = _check_graph(doc, set(seen))
    errors.extend(graph_errors)
    warnings.extend(graph_warnings)
    errors.extend(_check_blockers(doc))
    warnings.extend(_check_consistency(doc))

    current = doc.get("current")
    if current is not None:
        if not isinstance(current, str):
            errors.append("`current:` must be a string id.")
        elif current not in seen:
            errors.append(f"`current: {current}` does not match any node id.")

    if raw_text and re.search(r"(?m)^\s*#", raw_text):
        warnings.append(
            "file contains `#` comments - the CLI drops them on rewrite. "
            "Put prose in `goal:` or `note:` instead."
        )

    return errors, warnings


def _check_graph(doc: dict, ids: set[str]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    adjacency: dict[str, list[str]] = {nid: [] for nid in ids}
    outgoing: dict[str, set[str]] = {nid: set() for nid in ids}

    for i, edge in enumerate(doc.get("edges") or []):
        label = f"edge {i + 1}"
        if not isinstance(edge, dict):
            errors.append(f"{label}: must be a mapping.")
            continue
        for key in edge:
            if key not in EDGE_KEYS:
                warnings.append(f"{label}: unknown key `{key}:` (ignored) - typo?")
        source, target = edge.get("from"), edge.get("to")
        condition = edge.get("when", "next")
        if source not in ids:
            errors.append(f"{label}: `from: {source}` matches no node id.")
        if target not in ids:
            errors.append(f"{label}: `to: {target}` matches no node id.")
        if source == target and source in ids:
            errors.append(f"{label}: a node cannot point to itself ({source}).")
        if condition not in EDGE_CONDITIONS:
            errors.append(
                f"{label}: invalid `when: {condition}`. Allowed: {', '.join(EDGE_CONDITIONS)}."
            )
        if source in ids and target in ids and source != target:
            adjacency[source].append(target)
            outgoing[source].add(condition)

    # Containment is also directed and must remain acyclic; explicit research
    # edges may merge, but they may not loop back onto themselves.
    all_adjacency: dict[str, list[str]] = {nid: [] for nid in ids}
    for edge in graph_edges(doc):
        source, target = edge.get("from"), edge.get("to")
        if source in ids and target in ids:
            all_adjacency[source].append(target)
    cycle = _find_cycle(all_adjacency)
    if cycle:
        errors.append("research graph cycle: " + " -> ".join(cycle))

    by_id = index(doc)
    for nid, conditions in outgoing.items():
        if by_id.get(nid, {}).get("kind") == "gate":
            missing = {"pass", "fail"} - conditions
            if missing:
                warnings.append(
                    f"gate {nid!r} has no {', '.join(sorted(missing)).upper()} outgoing edge."
                )
    return errors, warnings


def _check_blockers(doc: dict) -> list[str]:
    """blocked_by entries without spaces are node ids and must resolve."""
    errors: list[str] = []
    ids = set(index(doc))
    edges: dict[str, list[str]] = {}

    for node, _d, _p in walk(doc):
        nid = node.get("id")
        deps = []
        for dep in node.get("blocked_by") or []:
            if ID_RE.match(dep):
                if dep not in ids:
                    errors.append(f"node {nid!r}: `blocked_by: {dep}` matches no node id.")
                elif dep == nid:
                    errors.append(f"node {nid!r}: blocked by itself.")
                else:
                    deps.append(dep)
        if isinstance(nid, str):
            edges[nid] = deps

    cycle = _find_cycle(edges)
    if cycle:
        errors.append("blocked_by cycle: " + " -> ".join(cycle))
    return errors


def _find_cycle(edges: dict[str, list[str]]) -> list[str] | None:
    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(edges, WHITE)
    stack: list[str] = []

    def visit(n):
        color[n] = GREY
        stack.append(n)
        for m in edges.get(n, []):
            if color.get(m) == GREY:
                return stack[stack.index(m):] + [m]
            if color.get(m, BLACK) == WHITE:
                found = visit(m)
                if found:
                    return found
        stack.pop()
        color[n] = BLACK
        return None

    for n in edges:
        if color[n] == WHITE:
            found = visit(n)
            if found:
                return found
    return None


def _check_consistency(doc: dict) -> list[str]:
    """Soft signals: contradictions between a parent and its children."""
    warnings: list[str] = []
    for node, _d, _p in walk(doc):
        children = node.get("children") or []
        if not children:
            continue
        statuses = {c.get("status") for c in children}
        if node.get("status") == "done" and statuses - {"done", "failed", "deferred"}:
            warnings.append(
                f"node {node.get('id')!r} is `done` but has unfinished children."
            )
        if node.get("status") == "todo" and statuses & {"done", "in_progress", "failed"}:
            warnings.append(
                f"node {node.get('id')!r} is `todo` but work on its children has started."
            )
        # "finished" needs at least one real result; an all-deferred phase is
        # shelved, not complete, and should be deferred rather than closed.
        if (node.get("status") in ("todo", "in_progress")
                and statuses & {"done", "failed"}
                and not statuses - {"done", "failed", "deferred"}):
            warnings.append(
                f"node {node.get('id')!r} is `{node['status']}` but every child is "
                f"finished - close it with `pnav done {node.get('id')}`."
            )
    return warnings


def require_valid(doc: dict, raw_text: str | None = None, strict: bool = False) -> list[str]:
    """Raise if the document is invalid; return warnings otherwise."""
    errors, warnings = validate(doc, raw_text)
    if errors:
        raise PnavError(
            "roadmap.yaml is invalid - nothing was written:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
    if strict and warnings:
        raise PnavError(
            "strict mode: warnings treated as errors:\n"
            + "\n".join(f"  - {w}" for w in warnings)
        )
    return warnings


# ------------------------------------------------------------------- writing


class _Dumper(yaml.SafeDumper):
    """Block-style dumper that keeps multi-line strings readable."""


def _str_representer(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _str_representer)


def _ordered(node: dict) -> dict:
    """Reorder a node's keys canonically, dropping empties that carry no meaning."""
    out: dict = {}
    for key in NODE_KEYS:
        if key not in node:
            continue
        val = node[key]
        if key == "children":
            kids = [_ordered(c) for c in (val or [])]
            if kids:
                out[key] = kids
            continue
        if key == "weight" and val == 1:
            continue
        if val is None:
            continue
        if key in LIST_KEYS and not val:
            continue
        out[key] = val
    # Preserve unknown keys rather than silently deleting the user's data.
    for key, val in node.items():
        if key not in NODE_KEYS:
            out[key] = val
    return out


def dumps(doc: dict) -> str:
    ordered: dict = {"project": doc.get("project", "unnamed")}
    ordered["plan_version"] = int(doc.get("plan_version", 1))
    if doc.get("current"):
        ordered["current"] = doc["current"]
    for key, val in doc.items():
        if key not in DOC_KEYS:
            ordered[key] = val
    ordered["nodes"] = [_ordered(n) for n in (doc.get("nodes") or [])]
    edges = []
    for edge in doc.get("edges") or []:
        item = {key: edge[key] for key in EDGE_KEYS if key in edge and edge[key] not in (None, "")}
        if item:
            edges.append(item)
    if edges:
        ordered["edges"] = edges

    body = yaml.dump(
        ordered,
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=10_000,
    )
    return body


def write(root: Path, doc: dict) -> None:
    """Validate, back up, then replace roadmap.yaml atomically."""
    require_valid(doc)
    p = paths(root)
    p["dir"].mkdir(parents=True, exist_ok=True)

    if p["roadmap"].is_file():
        shutil.copy2(p["roadmap"], p["backup"])

    text = dumps(doc)
    fd, tmp = tempfile.mkstemp(dir=str(p["dir"]), prefix=".roadmap.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p["roadmap"])
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def append_event(root: Path, event: dict) -> None:
    """Append one line to the immutable transition log."""
    p = paths(root)
    p["dir"].mkdir(parents=True, exist_ok=True)
    with p["events"].open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def write_state(root: Path, state: dict) -> None:
    """Refresh the generated snapshot other tools can read without PyYAML."""
    p = paths(root)
    p["dir"].mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["_generated"] = "GENERATED by pnav from .project/roadmap.yaml - do not edit"
    tmp = p["dir"] / ".state.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p["state"])


# --------------------------------------------------------- structural changes
#
# Status transitions are the normal path and go through the CLI. Changing the
# *shape* of the plan - adding, removing, re-parenting or re-weighting nodes -
# is a research decision, and the whole point of this tool is that it cannot
# happen without leaving a record. So the shape is fingerprinted on every write
# and compared on every read.


def structure_of(doc: dict) -> dict:
    """The shape of the plan, independent of how far along any of it is."""
    out = {}
    for node, _depth, parent in walk(doc):
        nid = node.get("id")
        if not isinstance(nid, str):
            continue
        out[nid] = {
            "name": node.get("name"),
            "kind": node.get("kind", "task"),
            "parent": parent.get("id") if parent else None,
            "weight": float(node.get("weight", 1) or 1),
        }
    edges = [
        {"from": e.get("from"), "to": e.get("to"), "when": e.get("when", "next"),
         **({"label": e["label"]} if e.get("label") else {})}
        for e in (doc.get("edges") or []) if isinstance(e, dict)
    ]
    return {"nodes": out, "edges": sorted(edges, key=lambda e: (
        str(e.get("from")), str(e.get("to")), str(e.get("when")), str(e.get("label", ""))))}


def read_structure(root: Path) -> dict | None:
    p = paths(root)["structure"]
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def write_structure(root: Path, doc: dict) -> None:
    p = paths(root)
    p["dir"].mkdir(parents=True, exist_ok=True)
    tmp = p["dir"] / ".structure.json.tmp"
    tmp.write_text(json.dumps(structure_of(doc), ensure_ascii=False, indent=2, sort_keys=True)
                   + "\n", encoding="utf-8")
    os.replace(tmp, p["structure"])


def diff_structure(old: dict | None, new: dict) -> list[str]:
    """Human-readable description of how the plan's shape changed."""
    if old is None:
        return []

    old_nodes = old.get("nodes", old) if isinstance(old, dict) else {}
    new_nodes = new.get("nodes", new) if isinstance(new, dict) else {}
    old_edges = old.get("edges", []) if isinstance(old, dict) and "nodes" in old else []
    new_edges = new.get("edges", []) if isinstance(new, dict) and "nodes" in new else []

    changes: list[str] = []
    for nid in new_nodes:
        if nid not in old_nodes:
            parent = new_nodes[nid]["parent"]
            where = f" under {parent}" if parent else " at the top level"
            changes.append(f'added {nid} "{new_nodes[nid]["name"]}"{where}')
    for nid in old_nodes:
        if nid not in new_nodes:
            changes.append(f'removed {nid} "{old_nodes[nid]["name"]}"')
    for nid in new_nodes:
        if nid not in old_nodes:
            continue
        a, b = old_nodes[nid], new_nodes[nid]
        if a["name"] != b["name"]:
            changes.append(f'renamed {nid}: "{a["name"]}" -> "{b["name"]}"')
        if a.get("kind", "task") != b.get("kind", "task"):
            changes.append(f'retyped {nid}: {a.get("kind", "task")} -> {b.get("kind", "task")}')
        if a["parent"] != b["parent"]:
            changes.append(f'moved {nid}: {a["parent"] or "top level"} -> {b["parent"] or "top level"}')
        if a["weight"] != b["weight"]:
            changes.append(f'reweighted {nid}: {a["weight"]:g} -> {b["weight"]:g}')

    def edge_key(edge: dict) -> tuple:
        return (edge.get("from"), edge.get("to"), edge.get("when", "next"), edge.get("label"))

    old_edge_map = {edge_key(e): e for e in old_edges}
    new_edge_map = {edge_key(e): e for e in new_edges}
    for key in new_edge_map.keys() - old_edge_map.keys():
        source, target, condition, label = key
        suffix = f' "{label}"' if label else ""
        changes.append(f"added edge {source} -[{condition}{suffix}]-> {target}")
    for key in old_edge_map.keys() - new_edge_map.keys():
        source, target, condition, label = key
        suffix = f' "{label}"' if label else ""
        changes.append(f"removed edge {source} -[{condition}{suffix}]-> {target}")
    return sorted(changes)


def pending_plan_change(root: Path, doc: dict) -> list[str]:
    """Structural edits made outside the CLI and not yet explained."""
    return diff_structure(read_structure(root), structure_of(doc))


def append_decision(root: Path, when: str, reason: str, changes: list[str]) -> None:
    p = paths(root)
    p["dir"].mkdir(parents=True, exist_ok=True)
    entry = [f"## {when} - PLAN CHANGE", "", "**Reason**", "", reason.strip(), "",
             "**Changes**", ""]
    entry += [f"- {c}" for c in changes] or ["- (none recorded)"]
    entry += ["", "---", ""]
    with p["decisions"].open("a", encoding="utf-8") as fh:
        if fh.tell() == 0:
            fh.write("# Plan changes\n\nAppend-only. Each entry explains a change to the "
                     "*shape* of the roadmap.\n\n---\n\n")
        fh.write("\n".join(entry))


# ----------------------------------------------------------- plan evolution


def plan_version(doc: dict) -> int:
    value = doc.get("plan_version", 1)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1


def snapshot_plan(root: Path, doc: dict) -> Path:
    """Persist one immutable full plan snapshot for the document's version."""
    p = paths(root)
    p["history"].mkdir(parents=True, exist_ok=True)
    target = p["history"] / f"v{plan_version(doc):03d}.yaml"
    text = dumps(doc)
    if target.exists():
        # Status/evidence may change while a plan version stays the same. The
        # first snapshot is intentionally immutable: it records the plan as it
        # was accepted, not every execution-state update afterwards.
        return target
    tmp = p["history"] / f".{target.name}.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return target


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def append_jsonl(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def append_change(root: Path, when: str, reason: str, changes: list[str],
                  from_version: int, to_version: int, evidence: list[str] | None = None,
                  proposal_id: str | None = None) -> dict:
    existing = read_jsonl(paths(root)["changes"])
    record = {
        "change_id": f"C{len(existing) + 1:04d}",
        "time": when,
        "from_version": from_version,
        "to_version": to_version,
        "reason": reason.strip(),
        "changes": list(changes),
        "evidence": list(evidence or []),
    }
    if proposal_id:
        record["proposal_id"] = proposal_id
    append_jsonl(paths(root)["changes"], record)
    return record


def append_proposal(root: Path, when: str, reason: str, suggested_changes: list[str],
                    evidence: list[str] | None = None) -> dict:
    rows = read_jsonl(paths(root)["proposals"])
    created = [r for r in rows if r.get("event") == "PROPOSED"]
    record = {
        "event": "PROPOSED",
        "proposal_id": f"P{len(created) + 1:04d}",
        "time": when,
        "reason": reason.strip(),
        "suggested_changes": list(suggested_changes),
        "evidence": list(evidence or []),
    }
    append_jsonl(paths(root)["proposals"], record)
    return record


def close_proposal(root: Path, proposal_id: str, when: str, status: str,
                   message: str, plan_version_value: int | None = None) -> None:
    if status not in ("ACCEPTED", "REJECTED"):
        raise ValueError(status)
    record = {
        "event": status,
        "proposal_id": proposal_id,
        "time": when,
        "message": message.strip(),
    }
    if plan_version_value is not None:
        record["plan_version"] = plan_version_value
    append_jsonl(paths(root)["proposals"], record)


def read_proposals(root: Path) -> list[dict]:
    latest: dict[str, dict] = {}
    created: dict[str, dict] = {}
    order: list[str] = []
    for row in read_jsonl(paths(root)["proposals"]):
        pid = row.get("proposal_id")
        if not isinstance(pid, str):
            continue
        if row.get("event") == "PROPOSED" and pid not in created:
            created[pid] = dict(row)
            order.append(pid)
        latest[pid] = row
    out = []
    for pid in order:
        item = dict(created[pid])
        tail = latest.get(pid, {})
        item["status"] = str(tail.get("event", "PROPOSED")).lower()
        if tail.get("event") != "PROPOSED":
            item["closed_time"] = tail.get("time")
            item["resolution"] = tail.get("message")
            item["plan_version"] = tail.get("plan_version")
        out.append(item)
    return out


def read_history(root: Path) -> list[dict]:
    directory = paths(root)["history"]
    if not directory.is_dir():
        return []
    out = []
    for file in sorted(directory.glob("v[0-9][0-9][0-9].yaml")):
        try:
            raw = yaml.safe_load(file.read_text(encoding="utf-8"))
            doc = normalize(raw) if isinstance(raw, dict) else None
        except (OSError, yaml.YAMLError, PnavError):
            continue
        if not doc:
            continue
        nodes = list(walk(doc))
        out.append({
            "version": plan_version(doc),
            "project": doc.get("project"),
            "current": doc.get("current"),
            "node_count": len(nodes),
            "file": str(file),
        })
    return out


def retired_nodes(root: Path, current_doc: dict) -> list[dict]:
    """Research paths no longer active, from current lifecycle or old plans."""
    current_ids = set(index(current_doc))
    retired_outcomes = {"superseded", "deferred", "abandoned", "not_needed"}
    out: dict[str, dict] = {}

    for node, _depth, _parent in walk(current_doc):
        if node.get("outcome") in retired_outcomes:
            out[node["id"]] = {
                "id": node["id"], "name": node.get("name"),
                "outcome": node.get("outcome"), "version": plan_version(current_doc),
                "note": node.get("note"), "evidence": list(node.get("evidence") or []),
            }

    directory = paths(root)["history"]
    if directory.is_dir():
        for file in sorted(directory.glob("v[0-9][0-9][0-9].yaml"), reverse=True):
            try:
                raw = yaml.safe_load(file.read_text(encoding="utf-8"))
                old = normalize(raw) if isinstance(raw, dict) else None
            except (OSError, yaml.YAMLError, PnavError):
                continue
            if not old:
                continue
            for node, _depth, _parent in walk(old):
                nid = node.get("id")
                if not isinstance(nid, str) or nid in current_ids or nid in out:
                    continue
                out[nid] = {
                    "id": nid, "name": node.get("name"),
                    "outcome": node.get("outcome") if node.get("outcome") not in (None, "active", "pending") else "superseded",
                    "version": plan_version(old), "note": node.get("note"),
                    "evidence": list(node.get("evidence") or []),
                }
    return list(out.values())


# ------------------------------------------------------------------ discovery
#
# The hub finds projects by walking the filesystem rather than reading a list of
# them from the tool's own directory. That keeps the tooling free of any record
# of which projects exist, and means a new project appears the moment it is
# initialised, with nothing to keep in sync.

PRUNE = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "build", "dist", ".cache", ".tox", ".mypy_cache", "site-packages",
    "target", ".next", ".gradle", "third_party", "third-party",
}


def discover_projects(roots, max_depth: int = 4) -> list[Path]:
    """Directories containing .project/roadmap.yaml, nearest first."""
    found: list[Path] = []
    seen: set[Path] = set()

    for raw in roots:
        base = Path(raw).expanduser().resolve()
        if not base.is_dir():
            continue
        base_depth = len(base.parts)

        for dirpath, dirnames, _files in os.walk(base, followlinks=False):
            here = Path(dirpath)
            depth = len(here.parts) - base_depth

            if depth >= max_depth:
                dirnames[:] = []
            else:
                # Prune noise, but never a hidden dir we might need to look into
                # for .project itself - that check happens on `here`, not below.
                dirnames[:] = [
                    d for d in dirnames
                    if d not in PRUNE and not (d.startswith(".") and d != ".project")
                ]

            if (here / PROJECT_DIR / ROADMAP).is_file() and here not in seen:
                seen.add(here)
                found.append(here)
                dirnames[:] = []   # projects do not nest

    return sorted(found, key=lambda p: str(p).lower())
