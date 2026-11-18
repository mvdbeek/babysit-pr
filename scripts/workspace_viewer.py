"""Read-only diff and agent transcript views for a workspace checkout.

Requests name a herdr workspace (anything Collie can open) or a row of the workspace
inventory, never a path: the checkout comes from herdr or that row, the diff from Git in
it, and transcripts only from the agent session stores (Claude's per-directory project
folder, Codex's dated session folders) whose recorded working directory is that checkout.
Output is bounded, and nothing is written.

A checkout on a worker machine is read the same way through ``remote_probe``: the worker
answers the diff, its session listing and the new bytes of one transcript, and parsing
stays here.

What a transcript parses to is kept in ``transcript_store`` (the dashboard's
``transcript-archive.sqlite``), so a transcript is parsed once, then only as its file
grows, and its entries can be searched. It stays there once its file is gone, and is
then listed and shown from the store alone; ``Archiver`` reads every session into it in
the background, so one never opened here is kept as well.
"""

import atexit
import json
import lzma
import os
import re
import selectors
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Iterable
from pathlib import Path

import claude_accounts
import github_cli
import machines
import owned_process
import remote_probe
import safe_git
import transcript_store
from pr_workspaces import herdr

MAX_DIFF = remote_probe.MAX_DIFF
MAX_FILE_LINES = remote_probe.MAX_FILE_LINES
GIT_TIMEOUT = 30
WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,63}$")
SESSION = remote_probe.SESSION
MAX_SESSIONS = remote_probe.MAX_SESSIONS
PAGE = 200
MAX_TEXT = 20000
MAX_TOOL = 4000
CODEX_CONTEXT = remote_probe.CODEX_CONTEXT
# How a failed Codex tool call reads: the script runner's own verdict, or, in older
# formats without one, a non-zero exit code.
CODEX_VERDICT = re.compile(r"^Script (completed|failed|error)")
CODEX_EXIT = re.compile(r'"exit_code"\s*:\s*[1-9]|exited with code [1-9]')
# The argument a tool call is about, searched in this order.
TOOL_SUBJECT = ("command", "cmd", "file_path", "path", "pattern", "query", "url", "skill", "prompt")
# Codex's exec tool runs a script; the shell command it wraps is its first `cmd:` string.
CODEX_CMD = re.compile(r'\bcmd:\s*("(?:[^"\\\n]|\\.)*")')
SHELLS = {"bash", "sh", "zsh"}
MAX_BRIEF = 200
# Tools that ask the user to choose: Claude's dialog, and Codex's request for input.
QUESTION_TOOLS = re.compile(r"^(AskUserQuestion|request_user_input(_async)?)$")
MAX_QUESTIONS = 8
MAX_OPTIONS = 12
CHUNK = 4 * 1024 * 1024
# A worker's diff and session listing take one ssh call each, and each transcript read
# at most this many: CHUNK bytes per call, so a first read of 40 MB takes ten. A poll
# reads for at most REMOTE_BUDGET seconds, each call within what is left of it, and the
# next poll goes on from there; it starts no call with less than REMOTE_MIN_READ left.
REMOTE_TIMEOUT = 60
REMOTE_READS = 64
REMOTE_BUDGET = 60
REMOTE_MIN_READ = 5
# A transcript line longer than this is skipped, with a notice, rather than parsed. A
# worker's transcript is held to the cap its session listing reads at: parsing one line
# costs many times its size.
MAX_LINE = 64 * 1024 * 1024
REMOTE_MAX_LINE = remote_probe.MAX_SCAN_LINE
# Entries a parsed transcript keeps; past it the oldest go, a tenth at a time, and the
# page indices stay those of the whole transcript. The store is the record of a session
# whose file is gone, so this only bounds a file that never stops growing.
MAX_ENTRIES = 200000
# The newest tool calls a parser keeps for their results (a later result replaces an
# earlier one), and the newest prompts it remembers to recognise Codex's echoes of them.
OPEN_TOOLS = 500
MAX_PROMPTS = 100
# Bumped whenever parsing changes what an entry holds: a saved transcript is parsed again
# when it is next opened or has grown, and stays as it was parsed once its file is gone.
PARSER = 2
# Sessions a transcript lists at most, those kept only in the store included.
KEPT_SESSIONS = 200
# The archiver's first sweep after the dashboard starts, how often it sweeps after that,
# and its rest after each transcript it read, so requests are answered meanwhile.
ARCHIVE_FIRST = 60
ARCHIVE_EVERY = 3600
ARCHIVE_PAUSE = 0.05
ARCHIVE_FAILURES = 3
# A session file larger than this is not kept as it is: only its transcript is.
RAW_MAX = 1024 * 1024 * 1024
# A search brings the checkout's sessions up to date first, for at most this long, and
# answers at most SEARCH_RESULTS entries.
SEARCH_BUDGET = 20
SEARCH_RESULTS = 100
# An open viewer polls every few seconds: session details are kept per file version, in
# memory and in the store. One lock guards the in-memory caches, since requests arrive on
# separate threads.
_lock = threading.Lock()
_details: dict[str, tuple[tuple, dict | None]] = {}
DETAIL_FILES = 20000
# The store, opened on first use: the dashboard's (``use_store``), or a private one.
_store_path: Path | None = None
_store: transcript_store.Store | None = None
# One reader at a time per transcript, keyed (machine id, file); this machine's id is "".
_reading: dict[tuple[str, str], threading.Lock] = {}
# Keyed by (machine id, workspace ID); this machine's id is None.
_checkouts: dict[tuple[str | None, str], tuple[float, str, frozenset[str]]] = {}
CHECKOUT_TTL = 30
# A worker's session listing per (machine id, checkout): a listing costs it a read of
# every session store, and a transcript only ever reads a file listed here.
_sessions: dict[tuple[str, str], tuple[float, list[dict]]] = {}
SESSION_TTL = 30
SESSION_LISTINGS = 50
block_text = remote_probe.block_text
claude_prompt = remote_probe.claude_prompt
claude_text = remote_probe.claude_text
claude_details = remote_probe.claude_details
codex_details = remote_probe.codex_details


class Closed(ValueError):
    """The herdr workspace is not open, and no checkout of it is left to read."""


def local_environment():
    env = {k: v for k, v in os.environ.items() if k not in safe_git.TOKENS}
    env.update(github_cli.NON_INTERACTIVE)
    return env


def git_output(path, *args, limit=None, timeout=GIT_TIMEOUT):
    """Run git in a checkout, keeping at most ``limit`` bytes; return (text, truncated)."""
    limit = MAX_DIFF if limit is None else limit
    # Unquoted paths: diff headers and file lists then carry names as they are.
    # Local reads need no GitHub token, and nothing the checkout configures may run.
    argv, env = safe_git.prepare(
        str(path), "-c", "core.quotePath=false", *args, env=local_environment()
    )
    chunks: list[bytes] = []
    size = 0
    truncated = False
    deadline = time.monotonic() + timeout
    with owned_process.command(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env=env,
    ) as proc:
        assert proc.stdout is not None and proc.stderr is not None
        errors: list[bytes] = []
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ, chunks)
            selector.register(proc.stderr, selectors.EVENT_READ, errors)
            open_streams = 2
            while open_streams:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError(f"git {args[0]} took longer than {timeout} seconds")
                for key, _ in selector.select(remaining):
                    data = os.read(key.fileobj.fileno(), 65536)  # type: ignore[union-attr]
                    if not data:
                        selector.unregister(key.fileobj)
                        open_streams -= 1
                    elif key.data is errors:
                        errors.append(data)
                    elif size < limit:
                        chunks.append(data[: limit - size])
                        size += len(chunks[-1])
                        truncated = truncated or size >= limit
                    else:
                        truncated = True
                        open_streams = 0  # Enough: the group is stopped on exit.
                        break
        if not truncated and proc.wait(timeout=max(0.1, deadline - time.monotonic())):
            detail = b"".join(errors).decode("utf-8", errors="replace").strip()
            raise ValueError(detail[-400:] or f"git {args[0]} failed")
    return b"".join(chunks).decode("utf-8", errors="replace"), truncated


