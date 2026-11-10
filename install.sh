#!/usr/bin/env bash
# install.sh -- install tech-founder-commands locally.
#
# Targets (one or more, space-separated; default: all):
#   cli      Put the `tfc` command and its runner on your PATH.
#   codex    Install the project-status skill into your Codex skills dir.
#   claude   Install the /project-status command into your Claude commands dir.
#   all      cli + codex + claude.
#
# Honors custom HOME / XDG_* / CODEX_HOME / CLAUDE_CONFIG_DIR. Never overwrites an
# existing skill, command, wrapper, or staged runtime without --force. No network
# access, no `eval`. Safe with paths containing spaces and shell metacharacters.
#
# Usage:
#   ./install.sh                 # install all targets
#   ./install.sh cli             # just the CLI
#   ./install.sh codex claude    # both agent integrations
#   ./install.sh all --force     # overwrite existing installs
#   ./install.sh --dry-run all   # print what would happen
set -euo pipefail

# --- locate the source tree (this script's dir), following symlinks ----------
src="${BASH_SOURCE[0]}"
while [ -h "$src" ]; do
  d="$(cd -P "$(dirname "$src")" >/dev/null 2>&1 && pwd)"
  src="$(readlink "$src")"
  [[ "$src" != /* ]] && src="$d/$src"
done
ROOT="$(cd -P "$(dirname "$src")" >/dev/null 2>&1 && pwd)"

FORCE=0
DRY=0
TARGETS=()
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --dry-run) DRY=1 ;;
    cli|codex|claude|all) TARGETS+=("$arg") ;;
    -h|--help)
      sed -n '2,25p' "$src"; exit 0 ;;
    *) printf 'install.sh: unknown argument %q (try --help)\n' "$arg" >&2; exit 2 ;;
  esac
done
[ ${#TARGETS[@]} -eq 0 ] && TARGETS=("all")

# --- resolve destination roots (XDG / HOME aware) ----------------------------
# CODEX_HOME / CLAUDE_CONFIG_DIR are read into differently-named locals rather
# than reassigned, so a common env var is never repurposed.
home_dir="${HOME:?HOME must be set}"
xdg_data="${XDG_DATA_HOME:-$home_dir/.local/share}"
xdg_bin="${XDG_BIN_HOME:-$home_dir/.local/bin}"
codex_root="${CODEX_HOME:-$home_dir/.codex}"
claude_root="${CLAUDE_CONFIG_DIR:-$home_dir/.claude}"

data_dir="$xdg_data/tech-founder-commands"      # runner lives here for the CLI
codex_skills="$codex_root/skills"
claude_commands="$claude_root/commands"

say() { printf '%s\n' "$*"; }

# Argv-based runner: executes its arguments directly (no `eval`, no shell
# re-parsing), so paths with spaces, quotes, $ and other metacharacters are
# passed through literally. In --dry-run it prints a safely-quoted preview.
run() {
  if [ "$DRY" -eq 1 ]; then
    printf 'DRY:'; printf ' %q' "$@"; printf '\n'
    return 0
  fi
  "$@"
}

# Copy a path into place, refusing to clobber unless --force.
install_tree() {
  local what="$1" from="$2" to="$3"
  if [ -e "$to" ] && [ "$FORCE" -ne 1 ]; then
    say "SKIP  $what already exists: $to (use --force to overwrite)"
    return 0
  fi
  run mkdir -p "$(dirname "$to")"
  if [ "$FORCE" -eq 1 ]; then run rm -rf "$to"; fi
  run cp -R "$from" "$to"
  say "OK    $what -> $to"
}

# Stage one runtime file, refusing to overwrite an existing one unless --force.
stage_file() {
  local what="$1" from="$2" to="$3"
  if [ -e "$to" ] && [ "$FORCE" -ne 1 ]; then
    say "SKIP  $what already present: $to (use --force to overwrite)"
    return 0
  fi
  run cp "$from" "$to"
  run chmod +x "$to" 2>/dev/null || true
  say "OK    $what -> $to"
}

install_cli() {
  # Stage the runner + launcher under the data dir (without clobbering an
  # existing install unless --force), then link `tfc` onto PATH.
  run mkdir -p "$data_dir/lib" "$data_dir/bin" "$xdg_bin"
  stage_file "runner" "$ROOT/lib/tfc_status.py" "$data_dir/lib/tfc_status.py"
  stage_file "launcher" "$ROOT/bin/tfc" "$data_dir/bin/tfc"
  local link="$xdg_bin/tfc"
  if [ -e "$link" ] && [ "$FORCE" -ne 1 ]; then
    say "SKIP  tfc already on PATH: $link (use --force to overwrite)"
  else
    run ln -sf "$data_dir/bin/tfc" "$link"
    say "OK    tfc -> $link"
  fi
  case ":$PATH:" in
    *":$xdg_bin:"*) : ;;
    *) say "NOTE  add to your shell profile:  export PATH=\"$xdg_bin:\$PATH\"" ;;
  esac
}

for t in "${TARGETS[@]}"; do
  case "$t" in
    all)
      install_cli
      install_tree "Codex skill" "$ROOT/skills/project-status" "$codex_skills/project-status"
      install_tree "Claude command" "$ROOT/adapters/claude/commands/project-status.md" "$claude_commands/project-status.md"
      ;;
    cli) install_cli ;;
    codex) install_cli; install_tree "Codex skill" "$ROOT/skills/project-status" "$codex_skills/project-status" ;;
    claude) install_cli; install_tree "Claude command" "$ROOT/adapters/claude/commands/project-status.md" "$claude_commands/project-status.md" ;;
  esac
done

say ""
say "Done. Verify with:  tfc --help"
say "Then, inside any GitHub-backed repo:  tfc status"
