# tech-founder-commands

A small, portable collection of **reusable commands** you can install once and run
in any repository. It starts with one command, `project-status`, and is built to
grow.

`project-status` answers, for the repo you are standing in: *what am I working
on, what's the state of my PRs, and what should I pick up next?* It reads GitHub
live (read-only) and keeps a durable work record that survives across chats and
Git worktrees.

- Assigned open issues and your authored open PRs.
- For each PR: head SHA, draft flag, CI (passing / failing / **pending or
  missing** -- never counted as green), review decision, mergeable + conflict
  state, unresolved review threads **with links**, and new comments/reviews
  since the last snapshot.
- Recently merged/closed work (bounded window).
- An **advisory** next-actions queue -- it never assigns, closes, comments, or
  reviews anything.
- Your own **manual work notes** (measured test results, decisions, blockers,
  handover knowledge), kept strictly separate from live GitHub state and never
  overwritten by a refresh.

Works on **GitHub.com and GitHub Enterprise Server (GHES)**. The host, owner, and
repo are detected from your git remote; the signed-in account comes from the
`gh` CLI. A non-GitHub or remote-less repository gets a plain local Git summary
and a clear "remote unsupported" note -- never a fabricated external status.

## Requirements

- `python3` (standard library only -- no pip, no npm, no third-party packages)
- `git`
- [`gh`](https://cli.github.com/) (GitHub CLI), already authenticated:
  `gh auth login` (or `gh auth login --hostname your-ghes-host`)

## Install

No network install script. Clone, then run the installer:

```bash
git clone https://github.com/yash0208/tech-founder-commands.git
cd tech-founder-commands
./install.sh            # installs everything: CLI + Codex skill + Claude command
```

Pick specific targets if you prefer:

```bash
./install.sh cli              # just the `tfc` command on your PATH
./install.sh codex            # the $project-status skill for Codex
./install.sh claude           # the /project-status command for Claude Code
./install.sh codex claude     # both agent integrations
./install.sh --dry-run all    # show what would happen, change nothing
./install.sh all --force      # overwrite existing installs (otherwise skipped)
```

The installer:

- puts the `tfc` launcher at `~/.local/bin/tfc` (honoring `XDG_BIN_HOME`) and the
  runner under `~/.local/share/tech-founder-commands` (honoring `XDG_DATA_HOME`);
- installs the Codex skill into `$CODEX_HOME/skills` (default `~/.codex/skills`);
- installs the Claude command into `~/.claude/commands` (or `$CLAUDE_CONFIG_DIR`);
- **never overwrites** an existing skill, command, or wrapper without `--force`;
- works from paths containing spaces.

If `~/.local/bin` is not on your `PATH`, the installer prints the line to add to
your shell profile.

## Use

From inside any GitHub-backed repository (or any worktree of it):

```bash
tfc status       # refresh from GitHub, then print the dashboard (the usual one)
tfc refresh      # read GitHub live and regenerate the dashboard only
tfc show         # print the current dashboard (STATUS.md) without re-querying
tfc paths        # show the resolved repo context and where state is stored
```

`tfc status` is just `tfc refresh` followed by `tfc show`. Global options
(`--repo`, `--host`, `--user`, `--cwd`, `--remote`) work before or after the
subcommand.

Record work that GitHub cannot show, so the next refresh keeps it:

```bash
tfc note --item pr123 --status "tests-pass, do-not-merge" \
         --text "unit + integration suites green locally; needs a staging check first"
tfc note --handover --append "Picked up #45; blocked on a test fixture."
tfc note --track-issue 45 --track-pr 123     # pull these live each refresh
tfc note --map 45 123                         # link issue 45 <-> PR 123
tfc note --exclude-rec issue45                # paused: drop from next-actions
```

Override detection when you need to:

```bash
tfc --repo acme/widgets refresh                 # force a repo
tfc --host github.example.com --repo acme/widgets refresh   # GHES
tfc --user someone-else refresh                 # force a login
tfc --cwd /path/to/another/clone refresh        # run against another checkout
```

### Agent commands

- **Codex:** the skill is invoked as `$project-status`, or just ask for your
  "project status" / to "plan my day on this repo".
- **Claude Code:** run `/project-status`.

Both adapters drive the same `tfc` CLI. Note that each app may already have a
built-in command named `/status` (showing session/account info); those are
unrelated and are **not** replaced or aliased by this collection.

## Example output

Running `tfc show` in a repo `acme/widgets` looks like:

```markdown
# Project work status -- acme/widgets

_Repo acme/widgets @ github.com | user octocat | generated 2026-01-15T09:30:00Z_

> Live sections are generated from GitHub on each refresh. Manual work notes
> live in `notes.json` and are preserved across refreshes. This dashboard does
> **not** assert merge-readiness or issue closure -- those are human calls.

## Next actions (advisory -- nothing is assigned, closed or posted)

- PR #123 has 1 unresolved review thread(s) to address.
- Issue #45 'Add CSV export' has no open PR from you -- candidate to pick up.

## Open PRs (authored)

- **[#123](https://github.com/acme/widgets/pull/123)** Add retry to uploader
  - branch `feat/retry` @ `a1b2c3d4e5f6` -> base `main` | updated 2026-01-15T08:00:00Z
  - CI: 4/5 passing | 1 pending/missing
    - pending: integration
  - review: decision `REVIEW_REQUIRED` | requested from alice | mergeable `MERGEABLE` / state `BLOCKED`
  - review threads: 1 unresolved / 3 total
    - unresolved (alice) `src/upload.py`: https://github.com/acme/widgets/pull/123#discussion_r1
  - note: merge-readiness is a human judgement -- green CI and zero unresolved
    threads do NOT mean approved or ready to merge.
  - your note: unit + integration suites green locally; needs a staging check first

## Assigned open issues

- **[#45](https://github.com/acme/widgets/issues/45)** Add CSV export | updated 2026-01-14T12:00:00Z

## Recently completed (last 30d)

**Merged/closed PRs:**
- [#120](https://github.com/acme/widgets/pull/120) `MERGED` Fix pagination | 2026-01-10T00:00:00Z

## Handover notes (manual -- not derived from GitHub)

Picked up #45; blocked on a test fixture.
```

## Where state lives (per project, outside this repo)

Durable state is written **outside any clone** so it is never committed and is
shared across every worktree of the same remote:

```
${XDG_STATE_HOME:-~/.local/state}/tech-founder-commands/<host>/<owner>/<repo>/
    STATUS.md        generated dashboard (safe to read/share)
    notes.json       your manual work notes (human-owned; never auto-overwritten)
    snapshot.json    machine snapshot; used to compute "new since last time"
    worklog.jsonl    bounded append-only activity log
```

For a local-only repository (no GitHub remote), the key is a hash of the repo's
common git directory instead of `host/owner/repo`. Run `tfc paths` to see the
exact directory for the repo you are in.

## Architecture

```
tech-founder-commands/
├── bin/tfc                         portable launcher (finds the runner, runs python3 -I)
├── lib/tfc_status.py               the whole runner (stdlib only)
├── skills/project-status/          Codex skill (SKILL.md + agents/openai.yaml)
├── adapters/claude/commands/       Claude /project-status command
├── install.sh                      local installer (cli | codex | claude | all)
├── docs/agent-rule-snippet.md      optional rule so agents keep notes current
└── tests/                          offline unit + installer/packaging tests
```

- **Read-only against GitHub.** Nothing is ever assigned, closed, commented, or
  reviewed. The next-actions list is advisory.
- **Pending/missing checks are never green**, and the dashboard never asserts
  merge-readiness or issue closure -- those stay human judgements. A merged PR
  never auto-closes a related issue here.
- **Durable and separated.** Live GitHub state is regenerated each refresh;
  your manual notes are never touched by a refresh. On any GitHub/auth failure,
  the last good data and timestamp are retained and the dashboard is clearly
  marked STALE rather than wiped or faked.
- **Safe writes.** All files are written atomically under a cross-process lock,
  so two agents refreshing/noting at once cannot corrupt the data.
- **No secrets.** `gh` uses its own stored authentication; this tool never reads
  or writes tokens, keyrings, or auth config, and never stores raw API responses.

## Limits

- GitHub scope only (github.com + GHES). There is no GitLab/Bitbucket
  integration -- those remotes fall back to a local Git summary.
- Queries are bounded: recently-completed work uses a 30-day window; pagination
  is capped; `gh` calls have timeouts. Very large result sets are truncated for
  readability.
- "New since last snapshot" needs a prior successful refresh; the first refresh
  does not flag everything as new.
- No background monitoring or scheduling. Status is produced when you run it.
  See `docs/agent-rule-snippet.md` to have an agent run it at the right moments.

## Development

```bash
python3 tests/test_tfc_status.py    # runner unit tests (git/gh mocked, offline)
python3 tests/test_install.py       # installer + packaging (no-private-data) tests
```

## License

MIT (c) 2026 Yash Mehta. See [LICENSE](LICENSE).
