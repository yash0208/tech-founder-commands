#!/usr/bin/env python3
"""tech-founder-commands :: project-status runner.

Deterministic, read-only project status dashboard for any Git repository whose
origin points at GitHub.com or a GitHub Enterprise Server (GHES) host.

What it does
------------
On ``refresh`` it reads live from GitHub (via the already-authenticated ``gh``
CLI) for the signed-in user and writes a human-readable ``STATUS.md`` plus a
machine snapshot into a per-project state directory that lives OUTSIDE the clone
(so it is never committed and is shared across worktrees of the same remote).

It separates two things that must never be conflated:

  * **Live GitHub state** (assigned open issues, authored open PRs, each PR's CI
    checks / review decision / merge+conflict status / unresolved review threads
    with links / new comments since the last snapshot, recently completed work).
    Regenerated from scratch on every refresh.
  * **Manual work notes** (what you measured, tested, decided; per-item status;
    handover knowledge; manual issue<->PR mapping; paused items). Owned by you,
    edited via ``note``, and NEVER overwritten by ``refresh``.

Design invariants
-----------------
  * Host / owner / repo are detected from ``git remote get-url`` (HTTPS and SSH,
    github.com and GHES). The signed-in account is read from ``gh`` for that
    host. Nothing is hardcoded. Explicit --host/--repo/--user override detection.
  * Non-GitHub or remote-less repositories get a local Git summary and a clear
    "remote unsupported" status -- never a fabricated external status.
  * GitHub is only ever read, never written. No assignment, closing, commenting
    or reviewing. The "next actions" queue is advisory.
  * Pending / missing checks are never counted as success. Green CI and zero
    unresolved threads are explicitly NOT asserted to mean approved or mergeable
    -- that stays a human judgement.
  * On any GitHub/auth failure the previous good STATUS.md + snapshot are kept,
    the failure is surfaced loudly, and the tracker (especially manual notes) is
    never wiped.
  * All writes are atomic (temp file + os.replace) under a cross-process lock so
    two agents refreshing/noting concurrently cannot corrupt the data files.
  * No secrets are read or written. ``gh`` uses its own stored auth; this script
    never touches token / keyring / settings files, and never stores raw API
    responses or tokens.

Pure Python standard library. No third-party dependencies, no npm.

Run ``tfc --help`` (or ``python3 tfc_status.py --help``) for commands.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

APP = "tech-founder-commands"

# Bounded windows so the dashboard stays scoped and queries stay cheap.
COMPLETED_WINDOW_DAYS = 30
WORKLOG_MAX_LINES = 500
GH_TIMEOUT = 90
GIT_TIMEOUT = 20


class CorruptDataError(Exception):
    """A user-owned data file exists but could not be parsed. We refuse to
    overwrite it with defaults; the caller must surface this and preserve the
    original file (and the previous good dashboard/snapshot)."""

    def __init__(self, path, cause):
        self.path = path
        self.cause = cause
        super().__init__(f"{path}: {cause}")


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
def now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def days_ago_iso(days: int) -> str:
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def state_home() -> str:
    """Base directory for durable per-project state, outside any clone."""
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return os.path.join(xdg, APP)
    return os.path.join(os.path.expanduser("~"), ".local", "state", APP)


# Patterns that could carry a credential into logged/rendered text.
_CRED_PATTERNS = [
    re.compile(r"(https?://)[^/\s:@]+(?::[^/\s@]+)?@", re.I),      # userinfo in URL
    re.compile(r"(?i)\b(gh[pousr]_[A-Za-z0-9]{10,})"),             # GitHub tokens
    re.compile(r"(?i)(authorization|token|bearer|password|secret)\s*[:=]\s*\S+"),
]


def _scrub(text) -> str:
    """Redact anything that looks like a credential before it is logged/shown."""
    s = str(text or "")
    s = _CRED_PATTERNS[0].sub(r"\1<redacted>@", s)
    s = _CRED_PATTERNS[1].sub("<redacted-token>", s)
    s = _CRED_PATTERNS[2].sub(r"\1=<redacted>", s)
    return s


def sanitize_remote_url(url: str) -> str:
    """Strip userinfo and query so a credential-bearing remote URL is never shown."""
    if not url:
        return ""
    # Drop scp-like or URL userinfo: scheme://user:pass@host -> scheme://host
    cleaned = re.sub(r"(https?://|ssh://|git://)[^/@\s]*@", r"\1", url)
    cleaned = re.sub(r"^([\w.+-]+):[^@/\s]*@", r"\1@", cleaned)  # scp user:pass@host
    cleaned = cleaned.split("?", 1)[0]
    return cleaned


# ---------------------------------------------------------------------------
# Git / remote detection
# ---------------------------------------------------------------------------
def git(args, cwd: str, timeout: int = GIT_TIMEOUT):
    """Run a git command. Returns (rc, stdout, stderr)."""
    try:
        r = subprocess.run(
            ["git", "-C", cwd] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 127, "", "git not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"git timed out after {timeout}s"


def git_out(args, cwd: str) -> str | None:
    rc, out, _ = git(args, cwd)
    return out.strip() if rc == 0 else None


def parse_remote_url(url: str) -> dict | None:
    """Parse a git remote URL into {host, owner, repo}.

    Handles:
      https://host/owner/repo(.git)
      http://host/owner/repo(.git)
      git@host:owner/repo(.git)              (scp-like SSH)
      ssh://git@host[:port]/owner/repo(.git)
      git://host/owner/repo(.git)
    Owner may contain nested path segments (e.g. GHES org/subgroup is rare on
    GitHub but we keep everything before the final segment as the owner path).
    Returns None if it does not look like a host/owner/repo triple.
    """
    if not url:
        return None
    url = url.strip()

    host = owner = repo = None

    scp = re.match(r"^[\w.+-]+@([^:/]+):(.+)$", url)
    if scp and "://" not in url:
        host = scp.group(1)
        path = scp.group(2)
    else:
        m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^@/]+@)?([^:/]+)(?::\d+)?/(.+)$", url)
        if not m:
            return None
        host = m.group(1)
        path = m.group(2)

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None
    repo = parts[-1]
    owner = "/".join(parts[:-1])
    if not host or not owner or not repo:
        return None
    return {"host": host, "owner": owner, "repo": repo}


def detect_remote(cwd: str, remote_name: str = "origin") -> dict | None:
    """Detect {host, owner, repo, url} from the given remote, else None."""
    url = git_out(["remote", "get-url", remote_name], cwd)
    if not url:
        # Fall back to the first configured remote if 'origin' is absent.
        listing = git_out(["remote"], cwd)
        if listing:
            first = listing.splitlines()[0].strip()
            if first:
                url = git_out(["remote", "get-url", first], cwd)
    if not url:
        return None
    parsed = parse_remote_url(url)
    if parsed:
        parsed["url"] = url
    return parsed


def is_github_host(host: str) -> bool:
    """github.com or a GHES host. We treat 'github.' hosts and *.github.*
    enterprise hosts, plus the canonical github.com, as GitHub. GHES hosts can
    be arbitrary (e.g. github.tools.example.com, git.corp.example.com) so we
    confirm via `gh` auth rather than by name alone where possible; this helper
    is only the name-based hint used when we cannot probe."""
    host = (host or "").lower()
    return host == "github.com" or "github" in host


def project_key(remote: dict | None, cwd: str) -> dict:
    """Stable identity for the state directory.

    For a GitHub remote: host/owner/repo -- identical across all worktrees of the
    same remote, so they share one tracker. For a local-only repo: a hash of the
    common git dir so each distinct local repo gets its own stable tracker.
    """
    if remote and remote.get("host"):
        return {
            "kind": "github",
            "segments": [
                _safe(remote["host"]),
                _safe(remote["owner"]),
                _safe(remote["repo"]),
            ],
        }
    common = git_out(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
    if not common:
        common = git_out(["rev-parse", "--git-common-dir"], cwd) or os.path.abspath(cwd)
        if not os.path.isabs(common):
            common = os.path.abspath(os.path.join(cwd, common))
    digest = hashlib.sha256(common.encode("utf-8")).hexdigest()[:16]
    base = os.path.basename(os.path.dirname(common.rstrip("/"))) or "repo"
    return {"kind": "local", "segments": ["_local", f"{_safe(base)}-{digest}"]}


def _safe(seg: str) -> str:
    """Make a path segment filesystem-safe without losing identity."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", seg or "_")


