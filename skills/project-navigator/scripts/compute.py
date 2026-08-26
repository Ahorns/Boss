"""Derived state: progress arithmetic, the current node, and the JSON payload.

Nothing here mutates the roadmap. Every number the dashboard and the CLI show
is computed from roadmap.yaml by these rules, never asserted by an agent.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from model import (GLYPH, ID_RE, graph_edges, index, paths, pending_plan_change,
                   plan_version, read_history, read_jsonl, read_proposals,
                   retired_nodes, walk)

# Fractional credit a leaf contributes.
#
# `failed` counts as 1.0 deliberately: execution stopped with a recorded failure.
# It is rendered distinctly and is never selected as the current node, but it
# must not hold the progress bar hostage. Scientific meaning lives in `outcome`.
#
# `deferred` is absent from this map: deferred leaves are excluded from the
# denominator entirely, so shelving work does not permanently drag the number
# down. See DEFERRED handling in subtree_progress().
LEAF_PROGRESS = {
    "done": 1.0,
    "failed": 1.0,
    "in_progress": 0.5,
    "todo": 0.0,
    "blocked": 0.0,
}

DONE_STATUSES = ("done", "failed")


def subtree_progress(node: dict) -> tuple[float | None, float]:
    """Return (fraction, weight). fraction is None when the node is excluded.

    A parent's fraction is the weighted mean over its children; its own status
    is ignored for arithmetic (validate() warns when the two contradict). A
    parent whose children are all deferred is itself excluded.
    """
    weight = float(node.get("weight", 1) or 1)
    children = node.get("children") or []

    if not children:
        if node.get("status") == "deferred":
            return None, 0.0
        return LEAF_PROGRESS.get(node.get("status"), 0.0), weight

    total_w = 0.0
    acc = 0.0
    for child in children:
        frac, w = subtree_progress(child)
        if frac is None:
            continue
        acc += frac * w
        total_w += w

    if total_w == 0.0:
        return None, 0.0
    return acc / total_w, weight


def progress_of(node: dict) -> float:
    frac, _ = subtree_progress(node)
    return 0.0 if frac is None else frac


def is_leaf(node: dict) -> bool:
    return not (node.get("children") or [])


def is_resolved(node: dict) -> bool:
    """True when no work is still owed on this node or anywhere beneath it.

    Uses the computed fraction rather than the node's own status, so a phase
    whose children are all done/failed stops blocking its dependents even if
    nobody remembered to close the phase itself. `deferred` (fraction None)
    counts as resolved - shelved work must not block forever.
    """
    frac, _ = subtree_progress(node)
    return frac is None or frac >= 1.0


def blockers_of(node: dict, by_id: dict[str, dict]) -> list[str]:
    """Unsatisfied blockers: id-typed entries not yet resolved, plus free text."""
    unmet = []
    for dep in node.get("blocked_by") or []:
        if ID_RE.match(dep):
            target = by_id.get(dep)
            if target is not None and is_resolved(target):
                continue
        unmet.append(dep)
    return unmet


def resolve_current(doc: dict) -> tuple[dict | None, str]:
    """Pick the 'YOU ARE HERE' node by rule. Returns (node, mode).

    mode is one of: explicit, in_progress, next_up, blocked, complete.
    """
    by_id = index(doc)
    leaves = [n for n, _d, _p in walk(doc) if is_leaf(n)]
    incoming: dict[str, list[dict]] = {nid: [] for nid in by_id}
    for edge in graph_edges(doc, include_contains=False):
        if edge.get("to") in incoming:
            incoming[edge["to"]].append(edge)

    def edge_satisfied(edge: dict) -> bool:
        source = by_id.get(edge.get("from"))
        if source is None:
            return False
        condition = edge.get("when", "next")
        if condition == "always":
            return True
        if condition == "pass":
            return source.get("outcome") == "passed"
        if condition == "fail":
            return source.get("outcome") == "failed"
        return is_resolved(source)

    def reachable(node: dict) -> bool:
        flow = incoming.get(node.get("id"), [])
        # Multiple incoming edges commonly represent alternative branches
        # rejoining. Parallel prerequisites remain explicit in `blocked_by:`.
        return not flow or any(edge_satisfied(edge) for edge in flow)

    explicit = doc.get("current")
    if explicit and explicit in by_id:
        return by_id[explicit], "explicit"

    for node in leaves:
        if node.get("status") == "in_progress":
            return node, "in_progress"

    for node in leaves:
        if (node.get("kind") in ("gate", "experiment")
                and node.get("status") == "done"
                and node.get("outcome") in ("active", "pending")):
            return node, "awaiting_outcome"

    for node in leaves:
        if (node.get("status") == "todo" and reachable(node)
                and not blockers_of(node, by_id)):
            return node, "next_up"

    for node in leaves:
        if node.get("status") in ("todo", "blocked") and reachable(node):
            return node, "blocked"

    return None, "complete"


def bar(fraction: float, width: int = 15) -> str:
    filled = int(round(fraction * width))
    return "█" * filled + "░" * (width - filled)


def criteria_state(node: dict) -> list[dict]:
    """Success criteria are text; we can only report them, not evaluate them.

    A criterion is shown as met when the node itself is done - that is the only
    honest inference available without a machine-checkable assertion language.
    """
    met = node.get("status") == "done"
    return [{"text": c, "met": met} for c in node.get("success_criteria") or []]


def node_payload(node: dict, depth: int, parent: dict | None, current_id: str | None,
                 by_id: dict[str, dict]) -> dict:
    frac, _ = subtree_progress(node)
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "kind": node.get("kind", "task"),
        "status": node.get("status"),
        "outcome": node.get("outcome", "active"),
        "glyph": GLYPH.get(node.get("status"), "?"),
        "depth": depth,
        "parent": parent.get("id") if parent else None,
        "leaf": is_leaf(node),
        "weight": float(node.get("weight", 1) or 1),
        "progress": None if frac is None else round(frac, 4),
        "goal": node.get("goal"),
        "question": node.get("question"),
        "experiment": node.get("experiment"),
        "criteria": criteria_state(node),
        "evidence": list(node.get("evidence") or []),
        "next_action": node.get("next_action"),
        "blocked_by": list(node.get("blocked_by") or []),
        "unmet_blockers": blockers_of(node, by_id),
        "note": node.get("note"),
        "is_current": node.get("id") == current_id,
    }


def read_events(root: Path, limit: int = 40) -> list[dict]:
    """The tail of the transition log, newest last."""
    p = paths(root)["events"]
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def build_state(doc: dict, root: Path) -> dict:
    """The single payload consumed by `pnav status`, state.json and /api/state."""
    by_id = index(doc)
    current, mode = resolve_current(doc)
    current_id = current.get("id") if current else None

    nodes = [
        node_payload(n, depth, parent, current_id, by_id)
        for n, depth, parent in walk(doc)
    ]

    total_w = 0.0
    acc = 0.0
    for top in doc.get("nodes") or []:
        frac, w = subtree_progress(top)
        if frac is None:
            continue
        acc += frac * w
        total_w += w
    overall = (acc / total_w) if total_w else 0.0

    counts: dict[str, int] = {}
    for n in nodes:
        if n["leaf"]:
            counts[n["status"]] = counts.get(n["status"], 0) + 1

    edges = graph_edges(doc)
    branches = []
    if current_id:
        for edge in edges:
            if edge.get("from") != current_id or edge.get("when") not in ("pass", "fail"):
                continue
            target = by_id.get(edge.get("to"), {})
            branches.append({
                "when": edge.get("when"), "to": edge.get("to"),
                "name": target.get("name"), "label": edge.get("label"),
            })

    state = {
        "project": doc.get("project"),
        "plan_version": plan_version(doc),
        "root": str(root),
        "progress": round(overall, 4),
        "mode": mode,
        "current": current_id,
        "current_node": next((n for n in nodes if n["id"] == current_id), None),
        "leaf_counts": counts,
        "nodes": nodes,
        "edges": edges,
        "current_branches": branches,
        "events": read_events(root),
        "changes": read_jsonl(paths(root)["changes"]),
        "history": read_history(root),
        "retired": retired_nodes(root, doc),
        "proposals": read_proposals(root),
        "plan_change": pending_plan_change(root, doc),
    }
    state["rev"] = hashlib.sha1(
        json.dumps(state, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]
    return state