def git_text(path, *args):
    return git_output(path, *args, limit=256 * 1024)[0].strip()


def workspace_checkout(workspace_id, recorded_checkout=None):
    """The checkout a herdr workspace was opened on, the same one Collie shows.

    Remembered briefly: an open transcript asks again every few seconds. Once seen, a
    workspace's checkout outlives it, so a viewer left open across a close (or a reopen,
    which gets a new ID) keeps its diff, transcript and sessions.
    """
    if not isinstance(workspace_id, str) or not WORKSPACE_ID.match(workspace_id):
        raise ValueError("Supply a herdr workspace")
    with _lock:
        cached = _checkouts.get((None, workspace_id))
    if cached and time.monotonic() - cached[0] < CHECKOUT_TTL and Path(cached[1]).is_dir():
        return cached[1]
    spaces = herdr("workspace", "list")["workspaces"]
    known = cached[1] if cached else None
    recovered = False
    if (
        known is None
        and recorded_checkout
        and not any(w.get("workspace_id") == workspace_id for w in spaces)
    ):
        known = recorded_checkout(workspace_id)
        recovered = known is not None
    root = resolve_workspace(workspace_id, spaces, known)
    remember(workspace_id, root, spaces, recovered=recovered)
    return root


def remember(workspace_id, root, spaces, *, recovered=False, machine=None):
    """Note a workspace's checkout, and the other workspaces on it while it is open.

    Those others are never taken for it once it closes: a clone's own checkout often
    holds several unrelated workspaces.
    """
    key = (machine.id if machine else None, workspace_id)
    with _lock:
        _, known, others = _checkouts.pop(key, (0, None, frozenset()))
        if known != root:
            others = frozenset()
        # After recovery from an operation, we cannot tell whether an existing workspace
        # is a reopen or was already beside the old one. Keep reading the checkout, but
        # do not select those workspaces as message recipients on that evidence alone.
        if recovered or any(w.get("workspace_id") == workspace_id for w in spaces):
            others |= {
                w["workspace_id"]
                for w in spaces
                if w.get("workspace_id") != workspace_id and checkout_of(w, machine) == root
            }
        # Re-inserted last, so the newest is evicted last.
        _checkouts[key] = (time.monotonic(), root, others)
        while len(_checkouts) > 200:
            _checkouts.pop(next(iter(_checkouts)), None)


def checkout_of(space, machine=None):
    """The resolved checkout a listed herdr workspace was opened on, if it still exists.

    A worker's is the path its herdr names, never looked up on this filesystem.
    """
    if machine is not None:
        path = ((space.get("worktree") or {}) if isinstance(space, dict) else {}).get(
            "checkout_path"
        )
        if (
            isinstance(path, str)
            and len(path) <= remote_probe.MAX_TEXT
            and path.startswith("/")
            and "\0" not in path
        ):
            return path
        return None
    path = (space.get("worktree") or {}).get("checkout_path")
    return str(Path(path).resolve()) if path and Path(path).is_dir() else None


def resolve_workspace(workspace_id, spaces, known=None):
    space = next((w for w in spaces if w.get("workspace_id") == workspace_id), None)
    if space is not None:
        root = checkout_of(space)
        if root is None:
            raise ValueError("That herdr workspace has no checkout to show")
    elif known and Path(known).is_dir():
        # Closed since it was last seen: its checkout is still the one to show.
        root = known
    else:
        raise Closed("That herdr workspace is not open; refresh")
    if Path(git_text(root, "rev-parse", "--show-toplevel")).resolve() != Path(root):
        raise ValueError("That herdr workspace is not at the top of a Git checkout")
    return root


def live_workspace(workspace_id, machine=None):
    """The open herdr workspace for the checkout this ID names, or None.

    That is the workspace itself while it is open. Once it is closed, it is the one
    workspace opened on the same checkout since (herdr gives a reopened workspace a new
    ID); with none, or several to choose from, there is none. A worker's comes from
    its own herdr.
    """
    if machine is None:
        root = workspace_checkout(workspace_id)
    else:
        root = remote_checkout(machine, workspace_id)
    spaces = listed_spaces(machine)
    own = next((w for w in spaces if w.get("workspace_id") == workspace_id), None)
    if own is not None:
        if checkout_of(own, machine) != root:
            # The ID now names another checkout: never act on the remembered one.
            raise ValueError("That herdr workspace now shows another checkout; refresh")
        remember(workspace_id, root, spaces, machine=machine)
        return workspace_id
    with _lock:
        key = (machine.id if machine else None, workspace_id)
        others = _checkouts.get(key, (0, None, frozenset()))[2]
    since = [
        w["workspace_id"]
        for w in spaces
        if checkout_of(w, machine) == root and w.get("workspace_id") not in others
    ]
    return since[0] if len(since) == 1 else None


def listed_spaces(machine=None):
    """herdr's workspaces; a worker's only those whose ID could name one."""
    spaces = herdr("workspace", "list", machine=machine)["workspaces"]
    if machine is None:
        return spaces
    return [
        w
        for w in (spaces if isinstance(spaces, list) else [])
        if isinstance(w, dict)
        and isinstance(w.get("workspace_id"), str)
        and machines.WORKSPACE.fullmatch(w["workspace_id"])
    ]


def checkout_workspace(root, machine=None):
    """The herdr workspace open on a checkout now, or None.

    For a Workspaces-tab row listed while none was open: with several open, the first
    is taken, as that row lists them.
    """
    spaces = listed_spaces(machine)
    found = next((w["workspace_id"] for w in spaces if checkout_of(w, machine) == root), None)
    if found:
        remember(found, root, spaces, machine=machine)
    return found


def remote_checkout(machine, workspace_id, recorded_checkout=None):
    """The checkout a worker's herdr workspace was opened on, as that worker spells it.

    Remembered like this machine's, so a closed workspace keeps its views, and recovered
    like it from ``recorded_checkout`` once forgotten. Nothing here looks at the path:
    the worker's probe resolves it and checks it is a checkout's top.
    """
    if not isinstance(workspace_id, str) or not WORKSPACE_ID.match(workspace_id):
        raise ValueError("Supply a herdr workspace")
    with _lock:
        cached = _checkouts.get((machine.id, workspace_id))
    if cached and time.monotonic() - cached[0] < CHECKOUT_TTL:
        return cached[1]
    try:
        spaces = listed_spaces(machine)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ValueError(f"{machine.label} did not list its workspaces: {exc}") from exc
    space = next((w for w in spaces if w["workspace_id"] == workspace_id), None)
    recovered = False
    if space is not None:
        root = checkout_of(space, machine)
        if root is None:
            raise ValueError("That herdr workspace has no checkout to show")
    elif cached:
        root = cached[1]  # Closed since it was last seen.
    elif recorded_checkout and (root := recorded_checkout(workspace_id)):
        recovered = True
    else:
        raise ValueError("That herdr workspace is not open; refresh")
    remember(workspace_id, root, spaces, recovered=recovered, machine=machine)
    return root


