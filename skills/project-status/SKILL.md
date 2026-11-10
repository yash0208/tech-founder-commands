---
name: project-status
description: Show the current user's work on the active Git repository -- assigned open issues, authored open PRs with CI/review/conflict/unresolved-thread status, recently merged work, and an advisory next-actions queue -- plus durable manual work notes kept separate from live GitHub state. Use for "what's my status here", "plan my day on this repo", or resuming work. Works on github.com and GitHub Enterprise Server; gives a local Git summary for non-GitHub remotes.
metadata:
  short-description: Current repo work status + durable notes
---

# project-status

Report where the current user stands on the repository they are working in, and
keep a durable work record that survives across chats and worktrees.

This skill is read-only against GitHub. It never assigns, closes, comments on, or
reviews anything -- the "next actions" it surfaces are advisory. It does not and
must not override any built-in `/status` command of the host application; those
are unrelated and named differently per tool.

## Tooling

Everything runs through the bundled CLI, `tfc` (installed on PATH, or invoked as
`python3 <install>/lib/tfc_status.py`). It needs only `git`, the authenticated
`gh` CLI, and `python3` (standard library only).

Context is detected from the repo's git remote:

- **Host, owner, repo** come from `git remote get-url origin` (HTTPS and SSH URLs
  on github.com and GHES are all handled).
- **The GitHub login** is whatever `gh` is signed in as for that host -- never
  hardcoded. Override any of these with `--host`, `--repo owner/name`, `--user`.
- A **non-GitHub or remote-less** repo gets a local Git summary and a clear
  "remote unsupported" note -- no fabricated external status.

Durable state lives OUTSIDE the clone (under `XDG_STATE_HOME` or
`~/.local/state/tech-founder-commands/<host>/<owner>/<repo>`), so it is never
committed and is shared across all worktrees of the same remote. Run
`tfc paths` to see the exact locations.

## When to use

- The user asks for their status / open work / PR review state / what to pick up
  next on this repo, or asks to plan the day.
- You are resuming work and need to know what changed since last time.
- After a meaningful chunk of work, to record what was measured/decided.

## How to respond

1. Run `tfc status` (one step: refresh from GitHub, then print the dashboard).
   Equivalent to `tfc refresh` followed by `tfc show`; use `tfc show` alone to
   reprint without re-querying. On a non-GitHub repo, this writes and prints a
   local Git summary instead of fabricating external status.
2. Summarise concisely in chat, with issue/PR numbers, links, and timestamps.
   Keep two things distinct and never conflate them:
   - **Live GitHub state**: assigned open issues; authored open PRs with head
     SHA, draft flag, CI (passing / FAILING / pending-or-missing -- pending and
     missing are never "green"), review decision, mergeable + conflict state,
     unresolved review-thread count with links, and new comments/reviews since
     the last snapshot; recently merged/closed work.
   - **Manual work notes**: your measured evidence, per-item status, handover
     knowledge, manual issue<->PR mapping. These are human-owned.
3. Never claim a PR is merge-ready or approved from green CI or zero unresolved
   threads alone -- that is a human judgement, and the dashboard says so.
4. Offer the advisory next-actions list for planning; do not act on it (no
   assigning, closing, commenting, reviewing) without the user asking.

## Recording work notes

After meaningful work, record what is not derivable from GitHub so the next
refresh keeps it:

```
tfc note --item pr123 --status "tests-pass, do-not-merge until acceptance" \
         --text "unit + integration suites green locally; needs a staging check"
tfc note --handover --append "Picked up #45; blocked on a test fixture."
tfc note --track-issue 45 --track-pr 123      # pull these live each refresh
tfc note --map 45 123                          # link issue 45 <-> PR 123
tfc note --exclude-rec issue45                 # paused: drop from next-actions
```

`refresh` regenerates only the live sections and never touches these notes.

There is no background monitoring: status is produced on request. If the
optional agent rule is installed (see the repo's `docs/agent-rule-snippet.md`),
record notes after each meaningful work session so the record stays current.

## Pausing an item

To stop recommending a paused issue/PR, add its key (`issue<N>` / `pr<N>`) via
`tfc note --exclude-rec`. There is no built-in list of paused items -- it is
whatever the user has excluded.
