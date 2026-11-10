# Optional agent rule: keep the work record current

`tech-founder-commands` does **no background monitoring**. Status is produced
only when you run it. If you want an agent (Codex, Claude Code, etc.) to keep the
durable record fresh as work proceeds, add a short rule to that agent's
instruction file (`AGENTS.md`, `CLAUDE.md`, a project rule, or similar).

Paste something like this and adapt it to your workflow:

```markdown
## Project status (tech-founder-commands)

- When I ask for my status, open work, PR/review state, or to plan the day on
  this repo, run `tfc refresh` then `tfc show` and summarise concisely, keeping
  live GitHub state and my manual notes separate. Treat the "next actions" list
  as advisory -- never assign, close, comment, or review without me asking.
- After a meaningful chunk of work, record what GitHub cannot show with
  `tfc note` (measured test results, a decision, a blocker, a handover line) so
  the next refresh preserves it. For example:
  `tfc note --item pr<N> --status "<short>" --text "<evidence>"`.
- Never claim a PR is merge-ready or approved from green CI or zero unresolved
  threads alone -- that is my judgement.
```

This is entirely optional. The CLI and skills work without it; the rule just
makes an agent proactively run the status check and log notes at the right
moments.