# ---------------------------------------------------------------------------
# gh CLI
# ---------------------------------------------------------------------------
def gh(args, host: str | None, timeout: int = GH_TIMEOUT):
    """Run a gh command (optionally pinned to a host). Returns (rc, out, err)."""
    env = dict(os.environ)
    if host:
        env["GH_HOST"] = host
    env.setdefault("GH_PROMPT_DISABLED", "1")
    env.setdefault("GH_NO_UPDATE_NOTIFIER", "1")
    try:
        r = subprocess.run(
            ["gh"] + args, capture_output=True, text=True, env=env, timeout=timeout
        )
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 127, "", "gh CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"gh timed out after {timeout}s: {' '.join(args[:3])}"


def gh_json(args, host: str | None, timeout: int = GH_TIMEOUT):
    rc, out, err = gh(args, host, timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}... failed (rc={rc}): {err.strip()[:300]}")
    try:
        return json.loads(out) if out.strip() else None
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gh {' '.join(args[:3])}... returned non-JSON: {e}")


def gh_logged_in_user(host: str | None) -> str | None:
    """The account gh is authenticated as for this host (no hardcoding)."""
    rc, out, _ = gh(["api", "user", "--jq", ".login"], host)
    if rc == 0 and out.strip():
        return out.strip()
    # Fallback: parse `gh auth status` for the active account on this host.
    rc, out, err = gh(["auth", "status"], host)
    text = (out or "") + "\n" + (err or "")
    m = re.search(r"Logged in to \S+ account (\S+)", text)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# GitHub collection
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# GitHub collection -- true REST pagination via `gh api search/issues`
# ---------------------------------------------------------------------------
# GitHub's search API caps results at 1000 (10 pages of 100) regardless of
# per_page/page. We paginate properly and report total_count + a truncation flag
# so the dashboard can label an incomplete result instead of silently dropping
# items. `incomplete_results` from the API (query timed out server-side) is also
# surfaced.
SEARCH_PER_PAGE = 100
SEARCH_HARD_CAP = 1000  # GitHub's own ceiling for the search API


def rest_search(repo: str, host: str | None, qualifiers: str) -> dict:
    """Paginate GitHub's search/issues REST endpoint.

    Returns {"items": [...], "total": int, "truncated": bool, "incomplete": bool}.
    `truncated` is True when total exceeds what the search API will return
    (1000) OR our page cap; `incomplete` mirrors the API's incomplete_results.
    """
    items: list[dict] = []
    seen: set = set()
    total = 0
    incomplete = False
    page = 1
    max_pages = SEARCH_HARD_CAP // SEARCH_PER_PAGE
    while page <= max_pages:
        rc, out, err = gh(
            ["api", "-X", "GET", "search/issues",
             "-f", "q=" + qualifiers,
             "-F", f"per_page={SEARCH_PER_PAGE}", "-F", f"page={page}"],
            host,
        )
        if rc != 0:
            raise RuntimeError(f"search page {page} failed (rc={rc}): {_scrub(err)[:200]}")
        try:
            payload = json.loads(out) if out.strip() else {}
        except json.JSONDecodeError as e:
            raise RuntimeError(f"search page {page} returned non-JSON: {e}")
        total = payload.get("total_count", total)
        incomplete = incomplete or bool(payload.get("incomplete_results"))
        batch = payload.get("items") or []
        for it in batch:
            key = it.get("number")
            if key in seen:
                continue
            seen.add(key)
            items.append(it)
        if len(batch) < SEARCH_PER_PAGE:
            break
        page += 1
    truncated = incomplete or total > len(items)
    return {"items": items, "total": total, "truncated": truncated,
            "incomplete": incomplete}


def _issue_view(it: dict) -> dict:
    return {
        "number": it.get("number"),
        "title": it.get("title"),
        "url": it.get("html_url"),
        "updatedAt": it.get("updated_at"),
        "closedAt": it.get("closed_at"),
        "labels": [{"name": l.get("name")} for l in (it.get("labels") or [])
                   if isinstance(l, dict)],
    }


def collect_assigned_open_issues(repo, host, user) -> dict:
    r = rest_search(repo, host, f"repo:{repo} is:issue is:open assignee:{user}")
    return {"items": [_issue_view(i) for i in r["items"]],
            "total": r["total"], "truncated": r["truncated"]}


def collect_authored_open_prs(repo, host, user) -> dict:
    r = rest_search(repo, host, f"repo:{repo} is:pr is:open author:{user}")
    return {"items": [_issue_view(i) for i in r["items"]],
            "total": r["total"], "truncated": r["truncated"]}


def _since_date(since_iso: str) -> str:
    return (since_iso or "")[:10]  # YYYY-MM-DD for the search qualifier


def collect_recent_closed_issues(repo, host, user, since_iso) -> dict:
    day = _since_date(since_iso)
    r = rest_search(repo, host,
                    f"repo:{repo} is:issue is:closed assignee:{user} closed:>={day}")
    return {"items": [_issue_view(i) for i in r["items"]],
            "total": r["total"], "truncated": r["truncated"]}


def collect_recent_closed_prs(repo, host, user, since_iso) -> dict:
    """Closed-OR-merged PRs in the window. The merged/closed distinction comes
    from the real `pull_request.merged_at` field, not a guess."""
    day = _since_date(since_iso)
    r = rest_search(repo, host,
                    f"repo:{repo} is:pr is:closed author:{user} closed:>={day}")
    out = []
    for it in r["items"]:
        merged = bool((it.get("pull_request") or {}).get("merged_at"))
        out.append({
            "number": it.get("number"),
            "title": it.get("title"),
            "url": it.get("html_url"),
            "closedAt": it.get("closed_at"),
            "state": "MERGED" if merged else "CLOSED",
            "merged": merged,
        })
    return {"items": out, "total": r["total"], "truncated": r["truncated"]}


