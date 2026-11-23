"""PR keys, checkout names, paths, settings and reviews, shared by the review sweep's scripts.

The sweep's settings and its record of finished reviews live in the dashboard: these
scripts read ``GET /api/review-sweep`` and ``GET /api/agent-reviews``, and record with
``POST /api/agent-reviews``, at ``$BABYSIT_DASHBOARD_URL`` or http://127.0.0.1:8765 (reachable
from inside Safehouse). A script that cannot reach the dashboard stops and says so.

A PR key names one pull request. With the settings' ``owner`` galaxyproject and
``default_repo`` galaxy:

    24191                 galaxyproject/galaxy#24191 (a bare number is the default repository)
    planemo#1612          galaxyproject/planemo#1612 (the owner's repositories)
    someone/thing#7       any other repository, with its owner

Every repository is cloned at ~/src/<repo> and its review checkouts live under
~/src/worktrees/<repo>/. The checkout directory, its branch and its herdr workspace share
one name: `review-pr-<N>` for the default repository, `review-<repo>-pr-<N>` for the owner's
other repositories and `review-<owner>-<repo>-pr-<N>` for everything else.

Standard library only: run with plain `python3`.
"""

import functools
import glob
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

DASHBOARD = (os.environ.get("BABYSIT_DASHBOARD_URL") or "http://127.0.0.1:8765").rstrip("/")
TAG = "review"  # the dashboard focus tag every review checkout gets
PART = r"[A-Za-z0-9][A-Za-z0-9_.-]*"  # an owner, or a repository in a short key
REPO = r"(?!-|\.{1,2}(?:[/#]|$))[A-Za-z0-9_.-]+"  # a repository may start with a dot (`.github`)
KEY = re.compile(rf"(?:({PART})/({REPO})#|({PART})#)?([1-9][0-9]*)")


class DashboardError(SystemExit):
    pass


