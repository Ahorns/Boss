#!/usr/bin/env python3
"""pnav - Project Navigator CLI.

The sanctioned way to read and mutate a project's state. Agents call these
commands instead of editing .project/roadmap.yaml by hand, so every transition
is validated, logged and reversible.

    pnav status            where am I
    pnav tree              the whole map
    pnav start|done|fail|block|defer|next|note   record execution
    pnav outcome           record scientific meaning independently
    pnav propose           suggest a plan edit without applying it
    pnav check             validate
    pnav serve             live dashboard
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from compute import bar, blockers_of, build_state, resolve_current  # noqa: E402
from model import (  # noqa: E402
    GLYPH,
    OUTCOMES,
    append_change,
    append_decision,
    append_proposal,
    STATUSES,
    PnavError,
    append_event,
    close_proposal,
    find_root,
    get_node,
    index,
    load,
    paths,
    pending_plan_change,
    plan_version,
    read_proposals,
    read_structure,
    require_valid,
    validate,
    walk,
    write,
    write_state,
    write_structure,
    snapshot_plan,
)

SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = SKILL_DIR / "templates"

MARK_BEGIN = "<!-- BEGIN project-navigator -->"
MARK_END = "<!-- END project-navigator -->"


# ------------------------------------------------------------------ printing


def _tty() -> bool:
    return sys.stdout.isatty()


def dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _tty() else s


def bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _tty() else s


def yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m" if _tty() else s


def red(s: str) -> str:
    return f"\033[31m{s}\033[0m" if _tty() else s


def field(label: str, value: str) -> None:
    print(f"{bold(label.ljust(9))} {value}")


def now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


# ----------------------------------------------------------------- mutations


def _refresh(root: Path, doc: dict) -> dict:
    write(root, doc)
    write_structure(root, doc)
    state = build_state(doc, root)
    write_state(root, state)
    return state


def _transition(root: Path, doc: dict, node: dict, new_status: str, args,
                extra: dict | None = None) -> None:
    old = node.get("status")
    node["status"] = new_status

    if new_status == "in_progress":
        doc["current"] = node["id"]
    elif doc.get("current") == node["id"]:
        # Stop pointing at a node that is no longer being worked on; the
        # next node is then resolved by rule rather than asserted here.
        doc.pop("current", None)

    event = {
        "time": now(),
        "event": new_status.upper(),
        "node": node["id"],
        "name": node.get("name"),
        "from": old,
        "to": new_status,
    }
    if getattr(args, "message", None):
        event["message"] = args.message
    if extra:
        event.update(extra)

    append_event(root, event)
    state = _refresh(root, doc)

    print(f"{GLYPH.get(new_status, '')} {bold(node['id'])} {node.get('name')}"
          f"  {dim(f'{old} -> {new_status}')}")
    _print_where_next(doc, state)


def _print_where_next(doc: dict, state: dict) -> None:
    cur = state.get("current_node")
    if cur is None:
        print(dim("  all nodes resolved - project complete."))
        return
    verb = {
        "explicit": "current",
        "in_progress": "current",
        "next_up": "next up",
        "blocked": "stalled on",
    }.get(state["mode"], "current")
    line = f"  {dim(verb + ':')} {cur['id']} {cur['name']}"
    if state["mode"] == "next_up":
        line += dim(f"   (pnav start {cur['id']})")
    print(line)
    print(f"  {dim('progress:')} {bar(state['progress'])} {state['progress'] * 100:.1f}%")


def _load_for_mutation(args) -> tuple[Path, dict]:
    root = find_root(args.project)
    doc = load(root)
    require_valid(doc)

    changes = pending_plan_change(root, doc)
    if changes and not getattr(args, "allow_plan_change", False):
        raise PnavError(
            "the shape of the roadmap changed outside the CLI and has not been "
            "explained:\n"
            + "\n".join(f"  - {c}" for c in changes)
            + "\n\n  Record why, then continue:\n"
              "    pnav plan-change -m \"<why the plan changed>\""
        )
    return root, doc


# ------------------------------------------------------------------ commands


def cmd_init(args) -> int:
    root = Path(args.project).expanduser().resolve() if args.project else Path.cwd()
    p = paths(root)

    if p["roadmap"].is_file() and not args.force:
        raise PnavError(f"{p['roadmap']} already exists. Use --force to overwrite it.")

    root.mkdir(parents=True, exist_ok=True)
    p["dir"].mkdir(parents=True, exist_ok=True)

    name = args.name or root.name
    starter = (TEMPLATES / "roadmap.starter.yaml").read_text(encoding="utf-8")
    p["roadmap"].write_text(starter.replace("{{PROJECT}}", name), encoding="utf-8")

    doc = load(root)
    write_structure(root, doc)
    snapshot_plan(root, doc)
    state = build_state(doc, root)
    write_state(root, state)
    append_event(root, {"time": now(), "event": "INIT", "project": name})

    print(f"created {p['roadmap']}")

    _install_docs(root)

    print()
    print("Next:")
    print(f"  1. edit {p['roadmap']} - replace the placeholder nodes with real ones")
    print("  2. pnav check")
    print("  3. pnav serve   ->  http://127.0.0.1:8765")
    return 0


def _install_docs(root: Path) -> None:
    """Claude reads CLAUDE.md, Codex reads AGENTS.md - same rules, one state file."""
    snippet = (TEMPLATES / "agent.snippet.md").read_text(encoding="utf-8")
    snippet = snippet.replace("{{PNAV}}", str(SKILL_DIR / "scripts" / "pnav"))
    for fname in ("CLAUDE.md", "AGENTS.md"):
        changed = _install_block(root / fname, snippet)
        print(f"{'updated' if changed else 'unchanged'} {root / fname}")


def cmd_install_docs(args) -> int:
    """Refresh the agent contract in a project after the skill itself changes."""
    root = find_root(args.project)
    _install_docs(root)
    return 0


def _install_block(path: Path, body: str) -> bool:
    """Insert or refresh the delimited block. Never overwrites other content."""
    block = f"{MARK_BEGIN}\n{body.strip()}\n{MARK_END}\n"

    if not path.exists():
        path.write_text(block, encoding="utf-8")
        return True

    text = path.read_text(encoding="utf-8")
    if MARK_BEGIN in text and MARK_END in text:
        head, _, rest = text.partition(MARK_BEGIN)
        _, _, tail = rest.partition(MARK_END)
        new = head + block + tail.lstrip("\n")
        if new == text:
            return False
        path.write_text(new, encoding="utf-8")
        return True

    sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    path.write_text(text + sep + block, encoding="utf-8")
    return True


def _adopt_structure(root: Path, doc: dict) -> None:
    """First read of a roadmap that predates this feature sets the baseline,
    so adopting the tool never looks like an unexplained plan change."""
    if read_structure(root) is None:
        write_structure(root, doc)
    if not pending_plan_change(root, doc):
        snapshot_plan(root, doc)


def cmd_status(args) -> int:
    root = find_root(args.project)
    doc = load(root)
    _adopt_structure(root, doc)
    state = build_state(doc, root)
    write_state(root, state)

    if args.json:
        import json

        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    pct = state["progress"] * 100
    field("PROJECT", f"{state['project']}   {dim(str(root))}")
    field("PLAN", f"v{state['plan_version']}")
    field("PROGRESS", f"{bar(state['progress'])} {pct:.1f}%")

    cur = state.get("current_node")
    if cur is None:
        field("CURRENT", "- project complete -")
        return 0

    if state.get("plan_change"):
        print(red("PLAN CHANGED") + "  " + dim("(unexplained - run `pnav plan-change -m ...`)"))
        for ch in state["plan_change"]:
            print(f"  {yellow('~')} {ch}")
        print()

    label = {"next_up": "NEXT UP", "blocked": "STALLED"}.get(state["mode"], "CURRENT")
    field(label, f"{cur['glyph']} {bold(cur['id'])} - {cur['name']}   "
                 f"{dim(cur['status'])} / {cur.get('outcome', 'active')}")

    if cur.get("question"):
        field("QUESTION", cur["question"])
    if cur.get("experiment"):
        field("EXPERIMENT", cur["experiment"])
    if cur.get("goal"):
        field("GOAL", cur["goal"])
    for i, crit in enumerate(cur.get("criteria") or []):
        field("CRITERIA" if i == 0 else "", f"{'✓' if crit['met'] else '·'} {crit['text']}")
    for i, ev in enumerate(cur.get("evidence") or []):
        field("EVIDENCE" if i == 0 else "", ev)
    if cur.get("next_action"):
        field("NEXT", cur["next_action"])
    for branch in state.get("current_branches") or []:
        field(f"IF {branch['when'].upper()}", f"{branch['to']} - {branch.get('name') or ''}")
    unmet = cur.get("unmet_blockers") or []
    field("BLOCKED", yellow(", ".join(unmet)) if unmet else dim("none"))

    counts = state["leaf_counts"]
    field("TASKS", "  ".join(
        f"{GLYPH[s]} {counts.get(s, 0)} {s}" for s in STATUSES if counts.get(s)
    ) or dim("none"))
    return 0


def cmd_tree(args) -> int:
    root = find_root(args.project)
    doc = load(root)
    state = build_state(doc, root)
    by_id = {n["id"]: n for n in state["nodes"]}

    print(f"{bold(state['project'])}  {bar(state['progress'])} {state['progress'] * 100:.1f}%")

    def render(nodes, prefix):
        for i, node in enumerate(nodes):
            last = i == len(nodes) - 1
            info = by_id[node["id"]]
            frac = info["progress"]
            pct = dim(f"{frac * 100:5.1f}%") if frac is not None else dim("    --")
            here = "  " + bold(yellow("<- YOU ARE HERE")) if info["is_current"] else ""
            print(f"{prefix}{'`- ' if last else '|- '}{info['glyph']} "
                  f"{bold(node['id'])} {node['name']} {pct}{here}")
            render(node.get("children") or [], prefix + ("   " if last else "|  "))

    render(doc.get("nodes") or [], "")
    return 0


def cmd_check(args) -> int:
    root = find_root(args.project)
    p = paths(root)
    raw = p["roadmap"].read_text(encoding="utf-8") if p["roadmap"].is_file() else None
    doc = load(root)
    errors, warnings = validate(doc, raw)

    by_id = index(doc)
    in_prog = [n["id"] for n, _d, _p in walk(doc)
               if n.get("status") == "in_progress" and not (n.get("children") or [])]
    if len(in_prog) > 1:
        warnings.append(
            f"{len(in_prog)} leaves are in_progress at once ({', '.join(in_prog)}) - "
            "only the first is shown as current."
        )
    for node, _d, _p in walk(doc):
        if node.get("status") == "in_progress":
            unmet = blockers_of(node, by_id)
            if unmet:
                warnings.append(
                    f"node {node['id']!r} is in_progress but blocked by: {', '.join(unmet)}"
                )

    for ch in pending_plan_change(root, doc):
        warnings.append(f"PLAN CHANGE not explained: {ch}")

    for w in warnings:
        print(f"{yellow('warning')} {w}")
    for e in errors:
        print(f"{red('error')}   {e}")

    if errors:
        print(f"\n{red('FAILED')} {len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    if warnings and args.strict:
        print(f"\n{red('FAILED')} (strict) {len(warnings)} warning(s)")
        return 1
    print(f"\nOK  {len(list(walk(doc)))} nodes, {len(warnings)} warning(s)")
    return 0


def cmd_start(args) -> int:
    root, doc = _load_for_mutation(args)
    node = get_node(doc, args.id)
    if node.get("children"):
        print(dim(f"note: {args.id} has children; current usually points at a leaf."))
    unmet = blockers_of(node, index(doc))
    if unmet and not args.force:
        raise PnavError(
            f"{args.id} is blocked by: {', '.join(unmet)}.\n"
            "  Resolve them first, or pass --force."
        )
    _transition(root, doc, node, "in_progress", args)
    return 0


def cmd_done(args) -> int:
    root, doc = _load_for_mutation(args)
    node = get_node(doc, args.id)
    # Evidence is owed by the work itself, so only leaves are gated; a parent's
    # evidence is whatever its children already recorded.
    if not args.evidence and not args.no_evidence and not node.get("children"):
        raise PnavError(
            "`done` requires evidence. Pass --evidence <file|commit|number> "
            "(repeatable), or --no-evidence to record that none exists."
        )
    if args.evidence:
        node.setdefault("evidence", [])
        for ev in args.evidence:
            if ev not in node["evidence"]:
                node["evidence"].append(ev)
    _transition(root, doc, node, "done", args,
                extra={"evidence": list(args.evidence or [])})
    return 0


def cmd_fail(args) -> int:
    root, doc = _load_for_mutation(args)
    node = get_node(doc, args.id)
    if args.evidence:
        node.setdefault("evidence", [])
        for ev in args.evidence:
            if ev not in node["evidence"]:
                node["evidence"].append(ev)
    _transition(root, doc, node, "failed", args,
                extra={"evidence": list(args.evidence or [])})
    return 0


def cmd_block(args) -> int:
    root, doc = _load_for_mutation(args)
    node = get_node(doc, args.id)
    node.setdefault("blocked_by", [])
    for dep in args.by:
        if dep not in node["blocked_by"]:
            node["blocked_by"].append(dep)
    _transition(root, doc, node, "blocked", args, extra={"blocked_by": list(args.by)})
    return 0


def cmd_defer(args) -> int:
    root, doc = _load_for_mutation(args)
    node = get_node(doc, args.id)
    _transition(root, doc, node, "deferred", args)
    return 0


def cmd_next(args) -> int:
    root, doc = _load_for_mutation(args)
    node = get_node(doc, args.id)
    old = node.get("next_action")
    node["next_action"] = args.message
    append_event(root, {"time": now(), "event": "NEXT", "node": node["id"],
                        "from": old, "to": args.message})
    state = _refresh(root, doc)
    print(f"{bold(node['id'])} next_action: {args.message}")
    _print_where_next(doc, state)
    return 0


def cmd_note(args) -> int:
    root, doc = _load_for_mutation(args)
    node = get_node(doc, args.id)
    stamp = _dt.date.today().isoformat()
    entry = f"{stamp}  {args.message}"
    node["note"] = (node.get("note", "").rstrip() + "\n" + entry).strip() if node.get("note") else entry
    append_event(root, {"time": now(), "event": "NOTE", "node": node["id"],
                        "message": args.message})
    _refresh(root, doc)
    print(f"{bold(node['id'])} note += {args.message}")
    return 0


def cmd_evidence(args) -> int:
    root, doc = _load_for_mutation(args)
    node = get_node(doc, args.id)
    node.setdefault("evidence", [])
    added = [e for e in args.evidence if e not in node["evidence"]]
    node["evidence"].extend(added)
    append_event(root, {"time": now(), "event": "EVIDENCE", "node": node["id"],
                        "evidence": added})
    _refresh(root, doc)
    print(f"{bold(node['id'])} evidence += {', '.join(added) or '(nothing new)'}")
    return 0


def cmd_outcome(args) -> int:
    """Record scientific meaning without conflating it with execution status."""
    root, doc = _load_for_mutation(args)
    node = get_node(doc, args.id)
    old = node.get("outcome", "active")
    if args.outcome in ("passed", "failed", "inconclusive"):
        available = list(node.get("evidence") or []) + list(args.evidence or [])
        if not available:
            raise PnavError(
                f"scientific outcome `{args.outcome}` requires evidence. "
                "Pass --evidence REF or attach evidence first."
            )
    if args.evidence:
        node.setdefault("evidence", [])
        for evidence in args.evidence:
            if evidence not in node["evidence"]:
                node["evidence"].append(evidence)
    node["outcome"] = args.outcome
    append_event(root, {
        "time": now(), "event": "OUTCOME", "node": node["id"],
        "name": node.get("name"), "from": old, "to": args.outcome,
        "message": args.message, "evidence": list(args.evidence or []),
    })
    _refresh(root, doc)
    print(f"{bold(node['id'])} scientific outcome: {old} -> {args.outcome}")
    return 0


def cmd_propose(args) -> int:
    """Record an agent/user suggestion without touching the canonical plan."""
    root = find_root(args.project)
    doc = load(root)
    require_valid(doc)
    proposal = append_proposal(root, now(), args.message, args.change, args.evidence)
    append_event(root, {
        "time": proposal["time"], "event": "PLAN_PROPOSED",
        "proposal_id": proposal["proposal_id"], "message": args.message,
        "changes": args.change, "evidence": list(args.evidence or []),
    })
    state = build_state(doc, root)
    write_state(root, state)
    print(f"{yellow(proposal['proposal_id'])} proposed only - current plan unchanged")
    for change in args.change:
        print(f"  ? {change}")
    return 0


def cmd_reject(args) -> int:
    root = find_root(args.project)
    proposals = {p["proposal_id"]: p for p in read_proposals(root)}
    proposal = proposals.get(args.proposal_id)
    if not proposal:
        raise PnavError(f"unknown proposal {args.proposal_id!r}.")
    if proposal.get("status") != "proposed":
        raise PnavError(
            f"proposal {args.proposal_id} is already {proposal.get('status')}."
        )
    when = now()
    close_proposal(root, args.proposal_id, when, "REJECTED", args.message)
    append_event(root, {"time": when, "event": "PLAN_PROPOSAL_REJECTED",
                        "proposal_id": args.proposal_id, "message": args.message})
    doc = load(root)
    write_state(root, build_state(doc, root))
    print(f"{args.proposal_id} rejected - current plan unchanged")
    return 0


def cmd_plan_change(args) -> int:
    root = find_root(args.project)
    doc = load(root)
    require_valid(doc)

    changes = pending_plan_change(root, doc)
    if not changes:
        print(dim("no unexplained structural change - nothing to record."))
        return 0

    old_version = plan_version(doc)
    old_snapshot = paths(root)["history"] / f"v{old_version:03d}.yaml"
    if not old_snapshot.is_file():
        raise PnavError(
            f"cannot version this change because baseline snapshot v{old_version} is missing.\n"
            "  Restore the pre-change roadmap, run `pnav status` once to adopt it, "
            "then reapply the edit."
        )

    proposal = None
    if args.proposal:
        proposals = {p["proposal_id"]: p for p in read_proposals(root)}
        proposal = proposals.get(args.proposal)
        if not proposal:
            raise PnavError(f"unknown proposal {args.proposal!r}.")
        if proposal.get("status") != "proposed":
            raise PnavError(f"proposal {args.proposal} is already {proposal.get('status')}.")

    when = now()
    new_version = old_version + 1
    doc["plan_version"] = new_version
    append_decision(root, when, args.message, changes)
    record = append_change(root, when, args.message, changes, old_version, new_version,
                           args.evidence, args.proposal)
    write(root, doc)
    snapshot_plan(root, doc)
    if args.proposal:
        close_proposal(root, args.proposal, when, "ACCEPTED", args.message, new_version)
    append_event(root, {"time": when, "event": "PLAN_CHANGE",
                        "message": args.message, "changes": changes,
                        "change_id": record["change_id"],
                        "from_version": old_version, "to_version": new_version,
                        "evidence": list(args.evidence or []),
                        **({"proposal_id": args.proposal} if args.proposal else {})})
    write_structure(root, doc)
    state = build_state(doc, root)
    write_state(root, state)

    print(bold(f"PLAN CHANGE recorded  v{old_version} -> v{new_version}"))
    for ch in changes:
        print(f"  {yellow('~')} {ch}")
    print(f"\n  {dim('reason:')} {args.message}")
    print(f"  {dim('written to:')} {paths(root)['decisions']}")
    return 0


def cmd_decisions(args) -> int:
    root = find_root(args.project)
    path = paths(root)["decisions"]
    if not path.is_file():
        print(dim("no plan changes recorded."))
        return 0
    print(path.read_text(encoding="utf-8").rstrip())
    return 0


def cmd_serve(args) -> int:
    import serve

    root = find_root(args.project)
    load(root)  # fail fast on a broken roadmap rather than in the browser
    serve.run([root], host=args.host, port=args.port)
    return 0


def cmd_hub(args) -> int:
    import serve
    from model import discover_projects

    scan = args.scan or [str(Path.home())]
    print(dim(f"scanning {', '.join(scan)} (depth {args.depth}) ..."))
    roots = discover_projects(scan, max_depth=args.depth)
    if not roots:
        raise PnavError(
            f"no projects found under {', '.join(scan)}.\n"
            "  Set one up with `pnav init --project <repo>`, or widen --depth."
        )
    serve.run(roots, host=args.host, port=args.port)
    return 0


def cmd_events(args) -> int:
    root = find_root(args.project)
    path = paths(root)["events"]
    if not path.is_file():
        print(dim("no events recorded yet."))
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines[-args.n:]:
        import json

        try:
            ev = json.loads(line)
        except ValueError:
            continue
        when = ev.get("time", "")[:19].replace("T", " ")
        node = ev.get("node", "")
        msg = ev.get("message") or ev.get("name") or ""
        print(f"{dim(when)}  {bold(ev.get('event', '?').ljust(11))} {node:<10} {msg}")
    return 0


# -------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    # --project is accepted on either side of the subcommand. SUPPRESS keeps the
    # subparser from clobbering a value given before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", metavar="PATH", default=argparse.SUPPRESS,
                        help="project root (default: nearest parent with .project/)")

    ap = argparse.ArgumentParser(prog="pnav", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", metavar="PATH", default=None,
                    help="project root (default: nearest parent with .project/)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", parents=[common], help="scaffold .project/ in a project")
    p.add_argument("--name", help="project display name (default: directory name)")
    p.add_argument("--force", action="store_true", help="overwrite an existing roadmap.yaml")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", parents=[common], help="where am I right now")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("tree", parents=[common], help="print the whole roadmap")
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("check", parents=[common], help="validate roadmap.yaml")
    p.add_argument("--strict", action="store_true", help="treat warnings as errors")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("start", parents=[common], help="mark a node in_progress and make it current")
    p.add_argument("id")
    p.add_argument("--force", action="store_true", help="start even if blocked")
    p.add_argument("-m", "--message")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("done", parents=[common], help="mark a node done (evidence required)")
    p.add_argument("id")
    p.add_argument("-e", "--evidence", action="append", metavar="REF",
                   help="file, commit or number proving it (repeatable)")
    p.add_argument("--no-evidence", action="store_true",
                   help="explicitly record that no evidence exists")
    p.add_argument("-m", "--message")
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("fail", parents=[common], help="record that execution failed")
    p.add_argument("id")
    p.add_argument("-e", "--evidence", action="append", metavar="REF")
    p.add_argument("-m", "--message", required=True, help="why execution failed")
    p.set_defaults(func=cmd_fail)

    p = sub.add_parser("block", parents=[common], help="mark a node blocked")
    p.add_argument("id")
    p.add_argument("--by", action="append", required=True, metavar="ID|TEXT",
                   help="a node id, or free text (repeatable)")
    p.add_argument("-m", "--message")
    p.set_defaults(func=cmd_block)

    p = sub.add_parser("defer", parents=[common], help="shelve a node (excluded from progress)")
    p.add_argument("id")
    p.add_argument("-m", "--message", required=True, help="why it is being shelved")
    p.set_defaults(func=cmd_defer)

    p = sub.add_parser("next", parents=[common], help="set a node's next_action")
    p.add_argument("id")
    p.add_argument("-m", "--message", required=True)
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("note", parents=[common], help="append a dated line to a node's note")
    p.add_argument("id")
    p.add_argument("-m", "--message", required=True)
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("evidence", parents=[common], help="attach evidence without changing status")
    p.add_argument("id")
    p.add_argument("evidence", nargs="+", metavar="REF")
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser("outcome", parents=[common],
                       help="record a scientific outcome separately from execution status")
    p.add_argument("id")
    p.add_argument("outcome", choices=OUTCOMES)
    p.add_argument("-m", "--message", required=True, help="scientific interpretation")
    p.add_argument("-e", "--evidence", action="append", metavar="REF")
    p.set_defaults(func=cmd_outcome)

    p = sub.add_parser("propose", parents=[common],
                       help="record a proposed plan change without changing the current plan")
    p.add_argument("-m", "--message", required=True, help="why the change is suggested")
    p.add_argument("--change", action="append", required=True, metavar="TEXT",
                   help="one proposed graph/plan edit (repeatable)")
    p.add_argument("-e", "--evidence", action="append", metavar="REF")
    p.set_defaults(func=cmd_propose)

    p = sub.add_parser("reject", parents=[common], help="reject an open plan proposal")
    p.add_argument("proposal_id")
    p.add_argument("-m", "--message", required=True, help="why it was not accepted")
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("events", parents=[common], help="show the transition log")
    p.add_argument("-n", type=int, default=20, help="how many recent entries (default 20)")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("plan-change", parents=[common],
                       help="explain a structural edit made outside the CLI")
    p.add_argument("-m", "--message", required=True, help="why the plan changed")
    p.add_argument("-e", "--evidence", action="append", metavar="REF",
                   help="evidence behind the decision (repeatable)")
    p.add_argument("--proposal", metavar="ID",
                   help="accept this previously recorded proposal into the new version")
    p.set_defaults(func=cmd_plan_change)

    p = sub.add_parser("decisions", parents=[common], help="show recorded plan changes")
    p.set_defaults(func=cmd_decisions)

    p = sub.add_parser("serve", parents=[common], help="run the live dashboard")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default 127.0.0.1; the server has no auth)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("install-docs", parents=[common],
                       help="refresh the CLAUDE.md / AGENTS.md contract block")
    p.set_defaults(func=cmd_install_docs)

    p = sub.add_parser("hub", help="dashboard across every project found by scanning")
    p.add_argument("--scan", action="append", metavar="PATH",
                   help="where to look (repeatable; default: your home directory)")
    p.add_argument("--depth", type=int, default=4, help="how deep to scan (default 4)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.set_defaults(func=cmd_hub, project=None)

    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except PnavError as exc:
        print(f"{red('pnav:')} {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