PR_FIELDS = (
    "number,title,url,state,isDraft,headRefName,headRefOid,mergeable,"
    "mergeStateStatus,reviewDecision,reviewRequests,statusCheckRollup,"
    "updatedAt,baseRefName,labels"
)

# Review threads, PR comments and reviews, each cursor-paginated so a PR with
# more than one page of any of them is counted fully (never a partial count
# reported as exhaustive).
PR_THREADS_Q = """
query($owner:String!,$repo:String!,$num:Int!,$tc:String,$cc:String,$rc:String){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$num){
      reviewThreads(first:100, after:$tc){
        totalCount
        pageInfo{ hasNextPage endCursor }
        nodes{ isResolved isOutdated path
          comments(first:1){nodes{url createdAt author{login}}} }
      }
      comments(first:100, after:$cc){
        pageInfo{ hasNextPage endCursor }
        nodes{ url createdAt author{login} }
      }
      reviews(first:100, after:$rc){
        pageInfo{ hasNextPage endCursor }
        nodes{ url state submittedAt author{login} }
      }
    }
  }
}"""


def summarize_checks(rollup) -> dict:
    """Classify CI. Pending/missing are NOT success; never equate green to merge-ready."""
    out = {"success": 0, "failure": 0, "pending": 0, "neutral": 0,
           "total": 0, "failed_names": [], "pending_names": []}
    for c in rollup or []:
        out["total"] += 1
        status = (c.get("status") or "").upper()
        concl = (c.get("conclusion") or "").upper()
        name = c.get("name") or c.get("context") or c.get("workflowName") or "?"
        if status and status != "COMPLETED":
            out["pending"] += 1
            out["pending_names"].append(name)
        elif concl == "SUCCESS":
            out["success"] += 1
        elif concl in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED",
                       "STARTUP_FAILURE", "STALE"):
            out["failure"] += 1
            out["failed_names"].append(name)
        elif concl in ("NEUTRAL", "SKIPPED"):
            out["neutral"] += 1
        else:
            out["pending"] += 1
            out["pending_names"].append(name)
    return out


def _collect_pr_threads(owner, name, num, host) -> dict:
    """Cursor-paginate reviewThreads/comments/reviews fully. Raises on gh error."""
    threads = {"total": 0, "unresolved": 0, "unresolved_links": []}
    comments: list[dict] = []
    reviews: list[dict] = []
    tc = cc = rc_cur = None
    more_t = more_c = more_r = True
    guard = 0
    while (more_t or more_c or more_r) and guard < 50:
        guard += 1
        args = ["api", "graphql", "-f", "query=" + PR_THREADS_Q,
                "-F", f"owner={owner}", "-F", f"repo={name}", "-F", f"num={num}"]
        # gh graphql: pass null cursors as empty -f so $var is JSON null-ish; use
        # -f var=<cursor> only when we have one.
        if tc:
            args += ["-f", f"tc={tc}"]
        if cc:
            args += ["-f", f"cc={cc}"]
        if rc_cur:
            args += ["-f", f"rc={rc_cur}"]
        code, out, err = gh(args, host)
        if code != 0 or not out.strip():
            raise RuntimeError(_scrub(err or "graphql returned nothing")[:200])
        pr = json.loads(out)["data"]["repository"]["pullRequest"]

        if more_t:
            rt = pr["reviewThreads"]
            threads["total"] = rt["totalCount"]
            for t in rt["nodes"]:
                if not t["isResolved"]:
                    threads["unresolved"] += 1
                    cn = t["comments"]["nodes"]
                    if cn:
                        threads["unresolved_links"].append({
                            "path": t.get("path"),
                            "url": cn[0]["url"],
                            "author": (cn[0].get("author") or {}).get("login"),
                        })
            pi = rt["pageInfo"]
            more_t, tc = pi["hasNextPage"], pi["endCursor"]
        if more_c:
            cm = pr["comments"]
            comments += [
                {"url": c["url"], "createdAt": c["createdAt"],
                 "author": (c.get("author") or {}).get("login")}
                for c in cm["nodes"]
            ]
            pi = cm["pageInfo"]
            more_c, cc = pi["hasNextPage"], pi["endCursor"]
        if more_r:
            rv = pr["reviews"]
            reviews += [
                {"url": r["url"], "state": r["state"], "submittedAt": r["submittedAt"],
                 "author": (r.get("author") or {}).get("login")}
                for r in rv["nodes"]
            ]
            pi = rv["pageInfo"]
            more_r, rc_cur = pi["hasNextPage"], pi["endCursor"]
    return {"threads": threads, "comments": comments, "reviews": reviews}


def enrich_pr(num: int, repo: str, host: str | None, owner: str, name: str,
              prev_pr: dict | None) -> dict:
    """Full detail for one open PR, including 'new since last snapshot' activity.

    Thread/comment/review collection is best-effort: if it fails, the PR is still
    reported with its core state and the thread section is marked unknown/partial
    rather than silently shown as zero.
    """
    base = gh_json(["pr", "view", str(num), "--repo", repo, "--json", PR_FIELDS], host)
    checks = summarize_checks(base.get("statusCheckRollup"))

    threads = {"total": 0, "unresolved": 0, "unresolved_links": [], "incomplete": False}
    recent_comments: list[dict] = []
    recent_reviews: list[dict] = []
    activity_incomplete = False
    try:
        got = _collect_pr_threads(owner, name, num, host)
        threads.update(got["threads"])
        recent_comments = got["comments"]
        recent_reviews = got["reviews"]
    except Exception as e:  # noqa: BLE001 - thread data is best-effort
        threads["error"] = _scrub(str(e))[:200]
        threads["incomplete"] = True
        activity_incomplete = True

    prev_time = (prev_pr or {}).get("snapshot_time") or ""
    new_comments = [c for c in recent_comments if (c.get("createdAt") or "") > prev_time]
    new_reviews = [r for r in recent_reviews if (r.get("submittedAt") or "") > prev_time]

    labels = [l.get("name") for l in (base.get("labels") or [])
              if isinstance(l, dict) and l.get("name")]

    return {
        "number": base["number"],
        "title": base["title"],
        "url": base["url"],
        "state": base["state"],
        "isDraft": base["isDraft"],
        "baseRefName": base.get("baseRefName"),
        "headRefName": base["headRefName"],
        "headRefOid": base["headRefOid"],
        "mergeable": base["mergeable"],
        "mergeStateStatus": base["mergeStateStatus"],
        "reviewDecision": base.get("reviewDecision"),
        "reviewRequests": [
            rr.get("slug") or rr.get("login") or rr.get("name")
            for rr in (base.get("reviewRequests") or [])
        ],
        "labels": labels,
        "updatedAt": base["updatedAt"],
        "checks": checks,
        "threads": threads,
        "activity_incomplete": activity_incomplete,
        "recent_comments": recent_comments,
        "recent_reviews": recent_reviews,
        "new_comments": new_comments,
        "new_reviews": new_reviews,
        "snapshot_time": now_utc(),
    }


