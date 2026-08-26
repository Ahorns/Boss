# Boss

Tooling home. Nothing project-specific lives here — every project's state lives
in that project's own `.project/` directory.

## project-navigator

`skills/project-navigator/` — a local, dependency-light way to answer one
question in under ten seconds:

> Where is this project right now, and what is the next action?

One YAML file per project is the source of truth, a Python CLI is the only
sanctioned way to change it, and a localhost dashboard renders it live while
Claude or Codex works.

### Install

Make the skill visible to Claude Code everywhere:

```bash
ln -s ~/Boss/skills/project-navigator ~/.claude/skills/project-navigator
```

Optionally put the CLI on `PATH`:

```bash
echo 'export PATH="$HOME/Boss/skills/project-navigator/scripts:$PATH"' >> ~/.bashrc
```

Requires Python 3 and PyYAML — both already present. Nothing else.

### Use

```bash
pnav init --project ~/some/repo --name "Some Repo"   # once per project
pnav status                                          # where am I
pnav tree                                            # the whole map
pnav serve                                           # http://127.0.0.1:8765
pnav hub                                             # every project it can find
```

Recording work:

```bash
pnav start P1.2
pnav done  P1.2 -e results/exp.csv -m "corr 0.81"
pnav outcome P1.2 failed -e results/exp.csv -m "criterion not met"
pnav propose -m "why" --change "specific suggested edit" -e results/exp.csv
# after acceptance and the YAML edit:
pnav plan-change -m "why accepted" --proposal P0001 -e results/exp.csv
pnav events / pnav decisions                         # history
```

The dashboard keeps the current work and next action visible. **NOW** offers a
continuous outline and directed graph; **HISTORY** keeps accepted versions and
proposals; **ACTIVITY** shows execution and outcome events; **RETIRED** preserves
superseded paths without cluttering the current plan.

`pnav init` also appends a rules block to the project's `CLAUDE.md` and
`AGENTS.md`, so Claude and Codex both read and update the same state.

### Layout

```
skills/project-navigator/
├── SKILL.md              the agent contract
├── scripts/
│   ├── pnav              shell wrapper
│   ├── pnav.py           CLI
│   ├── model.py          schema, validation, atomic writes
│   ├── compute.py        progress arithmetic, current-node resolution
│   └── serve.py          dashboard server
├── dashboard/            index.html + app.js + map.js + hub.html + hub.js + style.css
├── templates/            starter roadmap, CLAUDE/AGENTS snippet
└── tests/
    ├── selftest.py       python3 tests/selftest.py   (no pytest needed)
    ├── map_interaction.html   graph shapes + pan/zoom/tap
    └── tree_interaction.html  hierarchy expand/collapse + current context
                               (both need a browser:
                                python3 -m http.server 8899, then /tests/...)
```

Per project, `pnav init` creates:

```
<project>/.project/
├── roadmap.yaml     the only file edited by hand
├── state.json       generated snapshot
├── events.jsonl     append-only transition log
├── changes.jsonl    append-only accepted plan changes
├── proposals.jsonl  proposed/accepted/rejected changes
├── history/         immutable vNNN plan snapshots
├── decisions.md     why the plan's shape changed, and when
├── structure.json   fingerprint of the plan's shape
└── roadmap.bak      previous version
```

### Anti-drift

The plan's *shape* is fingerprinted on every write. Change it outside the CLI —
add, remove, re-parent or re-weight a node — and every mutating command refuses
until you run `pnav plan-change -m "<why>"`, which records the diff and the
reason to `decisions.md`. Status changes are the normal path and are never
blocked. The hub finds projects by scanning, so nothing in `~/Boss` records
which projects exist.

Directed acyclic research graphs support explicit `next`, `pass`, `fail`, and
`always` edges, including branches and merges. Execution `status` and scientific
`outcome` remain separate.