def probe(home, machine, op, timeout=REMOTE_TIMEOUT, **args):
    """One probe op on a worker; every failure, a timeout included, names the worker."""
    try:
        return remote_probe.call(home, machine, op, timeout, **args)
    except subprocess.TimeoutExpired as exc:
        raise remote_probe.Unreachable(
            f"{machine.label} did not answer within {timeout} seconds"
        ) from exc
    except OSError as exc:
        raise remote_probe.Unreachable(f"{machine.label} could not be reached: {exc}") from exc


def bases(path):
    return remote_probe.bases(git_output, path)


def split_patch(text, truncated):
    return remote_probe.split_patch(text, truncated, MAX_FILE_LINES)


def diff(path, scope="branch", base=None, commit=None):
    """Changes in a checkout: against its base branch (with uncommitted work) or HEAD.

    With ``commit``, one of the branch's listed commits alone.
    """
    return remote_probe.diff(git_output, path, scope, base, MAX_FILE_LINES, commit)


def remote_diff(home, machine, path, scope="branch", base=None, commit=None):
    """``diff`` of a worker's checkout, run there with the same limits."""
    if scope not in {"branch", "uncommitted"}:
        raise ValueError("Choose the branch or uncommitted changes")
    if commit is not None and not remote_probe.COMMIT.fullmatch(commit):
        raise ValueError("Expected a commit ID")
    return probe(home, machine, "diff", path=path, scope=scope, base=base, commit=commit)


# Transcripts -------------------------------------------------------------------


def within(child, root):
    try:
        return Path(child).resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def version(stat):
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)


def clip(text, limit):
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return (
        text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit} more characters)"
    )


def first_line(text):
    return text.strip().split("\n")[0][:MAX_BRIEF] if text else None


def tool_view(value):
    """A tool call's input as (about, brief, text) for reading.

    ``about`` is the call's own description, ``brief`` the gist of its main argument on
    one line, and ``text`` the whole input written out rather than as escaped JSON.
    """
    if isinstance(value, str):
        try:
            parsed = json.loads(value)  # Codex passes function arguments as JSON text.
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            value = parsed
    if not isinstance(value, dict):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        brief = text
        if found := CODEX_CMD.search(text):
            try:
                brief = json.loads(found.group(1))
            except ValueError:
                pass
        return None, first_line(brief), text
    fields = dict(value)
    about = fields.pop("description") if isinstance(fields.get("description"), str) else None
    key = next(
        (k for k in TOOL_SUBJECT if fields.get(k) and isinstance(fields[k], (str, list))), None
    )
    subject = fields.pop(key) if key else None
    if isinstance(subject, list):
        words = [w if isinstance(w, str) else json.dumps(w) for w in subject]
        # ["bash", "-lc", script] is the script itself.
        shell = len(words) == 3 and Path(words[0]).name in SHELLS and words[1] in {"-c", "-lc"}
        subject = words[2] if shell else shlex.join(words)
    short, long = [], []
    for name, item in fields.items():
        shown = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        if "\n" in shown:
            long.append(f"{name}:\n{shown}")
        else:
            short.append(f"{name}: {shown}")
    parts = [subject] if subject else []
    if short:
        parts.append("\n".join(short))
    text = "\n\n".join(parts + long)
    return about[:MAX_BRIEF] if about else None, first_line(subject or text), text


def question_list(value):
    """A question tool's questions as [{question, header, multi, options: [{label,
    description}]}], from Claude's or Codex's input shape; None if it holds none."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    raw = value.get("questions") if isinstance(value, dict) else None
    if not isinstance(raw, list):
        return None
    found = []
    for item in raw[:MAX_QUESTIONS]:
        if not isinstance(item, dict):
            continue
        text = item.get("question") or item.get("title")
        if not isinstance(text, str) or not text.strip():
            continue
        options = []
        for option in item.get("options") or []:
            if isinstance(option, str):
                option = {"label": option}
            if isinstance(option, dict) and isinstance(option.get("label"), str):
                description = option.get("description")
                options.append(
                    {
                        "label": clip(option["label"], MAX_BRIEF),
                        "description": clip(description, 1000)
                        if isinstance(description, str)
                        else None,
                    }
                )
        header = item.get("header")
        found.append(
            {
                "question": clip(text, 2000),
                "header": header[:40] if isinstance(header, str) else None,
                "multi": item.get("multiSelect") is True,
                "options": options[:MAX_OPTIONS],
            }
        )
    return found or None


def question_pending(tool):
    """An async acknowledgment opens a Codex dialog; it is not the user's answer."""
    if not tool.get("questions") or tool.get("error"):
        return False
    output = tool.get("output")
    if output is None:
        return True
    if tool.get("name") == "request_user_input_async":
        try:
            return json.loads(output) == {"accepted": True}
        except (ValueError, TypeError):
            pass
    return False


def details(path, agent):
    """A session file's details, read again only when the file changes."""
    try:
        current = version(path.stat())
    except OSError:
        return None
    key = str(path)
    with _lock:
        cached = _details.get(key)
    if cached and cached[0] == current:
        return cached[1]
    saved = store().details(key, current)
    if saved is not None:
        found = saved[0]
    else:
        try:
            found = claude_details(path) if agent == "claude" else codex_details(path)
        except BlockingIOError:
            return None  # Not cached: the first line is still being written.
        except (OSError, ValueError):
            found = None
        store().put_details(key, current, found)
    with _lock:
        _details.pop(key, None)
        _details[key] = (current, found)
        while len(_details) > DETAIL_FILES:
            _details.pop(next(iter(_details)), None)
    return found


def codex_store():
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"


def session_files(root=None):
    """Candidate transcript files: Claude folders for the checkout and its subdirectories
    (every folder without a ``root``), and every Codex rollout (each is matched on its
    recorded cwd)."""
    # Claude names a project folder after its cwd, every other character a dash.
    folder = None if root is None else re.sub(r"[^A-Za-z0-9]", "-", str(root))
    for home in claude_accounts.homes():
        try:
            folders = [
                entry
                for entry in (home / "projects").iterdir()
                if folder is None or entry.name == folder or entry.name.startswith(folder + "-")
            ]
        except OSError:
            folders = []
        for entry in folders:
            for path in entry.glob("*.jsonl"):
                yield "claude", path
    for path in codex_store().glob("*/*/*/rollout-*.jsonl"):
        yield "codex", path


def session_store(agent, file):
    """The session store of this machine that ``file`` is in, as ``session_files`` names
    its files: a Claude home's ``projects``, or Codex's ``sessions``. None for any other."""
    file = Path(file)
    if ".." in file.parts or file.suffix != ".jsonl":
        return None
    if agent == "claude":
        stores = [home / "projects" for home in claude_accounts.homes()]
        return next((found for found in stores if file.parent.parent == found), None)
    codex = codex_store()
    below = agent == "codex" and file.name.startswith("rollout-") and len(file.parents) > 3
    return codex if below and file.parents[3] == codex else None


def session_of(agent, file, root=None):
    """A session file as the listings name it; None unless it records a session, in
    ``root`` or below it where one is given."""
    found = details(file, agent)
    if (
        not found
        or not SESSION.match(found["id"])
        or (root is not None and not Path(found["cwd"]).is_relative_to(root))
    ):
        return None
    return session_row(agent, file, found)