def extract_checklist(body: str) -> dict:
    """Pull GitHub task-list items from an issue body WITHOUT inventing status."""
    done, todo, todo_text = 0, 0, []
    for line in (body or "").splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith(("- [ ]", "* [ ]")):
            todo += 1
            todo_text.append(s[5:].strip()[:140])
        elif low.startswith(("- [x]", "* [x]")):
            done += 1
    return {"done": done, "todo": todo, "todo_text": todo_text[:12]}


def collect_tracked(notes: dict, repo, host, owner, name, prev_tracked: dict) -> dict:
    """Live state for manually tracked issue/PR links from notes.json."""
    out = {"issues": [], "prs": []}
    for ref in notes.get("tracked", {}).get("issues", []):
        num = ref if isinstance(ref, int) else ref.get("number")
        try:
            d = gh_json(["issue", "view", str(num), "--repo", repo,
                         "--json", "number,title,url,state,body,updatedAt"], host)
            out["issues"].append({
                "number": d["number"], "title": d["title"], "url": d["url"],
                "state": d["state"], "updatedAt": d["updatedAt"],
                "checklist": extract_checklist(d.get("body") or ""),
            })
        except Exception as e:  # noqa: BLE001
            out["issues"].append({"number": num, "error": _scrub(str(e))[:200]})
    prev_prs = {p.get("number"): p for p in prev_tracked.get("prs", [])}
    for ref in notes.get("tracked", {}).get("prs", []):
        num = ref if isinstance(ref, int) else ref.get("number")
        try:
            out["prs"].append(enrich_pr(int(num), repo, host, owner, name,
                                        prev_prs.get(int(num))))
        except Exception as e:  # noqa: BLE001
            out["prs"].append({"number": num, "error": _scrub(str(e))[:200]})
    return out


# ---------------------------------------------------------------------------
# Recommendations (advisory; derived from live data + notes; never mutate GH)
# ---------------------------------------------------------------------------
def _blocking_labels(labels) -> list[str]:
    """Labels that signal a PR should not be merged yet."""
    out = []
    for lab in labels or []:
        low = (lab or "").lower()
        if any(k in low for k in ("do-not-merge", "do not merge", "dnm",
                                   "blocked", "wip", "work in progress", "hold")):
            out.append(lab)
    return out


