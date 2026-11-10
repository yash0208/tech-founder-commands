#!/usr/bin/env python3
"""Tests for tech-founder-commands project-status runner.

Standard-library unittest only. git and gh are never actually invoked -- the two
subprocess chokepoints (`git` and `gh` in the runner module) are monkeypatched
with deterministic fakes, so these tests are hermetic and offline.

Run:  python3 -m unittest -v tests/test_tfc_status.py
  or: python3 tests/test_tfc_status.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import tfc_status as t  # noqa: E402


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Pure helpers: remote parsing & host detection
# ---------------------------------------------------------------------------
class TestRemoteParsing(unittest.TestCase):
    def test_https_github_com(self):
        r = t.parse_remote_url("https://github.com/acme/widgets.git")
        self.assertEqual(r, {"host": "github.com", "owner": "acme", "repo": "widgets"})

    def test_https_no_dot_git(self):
        r = t.parse_remote_url("https://github.com/acme/widgets")
        self.assertEqual(r["repo"], "widgets")

    def test_https_ghes(self):
        r = t.parse_remote_url("https://github.example.com/acme/widgets.git")
        self.assertEqual(r["host"], "github.example.com")
        self.assertEqual(r["owner"], "acme")

    def test_ssh_scp_github_com(self):
        r = t.parse_remote_url("git@github.com:acme/widgets.git")
        self.assertEqual(r, {"host": "github.com", "owner": "acme", "repo": "widgets"})

    def test_ssh_scp_ghes(self):
        r = t.parse_remote_url("git@git.corp.example.com:team/service.git")
        self.assertEqual(r["host"], "git.corp.example.com")
        self.assertEqual(r["owner"], "team")
        self.assertEqual(r["repo"], "service")

    def test_ssh_url_with_port(self):
        r = t.parse_remote_url("ssh://git@github.example.com:2222/acme/widgets.git")
        self.assertEqual(r["host"], "github.example.com")
        self.assertEqual(r["owner"], "acme")
        self.assertEqual(r["repo"], "widgets")

    def test_git_protocol(self):
        r = t.parse_remote_url("git://github.com/acme/widgets.git")
        self.assertEqual(r["host"], "github.com")

    def test_non_github_remote_still_parses_host(self):
        # GitLab etc. parse structurally; is_github_host decides support.
        r = t.parse_remote_url("git@gitlab.com:group/proj.git")
        self.assertEqual(r["host"], "gitlab.com")
        self.assertFalse(t.is_github_host(r["host"]))

    def test_garbage_returns_none(self):
        self.assertIsNone(t.parse_remote_url("not a url"))
        self.assertIsNone(t.parse_remote_url(""))
        self.assertIsNone(t.parse_remote_url("https://host-only.example/"))

    def test_is_github_host(self):
        self.assertTrue(t.is_github_host("github.com"))
        self.assertTrue(t.is_github_host("github.tools.example.com"))
        self.assertFalse(t.is_github_host("gitlab.com"))
        self.assertFalse(t.is_github_host("bitbucket.org"))


# ---------------------------------------------------------------------------
# CI check classification: pending/missing are never success
# ---------------------------------------------------------------------------
class TestCheckClassification(unittest.TestCase):
    def test_success_only(self):
        s = t.summarize_checks([
            {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ])
        self.assertEqual(s["success"], 2)
        self.assertEqual(s["failure"], 0)
        self.assertEqual(s["pending"], 0)

    def test_pending_not_success(self):
        s = t.summarize_checks([
            {"name": "build", "status": "IN_PROGRESS", "conclusion": None},
            {"name": "queued", "status": "QUEUED", "conclusion": None},
        ])
        self.assertEqual(s["success"], 0)
        self.assertEqual(s["pending"], 2)
        self.assertIn("build", s["pending_names"])

    def test_empty_conclusion_completed_treated_pending(self):
        # A completed check with no/unknown conclusion must not be green.
        s = t.summarize_checks([{"name": "x", "status": "COMPLETED", "conclusion": ""}])
        self.assertEqual(s["success"], 0)
        self.assertEqual(s["pending"], 1)

    def test_failures(self):
        s = t.summarize_checks([
            {"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"name": "e2e", "status": "COMPLETED", "conclusion": "TIMED_OUT"},
            {"name": "ok", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ])
        self.assertEqual(s["failure"], 2)
        self.assertEqual(s["success"], 1)
        self.assertEqual(sorted(s["failed_names"]), ["e2e", "lint"])

    def test_skipped_is_neutral(self):
        s = t.summarize_checks([{"name": "opt", "status": "COMPLETED", "conclusion": "SKIPPED"}])
        self.assertEqual(s["neutral"], 1)
        self.assertEqual(s["success"], 0)

    def test_none_rollup(self):
        s = t.summarize_checks(None)
        self.assertEqual(s["total"], 0)


# ---------------------------------------------------------------------------
# Checklist extraction never invents completion
# ---------------------------------------------------------------------------
class TestChecklist(unittest.TestCase):
    def test_counts_and_text(self):
        body = "## Tasks\n- [x] done one\n- [ ] remaining A\n* [ ] remaining B\nprose\n"
        cl = t.extract_checklist(body)
        self.assertEqual(cl["done"], 1)
        self.assertEqual(cl["todo"], 2)
        self.assertIn("remaining A", cl["todo_text"])

    def test_no_checklist(self):
        cl = t.extract_checklist("just prose, no boxes")
        self.assertEqual(cl, {"done": 0, "todo": 0, "todo_text": []})


# ---------------------------------------------------------------------------
# Recommendations: advisory, respect pauses, never assert merge-readiness
# ---------------------------------------------------------------------------
class TestRecommendations(unittest.TestCase):
    def _pr(self, **kw):
        base = {"number": 1, "checks": {"failure": 0, "pending": 0}, "threads": {},
                "reviewDecision": None, "reviewRequests": [], "title": "x",
                "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                "isDraft": False, "labels": [], "activity_incomplete": False}
        base.update(kw)
        return base

    def test_failing_ci_recommended(self):
        data = {"open_prs": [self._pr(checks={"failure": 2, "pending": 0})], "open_issues": []}
        recs = t.build_recommendations(data, t.default_notes())
        self.assertTrue(any("failing check" in r for r in recs))

    def test_pending_ci_recommended_not_as_failure(self):
        data = {"open_prs": [self._pr(checks={"failure": 0, "pending": 1})], "open_issues": []}
        recs = t.build_recommendations(data, t.default_notes())
        self.assertTrue(any("pending/missing" in r for r in recs))
        self.assertFalse(any("failing" in r for r in recs))

    def test_excluded_pr_suppressed(self):
        notes = t.default_notes()
        notes["exclude_recommendations"] = ["pr1"]
        data = {"open_prs": [self._pr(checks={"failure": 3, "pending": 0})], "open_issues": []}
        self.assertEqual(t.build_recommendations(data, notes), [])

    def test_changes_requested_flagged_without_unresolved_threads(self):
        data = {"open_prs": [self._pr(reviewDecision="CHANGES_REQUESTED",
                                      threads={"unresolved": 0})], "open_issues": []}
        recs = t.build_recommendations(data, t.default_notes())
        self.assertTrue(any("requested changes" in r for r in recs))

    def test_merge_conflict_flagged(self):
        data = {"open_prs": [self._pr(mergeStateStatus="DIRTY")], "open_issues": []}
        recs = t.build_recommendations(data, t.default_notes())
        self.assertTrue(any("merge conflicts" in r for r in recs))

    def test_draft_flagged(self):
        data = {"open_prs": [self._pr(isDraft=True)], "open_issues": []}
        recs = t.build_recommendations(data, t.default_notes())
        self.assertTrue(any("draft" in r for r in recs))

    def test_blocking_label_flagged(self):
        data = {"open_prs": [self._pr(labels=["do-not-merge"])], "open_issues": []}
        recs = t.build_recommendations(data, t.default_notes())
        self.assertTrue(any("blocking label" in r for r in recs))

    def test_issue_without_pr_is_candidate(self):
        data = {"open_prs": [], "open_issues": [{"number": 7, "title": "do thing"}]}
        recs = t.build_recommendations(data, t.default_notes())
        self.assertTrue(any("#7" in r and "candidate" in r for r in recs))

    def test_paused_issue_excluded(self):
        notes = t.default_notes()
        notes["exclude_recommendations"] = ["issue7"]
        data = {"open_prs": [], "open_issues": [{"number": 7, "title": "do thing"}]}
        self.assertEqual(t.build_recommendations(data, notes), [])

    def test_mapped_open_pr_hides_candidate(self):
        notes = t.default_notes()
        notes["mapping"] = {"7": [1]}
        data = {"open_prs": [self._pr(number=1)], "open_issues": [{"number": 7, "title": "do thing"}]}
        recs = t.build_recommendations(data, notes)
        self.assertFalse(any("candidate" in r for r in recs))

    def test_no_title_substring_inference(self):
        # An open PR whose title merely contains the issue number must NOT be
        # treated as the issue's PR -- only explicit mapping counts.
        data = {"open_prs": [self._pr(number=99, title="fix thing (relates to 7)")],
                "open_issues": [{"number": 7, "title": "do thing"}]}
        recs = t.build_recommendations(data, t.default_notes())
        self.assertTrue(any("#7" in r and "candidate" in r for r in recs))

    def test_mapping_to_closed_pr_does_not_hide_issue(self):
        # Issue mapped only to a PR that is no longer open: issue work still
        # surfaces (mapping is not proof of completion).
        notes = t.default_notes()
        notes["mapping"] = {"7": [1]}
        data = {"open_prs": [], "open_issues": [{"number": 7, "title": "do thing"}]}
        recs = t.build_recommendations(data, notes)
        self.assertTrue(any("#7" in r and "no longer" in r for r in recs))


# ---------------------------------------------------------------------------
# Store: atomic writes, lock, worklog bound
# ---------------------------------------------------------------------------
class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = t.Store(os.path.join(self.tmp.name, "state"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_atomic_write_and_load(self):
        self.store.atomic_write(self.store.notes_json, json.dumps({"a": 1}))
        self.assertEqual(self.store.load_json(self.store.notes_json, None), {"a": 1})

    def test_load_missing_returns_default(self):
        self.assertEqual(self.store.load_json(self.store.notes_json, "D"), "D")

    def test_lock_mutual_exclusion(self):
        with self.store.file_lock():
            self.assertTrue(os.path.exists(self.store.lock))
            with self.assertRaises(TimeoutError):
                with self.store.file_lock(timeout=0.3):
                    pass
        self.assertFalse(os.path.exists(self.store.lock))

    def test_lock_not_reclaimed_while_owner_alive(self):
        # A live owner (our own PID stands in for a long-running refresh) must
        # NOT be reclaimed, even if old -- so acquisition times out.
        self.store.ensure()
        with open(self.store.lock, "w") as f:
            f.write(f"{os.getpid()} 2000-01-01T00:00:00Z\n")  # ancient but alive
        with self.assertRaises(TimeoutError):
            with self.store.file_lock(timeout=0.3):
                pass
        # lock left intact (we did not own/remove it)
        self.assertTrue(os.path.exists(self.store.lock))
        os.unlink(self.store.lock)

    def test_lock_reclaimed_when_owner_dead(self):
        # A dead PID's lock is reclaimable immediately.
        self.store.ensure()
        dead = _dead_pid()
        with open(self.store.lock, "w") as f:
            f.write(f"{dead} 2000-01-01T00:00:00Z\n")
        with self.store.file_lock(timeout=1.0):
            with open(self.store.lock) as f:
                self.assertEqual(int(f.read().split()[0]), os.getpid())

    def test_worklog_bounded(self):
        for i in range(t.WORKLOG_MAX_LINES + 50):
            self.store.append_worklog({"i": i})
        with open(self.store.worklog) as f:
            lines = f.readlines()
        self.assertLessEqual(len(lines), t.WORKLOG_MAX_LINES)
        # last line is the most recent
        self.assertEqual(json.loads(lines[-1])["i"], t.WORKLOG_MAX_LINES + 49)


def _dead_pid():
    """Find a PID that is not alive (best-effort)."""
    import os as _os
    for cand in range(999999, 990000, -1):
        try:
            _os.kill(cand, 0)
        except ProcessLookupError:
            return cand
        except OSError:
            continue
    return 999999


# ---------------------------------------------------------------------------
# Credential redaction / URL sanitization
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Credential redaction / URL sanitization.
# Fixtures are assembled from fragments so this source file contains no
# contiguous token/userinfo string (which the packaging scanner would flag).
# ---------------------------------------------------------------------------
GHP = "gh" + "p_"                       # GitHub token prefix, split
TOK = GHP + "A1B2C3D4E5F6G7H8"          # synthetic token value
SECRET = "s3" + "cr3t" + "value"        # synthetic secret value
USERINFO = lambda host: "https://user:" + SECRET + "@" + host  # noqa: E731


class TestRedaction(unittest.TestCase):
    def test_sanitize_https_userinfo(self):
        url = USERINFO("github.com") + "/acme/w.git"
        out = t.sanitize_remote_url(url)
        self.assertNotIn(SECRET, out)
        self.assertNotIn("user:", out)
        self.assertIn("github.com/acme/w.git", out)

    def test_sanitize_scp_userinfo(self):
        out = t.sanitize_remote_url("deploy:" + SECRET + "@github.com:acme/w.git")
        self.assertNotIn(SECRET, out)

    def test_sanitize_strips_query(self):
        out = t.sanitize_remote_url("https://github.com/acme/w.git?token=" + SECRET)
        self.assertNotIn(SECRET, out)

    def test_scrub_token_in_error(self):
        out = t._scrub("failed with token " + TOK)
        self.assertNotIn(TOK, out)
        self.assertIn("redacted", out)

    def test_scrub_url_userinfo_in_error(self):
        out = t._scrub("clone " + USERINFO("github.com") + "/x/y failed")
        self.assertNotIn(SECRET, out)


# ---------------------------------------------------------------------------
# State directory identity: worktrees share, distinct repos don't
# ---------------------------------------------------------------------------
class TestProjectKey(unittest.TestCase):
    def test_github_key_is_remote_identity(self):
        k1 = t.project_key({"host": "github.com", "owner": "acme", "repo": "widgets"}, "/any/a")
        k2 = t.project_key({"host": "github.com", "owner": "acme", "repo": "widgets"}, "/other/b")
        self.assertEqual(k1["segments"], k2["segments"])  # worktrees share
        self.assertEqual(k1["kind"], "github")

    def test_different_repos_differ(self):
        k1 = t.project_key({"host": "github.com", "owner": "acme", "repo": "widgets"}, "/a")
        k2 = t.project_key({"host": "github.com", "owner": "acme", "repo": "gadgets"}, "/a")
        self.assertNotEqual(k1["segments"], k2["segments"])

    def test_host_segregates(self):
        k1 = t.project_key({"host": "github.com", "owner": "acme", "repo": "w"}, "/a")
        k2 = t.project_key({"host": "github.example.com", "owner": "acme", "repo": "w"}, "/a")
        self.assertNotEqual(k1["segments"], k2["segments"])

    def test_path_segments_are_safe(self):
        k = t.project_key({"host": "h/../x", "owner": "o o", "repo": "r;r"}, "/a")
        for seg in k["segments"]:
            self.assertNotIn("/", seg)
            self.assertNotIn(";", seg)
            self.assertNotIn(" ", seg)


# ---------------------------------------------------------------------------
# Fakes for git/gh so refresh runs offline
# ---------------------------------------------------------------------------

class FakeGitHub:
    """Programmable fake for the runner's `git` and `gh` functions.

    Models the REST `gh api search/issues` shape (total_count, incomplete_results,
    items[] with html_url/state/pull_request.merged_at/labels) with real
    per_page/page pagination, and the GraphQL reviewThreads/comments/reviews
    shape with pageInfo cursors. Captures every search query so tests can assert
    the requested user actually drives the request.
    """

    def __init__(self):
        self.remote_url = "https://github.com/acme/widgets.git"
        self.login = "octocat"
        # Each is a flat list of raw REST "items" dicts.
        self.open_issues = []
        self.open_prs = []
        self.closed_issues = []
        self.closed_prs = []
        self.pr_details = {}       # num -> `gh pr view` json
        self.pr_threads = {}       # num -> {"threads":[...], "comments":[...], "reviews":[...]}
        self.fail_lists = False
        self.fail_graphql = False
        self.per_page = t.SEARCH_PER_PAGE
        self.queries = []          # captured q= strings

    # --- git -------------------------------------------------------------
    def git(self, args, cwd, timeout=t.GIT_TIMEOUT):
        if args[:1] == ["rev-parse"] and "--is-inside-work-tree" in args:
            return 0, "true\n", ""
        if args[:2] == ["remote", "get-url"]:
            return (0, self.remote_url + "\n", "") if self.remote_url else (1, "", "no remote")
        if args[:1] == ["remote"]:
            return (0, "origin\n", "") if self.remote_url else (0, "", "")
        if args[:1] == ["rev-parse"]:
            return 0, "/fake/.git\n", ""
        if args[:1] == ["status"]:
            return 0, "", ""
        if args[:1] == ["log"]:
            return 0, "abc123 commit one\n", ""
        return 0, "", ""

    # --- gh --------------------------------------------------------------
    def gh(self, args, host, timeout=t.GH_TIMEOUT):
        if args[:2] == ["api", "user"]:
            return 0, self.login + "\n", ""
        if args[:2] == ["auth", "status"]:
            return 0, f"Logged in to {host} account {self.login}", ""
        if args[:3] == ["api", "-X", "GET"] and "search/issues" in args:
            return self._rest_search(args)
        if args[:2] == ["pr", "view"]:
            num = int(args[2])
            return 0, json.dumps(self.pr_details.get(num, {})), ""
        if args[:2] == ["issue", "view"]:
            num = int(args[2])
            return 0, json.dumps(self.pr_details.get(num, {})), ""
        if args[:2] == ["api", "graphql"]:
            return self._graphql(args)
        return 1, "", f"unhandled gh {args[:3]}"

    def _rest_search(self, args):
        if self.fail_lists:
            return 1, "", "simulated GitHub outage"
        q = ""
        per_page = self.per_page
        page = 1
        for i, a in enumerate(args):
            if a == "-f" and i + 1 < len(args) and args[i + 1].startswith("q="):
                q = args[i + 1][2:]
            if a == "-F" and i + 1 < len(args):
                kv = args[i + 1]
                if kv.startswith("per_page="):
                    per_page = int(kv.split("=", 1)[1])
                if kv.startswith("page="):
                    page = int(kv.split("=", 1)[1])
        self.queries.append(q)
        if "is:pr" in q and "is:closed" in q:
            data = self.closed_prs
        elif "is:pr" in q:
            data = self.open_prs
        elif "is:closed" in q:
            data = self.closed_issues
        else:
            data = self.open_issues
        total = len(data)
        start = (page - 1) * per_page
        items = data[start:start + per_page]
        return 0, json.dumps({"total_count": total, "incomplete_results": False,
                              "items": items}), ""

    def _graphql(self, args):
        if self.fail_graphql:
            return 1, "", "graphql boom"
        num = None
        after = {"tc": None, "cc": None, "rc": None}
        for i, a in enumerate(args):
            if a == "-F" and args[i + 1].startswith("num="):
                num = int(args[i + 1].split("=", 1)[1])
            if a == "-f" and "=" in args[i + 1]:
                k, v = args[i + 1].split("=", 1)
                if k in after:
                    after[k] = v or None
        spec = self.pr_threads.get(num, {"threads": [], "comments": [], "reviews": []})
        payload = {
            "reviewThreads": self._page(spec["threads"], after["tc"], total=True),
            "comments": self._page(spec["comments"], after["cc"]),
            "reviews": self._page(spec["reviews"], after["rc"]),
        }
        return 0, json.dumps({"data": {"repository": {"pullRequest": payload}}}), ""

    @staticmethod
    def _page(items, cursor, total=False, size=100):
        start = int(cursor) if cursor else 0
        chunk = items[start:start + size]
        end = start + len(chunk)
        node = {
            "pageInfo": {"hasNextPage": end < len(items), "endCursor": str(end)},
            "nodes": chunk,
        }
        if total:
            node["totalCount"] = len(items)
        return node

    def install(self, test):
        test._orig_git, test._orig_gh = t.git, t.gh
        t.git = self.git
        t.gh = self.gh

    @staticmethod
    def uninstall(test):
        t.git, t.gh = test._orig_git, test._orig_gh


def _args(state_home, **over):
    ns = type("NS", (), {})()
    ns.cwd = over.get("cwd", "/fake/repo")
    ns.remote = "origin"
    ns.host = over.get("host")
    ns.repo = over.get("repo")
    ns.user = over.get("user")
    return ns


def _namespaced(state_home, **over):
    ns = _args(state_home, **{k: v for k, v in over.items()
                              if k in ("cwd", "host", "repo", "user")})
    ns.handover = over.get("handover")
    ns.append = over.get("append", False)
    ns.item = over.get("item")
    ns.text = over.get("text")
    ns.status = over.get("status")
    ns.track_issue = over.get("track_issue")
    ns.track_pr = over.get("track_pr")
    ns.map = over.get("map")
    ns.exclude_rec = over.get("exclude_rec")
    return ns


class RefreshBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["XDG_STATE_HOME"] = self.tmp.name
        self.fake = FakeGitHub()
        self.fake.install(self)

    def tearDown(self):
        FakeGitHub.uninstall(self)
        os.environ.pop("XDG_STATE_HOME", None)
        self.tmp.cleanup()

    def ctx_state_dir(self, **over):
        return t.resolve_context(_args(self.tmp.name, **over))["state_dir"]

    def status_of(self, **over):
        return _read(os.path.join(self.ctx_state_dir(**over), "STATUS.md"))


# ---------------------------------------------------------------------------
# User selection AND that --user actually drives queries
# ---------------------------------------------------------------------------
class TestUserSelection(RefreshBase):
    def test_login_from_gh_not_hardcoded(self):
        self.fake.login = "some-enterprise-user"
        self.assertEqual(t.gh_logged_in_user("github.example.com"), "some-enterprise-user")

    def test_login_falls_back_to_auth_status(self):
        orig = self.fake.gh

        def gh(args, host, timeout=t.GH_TIMEOUT):
            if args[:2] == ["api", "user"]:
                return 1, "", "nope"
            return orig(args, host, timeout)

        t.gh = gh
        self.fake.login = "fallback-user"
        self.assertEqual(t.gh_logged_in_user("github.com"), "fallback-user")

    def test_explicit_user_override(self):
        ctx = t.resolve_context(_args(self.tmp.name, user="explicit"))
        self.assertEqual(ctx["user"], "explicit")

    def test_signed_in_user_drives_query(self):
        self.fake.login = "octocat"
        t.cmd_refresh(_args(self.tmp.name))
        self.assertTrue(any("assignee:octocat" in q for q in self.fake.queries))
        self.assertTrue(any("author:octocat" in q for q in self.fake.queries))

    def test_user_override_drives_query(self):
        t.cmd_refresh(_args(self.tmp.name, user="someone-else"))
        self.assertTrue(any("assignee:someone-else" in q for q in self.fake.queries))
        self.assertFalse(any("assignee:octocat" in q for q in self.fake.queries))

    def test_no_user_available_is_hard_error(self):
        self.fake.login = ""  # api user returns empty, auth status has no match
        orig = self.fake.gh

        def gh(args, host, timeout=t.GH_TIMEOUT):
            if args[:2] == ["api", "user"]:
                return 1, "", "no user"
            if args[:2] == ["auth", "status"]:
                return 1, "", "not logged in"
            return orig(args, host, timeout)

        t.gh = gh
        rc = t.cmd_refresh(_args(self.tmp.name))
        self.assertEqual(rc, 2)

    def test_account_switch_resets_diff(self):
        # First user records a snapshot; switching user must not treat the
        # previous user's cached data as the new user's baseline.
        self.fake.open_prs = [_item(10, pr=True)]
        self.fake.pr_details[10] = _pr_detail(10)
        t.cmd_refresh(_args(self.tmp.name, user="alice"))
        snap = _read_json(os.path.join(self.ctx_state_dir(user="alice"), "snapshot.json"))
        self.assertEqual(snap["user"], "alice")
        # same state dir (same repo), different user -> prev snapshot discarded
        t.cmd_refresh(_args(self.tmp.name, user="bob"))
        snap2 = _read_json(os.path.join(self.ctx_state_dir(user="bob"), "snapshot.json"))
        self.assertEqual(snap2["user"], "bob")


# ---------------------------------------------------------------------------
# True pagination over the REST search endpoint
# ---------------------------------------------------------------------------
class TestPagination(RefreshBase):
    def test_multi_page_collected_once(self):
        self.fake.per_page = 2
        t.SEARCH_PER_PAGE = 2
        try:
            self.fake.open_prs = [_item(n, pr=True) for n in range(1, 6)]  # 5 -> 2,2,1
            r = t.collect_authored_open_prs("acme/widgets", "github.com", "octocat")
            self.assertEqual(sorted(p["number"] for p in r["items"]), [1, 2, 3, 4, 5])
            self.assertEqual(r["total"], 5)
            self.assertFalse(r["truncated"])
        finally:
            t.SEARCH_PER_PAGE = 100

    def test_dedupes_across_pages(self):
        self.fake.per_page = 2
        t.SEARCH_PER_PAGE = 2
        try:
            self.fake.open_prs = [_item(1, pr=True), _item(2, pr=True), _item(2, pr=True)]
            r = t.collect_authored_open_prs("acme/widgets", "github.com", "octocat")
            self.assertEqual(len(r["items"]), 2)
        finally:
            t.SEARCH_PER_PAGE = 100

    def test_truncation_flag_when_total_exceeds_cap(self):
        # total_count larger than items returned -> truncated True.
        self.fake.per_page = 2
        t.SEARCH_PER_PAGE = 2

        def rest_search(repo, host, qualifiers):  # override to simulate 5000 total
            return {"items": [_item(1, pr=True)], "total": 5000,
                    "truncated": True, "incomplete": False}

        orig = t.rest_search
        t.rest_search = rest_search
        try:
            r = t.collect_authored_open_prs("acme/widgets", "github.com", "octocat")
            self.assertTrue(r["truncated"])
        finally:
            t.rest_search = orig
            t.SEARCH_PER_PAGE = 100

    def test_truncation_surfaced_in_dashboard(self):
        def rest_search(repo, host, qualifiers):
            is_pr = "is:pr" in qualifiers
            return {"items": [_item(1, pr=is_pr)] if is_pr else [],
                    "total": 5000 if is_pr else 0, "truncated": is_pr,
                    "incomplete": False}

        orig = t.rest_search
        t.rest_search = rest_search
        self.fake.pr_details[1] = _pr_detail(1)
        try:
            t.cmd_refresh(_args(self.tmp.name))
            status = self.status_of()
            self.assertIn("INCOMPLETE RESULTS", status)
            self.assertIn("of 5000", status)
        finally:
            t.rest_search = orig


# ---------------------------------------------------------------------------
# Merged vs closed distinction from the real field
# ---------------------------------------------------------------------------
class TestMergedVsClosed(RefreshBase):
    def test_merged_and_closed_distinguished(self):
        self.fake.closed_prs = [
            _item(20, pr=True, merged_at="2026-01-05T00:00:00Z"),
            _item(21, pr=True, merged_at=None),
        ]
        t.cmd_refresh(_args(self.tmp.name))
        status = self.status_of()
        self.assertIn("#20", status)
        self.assertIn("MERGED", status)
        self.assertIn("#21", status)
        self.assertIn("CLOSED", status)


# ---------------------------------------------------------------------------
# Review-thread / comment / review cursor pagination
# ---------------------------------------------------------------------------
class TestThreadPagination(RefreshBase):
    def test_all_unresolved_threads_counted_past_one_page(self):
        # 150 unresolved threads spread across two GraphQL pages.
        threads = [{"isResolved": False, "isOutdated": False, "path": f"f{i}.py",
                    "comments": {"nodes": [{"url": f"https://c/{i}",
                                            "createdAt": "2026-01-02T00:00:00Z",
                                            "author": {"login": "rev"}}]}}
                   for i in range(150)]
        self.fake.open_prs = [_item(10, pr=True)]
        self.fake.pr_details[10] = _pr_detail(10)
        self.fake.pr_threads[10] = {"threads": threads, "comments": [], "reviews": []}
        t.cmd_refresh(_args(self.tmp.name))
        snap = _read_json(os.path.join(self.ctx_state_dir(), "snapshot.json"))
        pr = snap["open_prs"][0]
        self.assertEqual(pr["threads"]["unresolved"], 150)
        self.assertEqual(pr["threads"]["total"], 150)

    def test_graphql_failure_marks_incomplete_not_zero(self):
        self.fake.open_prs = [_item(10, pr=True)]
        self.fake.pr_details[10] = _pr_detail(10)
        self.fake.fail_graphql = True
        rc = t.cmd_refresh(_args(self.tmp.name))
        self.assertEqual(rc, 1)  # stale: activity incomplete
        status = self.status_of()
        self.assertIn("UNKNOWN", status)  # threads not silently shown as zero
        snap_exists = os.path.exists(os.path.join(self.ctx_state_dir(), "snapshot.json"))
        self.assertFalse(snap_exists)  # don't persist an incomplete snapshot


# ---------------------------------------------------------------------------
# Full refresh: success, stale/retention, notes separation, first-snapshot
# ---------------------------------------------------------------------------
class TestRefresh(RefreshBase):
    def _seed_one_pr(self, num=10, concl="SUCCESS", unresolved=0):
        self.fake.open_prs = [_item(num, pr=True)]
        self.fake.pr_details[num] = _pr_detail(num, concl=concl)
        threads = [{"isResolved": False, "isOutdated": False, "path": f"f{i}.py",
                    "comments": {"nodes": [{"url": f"https://c/{i}",
                                            "createdAt": "2026-01-02T00:00:00Z",
                                            "author": {"login": "rev"}}]}}
                   for i in range(unresolved)]
        self.fake.pr_threads[num] = {"threads": threads, "comments": [], "reviews": []}

    def test_successful_refresh_writes_status_and_snapshot(self):
        self._seed_one_pr(concl="SUCCESS", unresolved=1)
        rc = t.cmd_refresh(_args(self.tmp.name))
        self.assertEqual(rc, 0)
        sd = self.ctx_state_dir()
        self.assertTrue(os.path.exists(os.path.join(sd, "STATUS.md")))
        self.assertTrue(os.path.exists(os.path.join(sd, "snapshot.json")))
        status = _read(os.path.join(sd, "STATUS.md"))
        self.assertIn("#10", status)
        self.assertIn("1 unresolved", status)
        self.assertIn("human judgement", status)

    def test_status_command_refreshes_and_prints(self):
        self._seed_one_pr()
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = t.cmd_status(_args(self.tmp.name))
        self.assertEqual(rc, 0)
        self.assertIn("Project work status", buf.getvalue())

    def test_pending_checks_not_green(self):
        self._seed_one_pr(concl=None)
        self.fake.pr_details[10]["statusCheckRollup"] = [
            {"name": "ci", "status": "IN_PROGRESS", "conclusion": None}]
        t.cmd_refresh(_args(self.tmp.name))
        status = self.status_of()
        self.assertIn("pending/missing", status)
        self.assertNotIn("1/1 passing", status)

    def test_first_snapshot_does_not_mark_everything_new(self):
        self._seed_one_pr(unresolved=0)
        self.fake.pr_threads[10]["comments"] = [
            {"url": "https://c/x", "createdAt": "2999-01-01T00:00:00Z", "author": {"login": "rev"}}]
        t.cmd_refresh(_args(self.tmp.name))
        self.assertNotIn("NEW comment", self.status_of())

    def test_second_refresh_flags_new_comment(self):
        self._seed_one_pr(unresolved=0)
        t.cmd_refresh(_args(self.tmp.name))
        self.fake.pr_threads[10]["comments"] = [
            {"url": "https://c/new", "createdAt": "2999-01-01T00:00:00Z", "author": {"login": "rev"}}]
        t.cmd_refresh(_args(self.tmp.name))
        self.assertIn("NEW comment", self.status_of())

    def test_failure_retains_prior_good_and_marks_stale(self):
        self._seed_one_pr(concl="SUCCESS")
        t.cmd_refresh(_args(self.tmp.name))
        good = self.status_of()
        self.assertIn("#10", good)
        snap_before = _read(os.path.join(self.ctx_state_dir(), "snapshot.json"))
        self.fake.fail_lists = True
        rc = t.cmd_refresh(_args(self.tmp.name))
        self.assertEqual(rc, 1)
        status = self.status_of()
        self.assertIn("STALE", status)
        self.assertIn("#10", status)
        snap_after = _read(os.path.join(self.ctx_state_dir(), "snapshot.json"))
        self.assertEqual(snap_before, snap_after)  # good snapshot untouched

    def test_notes_survive_refresh_and_appear(self):
        self._seed_one_pr()
        t.cmd_note(_namespaced(self.tmp.name, item="pr10", text="all suites pass locally",
                               status="do-not-merge"))
        t.cmd_note(_namespaced(self.tmp.name, handover="blocked on a test fixture"))
        t.cmd_refresh(_args(self.tmp.name))
        status = self.status_of()
        self.assertIn("all suites pass locally", status)
        self.assertIn("blocked on a test fixture", status)
        notes = _read_json(os.path.join(self.ctx_state_dir(), "notes.json"))
        self.assertEqual(notes["items"]["pr10"]["status"], "do-not-merge")

    def test_notes_isolated_per_project(self):
        t.cmd_note(_namespaced(self.tmp.name, handover="A notes", repo="acme/widgets"))
        t.cmd_note(_namespaced(self.tmp.name, handover="B notes", repo="acme/gadgets"))
        a = _read_json(os.path.join(self.ctx_state_dir(repo="acme/widgets"), "notes.json"))
        b = _read_json(os.path.join(self.ctx_state_dir(repo="acme/gadgets"), "notes.json"))
        self.assertEqual(a["handover"], "A notes")
        self.assertEqual(b["handover"], "B notes")

    def test_worktrees_share_state(self):
        d1 = self.ctx_state_dir(cwd="/wt/one")
        d2 = self.ctx_state_dir(cwd="/wt/two")
        self.assertEqual(d1, d2)


# ---------------------------------------------------------------------------
# Corrupt manual notes must NOT be reset to defaults; original is preserved
# ---------------------------------------------------------------------------
class TestCorruptNotes(RefreshBase):
    def _write_notes(self, content):
        sd = self.ctx_state_dir()
        os.makedirs(sd, exist_ok=True)
        path = os.path.join(sd, "notes.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_load_json_strict_raises_on_corrupt(self):
        store = t.Store(self.ctx_state_dir())
        store.ensure()
        with open(store.notes_json, "w") as f:
            f.write("{ this is : not json")
        with self.assertRaises(t.CorruptDataError):
            store.load_json(store.notes_json, None, strict=True)

    def test_load_json_lenient_returns_default_on_corrupt(self):
        store = t.Store(self.ctx_state_dir())
        store.ensure()
        with open(store.snapshot_json, "w") as f:
            f.write("not json")
        self.assertEqual(store.load_json(store.snapshot_json, {}, strict=False), {})

    def test_refresh_refuses_and_preserves_corrupt_notes(self):
        self.fake.open_prs = [_item(10, pr=True)]
        self.fake.pr_details[10] = _pr_detail(10)
        # Seed a good refresh so a prior dashboard/snapshot exist.
        t.cmd_refresh(_args(self.tmp.name))
        sd = self.ctx_state_dir()
        good_status = _read(os.path.join(sd, "STATUS.md"))
        good_snap = _read(os.path.join(sd, "snapshot.json"))
        # Corrupt the notes file.
        bad = "{ corrupt notes -- hand-edited badly"
        with open(os.path.join(sd, "notes.json"), "w", encoding="utf-8") as f:
            f.write(bad)
        rc = t.cmd_refresh(_args(self.tmp.name))
        self.assertEqual(rc, 3)  # clear failure, not success/stale
        # Original corrupt file left byte-for-byte intact (not reset to defaults).
        self.assertEqual(_read(os.path.join(sd, "notes.json")), bad)
        # Previous good dashboard + snapshot untouched.
        self.assertEqual(_read(os.path.join(sd, "STATUS.md")), good_status)
        self.assertEqual(_read(os.path.join(sd, "snapshot.json")), good_snap)

    def test_note_refuses_and_preserves_corrupt_notes(self):
        sd = self.ctx_state_dir()
        bad = "{ corrupt"
        self._write_notes(bad)
        rc = t.cmd_note(_namespaced(self.tmp.name, handover="should not be written"))
        self.assertEqual(rc, 3)
        self.assertEqual(_read(os.path.join(sd, "notes.json")), bad)

    def test_missing_notes_is_not_corrupt(self):
        # A genuinely absent notes.json is fine -- defaults are created.
        self.fake.open_prs = []
        rc = t.cmd_refresh(_args(self.tmp.name))
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(self.ctx_state_dir(), "notes.json")))


# ---------------------------------------------------------------------------
# Host classification: non-GitHub stays local; GHES needs name or --host
# ---------------------------------------------------------------------------
class TestHostClassification(RefreshBase):
    def test_gitlab_is_local_not_github(self):
        self.fake.remote_url = "git@gitlab.com:group/proj.git"
        ctx = t.resolve_context(_args(self.tmp.name))
        self.assertEqual(ctx["kind"], "local")

    def test_github_named_ghes_autodetected(self):
        self.fake.remote_url = "https://github.tools.example.com/org/repo.git"
        ctx = t.resolve_context(_args(self.tmp.name))
        self.assertEqual(ctx["kind"], "github")
        self.assertEqual(ctx["host"], "github.tools.example.com")

    def test_opaque_ghes_name_stays_local_without_host(self):
        self.fake.remote_url = "git@git.corp.example.com:team/svc.git"
        ctx = t.resolve_context(_args(self.tmp.name))
        self.assertEqual(ctx["kind"], "local")

    def test_opaque_ghes_with_explicit_host_is_github(self):
        self.fake.remote_url = "git@git.corp.example.com:team/svc.git"
        ctx = t.resolve_context(_args(self.tmp.name, host="git.corp.example.com"))
        self.assertEqual(ctx["kind"], "github")


# ---------------------------------------------------------------------------
# Non-GitHub / remote-less -> local summary WITH notes, no fabricated status,
# and no raw credential-bearing URL
# ---------------------------------------------------------------------------
class TestLocalFallback(RefreshBase):
    def test_non_github_remote_local_summary(self):
        self.fake.remote_url = "git@gitlab.com:group/proj.git"
        rc = t.cmd_refresh(_args(self.tmp.name))
        self.assertEqual(rc, 0)
        status = self.status_of()
        self.assertIn("local Git summary", status)
        self.assertIn("GitHub status unavailable", status)
        self.assertNotIn("Open PRs (authored)", status)

    def test_no_remote_local_summary(self):
        self.fake.remote_url = None
        rc = t.cmd_refresh(_args(self.tmp.name))
        self.assertEqual(rc, 0)
        self.assertIn("no remote configured", self.status_of())

    def test_local_summary_includes_notes(self):
        self.fake.remote_url = "git@gitlab.com:group/proj.git"
        t.cmd_note(_namespaced(self.tmp.name, handover="local work in progress"))
        t.cmd_refresh(_args(self.tmp.name))
        status = self.status_of()
        self.assertIn("local work in progress", status)

    def test_local_summary_redacts_credential_url(self):
        self.fake.remote_url = "https://user:" + SECRET + "@gitlab.com/group/proj.git"
        t.cmd_refresh(_args(self.tmp.name))
        status = self.status_of()
        self.assertNotIn(SECRET, status)


# ---------------------------------------------------------------------------
# Raw item/detail builders for the fake (match REST shapes observed live)
# ---------------------------------------------------------------------------
def _item(number, pr=False, merged_at=None, title=None, labels=None):
    it = {
        "number": number,
        "title": title or (f"PR{number}" if pr else f"Issue{number}"),
        "html_url": f"https://github.com/acme/widgets/{'pull' if pr else 'issues'}/{number}",
        "url": f"https://api/{number}",
        "state": "closed" if merged_at is not None else "open",
        "updated_at": "2026-01-01T00:00:00Z",
        "closed_at": merged_at or ("2026-01-05T00:00:00Z" if merged_at is not None else None),
        "labels": [{"name": n} for n in (labels or [])],
    }
    if pr:
        it["pull_request"] = {"merged_at": merged_at,
                              "url": f"https://api/pulls/{number}"}
    return it


def _pr_detail(number, concl="SUCCESS"):
    return {
        "number": number, "title": f"PR{number}",
        "url": f"https://github.com/acme/widgets/pull/{number}",
        "state": "OPEN", "isDraft": False, "headRefName": "feat",
        "headRefOid": "abc123def456", "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN", "reviewDecision": "REVIEW_REQUIRED",
        "reviewRequests": [{"login": "rev"}], "baseRefName": "main",
        "updatedAt": "2026-01-01T00:00:00Z", "labels": [],
        "statusCheckRollup": [{"name": "ci", "status": "COMPLETED", "conclusion": concl}],
    }


if __name__ == "__main__":
    unittest.main(verbosity=2)
