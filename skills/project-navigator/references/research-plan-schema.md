# Research plan schema

Use this reference when creating or restructuring a roadmap. The dashboard and
CLI remain project-agnostic: every visible label and relationship comes from the
project's `.project/roadmap.yaml` or its append-only records.

## Three separate dimensions

- `status` describes execution: `todo`, `in_progress`, `done`, `failed`,
  `blocked`, or `deferred`.
- `outcome` describes scientific meaning: `active`, `pending`, `passed`,
  `failed`, `inconclusive`, `superseded`, `deferred`, `abandoned`, or
  `not_needed`.
- `kind` describes the node: `task`, `experiment`, `gate`, `milestone`, or
  `stop`.

Never infer an outcome from a status. A completed experiment can have an
`inconclusive` outcome; an execution failure can leave its outcome `pending`.

## Directed research graph

Nested `children` remain supported and create containment edges. Use explicit
top-level edges for research flow and branch/merge logic:

```yaml
project: Generic research plan
plan_version: 3
nodes:
  - id: G1
    name: Confirmed decision gate
    kind: gate
    status: done
    outcome: passed
    question: A question copied from the agreed plan.
    experiment: The agreed test, not one invented by the skill.
    evidence: [results/g1.csv]
  - {id: NEXT, name: Accepted continuation, kind: experiment, status: todo}
  - {id: REVISE, name: Accepted fallback, kind: task, status: todo}
edges:
  - {from: G1, to: NEXT, when: pass}
  - {from: G1, to: REVISE, when: fail}
```

`when` is one of `next`, `pass`, `fail`, or `always`. Graphs may branch and
merge, but must remain acyclic. Use `blocked_by` when all named prerequisites
must finish; multiple incoming flow edges are treated as alternative routes.

## Plan evolution

`roadmap.yaml` is only the current accepted plan. The other files answer how it
became current without mixing old paths into NOW:

- `history/vNNN.yaml`: immutable accepted-plan snapshots.
- `changes.jsonl`: append-only accepted changes and their reasons/evidence.
- `proposals.jsonl`: proposed, accepted, or rejected plan changes.
- `events.jsonl`: execution and outcome events.
- `decisions.md`: human-readable accepted structural changes.

Suggested changes do not mutate the plan:

```bash
pnav propose -m "evidence-backed reason" --change "specific edit" -e result.csv
```

After explicit user acceptance, apply the structural YAML edit and record it:

```bash
pnav plan-change -m "accepted reason" --proposal P0001 -e result.csv
```

This increments `plan_version`, records the diff, and saves a new snapshot.
Rejected paths remain inspectable under RETIRED or HISTORY; never erase them to
make the current graph look cleaner.