def build_recommendations(data: dict, notes: dict) -> list[str]:
    recs: list[str] = []
    excluded = set(notes.get("exclude_recommendations", []))
    open_pr_numbers = {p.get("number") for p in data.get("open_prs", []) if "error" not in p}

    for pr in data.get("open_prs", []):
        if "error" in pr:
            continue
        key = f"pr{pr['number']}"
        if key in excluded:
            continue
        n = pr["number"]
        ch = pr["checks"]
        if ch["failure"]:
            recs.append(f"PR #{n} has {ch['failure']} failing check(s) -- fix CI.")
        elif ch["pending"]:
            recs.append(f"PR #{n} has {ch['pending']} pending/missing check(s) -- wait or re-run.")
        # Review decision: flag CHANGES_REQUESTED independently of thread state.
        decision = (pr.get("reviewDecision") or "")
        if decision == "CHANGES_REQUESTED":
            recs.append(f"PR #{n} has requested changes from a reviewer -- address them.")
        if pr["threads"].get("unresolved"):
            recs.append(f"PR #{n} has {pr['threads']['unresolved']} unresolved review thread(s) to address.")
        if decision == "REVIEW_REQUIRED" and not pr["threads"].get("unresolved"):
            who = ", ".join(pr.get("reviewRequests") or []) or "reviewers"
            recs.append(f"PR #{n} awaits review from {who} -- nudge if stale.")
        # Merge conflicts / dirty merge state.
        mss = (pr.get("mergeStateStatus") or "").upper()
        if (pr.get("mergeable") or "").upper() == "CONFLICTING" or mss in ("DIRTY", "CONFLICTING"):
            recs.append(f"PR #{n} has merge conflicts -- rebase or resolve.")
        if pr.get("isDraft"):
            recs.append(f"PR #{n} is a draft -- mark ready for review when done.")
        bl = _blocking_labels(pr.get("labels"))
        if bl:
            recs.append(f"PR #{n} carries blocking label(s) [{', '.join(bl)}] -- not mergeable yet.")
        if pr.get("new_comments") or pr.get("new_reviews"):
            recs.append(f"PR #{n} has new review activity since last snapshot -- read it.")
        if pr.get("activity_incomplete"):
            recs.append(f"PR #{n}: review thread/activity data is incomplete this refresh -- re-run to confirm.")

    # Issues: an explicit mapping to an *open* PR means work is in flight. A
    # mapping to a merged/closed PR does NOT hide the issue -- the issue is still
    # open, so its remaining work still deserves attention. No title-substring
    # inference: only exact, user-provided mappings count.
    mapping = notes.get("mapping", {})
    for it in data.get("open_issues", []):
        key = f"issue{it['number']}"
        if key in excluded:
            continue
        mapped = mapping.get(str(it["number"]), [])
        mapped_open = [m for m in mapped if _as_int(m) in open_pr_numbers]
        if mapped_open:
            continue  # active PR in flight for this issue
        if mapped:
            recs.append(
                f"Issue #{it['number']} '{(it['title'] or '')[:60]}' is mapped to "
                f"PR(s) {', '.join('#' + str(m) for m in mapped)} that are no longer "
                "open -- confirm the remaining work is done or needs a new PR."
            )
        else:
            recs.append(
                f"Issue #{it['number']} '{(it['title'] or '')[:60]}' has no mapped "
                "open PR -- candidate to pick up."
            )
    return recs[:30]


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Persistence (atomic writes + cross-process lock)
# ---------------------------------------------------------------------------
class Store:
    def __init__(self, base_dir: str):
        self.dir = base_dir
        self.status_md = os.path.join(base_dir, "STATUS.md")
        self.notes_json = os.path.join(base_dir, "notes.json")
        self.snapshot_json = os.path.join(base_dir, "snapshot.json")
        self.worklog = os.path.join(base_dir, "worklog.jsonl")
        self.lock = os.path.join(base_dir, ".lock")
        self.meta_json = os.path.join(base_dir, "project.json")

    def ensure(self) -> None:
        os.makedirs(self.dir, exist_ok=True)

    @contextmanager
    def file_lock(self, timeout: float = 30.0):
        """Cross-process lock via an atomic O_CREAT|O_EXCL lockfile holding the
        owner PID. A lock is only reclaimed when its owner process is no longer
        alive -- a long-running refresh (which can exceed any fixed age) is never
        broken just for being slow. We only ever remove a lock we still own.
        """
        self.ensure()
        start = time.time()
        acquired_pid = os.getpid()
        while True:
            try:
                fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{acquired_pid} {now_utc()}\n".encode())
                os.close(fd)
                break
            except FileExistsError:
                if self._owner_dead():
                    # Holder process is gone; reclaim and retry immediately.
                    try:
                        os.unlink(self.lock)
                    except OSError:
                        pass
                    continue
                if time.time() - start > timeout:
                    raise TimeoutError(
                        f"Could not acquire lock {self.lock} within {timeout}s "
                        "(another project-status run is in progress)."
                    )
                time.sleep(0.2)
        try:
            yield
        finally:
            # Only remove the lock if we still own it (our PID is recorded).
            try:
                with open(self.lock, "r", encoding="utf-8") as f:
                    owner = int((f.read().split() or ["-1"])[0])
                if owner == acquired_pid:
                    os.unlink(self.lock)
            except (OSError, ValueError):
                pass

    def _owner_dead(self) -> bool:
        """True only if we can read the lock's PID and that process is not alive."""
        try:
            with open(self.lock, "r", encoding="utf-8") as f:
                pid = int((f.read().split() or ["-1"])[0])
        except (OSError, ValueError):
            return False  # can't read it -> be conservative, do not reclaim
        if pid <= 0:
            return False
        if pid == os.getpid():
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True   # no such process -> dead, safe to reclaim
        except PermissionError:
            return False  # exists but owned by another user -> alive, keep it
        except OSError:
            return False
        return False

    def atomic_write(self, path: str, data: str) -> None:
        self.ensure()
        d = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".swap")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def load_json(self, path: str, default, strict: bool = False):
        """Load JSON. A genuinely missing file returns `default`.

        When `strict`, a file that exists but is corrupt/unreadable raises
        (callers that own user data must NOT silently fall back to a default and
        clobber it). When not strict (e.g. the machine snapshot, which is
        regenerated anyway), a corrupt file is treated as absent.
        """
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            if strict:
                raise CorruptDataError(path, e)
            return default

    def append_worklog(self, entry: dict) -> None:
        entry = {"ts": now_utc(), **entry}
        try:
            lines = []
            if os.path.exists(self.worklog):
                with open(self.worklog, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            lines.append(json.dumps(entry) + "\n")
            lines = lines[-WORKLOG_MAX_LINES:]
            self.atomic_write(self.worklog, "".join(lines))
        except Exception:  # noqa: BLE001 - worklog is best-effort
            pass


def default_notes() -> dict:
    return {
        "_doc": "Manual, human-owned work notes. NEVER overwritten by `refresh`. "
                "Edit via the `note` command or by hand (keep it valid JSON).",
        "handover": "",
        "items": {},          # "pr<N>" / "issue<N>" -> {note, status, updated}
        "tracked": {"issues": [], "prs": []},   # extra refs pulled live each refresh
        "mapping": {},        # issue number (str) -> [related PR numbers], manual
        "exclude_recommendations": [],  # keys like "issue123" to suppress advice
    }


def load_notes_strict(store: "Store") -> dict:
    """Load manual notes, failing loudly on a corrupt file.

    A missing notes.json yields a fresh default (and is written). A notes.json
    that exists but cannot be parsed raises CorruptDataError so the caller can
    refuse to proceed -- we never replace a user's notes with defaults just
    because they are temporarily malformed.
    """
    notes = store.load_json(store.notes_json, None, strict=True)
    if notes is None:
        notes = default_notes()
        store.atomic_write(store.notes_json, json.dumps(notes, indent=2))
    return notes


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _pr_block(pr: dict) -> list[str]:
    if "error" in pr:
        return [f"- **#{pr['number']}** -- could not refresh: {pr['error']}"]
    ch = pr["checks"]
    draft = " `draft`" if pr.get("isDraft") else ""
    L = [f"- **[#{pr['number']}]({pr['url']})** {pr['title']}{draft}"]
    L.append(
        f"  - branch `{pr['headRefName']}` @ `{(pr['headRefOid'] or '')[:12]}` "
        f"-> base `{pr.get('baseRefName')}` | updated {pr['updatedAt']}"
    )
    ci = f"{ch['success']}/{ch['total']} passing" if ch["total"] else "no checks reported"
    if ch["failure"]:
        ci += f" | {ch['failure']} FAILING"
    if ch["pending"]:
        ci += f" | {ch['pending']} pending/missing"
    if ch["neutral"]:
        ci += f" | {ch['neutral']} skipped"
    L.append(f"  - CI: {ci}")
    if ch["failed_names"]:
        L.append("    - failing: " + ", ".join(ch["failed_names"][:8]))
    if ch["pending_names"]:
        L.append("    - pending: " + ", ".join(ch["pending_names"][:8]))
    rd = pr.get("reviewDecision") or "NONE"
    rr = ", ".join(pr.get("reviewRequests") or []) or "--"
    L.append(
        f"  - review: decision `{rd}` | requested from {rr} | "
        f"mergeable `{pr['mergeable']}` / state `{pr['mergeStateStatus']}`"
    )
    th = pr["threads"]
    if th.get("error"):
        L.append(f"  - review threads: UNKNOWN -- could not read ({th['error']})")
    elif th.get("incomplete"):
        L.append(
            f"  - review threads: at least {th['unresolved']} unresolved / "
            f"{th['total']} total (INCOMPLETE this refresh -- counts may be low)"
        )
        for t in th.get("unresolved_links", [])[:8]:
            L.append(f"    - unresolved ({t.get('author')}) `{t.get('path')}`: {t['url']}")
    else:
        L.append(f"  - review threads: {th['unresolved']} unresolved / {th['total']} total")
        for t in th.get("unresolved_links", [])[:8]:
            L.append(f"    - unresolved ({t.get('author')}) `{t.get('path')}`: {t['url']}")
    if pr.get("activity_incomplete"):
        L.append("  - NOTE: comment/review activity was not fully read this "
                 "refresh; 'new since last time' for this PR may be incomplete.")
    for c in pr.get("new_comments", [])[:8]:
        L.append(f"  - NEW comment {c.get('author')} {c['createdAt']}: {c['url']}")
    for r in pr.get("new_reviews", [])[:8]:
        L.append(f"  - NEW review {r.get('author')} `{r['state']}` {r['submittedAt']}: {r['url']}")
    L.append(
        "  - note: merge-readiness is a human judgement -- green CI and zero "
        "unresolved threads do NOT mean approved or ready to merge."
    )
    return L


def render_status(ctx: dict, data: dict, notes: dict) -> str:
    L: list[str] = []
    L.append(f"# Project work status -- {ctx['owner']}/{ctx['repo']}")
    L.append("")
    L.append(
        f"_Repo {ctx['owner']}/{ctx['repo']} @ {ctx['host']} | user "
        f"{ctx.get('user') or 'unknown'} | generated {data['generated_at']}_"
    )
    L.append("")
    L.append(
        "> Live sections are generated from GitHub on each refresh. Manual work "
        "notes live in `notes.json` and are preserved across refreshes. This "
        "dashboard does **not** assert merge-readiness or issue closure -- those "
        "are human calls."
    )
    L.append("")

    if data.get("stale"):
        L.append("## STALE / INCOMPLETE")
        L.append("")
        L.append(
            f"The refresh at {data['generated_at']} hit errors; the live data "
            "below is the **last good snapshot** plus whatever succeeded this "
            f"run. Previous good data was from {data.get('last_good_at') or 'n/a'}. "
            "Treat it as potentially out of date."
        )
        for e in data.get("errors", []):
            L.append(f"- {e}")
        L.append("")

    if data.get("truncations"):
        L.append("## INCOMPLETE RESULTS (GitHub search caps)")
        L.append("")
        L.append("GitHub's search API returns at most 1000 results per query. "
                 "These sections are truncated and do not list everything:")
        for tnote in data["truncations"]:
            L.append(f"- {tnote}")
        L.append("")

    # Advisory plan-the-day
    L.append("## Next actions (advisory -- nothing is assigned, closed or posted)")
    L.append("")
    for rec in data.get("recommendations", []):
        L.append(f"- {rec}")
    if not data.get("recommendations"):
        L.append("- _No recommendations generated this refresh._")
    L.append("")

    # Open PRs
    L.append("## Open PRs (authored)")
    L.append("")
    prs = data.get("open_prs", [])
    if not prs:
        L.append("_None._")
    for pr in prs:
        L += _pr_block(pr)
        ev = notes.get("items", {}).get(f"pr{pr.get('number')}")
        if ev and ev.get("note"):
            L.append(f"  - your note: {ev.get('note')}")
    L.append("")

    # Assigned open issues
    L.append("## Assigned open issues")
    L.append("")
    issues = data.get("open_issues", [])
    if not issues:
        L.append("_None._")
    for it in issues:
        labels = ""
        if it.get("labels"):
            labels = ", ".join(
                l.get("name") for l in it["labels"] if isinstance(l, dict) and l.get("name")
            )
        extra = f" _[{labels}]_" if labels else ""
        L.append(
            f"- **[#{it['number']}]({it['url']})** {it['title']}{extra} "
            f"| updated {it['updatedAt']}"
        )
        ev = notes.get("items", {}).get(f"issue{it['number']}")
        if ev and ev.get("note"):
            L.append(f"  - your note: {ev.get('note')}")
    L.append("")

    # Manually tracked
    tr = data.get("tracked", {})
    if tr.get("issues") or tr.get("prs"):
        L.append("## Manually tracked (from notes.json)")
        L.append("")
        for it in tr.get("issues", []):
            if "error" in it:
                L.append(f"- #{it['number']} -- could not refresh: {it['error']}")
                continue
            cl = it.get("checklist", {})
            clstr = (f" | checklist {cl['done']} done / {cl['todo']} open"
                     if (cl.get("done") or cl.get("todo")) else "")
            L.append(f"- **[#{it['number']}]({it['url']})** {it['title']} `{it['state']}`{clstr}")
            for t in cl.get("todo_text", [])[:12]:
                L.append(f"    - [ ] {t}")
        for pr in tr.get("prs", []):
            L += _pr_block(pr)
        L.append("")

    # Recently completed
    L.append(f"## Recently completed (last {COMPLETED_WINDOW_DAYS}d)")
    L.append("")
    rc_issues = data.get("closed_issues", [])
    rc_prs = data.get("closed_prs", [])
    if rc_prs:
        L.append("**Merged/closed PRs:**")
        for p in rc_prs:
            L.append(f"- [#{p['number']}]({p['url']}) `{p.get('state')}` {p['title']} | {p.get('closedAt')}")
    if rc_issues:
        L.append("")
        L.append("**Closed assigned issues:**")
        for i in rc_issues:
            L.append(f"- [#{i['number']}]({i['url']}) {i['title']} | {i.get('closedAt')}")
    if not rc_prs and not rc_issues:
        L.append("_None in window._")
    L.append("")
    L.append(
        "> Closure is recorded only from GitHub state or an explicit manual "
        "mapping in notes.json -- a merged PR never auto-closes a related issue here."
    )
    L.append("")

    # Handover + per-item manual notes (shared block).
    L += _notes_block(notes)
    return "\n".join(L) + "\n"


def _notes_block(notes: dict) -> list[str]:
    """Handover + per-item manual notes. Shared by GitHub and local renderers so
    recorded work is never hidden, even on an unsupported/absent remote."""
    L: list[str] = []
    L.append("## Handover notes (manual -- not derived from GitHub)")
    L.append("")
    ho = (notes.get("handover") or "").strip()
    L.append(ho if ho else "_No handover notes yet. Use `tfc note --handover \"...\"` to add some._")
    L.append("")
    items = notes.get("items", {})
    if items:
        L.append("### Per-item manual state (measured work, kept separate from GitHub state)")
        L.append("")
        for key, ev in sorted(items.items()):
            parts = []
            if ev.get("status"):
                parts.append(f"status: {ev['status']}")
            if ev.get("note"):
                parts.append(ev["note"])
            line = f"- **{key}** -- " + " | ".join(parts) if parts else f"- **{key}**"
            L.append(line)
            if ev.get("updated"):
                L.append(f"  - _(updated {ev['updated']})_")
        L.append("")
    return L


def render_local_summary(ctx: dict, cwd: str, reason: str, notes: dict) -> str:
    """Local Git summary for non-GitHub / remote-less repositories. Includes the
    user's manual notes, and never prints a raw (possibly credential-bearing)
    remote URL -- only a sanitized host/owner/repo or stripped URL."""
    branch = git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd) or "unknown"
    head = git_out(["rev-parse", "--short", "HEAD"], cwd) or "unknown"
    status = git_out(["status", "--porcelain"], cwd)
    dirty = len([l for l in (status or "").splitlines() if l.strip()])
    log = git_out(["log", "--oneline", "-10"], cwd) or ""
    remote = ctx.get("remote") or {}
    safe_url = sanitize_remote_url(remote.get("url", "")) if remote else ""

    L = ["# Project work status -- local Git summary", ""]
    L.append(f"_generated {now_utc()}_")
    L.append("")
    L.append(f"> **GitHub status unavailable:** {reason}")
    if remote.get("host"):
        L.append(">")
        L.append(f"> Detected remote: `{remote.get('host')}/{remote.get('owner')}/"
                 f"{remote.get('repo')}`")
    elif safe_url:
        L.append(">")
        L.append(f"> Detected remote: `{safe_url}`")
    L.append(
        "> Showing a local Git summary only. No external issue/PR/CI/review "
        "status is fabricated for an unsupported or absent remote."
    )
    L.append("")
    L.append("## Local working tree")
    L.append("")
    L.append(f"- branch: `{branch}` @ `{head}`")
    L.append(f"- uncommitted changes: {dirty} path(s)")
    L.append("")
    L.append("## Recent commits")
    L.append("")
    for line in log.splitlines()[:10]:
        L.append(f"- {line}")
    if not log:
        L.append("_No commits._")
    L.append("")
    L += _notes_block(notes)
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# Context resolution
# ---------------------------------------------------------------------------
def resolve_context(args) -> dict:
    """Determine {host, owner, repo, user, remote, kind, state_dir} for cwd."""
    cwd = os.path.abspath(args.cwd or os.getcwd())
    inside = git_out(["rev-parse", "--is-inside-work-tree"], cwd)
    is_git = inside == "true"

    remote = detect_remote(cwd, args.remote) if is_git else None

    # Explicit --repo (or --host) is a deliberate override: trust it as GitHub.
    host = args.host
    owner = repo_name = None
    if args.repo:
        parts = args.repo.strip("/").split("/")
        if len(parts) >= 2:
            repo_name = parts[-1]
            owner = "/".join(parts[:-1])
    if owner is None and remote:
        # Only adopt a detected remote as GitHub when its host actually looks
        # like GitHub.com or a GHES host (unless the user forced --host).
        if args.host or is_github_host(remote["host"]):
            host = host or remote["host"]
            owner = remote["owner"]
            repo_name = remote["repo"]

    if owner and repo_name:
        kind = "github"
    elif is_git:
        kind = "local"
    else:
        kind = "none"
    # github.com vs GHES: both are "github" kind here; host carries the distinction.
    if kind == "github":
        host = host or (remote["host"] if remote else "github.com")

    user = args.user
    key = project_key(
        {"host": host, "owner": owner, "repo": repo_name} if kind == "github" else None,
        cwd,
    )
    state_dir = os.path.join(state_home(), *key["segments"])

    return {
        "cwd": cwd,
        "is_git": is_git,
        "remote": remote,
        "host": host,
        "owner": owner,
        "repo": repo_name,
        "repo_slug": f"{owner}/{repo_name}" if owner and repo_name else None,
        "user": user,
        "kind": kind,
        "key_kind": key["kind"],
        "state_dir": state_dir,
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_refresh(args) -> int:
    ctx = resolve_context(args)
    store = Store(ctx["state_dir"])

    if ctx["kind"] == "none":
        print("Not a Git repository (and no --repo given). Nothing to report.",
              file=sys.stderr)
        return 2

    if ctx["kind"] == "local":
        reason = "this repository's remote is not a GitHub.com or GHES host."
        if not ctx["remote"]:
            reason = "this repository has no remote configured."
        with store.file_lock():
            try:
                notes = load_notes_strict(store)
            except CorruptDataError as e:
                print(
                    f"Refusing to refresh: manual notes file is corrupt and was "
                    f"left untouched ({_scrub(e)}). Fix or remove "
                    f"{store.notes_json}.",
                    file=sys.stderr,
                )
                return 3
            md = render_local_summary(ctx, ctx["cwd"], reason, notes)
            store.atomic_write(store.status_md, md)
            store.append_worklog({"event": "refresh", "kind": "local"})
        print(f"local summary written (remote unsupported) -> {store.status_md}")
        return 0

    # GitHub path.
    repo = ctx["repo_slug"]
    host = ctx["host"]
    owner, name = ctx["owner"], ctx["repo"]
    if not ctx["user"]:
        ctx["user"] = gh_logged_in_user(host)
    user = ctx["user"]
    if not user:
        print(
            f"Could not determine a GitHub login for {host}. Run "
            f"`gh auth login --hostname {host}` or pass --user <login>.",
            file=sys.stderr,
        )
        return 2

    with store.file_lock():
        try:
            notes = load_notes_strict(store)
        except CorruptDataError as e:
            print(
                f"Refusing to refresh: manual notes file is corrupt and was left "
                f"untouched ({_scrub(e)}). Fix or remove {store.notes_json}; the "
                "previous dashboard and snapshot are preserved.",
                file=sys.stderr,
            )
            return 3

        prev = store.load_json(store.snapshot_json, {})
        # If the identity changed (account switch / --user override), do not treat
        # the previous user's cached assignments as current: start a fresh diff.
        prev_user = prev.get("user")
        if prev_user and prev_user != user:
            prev = {}
        prev_open_prs = {p.get("number"): p for p in prev.get("open_prs", [])}
        first_snapshot = not prev  # don't flag everything "new" on the first run
        since = days_ago_iso(COMPLETED_WINDOW_DAYS)

        errors: list[str] = []
        truncations: list[str] = []
        data = {"generated_at": now_utc(), "stale": False, "errors": [],
                "user": user, "truncations": []}

        def _note_trunc(label, res):
            if res.get("truncated"):
                truncations.append(
                    f"{label}: showing {len(res['items'])} of {res['total']} "
                    "(GitHub search caps results; result is incomplete)."
                )
            return res["items"]

        try:
            oi = collect_assigned_open_issues(repo, host, user)
            op = collect_authored_open_prs(repo, host, user)
            ci = collect_recent_closed_issues(repo, host, user, since)
            cp = collect_recent_closed_prs(repo, host, user, since)
            open_issues = _note_trunc("assigned open issues", oi)
            open_pr_list = _note_trunc("authored open PRs", op)
            closed_issues = _note_trunc("recently closed issues", ci)
            closed_prs = _note_trunc("recently completed PRs", cp)
        except Exception as e:  # noqa: BLE001
            errors.append(f"list collection failed: {_scrub(e)}")
            open_issues = open_pr_list = closed_issues = closed_prs = None

        enriched_prs: list[dict] = []
        if open_pr_list is not None:
            for p in open_pr_list:
                num = p["number"]
                try:
                    prev_pr = None if first_snapshot else prev_open_prs.get(num)
                    pr = enrich_pr(num, repo, host, owner, name, prev_pr)
                    if first_snapshot:
                        pr["new_comments"], pr["new_reviews"] = [], []
                    if pr.get("activity_incomplete"):
                        errors.append(
                            f"PR #{num}: review threads/comments could not be "
                            "fully read -- thread counts and 'new since' activity "
                            "for this PR are incomplete."
                        )
                    enriched_prs.append(pr)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"PR #{num} enrich failed: {_scrub(e)}")
                    if num in prev_open_prs:
                        stale_pr = dict(prev_open_prs[num])
                        stale_pr["activity_incomplete"] = True
                        enriched_prs.append(stale_pr)

        try:
            tracked = collect_tracked(notes, repo, host, owner, name, prev.get("tracked", {}))
        except Exception as e:  # noqa: BLE001
            errors.append(f"tracked collection failed: {_scrub(e)}")
            tracked = prev.get("tracked", {"issues": [], "prs": []})

        if errors:
            # Degrade gracefully: keep prior good lists where this run failed, mark
            # stale, retain notes, and DO NOT overwrite the good snapshot below.
            data["stale"] = True
            data["errors"] = errors
            data["open_issues"] = open_issues if open_issues is not None else prev.get("open_issues", [])
            data["open_prs"] = enriched_prs if enriched_prs else prev.get("open_prs", [])
            data["closed_issues"] = closed_issues if closed_issues is not None else prev.get("closed_issues", [])
            data["closed_prs"] = closed_prs if closed_prs is not None else prev.get("closed_prs", [])
            data["tracked"] = tracked
            data["last_good_at"] = prev.get("generated_at")
        else:
            data["open_issues"] = open_issues
            data["open_prs"] = enriched_prs
            data["closed_issues"] = closed_issues
            data["closed_prs"] = closed_prs
            data["tracked"] = tracked

        data["truncations"] = truncations
        data["recommendations"] = build_recommendations(data, notes)

        md = render_status(ctx, data, notes)
        store.atomic_write(store.status_md, md)
        store.atomic_write(store.meta_json, json.dumps({
            "host": host, "owner": owner, "repo": name, "user": user,
            "repo_slug": repo, "updated": now_utc(),
        }, indent=2))
        # Only overwrite the machine snapshot with a full, non-stale result so a
        # future "new since last snapshot" diff compares against real good data.
        # A stale run keeps the last good snapshot untouched.
        if not data["stale"]:
            store.atomic_write(store.snapshot_json, json.dumps(data, indent=2))
        store.append_worklog({"event": "refresh", "stale": data["stale"],
                              "open_prs": len(data.get("open_prs", [])),
                              "open_issues": len(data.get("open_issues", [])),
                              "errors": len(errors), "truncations": len(truncations)})

    status = "STALE" if data["stale"] else "ok"
    print(
        f"refresh {status}: {len(data.get('open_prs', []))} open PRs, "
        f"{len(data.get('open_issues', []))} open issues, "
        f"{len(data.get('closed_prs', []))} recently completed PRs "
        f"({repo} @ {host}, user {user}) -> {store.status_md}"
    )
    for e in errors:
        print("  ! " + e, file=sys.stderr)
    return 1 if data["stale"] else 0


