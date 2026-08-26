#!/usr/bin/env python3
"""Self-test for project-navigator. No pytest required:

    python3 tests/selftest.py

Covers the arithmetic and the validation rules that the rest of the system
trusts. Run it after changing anything in scripts/.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import yaml  # noqa: E402

from compute import build_state, resolve_current  # noqa: E402
from model import (diff_structure, discover_projects, normalize,  # noqa: E402
                   read_proposals, structure_of, validate)

PNAV = [sys.executable, str(SCRIPTS / "pnav.py")]

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def doc_from(text: str) -> dict:
    return normalize(yaml.safe_load(text))


# ------------------------------------------------------------------ arithmetic

FIXTURE = """
project: Fixture
nodes:
  - id: P1
    name: Weighted phase
    status: in_progress
    weight: 2
    children:
      - {id: T1, name: a, status: done, evidence: [x]}
      - {id: T2, name: b, status: in_progress}
      - {id: T3, name: c, status: deferred}
  - id: P2
    name: Mixed phase
    status: in_progress
    children:
      - {id: T4, name: d, status: failed, evidence: [y]}
      - {id: T5, name: e, status: todo, weight: 3}
  - id: P4
    name: Entirely shelved
    status: todo
    weight: 5
    children:
      - {id: T6, name: f, status: deferred}
      - {id: T7, name: g, status: deferred}
"""


def test_arithmetic() -> None:
    state = build_state(doc_from(FIXTURE), Path("/tmp"))
    by = {n["id"]: n["progress"] for n in state["nodes"]}

    # done=1, in_progress=0.5, deferred excluded -> (1 + 0.5) / 2
    check("weighted parent excludes deferred child", by["P1"] == 0.75, str(by["P1"]))
    # failed counts as finished work: (1*1 + 0*3) / 4
    check("failed counts 1.0; weight 3 dominates", by["P2"] == 0.25, str(by["P2"]))
    # every child deferred -> the parent is excluded, not 0%
    check("all-deferred parent is excluded", by["P4"] is None, repr(by["P4"]))
    # (0.75*2 + 0.25*1) / (2+1); P4 contributes no weight at all
    want = round((0.75 * 2 + 0.25 * 1) / 3, 4)
    check("overall ignores excluded subtree", state["progress"] == want,
          f"{state['progress']} != {want}")


# --------------------------------------------------------- current resolution

def test_current() -> None:
    base = """
project: X
nodes:
  - {id: A, name: a, status: done, evidence: [e]}
  - {id: B, name: b, status: in_progress}
  - {id: C, name: c, status: todo}
"""
    node, mode = resolve_current(doc_from(base))
    check("current = first in_progress leaf", (node["id"], mode) == ("B", "in_progress"))

    node, mode = resolve_current(doc_from(base + "current: C\n"))
    check("explicit current wins", (node["id"], mode) == ("C", "explicit"))

    no_wip = """
project: X
nodes:
  - {id: A, name: a, status: done, evidence: [e]}
  - {id: C, name: c, status: todo}
"""
    node, mode = resolve_current(doc_from(no_wip))
    check("falls back to first unblocked todo", (node["id"], mode) == ("C", "next_up"))

    all_blocked = """
project: X
nodes:
  - {id: A, name: a, status: todo}
  - {id: C, name: c, status: todo, blocked_by: [A]}
"""
    node, mode = resolve_current(doc_from(all_blocked))
    check("A is unblocked so it wins over blocked C", (node["id"], mode) == ("A", "next_up"))

    stalled = """
project: X
nodes:
  - {id: A, name: a, status: blocked, blocked_by: ["cluster down"]}
"""
    node, mode = resolve_current(doc_from(stalled))
    check("everything blocked -> mode=blocked", (node["id"], mode) == ("A", "blocked"))

    done = """
project: X
nodes:
  - {id: A, name: a, status: done, evidence: [e]}
