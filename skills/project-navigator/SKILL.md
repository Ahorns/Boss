---
name: project-navigator
description: Track and update where a project actually stands. Use when the user asks "where am I", "where are we", "what's next", "continue the project", or asks to resume/pick up work; when starting substantial work in a repo that has a .project/ directory; after finishing a meaningful piece of work that should be recorded; when the roadmap's structure needs to change; or when the user wants a live project dashboard, a mind map of the plan, a roadmap, an overview across several projects, or to set up project tracking in a new repo.
---

# Project Navigator

A project's current accepted plan lives in `<project>/.project/roadmap.yaml`.
Append-only records preserve how it got there, without mixing superseded paths
into the current view. A local dashboard renders both live.

The `pnav` CLI is the only sanctioned way to change it. It validates every
transition, logs it to `.project/events.jsonl`, and keeps a backup — so state
cannot drift silently, and a wrong turn is always recoverable.

**The CLI is at `scripts/pnav` inside this skill directory.** Every project that
has been set up also records the absolute path in its own `CLAUDE.md`.

## Before doing substantial work

Run this first. Do not infer the current node from the code, the conversation,
or a `PLAN.md`:

```bash
scripts/pnav status --project <repo>     # or just `pnav status` from inside it
```

It prints the current node, its goal, its success criteria, the evidence so far,
the next action, and anything blocking it. That is enough to resume cold.

`pnav tree` shows the whole map when you need the wider picture.

## After finishing meaningful work

Record it immediately, with the command that matches what actually happened:

```bash
pnav start P2.3                                      # picking it up
pnav done  P2.3 -e results/seed2.csv -m "corr 0.81"  # it worked
pnav fail  P2.3 -e logs/run.txt -m "execution crashed before producing a result"
pnav outcome P2.3 failed -e results/seed2.csv -m "the agreed criterion was not met"
pnav block P2.4 --by "cluster quota exhausted"       # or --by P2.3
pnav defer P5   -m "out of scope until the predictor lands"
pnav next  P2.4 -m "run seeds 3 and 4"               # set the next action
pnav note  P2.3 -m "SPEF annotation only covers 12% of nets"
pnav evidence P2.3 results/seed3.csv                 # attach without transitioning
pnav check                                           # validate
pnav events -n 20                                    # what happened recently
pnav decisions                                       # why the plan changed, and when
```

`done` deliberately clears the current pointer rather than auto-starting the
next node, and tells you what is next. Start it explicitly — every transition
should be a decision someone made, not one the tool made.

## Rules

1. **Read before writing.** `pnav status` first, always.
2. **Keep execution separate from scientific meaning.** `status` is one of
   `todo in_progress done failed blocked deferred`; `outcome` is one of
   `active pending passed failed inconclusive superseded deferred abandoned
   not_needed`. `fail` means execution failure; `outcome ... failed` means a
   completed scientific test failed its criterion.
3. **Never state a progress percentage yourself.** It is computed from the tree
   (`done`/`failed` = 1, `in_progress` = 0.5, `todo`/`blocked` = 0, `deferred`
   excluded, scaled by each node's `weight`). Quote what `pnav status` prints.
4. **`done` requires evidence** — a file path, a commit, or a number. If there
   genuinely is none, pass `--no-evidence` so the absence is on the record.
5. **Never invent the research plan.** Do not create hypotheses, decision
   criteria, branch destinations, or delete branches unless they were already
   agreed by the user and their collaborator. Preserve negative and
   inconclusive results.
6. **Keep proposals inert.** If evidence suggests a plan edit, record it with
   `pnav propose`; it must not change NOW. Only after explicit acceptance may
   you edit the YAML and record the new version with `pnav plan-change`.
7. **Never silently restructure the roadmap.** Adding, removing, re-parenting,
   retyping or re-weighting nodes, changing edges, or changing the project's
   goal is a decision for the user. Recording status/outcome through the CLI is
   not a restructure.

   **This one is enforced, not just asked for.** The shape of the plan is
   fingerprinted on every write. If it changes outside the CLI, every mutating
   command refuses until the change is explained:

   ```bash
   pnav propose -m "P2 provides new evidence" --change "replace P3 with P2.4" -e results/p2.csv
   # after explicit acceptance and the corresponding roadmap.yaml edit:
   pnav plan-change -m "accepted because P2 changed the decision" --proposal P0001 -e results/p2.csv
   ```

   That writes the diff and the reason to `.project/decisions.md`, logs it, and
   raises a ⚠ PLAN CHANGED banner on the dashboard until recorded. Never work
   around it by reverting the file — explain the change.
8. **Do not hand-edit `roadmap.yaml` to change a status or outcome.** Use the CLI so the
   transition is validated and logged.

## Setting up a project that has none

```bash
pnav init --project <repo> --name "<display name>"
```

This creates `.project/`, writes a starter roadmap, and appends a delimited
block to the repo's `CLAUDE.md` and `AGENTS.md` (creating them if absent,
appending if present — it never overwrites existing content). Claude reads
`CLAUDE.md`, Codex reads `AGENTS.md`, and both drive the same state file.