def cmd_note(args) -> int:
    ctx = resolve_context(args)
    if ctx["kind"] == "none":
        print("Not a Git repository (and no --repo given); nowhere to attach notes.",
              file=sys.stderr)
        return 2
    store = Store(ctx["state_dir"])
    with store.file_lock():
        try:
            notes = load_notes_strict(store)
        except CorruptDataError as e:
            print(
                f"Refusing to edit notes: the existing notes file is corrupt and "
                f"was left untouched ({_scrub(e)}). Fix or remove "
                f"{store.notes_json} before adding notes.",
                file=sys.stderr,
            )
            return 3
        changed = []
        if args.handover is not None:
            if args.append and notes.get("handover"):
                notes["handover"] = notes["handover"].rstrip() + "\n" + args.handover
            else:
                notes["handover"] = args.handover
            changed.append("handover")
        if args.item:
            cur = notes.setdefault("items", {}).get(args.item, {})
            if args.text is not None:
                cur["note"] = args.text
            if args.status is not None:
                cur["status"] = args.status
            cur["updated"] = now_utc()
            notes["items"][args.item] = cur
            changed.append(args.item)
        if args.track_issue:
            lst = notes.setdefault("tracked", {}).setdefault("issues", [])
            for n in args.track_issue:
                if n not in lst:
                    lst.append(n)
            changed.append("tracked.issues")
        if args.track_pr:
            lst = notes.setdefault("tracked", {}).setdefault("prs", [])
            for n in args.track_pr:
                if n not in lst:
                    lst.append(n)
            changed.append("tracked.prs")
        if args.map:
            iss, prs = args.map[0], args.map[1:]
            notes.setdefault("mapping", {}).setdefault(iss, [])
            for p in prs:
                if p not in notes["mapping"][iss]:
                    notes["mapping"][iss].append(p)
            changed.append(f"mapping[{iss}]")
        if args.exclude_rec:
            lst = notes.setdefault("exclude_recommendations", [])
            for k in args.exclude_rec:
                if k not in lst:
                    lst.append(k)
            changed.append("exclude_recommendations")
        if not changed:
            print("nothing to change; see `tfc note --help`", file=sys.stderr)
            return 2
        store.atomic_write(store.notes_json, json.dumps(notes, indent=2))
        store.append_worklog({"event": "note", "changed": changed})
    print(f"notes updated: {', '.join(changed)} -> {store.notes_json}")
    print("Run `tfc refresh` to regenerate STATUS.md with these notes.")
    return 0