"""
    node, mode = resolve_current(doc_from(done))
    check("nothing left -> mode=complete", (node, mode) == (None, "complete"))


def test_parent_unblocks() -> None:
    """A phase whose children are all finished stops blocking its dependents."""
    text = """
project: X
nodes:
  - id: P0
    name: phase
    status: in_progress
    children:
      - {id: P0.1, name: a, status: done, evidence: [e]}
      - {id: P0.2, name: b, status: failed, evidence: [e]}
  - {id: P1, name: next, status: todo, blocked_by: [P0]}
"""
    node, mode = resolve_current(doc_from(text))
    check("resolved parent no longer blocks", (node["id"], mode) == ("P1", "next_up"),
          f"got {node['id']}/{mode}")


# ------------------------------------------------------------------ validation

BAD = {
    "invalid status": "project: X\nnodes: [{id: A, name: a, status: nearly_done}]",
    "dangling blocker": "project: X\nnodes: [{id: A, name: a, status: todo, blocked_by: [NOPE]}]",
    "cycle": ("project: X\nnodes:\n"
              "  - {id: A, name: a, status: todo, blocked_by: [B]}\n"
              "  - {id: B, name: b, status: todo, blocked_by: [A]}"),
    "duplicate id": ("project: X\nnodes:\n"
                     "  - {id: A, name: one, status: todo}\n"
                     "  - {id: A, name: two, status: todo}"),
    "blocked without blocker": "project: X\nnodes: [{id: A, name: a, status: blocked}]",
    "id with a space": "project: X\nnodes: [{id: 'A B', name: a, status: todo}]",
    "zero weight": "project: X\nnodes: [{id: A, name: a, status: todo, weight: 0}]",
    "current points nowhere": "project: X\ncurrent: ZZ\nnodes: [{id: A, name: a, status: todo}]",
    "graph points nowhere": ("project: X\nnodes: [{id: A, name: a, status: todo}]\n"
                              "edges: [{from: A, to: ZZ, when: next}]"),
    "graph cycle": ("project: X\nnodes:\n"
                    "  - {id: A, name: a, status: todo}\n"
                    "  - {id: B, name: b, status: todo}\n"
                    "edges:\n"
                    "  - {from: A, to: B, when: next}\n"
                    "  - {from: B, to: A, when: next}"),
}


def test_validation() -> None:
    for name, text in BAD.items():
        errors, _ = validate(doc_from(text))
        check(f"rejects: {name}", bool(errors), "no error raised")

    warn_only = "project: X\nnodes: [{id: A, name: a, status: todo, sucess_criteria: [x]}]"
    errors, warnings = validate(doc_from(warn_only))
    check("typo'd key warns but does not fail", not errors and bool(warnings))

    free_text = ("project: X\nnodes: [{id: A, name: a, status: blocked, "
                 "blocked_by: ['cluster quota exhausted']}]")
    errors, _ = validate(doc_from(free_text))
    check("free-text blocker is allowed", not errors, str(errors))


def test_research_graph() -> None:
    """PASS/FAIL paths are directed, explicit, and independent of status."""
    base = """
project: X
nodes:
  - {id: G, name: gate, kind: gate, status: done, outcome: passed, evidence: [result.csv]}
  - {id: PASS_PATH, name: continue, status: todo}
  - {id: FAIL_PATH, name: revise, status: todo}
edges:
  - {from: G, to: PASS_PATH, when: pass}
  - {from: G, to: FAIL_PATH, when: fail}
