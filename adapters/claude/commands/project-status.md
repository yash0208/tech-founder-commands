---
description: Show current repo work status (assigned issues, authored PRs with CI/review/conflict state, recent merges, advisory next actions) plus durable manual notes, via the tech-founder-commands tfc CLI.
---

# /project-status

Report where I stand on the repository in the current working directory, and keep
a durable work record across chats and worktrees. This is **read-only** against
GitHub -- never assign, close, comment on, or review anything; the "next actions"
are advisory only.

Note: this is a user command named `/project-status`. It does not replace Claude
Code's built-in `/status` (which shows the session/account) -- that is unrelated.

## Steps

1. Run `tfc status` (one step: refresh from GitHub, then print the dashboard;
   equivalent to `tfc refresh` then `tfc show`). (If `tfc` is not on PATH, run
   `python3 "$HOME/.local/share/tech-founder-commands/lib/tfc_status.py"` with
   the same arguments, or the path reported by `tfc paths`.)
   - Host/owner/repo are detected from the git remote; the GitHub login is
     whatever `gh` is signed in as for that host. On a non-GitHub remote, `tfc`
     writes a local Git summary instead of fabricating external status.
2. Summarise concisely with issue/PR numbers, links, and timestamps, keeping
   **live GitHub state** and **manual work notes** clearly separate.
   - For each open PR report: head SHA, draft, CI (passing / FAILING /
     pending-or-missing -- pending and missing are not "green"), review decision,
     mergeable + conflict state, unresolved review threads (with links), and new
     comments/reviews since the last snapshot.
   - Do not call a PR merge-ready or approved from green CI or zero unresolved
     threads alone -- that is a human judgement.
3. Offer the advisory next-actions list for planning; act only if I ask.

## Recording notes after work

```
tfc note --item pr123 --status "tests-pass, do-not-merge" --text "<measured evidence>"
tfc note --handover --append "<handover line>"
tfc note --track-issue 45 --track-pr 123
tfc note --map 45 123
tfc note --exclude-rec issue45      # paused: drop from next-actions
```

`tfc refresh` regenerates only the live sections and preserves these notes.

$ARGUMENTS
