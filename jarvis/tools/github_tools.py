"""GitHub by voice: notifications, issues and pull requests, build (GitHub Actions) status, why a build
failed, and a spoken alert when a watched repo's build fails.

Uses the GitHub CLI (`gh`) and its existing login, so no token is ever stored by Jarvis:
  1. install it: winget install GitHub.cli   (setup.ps1 offers this)
  2. sign in once: gh auth login
The tools only appear when `gh` is installed. Reading is free; creating an issue, commenting or re-running a
build asks "Shall I…?" first. Merging, deleting, force-pushing or changing settings is not possible by voice.

config.yaml / config.local.yaml:
  github:
    default_repo: owner/name        # what "my repo" / "the build" means when no repo is named
    watch: [owner/name, ...]        # repos whose failed builds Jarvis announces
"""
from __future__ import annotations

import calendar
import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time

from .registry import Spoken, tool

log = logging.getLogger(__name__)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
TRIGGERS = ("github", "git hub", "repo", "repository", "build", "ci", "workflow", "action", "pipeline",
            "pull request", "prs?\\b", "issue", "commit", "notification", "checks", "runs?\\b")


class GitHubError(Exception):
    pass


def gh_path() -> str | None:
    return shutil.which("gh")


def github_enabled(cfg) -> bool:
    return (cfg.get("github") or {}).get("enabled", True) and gh_path() is not None


def gh(*args: str, timeout: int = 20) -> str:
    """Run the GitHub CLI and return stdout; raise GitHubError with a sayable message on failure."""
    exe = gh_path()
    if not exe:
        raise GitHubError("The GitHub CLI isn't installed. Install it with: winget install GitHub.cli")
    try:
        out = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=timeout, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired as e:
        raise GitHubError("GitHub didn't answer in time.") from e
    if out.returncode != 0:
        err = (out.stderr or out.stdout).strip()
        if "auth login" in err or "not logged" in err.lower():
            raise GitHubError("GitHub isn't signed in on this PC. Run: gh auth login")
        if "Could not resolve to a Repository" in err or "HTTP 404" in err:
            raise GitHubError("I couldn't find that repository, or this GitHub account can't see it.")
        raise GitHubError(err.splitlines()[-1][:200] if err else "GitHub returned an error.")
    return out.stdout


def gh_json(*args: str, timeout: int = 20):
    text = gh(*args, timeout=timeout)
    return json.loads(text) if text.strip() else []


# ------------------------------------------------------------------ which repo?

_login: str | None = None
_repo_cache: tuple[float, list[str]] = (0.0, [])


def _me() -> str:
    global _login
    if _login is None:
        _login = gh("api", "user", "--jq", ".login").strip()
    return _login


def _my_repos() -> list[str]:
    global _repo_cache
    if time.time() - _repo_cache[0] < 600 and _repo_cache[1]:
        return _repo_cache[1]
    try:
        names = gh_json("api", "user/repos?per_page=100&sort=pushed", "--jq", "[.[].full_name]")
    except (GitHubError, ValueError, KeyError):
        names = []
    _repo_cache = (time.time(), names)
    return names


def _spoken(s: str) -> str:
    return re.sub(r"[\s_.-]+", "", s.lower())


def resolve_repo(cfg, repo: str = "") -> str:
    """'jarvis', 'my jarvis repo', 'owner/name' or nothing -> 'owner/name'."""
    g = cfg.get("github") or {}
    repo = re.sub(r"\b(my|the|repo|repository|project|on github|github)\b", " ", (repo or "").lower()).strip()
    if not repo:
        if g.get("default_repo"):
            return g["default_repo"]
        watch = g.get("watch") or []
        if watch:
            return watch[0]
        raise GitHubError("Which repository? You can set a default under github: default_repo in your settings.")
    if re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        return repo
    known = list(dict.fromkeys([*(g.get("watch") or []), *([g["default_repo"]] if g.get("default_repo") else []),
                                *_my_repos()]))
    by_name = {_spoken(k.split("/", 1)[1]): k for k in known}
    key = _spoken(repo)
    if key in by_name:
        return by_name[key]
    close = difflib.get_close_matches(key, list(by_name), n=1, cutoff=0.6)
    if close:
        return by_name[close[0]]
    try:
        return f"{_me()}/{repo.replace(' ', '-')}"
    except GitHubError:
        raise GitHubError(f"I don't know a repository called {repo}.") from None