"""
    doc = doc_from(base)
    errors, warnings = validate(doc)
    check("valid PASS/FAIL graph", not errors and not warnings, str(errors + warnings))
    node, mode = resolve_current(doc)
    check("PASS selects the PASS branch", (node["id"], mode) == ("PASS_PATH", "next_up"))

    doc["nodes"][0]["outcome"] = "failed"
    node, mode = resolve_current(doc)
    check("FAIL selects the FAIL branch", (node["id"], mode) == ("FAIL_PATH", "next_up"))

    doc["nodes"][0]["outcome"] = "pending"
    node, mode = resolve_current(doc)
    check("finished gate waits for a scientific outcome",
          (node["id"], mode) == ("G", "awaiting_outcome"))


# ------------------------------------------------------------------- end-to-end

def test_roundtrip() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pnav-selftest-"))
    try:
        def pnav(*args, expect=0):
            r = subprocess.run(PNAV + ["--project", str(tmp), *args],
                               capture_output=True, text=True)
            check(f"`pnav {args[0]}` exit {expect}", r.returncode == expect,
                  f"rc={r.returncode} {r.stderr.strip()}")
            return r

        pnav("init", "--name", "RT")
        pnav("start", "P0.2")
        pnav("done", "P0.2", expect=2)  # evidence gate
        pnav("done", "P0.2", "-e", "results/x.csv", "-m", "ok")
        pnav("check")

        state = json.loads(pnav("status", "--json").stdout)
        node = next(n for n in state["nodes"] if n["id"] == "P0.2")
        check("evidence persisted", node["evidence"] == ["results/x.csv"], str(node["evidence"]))
        check("status persisted", node["status"] == "done", node["status"])

        events = (tmp / ".project" / "events.jsonl").read_text().strip().splitlines()
        kinds = [json.loads(e)["event"] for e in events]
        check("events logged in order", kinds == ["INIT", "IN_PROGRESS", "DONE"], str(kinds))

        check("backup written", (tmp / ".project" / "roadmap.bak").is_file())
        check("state.json written", (tmp / ".project" / "state.json").is_file())

        for fname in ("CLAUDE.md", "AGENTS.md"):
            text = (tmp / fname).read_text()
            check(f"{fname} carries the contract", "pnav status" in text)

        # init is append-only: pre-existing content must survive a re-run
        (tmp / "CLAUDE.md").write_text("# House rules\nkeep me\n\n" + (tmp / "CLAUDE.md").read_text())
        pnav("init", "--force")
        text = (tmp / "CLAUDE.md").read_text()
        check("init never clobbers existing CLAUDE.md", "keep me" in text)
        check("init does not duplicate its block", text.count("BEGIN project-navigator") == 1,
              str(text.count("BEGIN project-navigator")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_plan_change() -> None:
    """Structural edits must be detected and must block mutations until explained."""
    before = doc_from("""
project: X
nodes:
  - id: A
    name: phase
    status: todo
    children:
      - {id: A1, name: one, status: todo}
  - {id: B, name: two, status: todo}
""")
    after = doc_from("""
project: X
nodes:
  - id: A
    name: renamed phase
    status: todo
    weight: 3
    children:
      - {id: A1, name: one, status: todo}
      - {id: A2, name: added, status: todo}