def sessions(path):
    """Agent sessions recorded in this checkout or below it, newest first."""
    root = Path(path).resolve()
    listed = []
    for agent, file in session_files(root):
        session = session_of(agent, file, root)
        if session is None:
            continue
        listed.append(session)
    listed.sort(key=lambda s: s["updated"], reverse=True)
    return listed[:MAX_SESSIONS]


def session_row(agent, file, found):
    """A listing's row for a session with these ``details``, or None once its file is gone."""
    try:
        stat = file.stat()
    except OSError:
        return None
    title = (found["title"] or "").strip()
    row = {
        "id": found["id"],
        "agent": agent,
        "updated": stat.st_mtime,
        "size": stat.st_size,
        "title": title.splitlines()[0][:160] if title else None,
        "_file": file,
        "_cwd": found["cwd"],
    }
    if agent == "claude":
        home = claude_accounts.transcript_home(file)
        account = claude_accounts.account_name(home) if home else None
        if home and (account is not None or home != claude_accounts.current_home()):
            row.update(
                claude_config_dir=str(home),
                claude_config_env=claude_accounts.config_environment(home),
                claude_account=account,
            )
    return row


def session_anywhere(session):
    """A session's row wherever on this machine it was recorded, or None.

    ``sessions`` lists what was recorded in the checkout. An agent started in a pane
    that had left the checkout records its session under that other directory, though
    herdr lists it in the checkout's workspace.
    """
    files = [
        ("claude", file)
        for home in claude_accounts.homes()
        for file in (home / "projects").glob(f"*/{session}.jsonl")
    ]
    # A rollout is named rollout-STARTED-SESSION.jsonl.
    files += [("codex", file) for file in codex_store().glob(f"*/*/*/rollout-*-{session}.jsonl")]
    rows = []
    for agent, file in files:
        found = details(file, agent)
        row = session_row(agent, file, found) if found and found["id"] == session else None
        if row:
            rows.append(row)
    return max(rows, key=lambda s: s["updated"], default=None)


def remote_sessions(home, machine, path):
    """A worker's sessions recorded in its checkout or below it, as ``sessions`` lists.

    Kept for SESSION_TTL per (machine, checkout). ``_file`` is the worker's own path: a
    transcript is read only from a file listed here, and the worker refuses any other.
    The worker's working directory check and Claude home ride along for resuming.
    """
    key = (machine.id, path)
    with _lock:
        cached = _sessions.get(key)
    if cached and time.monotonic() - cached[0] < SESSION_TTL:
        return cached[1]
    listed = [remote_row(found) for found in probe(home, machine, "sessions", root=path)]
    with _lock:
        _sessions.pop(key, None)
        _sessions[key] = (time.monotonic(), listed)
        while len(_sessions) > SESSION_LISTINGS:
            _sessions.pop(next(iter(_sessions)), None)
    return listed


def remote_row(found):
    """A listing's row for a session a worker's probe ``found``."""
    row = {
        "id": found["id"],
        "agent": found["agent"],
        "updated": found["updated"],
        "size": found["size"],
        "title": remote_probe.display_text(found["title"]),
        "_file": found["file"],
        "_cwd": found["cwd"],
        "_cwd_exists": found["cwd_exists"],
        "_home": found["home"],
    }
    if found["account"] is not None:
        row["claude_account"] = found["account"]
    return row


def remote_session_anywhere(home, machine, session):
    """A worker's session wherever on it it was recorded, as ``session_anywhere``."""
    found = probe(home, machine, "session", session=session)
    return remote_row(found[0]) if found and found[0]["id"] == session else None


def remote_kept(home, machine, path):
    """A worker's sessions for its checkout with those kept here for it, as ``with_kept``.

    A worker that does not answer, or whose checkout is gone, still shows what was kept.
    """
    try:
        listed = remote_sessions(home, machine, path)
    except (OSError, ValueError):
        kept = with_kept([], machine.id, path)
        if not kept:
            raise
        return kept
    return with_kept(listed, machine.id, path)