def _ago(iso: str) -> str:
    try:
        t = calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return ""
    mins = max(0, int((time.time() - t) / 60))
    if mins < 2:
        return "just now"
    if mins < 60:
        return f"{mins} minutes ago"
    hours = mins // 60
    if hours < 48:
        return f"{hours} hour{'s' * (hours != 1)} ago"
    return f"{hours // 24} days ago"


def _run_state(r: dict) -> str:
    if r.get("status") != "completed":
        return "still running" if r.get("status") == "in_progress" else (r.get("status") or "queued")
    return {"success": "passed", "failure": "failed", "cancelled": "was cancelled",
            "timed_out": "timed out", "skipped": "was skipped"}.get(r.get("conclusion"), r.get("conclusion") or "?")


RUN_FIELDS = "databaseId,displayTitle,workflowName,headBranch,status,conclusion,createdAt,event,url"


# ------------------------------------------------------------------ read-only tools

@tool("Check GitHub notifications (unread mentions, review requests, failed builds, new issues).",
      enabled=github_enabled, triggers=TRIGGERS)
def github_notifications():
    rows = gh_json("api", "notifications", "--jq",
                   "[.[] | {repo: .repository.full_name, title: .subject.title, type: .subject.type, "
                   "reason: .reason}]")
    if not rows:
        return Spoken("No new GitHub notifications.")
    lines = [f"{len(rows)} unread GitHub notification{'s' * (len(rows) != 1)}:"]
    for n in rows[:8]:
        kind = re.sub(r"(?<!^)(?=[A-Z])", " ", n["type"]).lower()        # PullRequest -> pull request
        lines.append(f"- {n['repo'].split('/', 1)[1]}: {kind} '{n['title']}', {n['reason'].replace('_', ' ')}.")
    return "\n".join(lines)


@tool("GitHub Actions builds of a repository: did the last build pass, what's running now.",
      {"repo": {"type": "string", "description": "Repo name like 'jarvis' or 'owner/name'. Empty = default."},
       "count": {"type": "integer", "description": "How many recent runs (default 3)"}},
      required=[], enabled=github_enabled, triggers=TRIGGERS)
def github_builds(repo: str = "", count: int = 3, ctx=None):
    name = resolve_repo(ctx.cfg, repo)
    runs = gh_json("run", "list", "-R", name, "--limit", str(max(1, min(count, 10))), "--json", RUN_FIELDS)
    if not runs:
        return Spoken(f"{name} has no GitHub Actions runs yet.")
    if count == 1:                    # "did my last build pass?": one sentence, said as-is
        r = runs[0]
        tail = " Ask me why it failed if you want details." if r.get("conclusion") in ("failure", "timed_out") else ""
        return Spoken(f"The last {r['workflowName']} build on {name.split('/', 1)[1]}, for '{r['displayTitle']}', "
                      f"{_run_state(r)} {_ago(r['createdAt'])}.{tail}")
    lines = [f"Builds on {name}:"]
    for r in runs:
        lines.append(f"- {r['workflowName']} on {r['headBranch']} for '{r['displayTitle']}' {_run_state(r)}, "
                     f"{_ago(r['createdAt'])} (run {r['databaseId']})")
    return "\n".join(lines)


ERROR_LINE = re.compile(r"(error|fail|exception|traceback|assert|denied|not found|cannot|could not|"
                        r"exit code [1-9]|##\[error\])", re.I)


def _failure_excerpt(log_text: str, limit: int = 60) -> str:
    """The lines around errors at the end of a failed job's log, without timestamps: enough for the brain to
    explain the failure in a sentence without reading thousands of lines."""
    lines = []
    for raw in log_text.splitlines():
        parts = raw.split("\t", 2)          # gh --log-failed: "job<TAB>step<TAB>2026-...Z message"
        msg = parts[-1]
        msg = re.sub(r"^\ufeff?\d{4}-\d\d-\d\dT[\d:.]+Z ?", "", msg)
        step = parts[1] if len(parts) == 3 else ""
        lines.append((step, msg.rstrip()))
    keep: set[int] = set()
    for i, (_, msg) in enumerate(lines):
        if ERROR_LINE.search(msg):
            keep.update(range(max(0, i - 3), min(len(lines), i + 4)))
    picked = [lines[i] for i in sorted(keep)] or lines[-limit:]
    picked = picked[-limit:]
    out, last_step = [], None
    for step, msg in picked:
        if step and step != last_step:
            out.append(f"[step: {step}]")
            last_step = step
        if msg.strip():
            out.append(msg[:300])
    return "\n".join(out)