""")
    d = diff_structure(structure_of(before), structure_of(after))
    joined = " | ".join(d)
    check("detects an added node", any(x.startswith("added A2") for x in d), joined)
    check("detects a removed node", any(x.startswith("removed B") for x in d), joined)
    check("detects a rename", any(x.startswith("renamed A") for x in d), joined)
    check("detects a reweight", any(x.startswith("reweighted A") for x in d), joined)
    check("status-only change is not structural",
          diff_structure(structure_of(before), structure_of(before)) == [], "")
    check("no baseline means no false alarm", diff_structure(None, structure_of(after)) == [])

    tmp = Path(tempfile.mkdtemp(prefix="pnav-plan-"))
    try:
        def pnav(*args, expect=0):
            r = subprocess.run(PNAV + ["--project", str(tmp), *args],
                               capture_output=True, text=True)
            check(f"`pnav {args[0]}` exit {expect}", r.returncode == expect,
                  f"rc={r.returncode} {r.stderr.strip()[:120]}")
            return r

        pnav("init", "--name", "PC")
        road = tmp / ".project" / "roadmap.yaml"
        doc = yaml.safe_load(road.read_text())
        doc["nodes"][0]["children"].append({"id": "P0.9", "name": "snuck in", "status": "todo"})
        road.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))

        r = pnav("start", "P0.2", expect=2)
        check("mutation refused with a usable message",
              "plan-change" in r.stderr, r.stderr.strip()[:120])
        pnav("plan-change", "-m", "because the pilot showed a gap")
        pnav("start", "P0.2")
        text = (tmp / ".project" / "decisions.md").read_text()
        check("decision recorded with its reason", "because the pilot showed a gap" in text)
        check("decision records the change", "added P0.9" in text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_plan_lifecycle() -> None:
    """Proposals are inert; accepted structural edits create an auditable version."""
    tmp = Path(tempfile.mkdtemp(prefix="pnav-lifecycle-"))
    try:
        def pnav(*args, expect=0):
            r = subprocess.run(PNAV + ["--project", str(tmp), *args],
                               capture_output=True, text=True)
            check(f"lifecycle `pnav {args[0]}` exit {expect}", r.returncode == expect,
                  f"rc={r.returncode} {r.stderr.strip()[:160]}")
            return r

        pnav("init", "--name", "Lifecycle")
        road = tmp / ".project" / "roadmap.yaml"
        before = road.read_text()

        pnav("outcome", "P0.2", "failed", "-e", "results/falsification.csv",
             "-m", "criterion was not met")
        state = json.loads(pnav("status", "--json").stdout)
        node = next(n for n in state["nodes"] if n["id"] == "P0.2")
        check("scientific outcome is stored", node["outcome"] == "failed", str(node))
        check("scientific outcome does not overwrite execution status",
              node["status"] == "todo", str(node))

        proposed_from = road.read_text()
        r = pnav("propose", "-m", "evidence suggests one follow-up",
                 "--change", "add a follow-up measurement", "-e", "results/falsification.csv")
        proposal_id = next(p["proposal_id"] for p in read_proposals(tmp))
        check("proposal is visible as proposed", proposal_id in r.stdout, r.stdout)
        check("proposal does not mutate canonical plan", road.read_text() == proposed_from)

        doc = yaml.safe_load(road.read_text())
        doc["nodes"].append({"id": "FOLLOW", "name": "Follow-up measurement", "status": "todo"})
        road.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
        pnav("plan-change", "-m", "accepted after review", "--proposal", proposal_id,
             "-e", "results/falsification.csv")
        state = json.loads(pnav("status", "--json").stdout)
        check("accepted change increments plan version", state["plan_version"] == 2,
              str(state["plan_version"]))
        check("immutable v1 and v2 snapshots exist",
              all((tmp / ".project" / "history" / f"v{v:03d}.yaml").is_file()
                  for v in (1, 2)))
        proposal = read_proposals(tmp)[0]
        check("accepted proposal is closed", proposal.get("status") == "accepted", str(proposal))
        changes = (tmp / ".project" / "changes.jsonl").read_text().splitlines()
        check("accepted change has an append-only log entry", len(changes) == 1)
        check("pre-proposal plan was not accidentally restored", before != road.read_text())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_discovery() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pnav-scan-"))
    try:
        for rel in ("alpha", "nested/beta"):
            subprocess.run(PNAV + ["init", "--project", str(tmp / rel)],
                           capture_output=True, text=True)
        (tmp / "node_modules" / "pkg").mkdir(parents=True)
        subprocess.run(PNAV + ["init", "--project", str(tmp / "node_modules" / "pkg")],
                       capture_output=True, text=True)

        found = {p.name for p in discover_projects([tmp], max_depth=4)}
        check("finds a project at the top", "alpha" in found, str(found))
        check("finds a nested project", "beta" in found, str(found))
        check("prunes node_modules", "pkg" not in found, str(found))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    for fn in (test_arithmetic, test_current, test_parent_unblocks,
               test_validation, test_research_graph, test_roundtrip,
               test_plan_change, test_plan_lifecycle, test_discovery):
        print(f"\n-- {fn.__name__} --")
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): " + ", ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