class Transcript:
    """A session's conversation, parsed once and then only as its file grows.

    Entries go to the store as they are parsed (``take``), and a parser picks up from
    the place the store kept (``restore``), so only new bytes are ever parsed.
    """

    def __init__(self, agent, max_line=None, load=None):
        self.agent = agent
        self.max_line = max_line  # None: MAX_LINE.
        # ``load(index)`` reads a saved entry back, None once it is gone.
        self.load = load or (lambda index: None)
        self.reset(None)

    def reset(self, inode):
        self.inode = inode
        self.offset = 0
        # The unfinished line after ``offset``, as chunks, and its length; and whether
        # the read is inside a line too long to parse.
        self.pending: list[bytes] = []
        self.held = 0
        self.skipping = False
        # Entries before ``saved`` were taken for the store, so ``entries[0]`` has that
        # index; the store keeps those from ``first``, dropping older past MAX_ENTRIES.
        self.saved = 0
        self.first = 0
        self.entries: list[dict] = []
        # The newest tool calls, by call ID: (index, entry), the entry None until it is
        # loaded back from the store. A result for one already taken changes that saved
        # entry (``changed``).
        self.tools: dict[str, tuple[int, dict | None]] = {}
        self.changed: dict[int, dict] = {}
        # Codex versions that record typed prompts as events also echo them as items;
        # each is an ordered set of the newest MAX_PROMPTS.
        self.prompts: dict[str, dict[str, None]] = {"event": {}, "item": {}}
        # Started again from the file's start: what the store kept for it goes.
        self.restarted = True

    def restore(self, row):
        """Pick up from a store ``row``: its place in the file and its tool calls."""
        state = json.loads(row["state"])
        self.inode = row["inode"]
        self.offset = row["offset"]
        self.skipping = bool(row["skipping"])
        self.saved = row["saved"]
        self.first = row["first"]
        self.tools = {str(key): (index, None) for key, index in state.get("tools", {}).items()}
        self.prompts = {
            source: dict.fromkeys(state.get("prompts", {}).get(source, []))
            for source in ("event", "item")
        }
        self.restarted = False

    def take(self):
        """What was parsed since the last take, for the store, as (restarted, added,
        changed): new entries as (index, entry) pairs and changed saved entries by index.

        Past MAX_ENTRIES the oldest go, a tenth at a time; indices stay those of the
        whole transcript.
        """
        added = list(enumerate(self.entries, self.saved))
        changed, restarted = self.changed, self.restarted
        self.saved += len(self.entries)
        self.entries, self.changed, self.restarted = [], {}, False
        if self.saved - self.first > MAX_ENTRIES:
            self.first = self.saved - MAX_ENTRIES + MAX_ENTRIES // 10
            self.tools = {k: v for k, v in self.tools.items() if v[0] >= self.first}
            added = [(i, e) for i, e in added if i >= self.first]
            changed = {i: e for i, e in changed.items() if i >= self.first}
        return restarted, added, changed

    def place(self):
        """Where the parser is, as the store keeps it."""
        state = {
            "tools": {key: index for key, (index, _) in self.tools.items()},
            "prompts": {source: list(seen) for source, seen in self.prompts.items()},
        }
        return (self.inode, self.offset, self.skipping, self.saved, self.first, json.dumps(state))

    # Reading is split so the bytes can come from anywhere: ``position`` says where to read
    # from for the file as it is now, and ``consume`` parses what was read from there.
    # This machine reads its own files (``update``); a worker's arrive through its probe.

    def position(self, inode, size):
        """The offset to read on from, for the file now at ``inode`` with ``size`` bytes."""
        # A replaced or truncated file is read again from the start.
        if inode != self.inode or size < self.offset + self.held:
            self.reset(inode)
        return self.offset + self.held

    def consume(self, data):
        """Parse the complete lines of ``data``, read from ``offset``; returns bytes used."""
        end = data.rfind(b"\n")
        if end < 0:
            return 0
        # Only complete lines: a line still being written is read next time.
        for line in data[: end + 1].split(b"\n"):
            # Only an object becomes an entry; anything else is not worth parsing.
            if not line[:64].lstrip().startswith(b"{"):
                continue
            if len(line) > self.limit():
                self.too_long()
                continue
            if line.strip():
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        self.feed(item)
                except (ValueError, RecursionError):
                    continue  # Not JSON, or nested too deeply to parse or show.
        self.offset += end + 1
        return end + 1

    def absorb(self, data):
        """Parse what ``data``, read from ``offset + held``, completes.

        An unfinished line is kept, as chunks, for the next read. One longer than
        MAX_LINE is skipped with a notice and its bytes passed over as they arrive, so
        reading moves on past it rather than fetching it again.
        """
        if self.skipping:
            end = data.find(b"\n")
            if end < 0:
                self.offset += len(data)
                return
            self.offset += end + 1
            self.skipping = False
            data = data[end + 1 :]
        end = data.rfind(b"\n")
        if end < 0:
            if data:
                self.pending.append(data)
                self.held += len(data)
            if self.held > self.limit():
                self.offset += self.held
                self.pending, self.held = [], 0
                self.skipping = True
                self.too_long()
            return
        self.consume(b"".join([*self.pending, data[: end + 1]]))
        rest = data[end + 1 :]
        self.pending, self.held = ([rest], len(rest)) if rest else ([], 0)

    def limit(self):
        return MAX_LINE if self.max_line is None else self.max_line

    def too_long(self):
        self.entries.append(
            {
                "role": "user",
                "kind": "notification",
                "text": f"Transcript line too long (over {self.limit() >> 20} MiB); skipped",
                "time": None,
            }
        )

    def update(self, path, save=None):
        """Read on to the end of this machine's ``path``, calling ``save`` after each
        chunk; returns True, as the end was reached."""
        with remote_probe.open_transcript(path) as stream:
            info = os.fstat(stream.fileno())
            stream.seek(self.position(info.st_ino, info.st_size))
            while chunk := stream.read(CHUNK):
                self.absorb(chunk)
                if save:
                    save()
        return True

    def update_remote(self, tail, file, save=None, stop=None):
        """Read on through a worker's ``tail(file, inode, offset, limit, timeout)`` probe op.

        Each answer ends at a line end, except a full read inside one longer line: those
        pieces are kept, as ``update`` keeps chunks, until the line ends. A poll stops
        after REMOTE_BUDGET seconds (or at monotonic ``stop``), and the next goes on from
        there. ``save`` is called after each answer; returns whether the end was reached.
        """
        stop = time.monotonic() + REMOTE_BUDGET if stop is None else stop
        for _ in range(REMOTE_READS):
            left = stop - time.monotonic()
            if left < REMOTE_MIN_READ:
                return False
            at = self.offset + self.held
            answer = tail(
                file=file,
                inode=self.inode,
                offset=at,
                limit=CHUNK,
                timeout=max(1, int(min(REMOTE_TIMEOUT, left))),
            )
            if answer["inode"] != self.inode or answer["offset"] != at:
                # Replaced or truncated: the worker read from the start, and so does this.
                if answer["offset"]:
                    raise ValueError("The transcript changed while it was read; refresh")
                self.reset(answer["inode"])
            data = answer["data"]
            if answer["offset"] + len(data) > answer["size"]:
                raise ValueError("The worker read past the end of the transcript; refresh")
            self.absorb(data)
            if save:
                save()
            if not data or answer["offset"] + len(data) >= answer["size"]:
                return True
        return False

    def feed(self, item):
        if self.agent == "claude":
            self.claude(item)
        else:
            self.codex(item)

    def tool(self, key, name, value, when):
        about, brief, text = tool_view(value)
        tool = {
            "role": "tool",
            "id": str(key or ""),
            "name": str(name or "tool"),
            "about": about,
            "brief": brief,
            "input": clip(text, MAX_TOOL),
            "output": None,
            "error": False,
            "time": when,
        }
        if QUESTION_TOOLS.match(tool["name"]):
            tool["questions"] = question_list(value)
        self.tools.pop(key or "", None)
        self.tools[key or ""] = (self.saved + len(self.entries), tool)
        while len(self.tools) > OPEN_TOOLS:
            self.tools.pop(next(iter(self.tools)))
        self.entries.append(tool)

    def answered(self, key):
        """The tool call ``key`` names, to take its result; None if none is kept."""
        found = self.tools.get(key or "")
        if found is None:
            return None
        index, tool = found
        if tool is None:
            tool = self.load(index)
            if tool is None:
                del self.tools[key or ""]
                return None
            self.tools[key or ""] = (index, tool)
        if index < self.saved:
            self.changed[index] = tool
        return tool

    def claude(self, item):
        if item.get("isSidechain") or item.get("isMeta"):
            return
        kind = item.get("type")
        content = (item.get("message") or {}).get("content")
        blocks = content if isinstance(content, list) else []
        when = item.get("timestamp")
        if kind == "user":
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool = self.answered(block.get("tool_use_id"))
                    if tool is not None:
                        tool["output"] = clip(block_text(block.get("content")), MAX_TOOL)
                        tool["error"] = bool(block.get("is_error"))
                        # Claude records a dialog's answers by question as well.
                        result = item.get("toolUseResult")
                        answers = result.get("answers") if isinstance(result, dict) else None
                        if tool.get("questions") and isinstance(answers, dict):
                            tool["answers"] = {
                                str(q): clip(a, 2000)
                                for q, a in answers.items()
                                if isinstance(a, str)
                            }
            view = claude_prompt(claude_text(item))
            if view:
                kind, text = view
                self.entries.append(
                    {"role": "user", "kind": kind, "text": clip(text, MAX_TEXT), "time": when}
                )
        elif kind == "assistant":
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.entries.append(
                        {"role": "assistant", "text": clip(block["text"], MAX_TEXT), "time": when}
                    )
                elif block.get("type") == "tool_use":
                    self.tool(block.get("id"), block.get("name"), block.get("input"), when)

    def codex(self, item):
        payload = item.get("payload") or {}
        kind = payload.get("type")
        when = item.get("timestamp")
        if item.get("type") == "event_msg" and kind == "user_message":
            # The prompt as typed, where this Codex version records it.
            text = str(payload.get("message") or "").strip()
            if text:
                self.prompt("event", text, when)
        elif item.get("type") == "event_msg" and kind == "task_complete":
            # A turn that ended in an error has no reply: a safety filter refused it,
            # the model was at capacity, the usage limit was reached. Codex shows that
            # on the screen and records it only here.
            error = payload.get("error")
            if isinstance(error, dict):
                code = error.get("codex_error_info")
                code = code if isinstance(code, str) else None
                reason = str(error.get("message") or "").strip() or code
                if reason:
                    self.entries.append(
                        {
                            "role": "user",
                            "kind": "error",
                            "text": clip(reason, MAX_TEXT),
                            "code": code,
                            "time": when,
                        }
                    )
        elif item.get("type") != "response_item":
            return
        elif kind == "message" and payload.get("role") in {"user", "assistant"}:
            text = block_text(payload.get("content")).strip()
            if payload["role"] == "assistant" and text:
                self.entries.append(
                    {"role": "assistant", "text": clip(text, MAX_TEXT), "time": when}
                )
            elif text and not CODEX_CONTEXT.match(text):
                self.prompt("item", text, when)
        elif kind in {"function_call", "custom_tool_call", "local_shell_call"}:
            value = payload.get("arguments") or payload.get("input") or payload.get("action")
            self.tool(payload.get("call_id"), payload.get("name") or kind, value, when)
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            tool = self.answered(payload.get("call_id"))
            if tool is not None:
                output = payload.get("output")
                shown = block_text(output) if isinstance(output, list) else output
                shown = shown if isinstance(shown, str) else json.dumps(shown)
                verdict = CODEX_VERDICT.match(shown.lstrip())
                tool["output"] = clip(shown, MAX_TOOL)
                tool["error"] = (
                    verdict.group(1) != "completed" if verdict else bool(CODEX_EXIT.search(shown))
                )

    def prompt(self, source, text, when):
        # Whichever copy arrives first is kept; the echo of an already-shown prompt is not.
        other = self.prompts["item" if source == "event" else "event"]
        if text in other:
            del other[text]
            return
        seen = self.prompts[source]
        seen.pop(text, None)
        seen[text] = None
        while len(seen) > MAX_PROMPTS:
            seen.pop(next(iter(seen)))
        self.entries.append(
            {"role": "user", "kind": "prompt", "text": clip(text, MAX_TEXT), "time": when}
        )