def _failure_from_jobs(name: str, run_id: int) -> str:
    jobs = gh_json("api", f"repos/{name}/actions/runs/{run_id}/jobs", "--jq",
                   "[.jobs[] | select(.conclusion == \"failure\" or .conclusion == \"timed_out\") | "
                   "{id, name, steps: [.steps[] | select(.conclusion == \"failure\") | .name]}]")
    out = []
    for j in jobs[:3]:
        out.append(f"[job: {j['name']}; failed step: {', '.join(j['steps']) or '?'}]")
        try:
            notes = gh_json("api", f"repos/{name}/check-runs/{j['id']}/annotations", "--jq",
                            "[.[] | select(.annotation_level == \"failure\") | .message]")
        except (GitHubError, ValueError):
            notes = []
        out += [n[:400] for n in notes[:5]]
    return "\n".join(out)


@tool("Explain why a GitHub Actions build failed: reads the failed job's log. Summarize the cause for the "
      "user in one or two plain sentences (which test or step, and the error), not the whole log.",
      {"repo": {"type": "string", "description": "Repo name like 'jarvis'. Empty = default."},
       "run_id": {"type": "integer", "description": "A specific run; omit for the latest failed one"}},
      required=[], enabled=github_enabled, triggers=TRIGGERS)
def github_build_failure(repo: str = "", run_id: int | None = None, ctx=None):
    name = resolve_repo(ctx.cfg, repo)
    if not run_id:
        runs = gh_json("run", "list", "-R", name, "--limit", "20", "--json", RUN_FIELDS)
        failed = [r for r in runs if r.get("conclusion") in ("failure", "timed_out")]
        if not failed:
            return Spoken(f"None of the recent builds on {name} failed.")
        run = failed[0]
        run_id = run["databaseId"]
        newer_ok = any(r.get("conclusion") == "success" for r in runs[:runs.index(run)])
    else:
        run = gh_json("run", "view", str(run_id), "-R", name, "--json", RUN_FIELDS)
        newer_ok = False
    try:
        excerpt = _failure_excerpt(gh("run", "view", str(run_id), "-R", name, "--log-failed", timeout=60))
    except GitHubError as e:          # logs expired or blocked: fall back to failed steps + error annotations
        log.info("failed log unavailable (%s); using job annotations", e)
        excerpt = _failure_from_jobs(name, run_id)
    head = (f"Run {run_id} of {run['workflowName']} on {name} ({run['headBranch']}, '{run['displayTitle']}') "
            f"{_run_state(run)} {_ago(run['createdAt'])}.")
    if newer_ok:
        head += " A newer build has passed since."
    return head + "\nFailed log excerpt:\n" + (excerpt or "(no log output)")


@tool("List open issues or pull requests on a GitHub repository.",
      {"repo": {"type": "string", "description": "Repo name like 'jarvis'. Empty = default."},
       "kind": {"type": "string", "enum": ["issues", "pull_requests", "both"]},
       "count": {"type": "integer", "description": "How many (default 5)"}},
      required=[], enabled=github_enabled, triggers=TRIGGERS)
def github_open_items(repo: str = "", kind: str = "both", count: int = 5, ctx=None):
    name = resolve_repo(ctx.cfg, repo)
    n = str(max(1, min(count, 20)))
    out = []
    if kind in ("issues", "both"):
        rows = gh_json("api", f"repos/{name}/issues?state=open&per_page=50", "--jq",
                       "[.[] | select(.pull_request == null) | {number, title, createdAt: .created_at}]")[:int(n)]
        out.append(f"{len(rows) or 'No'} open issue{'s' * (len(rows) != 1)} on {name}" + (":" if rows else "."))
        out += [f"- #{r['number']} {r['title']} ({_ago(r['createdAt'])})" for r in rows]
    if kind in ("pull_requests", "both"):
        rows = gh_json("api", f"repos/{name}/pulls?state=open&per_page={n}", "--jq",
                       "[.[] | {number, title, isDraft: .draft}]")
        out.append(f"{len(rows) or 'No'} open pull request{'s' * (len(rows) != 1)} on {name}" +
                   (":" if rows else "."))
        out += [f"- #{r['number']} {r['title']}{' (draft)' if r.get('isDraft') else ''}" for r in rows]
    return "\n".join(out)


