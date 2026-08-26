## Project state (project-navigator)

This project's state lives in `.project/roadmap.yaml`. It is the single source
of truth for what is done, what is being worked on, and what comes next.

**Read it before doing substantial work:**

```bash
{{PNAV}} status
```

**Record meaningful work when it finishes.** Use the CLI, never a hand edit:

```bash
{{PNAV}} start P1.2
{{PNAV}} done  P1.2 --evidence results/exp_012.csv -m "corr 0.81 on held-out"
{{PNAV}} fail  P1.2 --evidence logs/run.txt -m "job crashed before producing a result"
{{PNAV}} outcome P1.2 failed --evidence results/exp_012.csv -m "criterion was not met"
{{PNAV}} block P1.3 --by "cluster quota exhausted"
{{PNAV}} next  P1.3 -m "run seeds 2 and 3"
{{PNAV}} check
```

If evidence suggests changing the plan, record an inert proposal first. Do not
edit the canonical plan until the user explicitly accepts it:

```bash
{{PNAV}} propose -m "<why>" --change "<specific suggested edit>" -e <evidence>
# after explicit acceptance: edit roadmap.yaml, then
{{PNAV}} plan-change -m "<why accepted>" --proposal P0001 -e <evidence>
```

### Rules

1. **Run `pnav status` first.** Do not guess the current node from the code or
   from this conversation.
2. **Six statuses only:** `todo in_progress done failed blocked deferred`.
   Never invent one, and never report progress as a percentage you made up -
   the number is computed from the tree.
3. **`done` requires evidence** - a file path, a commit, or a number. If there
   genuinely is none, say so with `--no-evidence`.
4. **Keep execution and science separate.** `fail` means execution failed.
   `outcome ... failed` means the run completed and the scientific criterion
   failed. Never delete a negative result.
5. **Never invent research content.** Do not add hypotheses, decision criteria,
   PASS/FAIL destinations, or remove branches unless they were already agreed.
6. **Never silently restructure the roadmap.** An unaccepted idea stays in
   `proposals.jsonl`; only an explicitly accepted edit becomes a new
   `plan_version`. Structural mutations are blocked until `plan-change` records
   the reason, diff, evidence, and immutable snapshot.
7. **See the whole picture** with `{{PNAV}} serve` (one project) or
   `{{PNAV}} hub` (all of them), then open http://127.0.0.1:8765.