def use_store(home):
    """Keep parsed transcripts in ``home``'s ``transcript-archive.sqlite`` from now on."""
    global _store_path, _store
    with _lock:
        _store_path, _store = Path(home) / transcript_store.FILE, None


def store():
    """The transcript store, opened on first use. Without ``use_store`` it is a private
    temporary one, removed at exit."""
    global _store
    with _lock:
        if _store is None:
            path = _store_path
            if path is None:
                folder = tempfile.mkdtemp(prefix="babysit-transcripts-")
                atexit.register(shutil.rmtree, folder, True)
                path = Path(folder) / transcript_store.FILE
            _store = transcript_store.Store(path)
        return _store


def search_text(entry):
    """What an entry is found by: its text (and a failed turn's error code), or a tool
    call's name, input and output."""
    parts = [entry.get(name) for name in ("name", "about", "code", "text", "input", "output")]
    for question in entry.get("questions") or []:
        parts += [question["question"], *(option["label"] for option in question["options"])]
    parts += (entry.get("answers") or {}).values()
    return "\n".join(part for part in parts if isinstance(part, str))


def with_kept(listed, machine, root):
    """``listed`` sessions of a checkout and those kept in the store for it that are not
    among them, newest first, KEPT_SESSIONS at most.

    ``machine`` is the worker's id, "" for this machine, and ``root`` the checkout as
    that machine spells it. A kept session whose file is gone, or any a worker did not
    list, is marked ``archived``: it is shown from the store and never read on.
    """
    files = {str(session["_file"]) for session in listed}
    ids = {session["id"] for session in listed}
    kept = []
    for row in store().recorded(machine, str(root), KEPT_SESSIONS + len(listed)):
        if row["file"] in files or row["session"] in ids:
            continue
        ids.add(row["session"])
        session = {
            "id": row["session"],
            "agent": row["agent"],
            "updated": row["updated"],
            "size": row["size"],
            "title": row["title"],
            "_file": row["file"],
            "_cwd": row["cwd"],
        }
        if machine or not os.path.exists(row["file"]):
            session["archived"] = True
            # Its file was kept too, all of it as it was last listed: ``restore``.
            if (
                not machine
                and row["raw_size"]
                and row["raw_size"] >= (row["size"] or 0)
                and session_store(row["agent"], row["file"])
            ):
                session["restorable"] = True
        else:
            # Listed no longer only because newer ones filled the list: read on as ever.
            session["_file"] = Path(row["file"])
        kept.append(session)
    found = [*listed, *kept[: max(0, KEPT_SESSIONS - len(listed))]]
    found.sort(key=lambda s: s["updated"] or 0, reverse=True)
    return found


def bring_up_to_date(key, agent, read, wait=1.0, session=None, index=True):
    """Parse a transcript on from where the store has it, saving as it goes.

    ``key`` is (machine id, file), "" for this machine. ``read(parser, file, save)``
    reads on, calling ``save`` as it goes, and says whether it reached the end. Returns
    the store row; while another request reads the same transcript for longer than
    ``wait`` seconds, the row as last saved, or None if it was never read to its end.

    The row keeps what lists ``session`` (its listing entry), so the transcript is still
    found for its checkout once the file is gone. Without ``index`` the new entries are
    left for the first search that needs them.
    """
    with _lock:
        lock = _reading.setdefault(key, threading.Lock())
        if len(_reading) > 1000:
            for other in [k for k, held in _reading.items() if k != key and not held.locked()]:
                del _reading[other]
    kept = store()
    if not (lock.acquire(timeout=wait) if wait else lock.acquire(blocking=False)):
        row = kept.row(*key)
        return row if row and row["ready"] else None
    try:
        machine, file = key
        row = kept.open(machine, file, agent, PARSER)
        if session is not None:
            kept.describe(
                row,
                session["id"],
                session.get("_cwd"),
                session["title"],
                session["updated"],
                session["size"],
            )
        parser = Transcript(
            agent,
            REMOTE_MAX_LINE if machine else None,
            lambda index: kept.entry(row["id"], index),
        )
        parser.restore(row)
        generation = row["generation"]
        saved = (parser.place(), row["ready"])

        def save(ready=None):
            nonlocal generation, saved
            restarted, added, changed = parser.take()
            if restarted and ready is None:
                ready = False  # Read again from the start: not shown until it is whole.
            now = (parser.place(), saved[1] if ready is None else int(ready))
            if not (restarted or added or changed) and now == saved:
                return  # An open viewer's poll that found nothing new writes nothing.
            saved = now
            generation = kept.save(
                row["id"],
                generation,
                restarted=restarted,
                added=[(i, json.dumps(e), search_text(e)) for i, e in added],
                changed=[(i, json.dumps(e), search_text(e)) for i, e in changed.items()],
                place=parser.place(),
                ready=ready,
            )

        save(ready=True if read(parser, file, save) else None)
    finally:
        lock.release()
    # Indexed once the read is done, so its saves never wait behind the indexing.
    if index:
        kept.index_soon(row["id"])
    if kept.prune_due():
        threading.Thread(target=kept.prune, name="transcript-prune", daemon=True).start()
    return kept.row(*key)


def local_read(parser, file, save):
    return parser.update(file, save)