@tool("Recent commits on a GitHub repository's default branch.",
      {"repo": {"type": "string", "description": "Repo name like 'jarvis'. Empty = default."},
       "count": {"type": "integer", "description": "How many (default 5)"}},
      required=[], enabled=github_enabled, triggers=TRIGGERS)
def github_commits(repo: str = "", count: int = 5, ctx=None):
    name = resolve_repo(ctx.cfg, repo)
    rows = gh_json("api", f"repos/{name}/commits?per_page={max(1, min(count, 20))}", "--jq",
                   "[.[] | {sha: .sha[0:7], msg: (.commit.message | split(\"\\n\")[0]), "
                   "who: .commit.author.name, when: .commit.author.date}]")
    if not rows:
        return Spoken(f"{name} has no commits yet.")
    return f"Latest commits on {name}:\n" + "\n".join(
        f"- {r['sha']} {r['msg']} by {r['who']}, {_ago(r['when'])}" for r in rows)


@tool("Overview of a GitHub repository: stars, forks, open issues and pull requests, latest build.",
      {"repo": {"type": "string", "description": "Repo name like 'jarvis'. Empty = default."}},
      required=[], enabled=github_enabled, triggers=TRIGGERS)
def github_repo_overview(repo: str = "", ctx=None):
    name = resolve_repo(ctx.cfg, repo)
    info = gh_json("api", f"repos/{name}", "--jq",
                   "{nameWithOwner: .full_name, description, stargazerCount: .stargazers_count, "
                   "forkCount: .forks_count, visibility, pushedAt: .pushed_at, open: .open_issues_count}")
    prs = gh_json("api", f"repos/{name}/pulls?state=open&per_page=100", "--jq", "[.[].number]")
    issues = range(max(0, info["open"] - len(prs)))      # GitHub counts open PRs as issues too
    runs = gh_json("run", "list", "-R", name, "--limit", "1", "--json", RUN_FIELDS)
    build = f" Latest build {_run_state(runs[0])} {_ago(runs[0]['createdAt'])}." if runs else ""
    return (f"{info['nameWithOwner']} ({info['visibility'].lower()}): {(info.get('description') or 'no description').rstrip('.')}. "
            f"{info['stargazerCount']} stars, {info['forkCount']} forks, {len(issues)} open issues, "
            f"{len(prs)} open pull requests, last push {_ago(info['pushedAt'])}.{build}")


# ------------------------------------------------------------------ writes (always confirmed)

@tool("Create a GitHub issue from what the user dictates. Asks before creating.",
      {"title": {"type": "string"}, "body": {"type": "string", "description": "Details (optional)"},
       "repo": {"type": "string", "description": "Repo name like 'jarvis'. Empty = default."}},
      required=["title"], dangerous=True, enabled=github_enabled, triggers=("issue", "bug", "ticket"),
      confirm=lambda a: f"Shall I create an issue titled '{a.get('title', '')}'"
                        f"{' on ' + a['repo'] if a.get('repo') else ''}?")
def github_create_issue(title: str, body: str = "", repo: str = "", ctx=None):
    name = resolve_repo(ctx.cfg, repo)
    url = gh("issue", "create", "-R", name, "--title", title,
             "--body", (body or "") + "\n\n_Created by voice with Jarvis._").strip().splitlines()[-1]
    num = url.rsplit("/", 1)[-1]
    return Spoken(f"Created issue {num} on {name}.")


@tool("Comment on a GitHub issue or pull request. Asks before posting.",
      {"number": {"type": "integer"}, "text": {"type": "string"},
       "repo": {"type": "string", "description": "Repo name like 'jarvis'. Empty = default."}},
      required=["number", "text"], dangerous=True, enabled=github_enabled, triggers=("comment", "reply"),
      confirm=lambda a: f"Shall I comment on number {a.get('number')}: '{a.get('text', '')}'?")