def api(path, body=None):
    """One JSON request to the dashboard; a refusal or no answer stops the script."""
    headers, data = {}, None
    if body is not None:
        data = json.dumps(body).encode()
        # The dashboard's own guard against browsers: a custom header and no Origin.
        headers = {
            "Content-Type": "application/json",
            "X-Babysit-Action": path.split("?")[0].rsplit("/", 1)[1],
        }
    request = urllib.request.Request(DASHBOARD + path, data=data, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get("error")
        except (ValueError, AttributeError):
            detail = None
        raise DashboardError(
            f"the dashboard refused {path}: {detail or f'HTTP {exc.code}'}"
        ) from None
    except (OSError, ValueError) as exc:
        raise DashboardError(
            f"cannot reach the dashboard at {DASHBOARD} ({getattr(exc, 'reason', exc)})"
        ) from None


@functools.cache
def settings():
    """The sweep's settings (references/review-sweep.md); REVIEW_SWEEP_OUT overrides the
    report folder, for trying the scripts out."""
    found = api("/api/review-sweep")
    if os.environ.get("REVIEW_SWEEP_OUT"):
        found["out"] = os.environ["REVIEW_SWEEP_OUT"]
    return found


def owner():
    return settings().get("owner") or ""


def default_repo():
    return settings().get("default_repo") or ""


def out():
    """The folder reports and indexes go to."""
    return os.path.expanduser(settings()["out"])


def launched_file():
    # Every head the scheduler made a checkout at (pr key, head sha, date): what tells a
    # checkout whose review never finished from one the user committed in.
    return os.path.join(out(), "launched.tsv")


class PR(NamedTuple):
    owner: str
    repo: str
    n: int

    @property
    def home(self):
        return self.owner == owner()

    @property
    def key(self):
        if not self.home:
            return f"{self.owner}/{self.repo}#{self.n}"
        return str(self.n) if self.repo == default_repo() else f"{self.repo}#{self.n}"

    @property
    def slug(self):
        return f"{self.owner}/{self.repo}"

    @property
    def url(self):
        return f"https://github.com/{self.slug}/pull/{self.n}"

    @property
    def stem(self):
        """What tells this PR's files apart: `pr`, `planemo-pr` or `someone-thing-pr`."""
        if not self.home:
            return f"{self.owner}-{self.repo}-pr"
        return "pr" if self.repo == default_repo() else f"{self.repo}-pr"

    @property
    def name(self):
        """Checkout directory, branch and herdr workspace label."""
        return f"review-{self.stem}-{self.n}"

    @property
    def clone(self):
        return os.path.expanduser(f"~/src/{self.repo}")

    @property
    def worktrees(self):
        return os.path.expanduser(f"~/src/worktrees/{self.repo}")

    @property
    def checkout(self):
        return f"{self.worktrees}/{self.name}"

    def report_file(self, label):
        """`<label>-pr24191.md`, `<label>-planemo-pr1612.md`, `<label>-someone-thing-pr7.md`."""
        return f"{label}-{self.stem}{self.n}.md"


def parse(key):
    """`24191`, `planemo#12` or `someone/thing#7` -> PR."""
    m = KEY.fullmatch(str(key).strip())
    if not m:
        raise ValueError(f"not a PR key: {key!r} (expected <N>, <repo>#<N> or <owner>/<repo>#<N>)")
    who, repo, short, n = m.groups()
    who = who or owner()
    repo = repo or short or default_repo()
    if not who or not repo:
        raise ValueError(f"{key!r} needs an owner and repository: the settings name no default")
    return PR(who, repo, int(n))


def from_url(url):
    m = re.fullmatch(rf"https://github\.com/({PART})/({REPO})/pull/([1-9][0-9]*)/?", url.strip())
    if not m:
        raise ValueError(f"not a pull request URL: {url!r}")
    return PR(m.group(1), m.group(2), int(m.group(3)))


def entry(p):
    """The PR of a prs.json entry."""
    return PR(p["owner"], p["repo"], p["number"])


def from_checkout(path):
    """The PR a review checkout belongs to, from its place and name, or None."""
    repo = os.path.basename(os.path.dirname(os.path.abspath(path)))
    m = re.fullmatch(
        rf"review-(?:({PART})-)?{re.escape(repo)}-pr-([1-9][0-9]*)", os.path.basename(path)
    )
    if m and (m.group(1) or owner()):
        return PR(m.group(1) or owner(), repo, int(m.group(2)))
    m = re.fullmatch(r"review-pr-([1-9][0-9]*)", os.path.basename(path))
    if m and repo == default_repo() and owner():
        return PR(owner(), repo, int(m.group(1)))
    return None


def review_checkouts():
    """Every review checkout on disk, in every repository, as (PR, path)."""
    found = []
    for path in sorted(glob.glob(os.path.expanduser("~/src/worktrees/*/review-*"))):
        pr = from_checkout(path)
        if pr and os.path.isdir(path):
            found.append((pr, path))
    return found


def sh(*cmd, cwd=None, stdin=None):
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, input=stdin)


def gh(*args, tries=4):
    """Output of a gh command. GitHub's 5xx answers and rate-limit hiccups are retried."""
    for attempt in range(tries):
        r = sh("gh", *args)
        if not r.returncode:
            return r.stdout
        if not re.search(
            r"HTTP 5\d\d|timeout|connection reset|secondary rate limit|EOF", r.stderr, re.I
        ):
            break
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"gh {' '.join(args)}: {r.stderr.strip()[:400]}")


def base_remote(pr):
    """The remote of the main clone that is the PR's own repository, or None.

    Clones differ: a clone's `origin` may be a fork with `upstream` the PR's repository, or
    have only `origin`. So the remote is found by URL, preferring `upstream`, then `origin`.
    """
    wanted = re.compile(rf"github\.com[:/]{re.escape(pr.slug)}(?:\.git)?/?$", re.I)
    names = []
    for line in sh("git", "-C", pr.clone, "remote", "-v").stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == "(fetch)" and wanted.search(parts[1]):
            names.append(parts[0])
    for preferred in ("upstream", "origin"):
        if preferred in names:
            return preferred
    return sorted(names)[0] if names else None


def writable(directory):
    """Whether this process may create files there. A sandbox denies it whatever the mode bits say."""
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".review-probe-"):
            return True
    except OSError:
        return False


def clone_problem(pr):
    """Why no checkout can be made for this PR here, or "" when one can."""
    git_dir = os.path.join(pr.clone, ".git")
    if not os.path.isdir(git_dir):
        return f"no clone at ~/src/{pr.repo}"
    if not writable(git_dir):
        return f"~/src/{pr.repo} is read-only in this sandbox"
    root = pr.worktrees if os.path.isdir(pr.worktrees) else os.path.dirname(pr.worktrees)
    if not writable(root):
        return f"{root} is read-only in this sandbox"
    return ""