def transcript(
    path,
    session=None,
    before=None,
    after=None,
    prefer=None,
    around=None,
    later=None,
    running=None,
):
    """One page of a session's conversation.

    The newest page by default; ``before`` pages back from an index, and ``after``
    returns what follows an index, so an open viewer fetches only what is new.
    ``around`` centres a page on an index (a search hit) and ``later`` pages on from
    one. Without a ``session``, ``prefer`` is chosen when it is recorded here, else the
    newest.

    ``running`` answers the sessions of the agents in the checkout's workspace. It is
    asked only about a ``session`` not recorded in the checkout, which is then read from
    wherever it was recorded: the agent runs here, whichever directory it started in.
    """
    return transcript_page(
        with_kept(sessions(path), "", Path(path).resolve()),
        lambda file: ("", str(file)),
        local_read,
        session,
        before,
        after,
        prefer,
        around,
        later,
        elsewhere(running, session_anywhere),
    )


def remote_transcript(
    home,
    machine,
    path,
    session=None,
    before=None,
    after=None,
    prefer=None,
    around=None,
    later=None,
    running=None,
):
    """``transcript`` of a worker's checkout: its listing and new bytes, parsed here."""

    def tail(**args):
        return probe(home, machine, "tail", **args)

    return transcript_page(
        remote_kept(home, machine, path),
        lambda file: (machine.id, file),
        lambda parser, file, save: parser.update_remote(tail, file, save),
        session,
        before,
        after,
        prefer,
        around,
        later,
        elsewhere(running, lambda session: remote_session_anywhere(home, machine, session)),
    )


def elsewhere(running, find):
    """Finds a session no checkout listing has, if an agent in the workspace runs it."""
    if running is None:
        return None
    return lambda session: find(session) if session in running() else None