def cmd_show(args) -> int:
    ctx = resolve_context(args)
    store = Store(ctx["state_dir"])
    if not os.path.exists(store.status_md):
        print("No STATUS.md yet -- run `tfc refresh` first.", file=sys.stderr)
        return 1
    with open(store.status_md, "r", encoding="utf-8") as f:
        sys.stdout.write(f.read())
    return 0


def cmd_status(args) -> int:
    """Convenience: refresh, then print the dashboard. Returns show's status if
    refresh only went stale (dashboard still printed), else refresh's code."""
    rc = cmd_refresh(args)
    if rc == 2:
        return rc  # hard failure (not a repo / no login) -- nothing to show
    show_rc = cmd_show(args)
    # A stale refresh (rc==1) still produced a dashboard; surface staleness.
    return rc or show_rc


def cmd_paths(args) -> int:
    ctx = resolve_context(args)
    store = Store(ctx["state_dir"])
    print(json.dumps({
        "repo_slug": ctx["repo_slug"],
        "host": ctx["host"],
        "user": ctx["user"],
        "kind": ctx["kind"],
        "state_dir": ctx["state_dir"],
        "status_md": store.status_md,
        "notes_json": store.notes_json,
        "snapshot_json": store.snapshot_json,
        "worklog": store.worklog,
    }, indent=2))
    return 0