@functools.cache
def reviewed_heads():
    """{`owner/repo#number` (lowercase): [heads the sweep reviewed]}, from the dashboard."""
    heads = api("/api/agent-reviews?heads=1")["heads"]
    return {key.lower(): value for key, value in heads.items()}


def reviewed(pr, sha):
    """Whether the sweep reviewed this head. Heads have mixed lengths: match by prefix."""
    return any(
        h and sha.startswith(h) for h in reviewed_heads().get(f"{pr.slug}#{pr.n}".lower(), [])
    )


def last_review(pr):
    """The sweep's newest review of this PR (head, date, report, session, …), or None."""
    query = urllib.parse.urlencode({"url": pr.url})
    found = [r for r in api(f"/api/agent-reviews?{query}")["reviews"] if r["source"] == "sweep"]
    return found[0] if found else None


def record(pr, head, base, label, report, session, checkout):
    """Record one finished review; the dashboard reads its verdict and findings from the report."""
    return api(
        "/api/agent-reviews",
        {
            "source": "sweep",
            "repo": pr.slug,
            "number": pr.n,
            "head": head or None,
            "base": base or None,
            "date": label[:10],
            "label": label,
            "report": report,
            "session": session or None,
            "checkout": checkout or None,
        },
    )["review"]


def record_launch(pr, head):
    with open(launched_file(), "a") as f:
        f.write(f"{pr.key}\t{head}\t{time.strftime('%Y-%m-%d')}\n")


def launched(pr, sha):
    """Whether the scheduler once made this PR's checkout at this head."""
    if not os.path.exists(launched_file()):
        return False
    rows = (line.rstrip("\n").split("\t") for line in open(launched_file()))
    return any(len(r) > 1 and r[0] == pr.key and r[1] and sha.startswith(r[1]) for r in rows)


def herdr_workspaces():
    """Open herdr workspaces by label, or None when herdr does not answer."""
    try:
        data = json.loads(sh("herdr", "workspace", "list").stdout)
        return {w["label"]: w for w in data["result"]["workspaces"]}
    except Exception:
        return None


def held(pr, workspaces, heads=()):
    """Why an existing checkout must not be replaced or removed, or "" when it may be.

    A checkout is the user's once they work in it: an open workspace (a conversation that may
    still be going), uncommitted changes, or a HEAD that is neither a head the sweep reviewed
    or launched nor one of `heads` (the PR's current head), which means local commits.
    `workspaces` is herdr_workspaces(); when herdr did not answer (None), an open workspace
    cannot be ruled out, so the checkout is held.
    """
    path = pr.checkout
    if not os.path.lexists(path):
        return ""
    if workspaces is None:
        return "herdr did not answer, so an open workspace cannot be ruled out"
    w = workspaces.get(pr.name)
    if w:
        return f"workspace open (agent {w.get('agent_status') or 'unknown'})"
    head = sh("git", "-C", path, "rev-parse", "HEAD").stdout.strip()
    if not head:
        return ""  # not a working checkout: nothing in it to lose
    status = sh("git", "-C", path, "status", "--short")
    if status.returncode:
        return "git status failed there, so uncommitted changes cannot be ruled out"
    if status.stdout.strip():
        return "uncommitted changes"
    known = reviewed(pr, head) or launched(pr, head) or any(h and head.startswith(h) for h in heads)
    if not known:
        return f"local commits (HEAD {head[:11]} was never a reviewed head)"
    return ""


def session_file(pr, session=""):
    """(session id, Claude config home) of a reviewer session recorded for this PR's checkout.

    With no id, the newest session there. The home is where its transcript is; resuming it needs
    CLAUDE_CONFIG_DIR to name that home (unset for ~/.claude).
    """
    encoded = re.sub(r"[^A-Za-z0-9]", "-", pr.checkout)
    homes = [os.path.expanduser("~/.claude")] + sorted(
        glob.glob(os.path.expanduser("~/.claude/accounts/*"))
    )
    files = []
    for home in homes:
        for f in glob.glob(f"{home}/projects/{encoded}/{session or '*'}.jsonl"):
            files.append((os.path.getmtime(f), f, home))
    if not files:
        return "", ""
    _, f, home = max(files)
    return os.path.splitext(os.path.basename(f))[0], home