Then replace the placeholder nodes with the already agreed plan. Evidence may
confirm execution state, but it does not authorize the skill to invent a
research direction. For gates, merges, or plan versions, read
`references/research-plan-schema.md` first.

## The dashboard

```bash
pnav serve --project <repo>        # http://127.0.0.1:8765
pnav hub                           # every project found under your home dir
pnav hub --scan ~/work --depth 3   # or scan somewhere specific
```

Reads `roadmap.yaml` fresh on every request and polls once a second, so a `pnav`
command in another terminal shows up in the browser within about a second. It
binds to localhost only. Leave it running while you work.

The project page keeps the current node and its next action visible above every
view. Selecting another node opens it in the inspector without losing that
current-work context.

The workspace separates four meanings:

- **NOW** — the current accepted plan, switchable between an expandable outline
  and a directed graph. Both use the same node/status/outcome language.
- **HISTORY** — immutable plan versions, accepted changes, and still-inert
  proposals: how the current plan came to be.
- **ACTIVITY** — the chronological execution/outcome event stream: what
  happened during work, separate from changes to the accepted plan.
- **RETIRED** — superseded, abandoned, deferred, or no-longer-needed paths,
  kept visible without cluttering NOW.

`pnav hub` discovers projects by scanning the filesystem rather than keeping a
list of them, so a newly initialised project appears with nothing to update.

## Editing roadmap.yaml directly

Legitimate when adding or restructuring nodes (with the user's agreement), never
for status changes. The CLI rewrites the file canonically, **which drops `#`
comments** — put prose in `goal:` or `note:` instead.

```yaml
project: <name>
plan_version: 3             # incremented only by accepted plan-change
current: P1.2                # optional; omit and it is resolved by rule
nodes:
  - id: P1                   # unique, [A-Za-z0-9._-], no spaces
    name: QoR predictor
    kind: experiment         # task | experiment | gate | milestone | stop
    status: in_progress
    outcome: pending         # scientific meaning; independent of status
    weight: 2                # default 1; use it so big work counts for more
    goal: One sentence on what this node is for.
    success_criteria:        # how you will know it worked
      - unseen-circuit correlation > 0.7
    evidence:                # files, commits, numbers
      - results/exp_012.csv
    next_action: Run seeds 2 and 3.
    blocked_by: [P0]         # a node id, or free text like "waiting on quota"
    note: |
      Longer prose. YAML comments do not survive; this does.
    children:                # nests arbitrarily deep
      - id: P1.1
        name: Generate the dataset
        status: done
        evidence: [data/v3/]
edges:
  - {from: P1.1, to: P1.2, when: next}
  - {from: P1.2, to: P2, when: pass}
  - {from: P1.2, to: R1, when: fail}
```

Read `references/research-plan-schema.md` before using explicit graph edges,
gates, outcomes, proposals, or version history.

Run `pnav check` afterwards. It rejects unknown statuses, duplicate or malformed
ids, dangling and circular `blocked_by`, and `blocked` with nothing blocking it;
it warns about likely typos and about parents that contradict their children.

If the edit changed the plan's *shape*, follow it with `pnav plan-change -m
"<why>"`. Until you do, every mutating command will refuse to run.

## Verifying a change to the tooling

```bash
python3 tests/selftest.py                    # logic; no browser, no pytest
python3 -m http.server 8899                  # then /tests/map_interaction.html
                                             #      /tests/tree_interaction.html
```

The browser harnesses drive the real event handlers and must report every check
true. The map harness also checks deep chains, wide branches, forests, uneven
depths, and long labels in both orientations. They exist because graph geometry,
tap-versus-drag, and expand/collapse are easy to break silently.