def _add_global_opts(parser) -> None:
    parser.add_argument("--cwd", help="Repository path (default: current directory)")
    parser.add_argument("--remote", default="origin",
                        help="Git remote to read (default: origin)")
    parser.add_argument("--host", help="Override detected GitHub host (e.g. github.example.com)")
    parser.add_argument("--repo", help="Override detected repo as owner/name")
    parser.add_argument("--user",
                        help="Override the GitHub login (default: gh's signed-in account)")


def build_parser() -> argparse.ArgumentParser:
    # Global options live on a shared parent so they are accepted BOTH before and
    # after the subcommand (e.g. `tfc --repo a/b status` and `tfc status --repo a/b`).
    globals_parent = argparse.ArgumentParser(add_help=False)
    _add_global_opts(globals_parent)

    p = argparse.ArgumentParser(
        prog="tfc",
        parents=[globals_parent],
        description="Project status dashboard for a GitHub-backed Git repo "
                    "(github.com or GHES). Read-only against GitHub.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("status", parents=[globals_parent],
                        help="Refresh from GitHub and print the dashboard (refresh + show)")
    st.set_defaults(func=cmd_status)

    sr = sub.add_parser("refresh", parents=[globals_parent],
                        help="Read GitHub live and regenerate STATUS.md")
    sr.set_defaults(func=cmd_refresh)

    sn = sub.add_parser("note", parents=[globals_parent],
                        help="Edit manual work notes (preserved across refreshes)")
    sn.add_argument("--handover", help="Set (or with --append, append to) handover notes")
    sn.add_argument("--append", action="store_true", help="Append to handover instead of replacing")
    sn.add_argument("--item", help="Item key, e.g. pr123 or issue45")
    sn.add_argument("--text", help="Note text for --item (your measured evidence/decision)")
    sn.add_argument("--status", help="Short status for --item, e.g. 'tests-pass, do-not-merge'")
    sn.add_argument("--track-issue", type=int, nargs="+", help="Add issue number(s) to live tracking")
    sn.add_argument("--track-pr", type=int, nargs="+", help="Add PR number(s) to live tracking")
    sn.add_argument("--map", nargs="+", metavar=("ISSUE", "PR"),
                    help="Manually map an issue to related PR(s): --map 45 123")
    sn.add_argument("--exclude-rec", nargs="+",
                    help="Suppress next-action suggestions for keys, e.g. issue45 (paused work)")
    sn.set_defaults(func=cmd_note)

    ss = sub.add_parser("show", parents=[globals_parent], help="Print the current STATUS.md")
    ss.set_defaults(func=cmd_show)

    pp = sub.add_parser("paths", parents=[globals_parent],
                        help="Print resolved project context and file paths")
    pp.set_defaults(func=cmd_paths)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