def github_comment(number: int, text: str, repo: str = "", ctx=None):
    name = resolve_repo(ctx.cfg, repo)
    gh("issue", "comment", str(number), "-R", name, "--body", text)   # works for PRs too
    return Spoken(f"Commented on number {number}.")


@tool("Re-run the failed jobs of a GitHub Actions build. Asks first.",
      {"repo": {"type": "string", "description": "Repo name like 'jarvis'. Empty = default."},
       "run_id": {"type": "integer", "description": "Omit for the latest failed run"}},
      required=[], dangerous=True, enabled=github_enabled, triggers=("rerun", "re-run", "run again", "retry",
                                                                     "restart", "again"),
      confirm=lambda a: f"Shall I re-run the failed build{' ' + str(a['run_id']) if a.get('run_id') else ''}"
                        f"{' on ' + a['repo'] if a.get('repo') else ''}?")
def github_rerun_build(repo: str = "", run_id: int | None = None, ctx=None):
    name = resolve_repo(ctx.cfg, repo)
    if not run_id:
        runs = gh_json("run", "list", "-R", name, "--limit", "20", "--json", RUN_FIELDS)
        failed = [r for r in runs if r.get("conclusion") in ("failure", "timed_out", "cancelled")]
        if not failed:
            return Spoken(f"There's no failed build to re-run on {name}.")
        run_id = failed[0]["databaseId"]
    gh("run", "rerun", str(run_id), "-R", name, "--failed")
    return Spoken(f"Re-running the failed jobs of build {run_id}. I'll tell you if it fails again."
                  if (ctx.cfg.get("github") or {}).get("watch") else f"Re-running the failed jobs of build {run_id}.")


# ------------------------------------------------------------------ build-failure alerts

class BuildWatcher:
    """Polls the latest GitHub Actions runs of github.watch repos and announces each newly failed run once.
    Runs that had already failed when Jarvis started are not announced."""

    def __init__(self, cfg, notify, interval: int | None = None):
        g = cfg.get("github") or {}
        self.repos = list(g.get("watch") or [])
        self.interval = max(60, int(interval or g.get("poll_minutes", 3) * 60))
        self.notify = notify
        self.seen: dict[str, set[int]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check(self) -> list[str]:
        """One poll; returns the announcements made (for tests)."""
        said = []
        for repo in self.repos:
            try:
                runs = gh_json("run", "list", "-R", repo, "--limit", "10", "--json", RUN_FIELDS)
            except (GitHubError, ValueError) as e:
                log.debug("watch %s: %s", repo, e)
                continue
            failed = {r["databaseId"]: r for r in runs if r.get("conclusion") in ("failure", "timed_out")}
            if repo not in self.seen:          # first look: remember, don't announce old failures
                self.seen[repo] = set(failed)
                continue
            for rid, r in failed.items():
                if rid in self.seen[repo]:
                    continue
                self.seen[repo].add(rid)
                short = repo.split("/", 1)[1]
                msg = (f"Heads up: the {r['workflowName']} build on {short} failed, for '{r['displayTitle']}'. "
                       "Ask me why it failed if you want details.")
                said.append(msg)
                self.notify(msg)
        return said

    def _loop(self):
        while not self._stop.wait(self.interval if self.seen else 5):
            self.check()

    def start(self) -> "BuildWatcher":
        if self.repos and gh_path():
            self._thread = threading.Thread(target=self._loop, daemon=True, name="github-watch")
            self._thread.start()
            log.info("watching GitHub builds of %s every %ds", ", ".join(self.repos), self.interval)
        return self

    def stop(self):
        self._stop.set()


def start_watcher(cfg, notify) -> BuildWatcher | None:
    g = cfg.get("github") or {}
    if not g.get("enabled", True) or not g.get("alerts", True) or not g.get("watch"):
        return None
    return BuildWatcher(cfg, notify).start()


def status_line() -> str:
    """For --check: is gh installed and signed in?"""
    if not gh_path():
        return "not set up (optional): winget install GitHub.cli, then gh auth login"
    try:
        return f"signed in as {_me()}"
    except GitHubError as e:
        return str(e)
