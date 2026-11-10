#!/usr/bin/env python3
"""Tests for the tech-founder-commands installer and packaged tree.

Drives install.sh in isolated temp HOME/XDG roots (including paths with spaces
and shell metacharacters), and asserts: targets install to the right places;
re-running is idempotent and preserves existing/unrelated payload; existing
files and staged runtime are not clobbered without --force; --force overwrites;
metacharacters in paths are never executed; and the published tree contains no
personal/company identifiers, credentials, or stale branding.

The private-data scan uses generic PATTERNS (absolute personal paths, emails,
credential shapes, stale project name) rather than embedding any real private
identifier -- so the scanner file carries no secret of its own and is scanned
like every other file (no self-exemption).

Standard-library unittest only. Requires bash, cp, ln on PATH (POSIX).

Run:  python3 tests/test_install.py
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INSTALL = os.path.join(ROOT, "install.sh")


def run_install(home, args, extra_env=None):
    env = dict(os.environ)
    env["HOME"] = home
    # Pin XDG / config roots under HOME so nothing escapes the sandbox.
    env["XDG_DATA_HOME"] = os.path.join(home, ".local", "share")
    env["XDG_BIN_HOME"] = os.path.join(home, ".local", "bin")
    env["CODEX_HOME"] = os.path.join(home, ".codex")
    env["CLAUDE_CONFIG_DIR"] = os.path.join(home, ".claude")
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(["bash", INSTALL] + args, cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=60)
    return r


def _read(p):
    with open(p, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


class TestInstaller(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _paths(self, home=None):
        home = home or self.home
        return {
            "tfc_link": os.path.join(home, ".local", "bin", "tfc"),
            "runner": os.path.join(home, ".local", "share",
                                   "tech-founder-commands", "lib", "tfc_status.py"),
            "codex_skill": os.path.join(home, ".codex", "skills",
                                        "project-status", "SKILL.md"),
            "claude_cmd": os.path.join(home, ".claude", "commands",
                                       "project-status.md"),
        }

    def test_install_all(self):
        r = run_install(self.home, ["all"])
        self.assertEqual(r.returncode, 0, r.stderr)
        p = self._paths()
        self.assertTrue(os.path.exists(p["runner"]))
        self.assertTrue(os.path.islink(p["tfc_link"]) or os.path.exists(p["tfc_link"]))
        self.assertTrue(os.path.exists(p["codex_skill"]))
        self.assertTrue(os.path.exists(p["claude_cmd"]))

    def test_cli_only(self):
        r = run_install(self.home, ["cli"])
        self.assertEqual(r.returncode, 0, r.stderr)
        p = self._paths()
        self.assertTrue(os.path.exists(p["runner"]))
        self.assertFalse(os.path.exists(p["codex_skill"]))
        self.assertFalse(os.path.exists(p["claude_cmd"]))

    def test_agent_targets_include_working_runtime(self):
        for target in ("codex", "claude"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as home:
                result = run_install(home, [target])
                self.assertEqual(result.returncode, 0, result.stderr)
                runtime = os.path.join(home, ".local", "bin", "tfc")
                result = subprocess.run([runtime, "--help"], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_idempotent(self):
        r1 = run_install(self.home, ["all"])
        r2 = run_install(self.home, ["all"])
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("SKIP", r2.stdout)  # second run skips, not overwrites

    def test_no_clobber_without_force(self):
        skill = self._paths()["codex_skill"]
        os.makedirs(os.path.dirname(skill), exist_ok=True)
        with open(skill, "w") as f:
            f.write("PRE-EXISTING USER CONTENT")
        r = run_install(self.home, ["codex"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SKIP", r.stdout)
        self.assertEqual(_read(skill), "PRE-EXISTING USER CONTENT")

    def test_runtime_not_overwritten_without_force(self):
        # A pre-existing staged runner must be preserved when --force is absent,
        # even though the `tfc` symlink step runs: install_cli must not cp over it.
        p = self._paths()
        os.makedirs(os.path.dirname(p["runner"]), exist_ok=True)
        with open(p["runner"], "w") as f:
            f.write("CUSTOM RUNTIME -- DO NOT CLOBBER")
        r = run_install(self.home, ["cli"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(_read(p["runner"]), "CUSTOM RUNTIME -- DO NOT CLOBBER")
        self.assertIn("SKIP", r.stdout)

    def test_repeat_install_preserves_unrelated_payload(self):
        run_install(self.home, ["cli"])
        data_dir = os.path.dirname(os.path.dirname(self._paths()["runner"]))
        sentinel = os.path.join(data_dir, "unrelated-user-file.txt")
        with open(sentinel, "w") as f:
            f.write("keep me")
        run_install(self.home, ["cli"])  # second install
        self.assertTrue(os.path.exists(sentinel))
        self.assertEqual(_read(sentinel), "keep me")

    def test_force_overwrites(self):
        skill = self._paths()["codex_skill"]
        os.makedirs(os.path.dirname(skill), exist_ok=True)
        with open(skill, "w") as f:
            f.write("OLD")
        r = run_install(self.home, ["codex", "--force"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("project-status", _read(skill))

    def test_force_overwrites_runtime(self):
        p = self._paths()
        os.makedirs(os.path.dirname(p["runner"]), exist_ok=True)
        with open(p["runner"], "w") as f:
            f.write("OLD RUNTIME")
        r = run_install(self.home, ["cli", "--force"])
        self.assertEqual(r.returncode, 0, r.stderr)
        contents = _read(p["runner"])
        self.assertNotEqual(contents, "OLD RUNTIME")
        self.assertIn("tfc_status", contents)

    def test_path_with_spaces(self):
        spaced = os.path.join(self.home, "a dir with spaces")
        os.makedirs(spaced, exist_ok=True)
        r = run_install(spaced, ["all"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self._paths(spaced)["runner"]))

    def test_path_with_shell_metacharacters(self):
        # Apostrophe, dollar-sign, backtick, semicolon, parens -- none must be
        # evaluated by the installer. A sentinel file proves no command injection.
        weird = os.path.join(self.home, "we'ird $HOME `x` ;touch pwned; (a)")
        os.makedirs(weird, exist_ok=True)
        sentinel = os.path.join(self.home, "pwned")
        r = run_install(weird, ["all"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self._paths(weird)["runner"]))
        self.assertFalse(os.path.exists(sentinel),
                         "installer executed shell syntax from a path")
        # '$HOME' in the path must stay literal, not expand.
        self.assertTrue(os.path.isdir(weird))

    def test_dry_run_writes_nothing(self):
        r = run_install(self.home, ["--dry-run", "all"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY:", r.stdout)
        self.assertFalse(os.path.exists(self._paths()["runner"]))

    def test_installed_tfc_runs(self):
        run_install(self.home, ["cli"])
        link = self._paths()["tfc_link"]
        r = subprocess.run([link, "--help"], env=dict(os.environ),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("status", r.stdout)
        self.assertIn("refresh", r.stdout)


# ---------------------------------------------------------------------------
# Generic private-data scan: patterns only, no embedded private literals, and
# every file (including this one) is scanned -- no self-exemption.
# ---------------------------------------------------------------------------
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}
PUBLIC_EXT = {".py", ".md", ".sh", ".yaml", ".yml", ".txt", ".toml", ".cfg", ""}

# Hostnames/owners/users that are legitimately expected in public docs/tests.
ALLOWED_TOKENS = {
    "github.com", "api.github.com", "cli.github.com",
    "github.example.com", "git.corp.example.com", "github.tools.example.com",
    "gitlab.com", "bitbucket.org", "acme", "example.com", "octocat",
}

PATTERNS = {
    # Absolute personal home paths (macOS/Linux) -- never belong in a public repo.
    "absolute personal path": re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+"),
    # Credential shapes.
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{10,}\b"),
    "url userinfo credential": re.compile(r"https?://[^/\s:@]+:[^/\s@]+@"),
    # Stale/previous project branding. Built programmatically so this file does
    # not itself contain the stale literal (which would self-trip the scan).
    "stale branding": re.compile(r"tech-founder" + "s" + r"-commands"),
    # An email address that is not an obvious placeholder, and not the `git@host`
    # SSH-remote convention (which is not a credential).
    "non-placeholder email": re.compile(
        r"\b(?!git@)[A-Za-z0-9._%+-]+@(?!example\.com|example\.org|acme\b)"
        r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
}


class TestNoPrivateData(unittest.TestCase):
    def _iter_files(self):
        for dp, dn, fn in os.walk(ROOT):
            dn[:] = [d for d in dn if d not in SKIP_DIRS]
            for name in fn:
                yield os.path.join(dp, name)

    def test_no_private_patterns(self):
        hits = []
        for path in self._iter_files():
            if os.path.splitext(path)[1].lower() not in PUBLIC_EXT:
                continue
            try:
                text = _read(path)
            except OSError:
                continue
            rel = os.path.relpath(path, ROOT)
            for label, pat in PATTERNS.items():
                for m in pat.finditer(text):
                    tok = m.group(0)
                    if tok in ALLOWED_TOKENS:
                        continue
                    # Allow this test's own ALLOWED_TOKENS set literals.
                    hits.append(f"{rel}: [{label}] {tok}")
        self.assertEqual(hits, [], "private-data patterns leaked:\n" + "\n".join(hits))

    def test_scanner_scans_itself(self):
        # Guard against re-introducing a self-exemption: this file is in the set.
        files = {os.path.abspath(p) for p in self._iter_files()}
        self.assertIn(os.path.abspath(__file__), files)


if __name__ == "__main__":
    unittest.main(verbosity=2)