def transcript_page(
    listed, key_of, read, session, before, after, prefer, around=None, later=None, unlisted=None
):
    """A page of one ``listed`` session, kept in the store under ``key_of(file)`` and
    brought up to date by ``read(parser, file, save)``. ``unlisted(session)`` is the row
    of an asked-for session the listing lacks, or None; it is then listed with the rest."""
    if session is None:
        if not listed:
            return {
                "sessions": [],
                "session": None,
                "entries": [],
                "start": 0,
                "first": 0,
                "total": 0,
            }
        chosen = next((s for s in listed if s["id"] == prefer), listed[0])
    else:
        if not isinstance(session, str) or not SESSION.match(session):
            raise ValueError("Choose a listed session")
        chosen = next((s for s in listed if s["id"] == session), None)
        if chosen is None and unlisted:
            chosen = unlisted(session)
            if chosen:
                listed = sorted([chosen, *listed], key=lambda s: s["updated"] or 0, reverse=True)
        if chosen is None:
            raise ValueError("That session is not recorded for this workspace")
    if chosen.get("archived"):
        row = store().row(*key_of(chosen["_file"]))
        if row is None:
            raise ValueError("That session is not recorded for this workspace")
        # Nothing is left to read on: the last read that reached the file's end shows.
        kept, first, total = store().whole(row)
    else:
        # A first parse of a large file takes a while; a request meanwhile is answered
        # from what was saved by the last complete read.
        row = bring_up_to_date(key_of(chosen["_file"]), chosen["agent"], read, session=chosen)
        if row is None:
            raise ValueError("This transcript is still being read; try again shortly")
        # Indices count from the transcript's start, the dropped oldest entries included.
        kept, first, total = row["id"], row["first"], row["saved"]
    if after is not None and first <= after <= total and total - after <= PAGE:
        start, end = after, total
    elif around is not None:
        start = max(first, min(around, total) - PAGE // 2)
        end = min(total, start + PAGE)
    elif later is not None:
        start = max(first, min(later, total))
        end = min(total, start + PAGE)
    else:
        end = total if before is None else max(first, min(before, total))
        start = max(first, end - PAGE)
    # Underscored fields (the file, and a worker's facts for resuming) stay here.
    return {
        "sessions": [{k: v for k, v in s.items() if not k.startswith("_")} for s in listed],
        "session": {k: v for k, v in chosen.items() if not k.startswith("_")},
        "entries": store().page(kept, start, end),
        "start": start,
        "first": first,
        "total": total,
    }


def search(path, text):
    """Entries of this checkout's sessions that contain ``text``, newest sessions first."""
    expression = transcript_store.match_expression(text)
    return search_result(text, matching(path, expression, SEARCH_RESULTS))


def remote_search(home, machine, path, text):
    """``search`` of a worker's checkout, reading its transcripts on through the probe."""
    expression = transcript_store.match_expression(text)
    return search_result(text, remote_matching(home, machine, path, expression, SEARCH_RESULTS))


def search_result(text, answer):
    """What the viewer shows of ``matching``'s ``answer`` to a search for ``text``."""
    matches, total, searched, behind = answer
    found = []
    for session, index, entry, snippet in matches:
        found.append(
            {
                "session": session["id"],
                "agent": session["agent"],
                "title": session["title"],
                "updated": session["updated"],
                "index": index,
                "role": entry.get("role"),
                "kind": entry.get("kind"),
                "name": entry.get("name"),
                "time": entry.get("time"),
                "snippet": [
                    {"text": part, "match": matched}
                    for part, matched in transcript_store.snippet_parts(snippet)
                ],
            }
        )
    return {
        "query": " ".join(text.split()),
        "results": found,
        "total": total,
        "searched": searched,
        "behind": behind,
    }


def matching(path, expression, limit):
    """This checkout's entries matching an FTS ``expression``, as ``matching_sessions``."""
    return matching_sessions(
        with_kept(sessions(path), "", Path(path).resolve()),
        lambda file: ("", str(file)),
        local_read,
        expression,
        limit,
    )


def remote_matching(home, machine, path, expression, limit):
    """``matching`` of a worker's checkout, reading its transcripts on through the probe."""

    def tail(**args):
        return probe(home, machine, "tail", **args)

    stop = time.monotonic() + SEARCH_BUDGET
    return matching_sessions(
        remote_kept(home, machine, path),
        lambda file: (machine.id, file),
        lambda parser, file, save: parser.update_remote(tail, file, save, stop),
        expression,
        limit,
        stop,
    )


def matching_sessions(listed, key_of, read, expression, limit, stop=None):
    """Entries of ``listed`` sessions matching ``expression``, bringing each session up
    to date first until ``stop``.

    Returns (matches, total, searched, behind): at most ``limit`` (session, index, entry,
    snippet) tuples, newest session first and newest entry first within it, every
    match's count, and how many sessions were searched. Sessions not read to their end
    in time (or busy, or unreadable), or not indexed to their end in as long again, are
    searched as far as they were and counted in ``behind``; searching again goes on. An
    archived session is searched as it was kept: there is nothing left to read on.
    """
    stop = time.monotonic() + SEARCH_BUDGET if stop is None else stop
    rows: dict[int, dict] = {}
    # Sessions with nothing kept to search, and the rows of those kept only in part.
    behind = 0
    partial = set()
    for session in listed:
        key = key_of(session["_file"])
        row = None
        if session.get("archived"):
            row = store().row(*key)
            if row is not None:
                rows[store().whole(row)[0]] = session
            continue
        if time.monotonic() < stop:
            try:
                row = bring_up_to_date(key, session["agent"], read, wait=0, session=session)
            except (OSError, ValueError):
                if key[0]:
                    stop = 0  # A worker that no longer answers: search what is kept.
        if row is None:
            row = store().row(*key)
        if row is None:
            behind += 1
        else:
            rows[row["id"]] = session
            if not row["ready"]:
                partial.add(row["id"])
    # What the archive read in the background is indexed here, for as long again at most.
    matches, counts = store().search(
        list(rows), expression, limit, stop=time.monotonic() + SEARCH_BUDGET
    )
    partial |= store().unindexed(list(rows))
    found = [
        (rows[transcript_id], index, entry, snippet)
        for transcript_id, index, entry, snippet in matches
    ]
    return found, sum(counts.values()), len(rows), behind + len(partial)


def archive(listed, machine, read, stop=None):
    """Read into the store every ``listed`` session it is behind on; returns how many.

    ``machine`` is the worker's id, "" for this machine, and ``read`` as
    ``bring_up_to_date`` takes it. A session read to its end, whose file has not grown
    since, is passed over. ``stop`` is an event that ends the work early.
    """
    read_to = store().read_to(machine)
    done = 0
    for session in listed:
        if stop is not None and stop.is_set():
            break
        file = str(session["_file"])
        if read_to.get(file) == session["size"]:
            continue
        try:
            bring_up_to_date(
                (machine, file), session["agent"], read, wait=0, session=session, index=False
            )
        except remote_probe.Unreachable:
            raise  # Asking a worker that does not answer for the next would wait as long.
        except (OSError, ValueError, sqlite3.Error):
            continue  # This one could not be read: the others still are.
        done += 1
        if stop is not None:
            stop.wait(ARCHIVE_PAUSE)
    return done


def archive_local(stop=None):
    """Read every agent session recorded on this machine into the store, and keep its
    file there as it is."""
    listed = []
    for agent, file in session_files():
        if stop is not None and stop.is_set():
            return 0
        session = session_of(agent, file)
        if session is not None:
            listed.append(session)
    done = archive(listed, "", local_read, stop)
    keep_files(listed, stop)
    return done


def keep_files(listed, stop=None):
    """Keep each ``listed`` session file of this machine as it is, for ``restore``: the
    bytes added since it was last kept, or all of a file that took another's place.
    Returns how many were kept on. ``stop`` is an event that ends the work early."""
    kept = store()
    known = kept.kept_files("")
    done = 0
    for session in listed:
        if stop is not None and stop.is_set():
            break
        found = known.get(str(session["_file"]))
        # One without a row had no transcript read; one as long as its file is kept,
        # unless the copy is of no file (``restored``) and only happens to be as long.
        if (
            found is None
            or (found[1] is not None and found[2] == session["size"])
            or session["size"] > RAW_MAX
        ):
            continue
        try:
            keep_file(kept, session["_file"], *found)
        except (OSError, ValueError, sqlite3.Error, lzma.LZMAError):
            continue  # This one could not be kept: the others still are.
        done += 1
        if stop is not None:
            stop.wait(ARCHIVE_PAUSE)
    return done


def keep_file(kept, file, transcript, inode, size):
    """Keep ``file`` on from the ``size`` bytes kept of the file ``inode``."""
    with remote_probe.open_transcript(file) as stream:
        info = os.fstat(stream.fileno())

        def read_from(offset):
            stream.seek(offset)
            while chunk := stream.read(CHUNK):
                yield chunk

        # Another file in its place, or a shorter one, is kept from its start.
        if inode != info.st_ino or size > info.st_size:
            size = 0
        try:
            kept.keep(transcript, info.st_ino, size, read_from(size))
        except (transcript_store.Conflict, lzma.LZMAError):
            if not size:
                raise
            # A copy that cannot be kept on is made again.
            kept.keep(transcript, info.st_ino, 0, read_from(0))


def resumable(path):
    """The sessions of a checkout on this machine that can be resumed: those ``sessions``
    lists, and the archived ones whose file the store kept (``restore``)."""
    listed = sessions(path)
    live = {session["id"] for session in listed}
    return [
        session
        for session in with_kept(listed, "", Path(path).resolve())
        if session["id"] in live or session.get("restorable")
    ]


def restore(session):
    """Write an archived session's file back where its agent kept it, from the copy the
    store has of it; returns the file.

    Nothing resumes a session without its file, and Claude deletes one a month after it
    last changed. It is written back as it was kept, up to its last whole line, only into
    an agent's session store and never over a file that is there. It is a new file, so
    Claude keeps it another month.
    """
    file = Path(session["_file"])
    found = session_store(session["agent"], file)
    row = store().row("", str(file))
    if row is None or not row["raw_size"]:
        raise ValueError("No copy of that session's file was kept to resume it from")
    if found is None or not found.is_dir():
        raise ValueError("The agent's session store that session was in is gone")
    file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=file.parent, prefix=".restoring-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            size = end = 0
            try:
                for piece in store().kept(row["id"]):
                    stream.write(piece)
                    line = piece.rfind(b"\n")
                    if line >= 0:
                        end = size + line + 1
                    size += len(piece)
            except lzma.LZMAError as exc:
                raise ValueError("The copy kept of that session's file cannot be read") from exc
            if size != row["raw_size"] or not end:
                raise ValueError("The copy kept of that session's file is not whole")
            # A line the agent was still writing as the file was last kept is left out.
            stream.truncate(end)
            # On disk before it has a name: a file cut short by a power cut would be
            # kept in place of the copy.
            os.fsync(stream.fileno())
            inode = os.fstat(stream.fileno()).st_ino
        try:
            os.link(name, file)
        except FileExistsError:
            return file  # Written back by another request meanwhile.
    finally:
        os.unlink(name)
    store().restored(row["id"], inode, end)
    return file


def archive_remote(home, machine, path, stop=None):
    """Read the sessions a worker lists for its checkout ``path`` into the store."""

    def tail(**args):
        return probe(home, machine, "tail", **args)

    return archive(
        remote_sessions(home, machine, path),
        machine.id,
        lambda parser, file, save: parser.update_remote(tail, file, save),
        stop,
    )


class Archiver:
    """Keeps every session's transcript in the store, before its file is gone.

    Claude deletes a session file a month after it last changed, and a worker's go with
    its worktree's cleanup or the worker itself, so a transcript nobody opened here would
    be lost with them. A minute after the dashboard starts, and hourly after that, this
    reads on every session of this machine's stores, and those each worker lists for the
    checkouts ``checkouts()`` names as (machine, path) pairs: only the bytes added since
    the last sweep, and nothing of a session that has not grown. A worker's worktree is
    read once more as its cleanup removes it (``archive_remote``).
    """

    def __init__(self, home, checkouts=None):
        self.home = home
        self.checkouts: Callable[[], Iterable[tuple]] = checkouts or (lambda: [])
        self.stopping = threading.Event()
        self.worker: threading.Thread | None = None

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.worker = threading.Thread(target=self.run, name="transcript-archive", daemon=True)
        self.worker.start()

    def close(self):
        self.stopping.set()

    def run(self):
        wait = ARCHIVE_FIRST
        while not self.stopping.wait(wait):
            wait = ARCHIVE_EVERY
            try:
                self.sweep()
            except Exception:
                traceback.print_exc()

    def sweep(self):
        """One pass over this machine's sessions, then each worker's listed checkouts."""
        archive_local(self.stopping)
        failed: dict[str, int] = {}
        for machine, path in self.checkouts():
            if self.stopping.is_set():
                return
            # A worker that keeps giving no answer is not asked again this sweep: each
            # call would wait out its timeout.
            if failed.get(machine.id, 0) >= ARCHIVE_FAILURES:
                continue
            try:
                archive_remote(self.home, machine, path, self.stopping)
            except remote_probe.Unreachable:
                failed[machine.id] = failed.get(machine.id, 0) + 1
            except (OSError, ValueError, sqlite3.Error):
                pass  # This checkout only: removed since the inventory listed it, say.
