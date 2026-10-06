"""Read-only diff and agent transcript views for a workspace checkout.

Requests name a herdr workspace (anything Collie can open) or a row of the workspace
inventory, never a path: the checkout comes from herdr or that row, the diff from Git in
it, and transcripts only from the agent session stores (Claude's per-directory project
folder, Codex's dated session folders) whose recorded working directory is that checkout.
Output is bounded, and nothing is written.
"""

import json
import os
import re
import selectors
import shlex
import subprocess
import threading
import time
from pathlib import Path

import claude_accounts
import github_cli
import owned_process
from pr_workspaces import herdr

# Branch names a topic branch is cut from, on the remotes pull requests target.
BASE_BRANCH = re.compile(r"^(main|master|dev|develop|trunk|next|release[_-].+|\d+\.\d+)$")
BASE_PATTERNS = (
    "main",
    "master",
    "dev",
    "develop",
    "trunk",
    "next",
    "release_*",
    "release-*",
    "[0-9]*",
)
BASE_REMOTES = ("upstream", "origin")
MAX_DIFF = 3 * 1024 * 1024
MAX_FILE_LINES = 4000
MAX_UNTRACKED = 200
MAX_COMMITS = 200
GIT_TIMEOUT = 30
WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,63}$")
SESSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z-]{7,63}$")
MAX_SESSIONS = 50
PAGE = 200
MAX_TEXT = 20000
MAX_TOOL = 4000
# Context Codex injects as user messages ahead of the typed prompt.
CODEX_CONTEXT = re.compile(r"^\s*(# AGENTS\.md instructions|<[a-z_]+>)")
# How a failed Codex tool call reads: the script runner's own verdict, or, in older
# formats without one, a non-zero exit code.
CODEX_VERDICT = re.compile(r"^Script (completed|failed|error)")
CODEX_EXIT = re.compile(r'"exit_code"\s*:\s*[1-9]|exited with code [1-9]')
# Claude records slash commands, `!` shell runs, their output and background-task
# notifications as user turns wrapped in tags; reminders it injects ride along inside them.
CLAUDE_WRAPPED = re.compile(r"<(command-|bash-|local-command-|task-notification>)")
CLAUDE_TAG = re.compile(r"<([a-z][a-z-]*)>(.*?)</\1>", re.S)
SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
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
# An open viewer polls every few seconds: session details are kept per file version, and
# parsed transcripts keep their read position so a growing file is read from where it
# stopped. One lock guards both, since requests arrive on separate threads.
_lock = threading.Lock()
_details: dict[str, tuple[tuple, dict | None]] = {}
DETAIL_FILES = 5000
_parsers: dict[str, "Transcript"] = {}
_checkouts: dict[str, tuple[float, str]] = {}
CHECKOUT_TTL = 30
PARSED_FILES = 4


TOKENS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


def local_environment():
    env = {k: v for k, v in os.environ.items() if k not in TOKENS}
    env.update(github_cli.NON_INTERACTIVE)
    return env


def git_output(path, *args, limit=None, timeout=GIT_TIMEOUT):
    """Run git in a checkout, keeping at most ``limit`` bytes; return (text, truncated)."""
    limit = MAX_DIFF if limit is None else limit
    # Unquoted paths: diff headers and file lists then carry names as they are.
    argv = [
        "git",
        "-C",
        str(path),
        "--no-optional-locks",
        "-c",
        "core.quotePath=false",
        # A checkout's own config must not start an fsmonitor daemon for a read.
        "-c",
        "core.fsmonitor=false",
        *args,
    ]
    chunks: list[bytes] = []
    size = 0
    truncated = False
    deadline = time.monotonic() + timeout
    with owned_process.command(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        # Local reads need no GitHub token, and filters or hooks must not inherit one.
        env=local_environment(),
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


def workspace_checkout(workspace_id):
    """The checkout a herdr workspace was opened on, the same one Collie shows.

    Remembered briefly: an open transcript asks again every few seconds.
    """
    if not isinstance(workspace_id, str) or not WORKSPACE_ID.match(workspace_id):
        raise ValueError("Supply a herdr workspace")
    with _lock:
        cached = _checkouts.get(workspace_id)
    if cached and time.monotonic() - cached[0] < CHECKOUT_TTL and Path(cached[1]).is_dir():
        return cached[1]
    root = resolve_workspace(workspace_id)
    with _lock:
        _checkouts[workspace_id] = (time.monotonic(), root)
        while len(_checkouts) > 200:
            _checkouts.pop(next(iter(_checkouts)), None)
    return root


def resolve_workspace(workspace_id):
    space = next(
        (
            w
            for w in herdr("workspace", "list")["workspaces"]
            if w.get("workspace_id") == workspace_id
        ),
        None,
    )
    if space is None:
        raise ValueError("That herdr workspace is not open; refresh")
    path = (space.get("worktree") or {}).get("checkout_path")
    if not path or not Path(path).is_dir():
        raise ValueError("That herdr workspace has no checkout to show")
    root = Path(path).resolve()
    if Path(git_text(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("That herdr workspace is not at the top of a Git checkout")
    return str(root)


def bases(path):
    """Remote base branches by how few commits this checkout has beyond each.

    One for-each-ref call counts them all (``ahead-behind`` needs Git 2.41): its second
    number is the commits HEAD has that the ref does not.
    """
    listing = git_text(
        path,
        "for-each-ref",
        "--format=%(refname:short) %(ahead-behind:HEAD)",
        *[f"refs/remotes/{remote}/{name}" for remote in BASE_REMOTES for name in BASE_PATTERNS],
    )
    found = []
    for line in listing.splitlines():
        parts = line.split(" ")
        if (
            len(parts) != 3
            or not (parts[1].isdigit() and parts[2].isdigit())
            or "/" not in parts[0]
        ):
            continue
        remote, name = parts[0].split("/", 1)
        if remote in BASE_REMOTES and BASE_BRANCH.match(name):
            # On a tie, the base that moved on least since, then the pull request target.
            found.append((int(parts[2]), int(parts[1]), BASE_REMOTES.index(remote), parts[0]))
    found.sort()
    return [{"ref": ref, "ahead": ahead} for ahead, _, _, ref in found]


def split_patch(text, truncated):
    """One entry per file of a unified diff, each bounded in lines."""
    files: list[dict] = []
    for block in re.split(r"(?m)^(?=diff --git )", text):
        if not block.startswith("diff --git "):
            continue
        # Only newlines end a diff line; splitlines would also split on form feeds.
        lines = block.removesuffix("\n").split("\n")
        header = lines[0]
        old = new = None
        status = "modified"
        binary = False
        for line in lines[1:]:
            if line.startswith("@@"):
                break
            if line.startswith(("--- ", "+++ ")):
                # Git ends a name that contains a space with a tab in these two lines.
                name = line[4:].removesuffix("\t")
                name = (
                    None if name == "/dev/null" else name[2:] if name[:2] in {"a/", "b/"} else name
                )
                if line.startswith("---"):
                    old = name
                else:
                    new = name
            elif line.startswith("new file mode"):
                status = "added"
            elif line.startswith("deleted file mode"):
                status = "deleted"
            elif line.startswith("rename from "):
                status, old = "renamed", line[len("rename from ") :]
            elif line.startswith("rename to "):
                new = line[len("rename to ") :]
            elif line.startswith("Binary files "):
                binary = True
        if old is None and new is None:
            # A pure mode change or binary file: take the names from the header.
            match = re.match(r"diff --git a/(.*) b/(.*)$", header)
            old = new = match[2] if match else header[len("diff --git ") :]
        start = next((i for i, line in enumerate(lines) if line.startswith("@@")), len(lines))
        body = lines[start:]
        added = sum(1 for line in body if line.startswith("+"))
        removed = sum(1 for line in body if line.startswith("-"))
        files.append(
            {
                "path": new or old,
                "old_path": old if status == "renamed" else None,
                "status": status,
                "binary": binary,
                "added": added,
                "removed": removed,
                "lines": body[:MAX_FILE_LINES],
                "truncated": len(body) > MAX_FILE_LINES,
            }
        )
    if truncated and files:
        files[-1]["truncated"] = True
    return files


def diff(path, scope="branch", base=None):
    """Changes in a checkout: against its base branch (with uncommitted work) or HEAD."""
    if scope not in {"branch", "uncommitted"}:
        raise ValueError("Choose the branch or uncommitted changes")
    found = bases(path) if scope == "branch" else []
    commits: list[dict] = []
    note = None
    if scope == "branch" and not found and base is None:
        scope = "uncommitted"
        note = "No base branch on the upstream or origin remote; showing uncommitted changes."
    if scope == "branch":
        if base is None:
            base = found[0]["ref"]
        elif base not in {b["ref"] for b in found}:
            raise ValueError("Choose one of the listed base branches")
        since = git_text(path, "merge-base", base, "HEAD")
        log = git_text(
            path,
            "log",
            "-z",
            f"--max-count={MAX_COMMITS}",
            "--format=%H%x00%an%x00%at%x00%s%x00%b",
            f"{since}..HEAD",
        )
        # NUL-delimited fields keep multiline bodies separate from other commits.
        fields = log.split("\0")
        for offset in range(0, len(fields) - 4, 5):
            sha, author, when, subject, body = fields[offset : offset + 5]
            commits.append(
                {
                    "sha": sha[:12],
                    "author": author,
                    "time": int(when or 0),
                    "subject": subject,
                    "body": body.rstrip("\n"),
                }
            )
    else:
        since = "HEAD"
    # The working tree against the starting point: committed and uncommitted together.
    text, truncated = git_output(
        path,
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--find-renames",
        # Fixed prefixes, whatever diff.noprefix or diff.mnemonicPrefix say.
        "--src-prefix=a/",
        "--dst-prefix=b/",
        since,
        "--",
    )
    # An untracked directory is listed once, not file by file.
    untracked = git_text(
        path, "ls-files", "--others", "--exclude-standard", "--directory", "--no-empty-directory"
    ).splitlines()
    files = split_patch(text, truncated)
    return {
        "scope": scope,
        "note": note,
        "base": base,
        "bases": found[:10],
        "commits": commits,
        "files": files,
        "untracked": untracked[:MAX_UNTRACKED],
        "untracked_more": max(0, len(untracked) - MAX_UNTRACKED),
        "added": sum(f["added"] for f in files),
        "removed": sum(f["removed"] for f in files),
        "truncated": truncated,
    }


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


def block_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in content
            if not isinstance(b, dict) or b.get("type") in {"text", "input_text", "output_text"}
        )
    return ""


def claude_prompt(text):
    """How a Claude user turn reads, as (kind, text), or None when it shows nothing.

    Kinds: "prompt" as typed, "command" for a slash command or `!` shell run, "output"
    for what one printed, and "notification" for a background task's report.
    """
    text = SYSTEM_REMINDER.sub("", text).strip()
    # Tags are parsed only in Claude's own wrappers: in a long typed prompt, unclosed
    # tags would each be searched for to its end.
    if not CLAUDE_WRAPPED.match(text):
        return ("prompt", text) if text else None
    tags = {m.group(1): m.group(2).strip() for m in CLAUDE_TAG.finditer(text)}
    if "command-name" in tags:
        return "command", " ".join(filter(None, (tags["command-name"], tags.get("command-args"))))
    if "bash-input" in tags:
        return "command", f"! {tags['bash-input']}"
    printed = [tags.get(k) for k in ("local-command-stdout", "bash-stdout", "bash-stderr")]
    if any(k in tags for k in ("local-command-stdout", "bash-stdout", "bash-stderr")):
        return ("output", "\n".join(filter(None, printed))) if any(printed) else None
    if "local-command-caveat" in tags:
        return None
    if "task-notification" in tags:
        inner = {
            m.group(1): m.group(2).strip() for m in CLAUDE_TAG.finditer(tags["task-notification"])
        }
        return "notification", inner.get(
            "summary"
        ) or f"Background task {inner.get('status', 'ended')}"
    return "prompt", text


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


def claude_text(item):
    content = (item.get("message") or {}).get("content")
    if isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    ):
        return ""
    return block_text(content).strip()


def records(stream):
    for line in stream:
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def claude_details(path):
    """Session ID, working directory and first prompt of a Claude transcript."""
    cwd = title = command = None
    with path.open(encoding="utf-8", errors="replace") as stream:
        for item in records(stream):
            if item.get("type") not in {"user", "assistant"} or item.get("isSidechain"):
                continue
            cwd = cwd or item.get("cwd")
            if item.get("type") == "user" and not item.get("isMeta"):
                view = claude_prompt(claude_text(item))
                if view and not item.get("isCompactSummary"):
                    # A typed prompt names the session; failing one, its first command.
                    if view[0] == "prompt":
                        title = view[1]
                        break
                    if view[0] == "command":
                        command = command or view[1]
    if not cwd:
        return None
    return {
        "agent": "claude",
        "id": path.stem,
        "cwd": str(Path(cwd).resolve()),
        "title": title or command,
    }


def codex_details(path):
    """Session ID, working directory and first typed prompt of a Codex rollout."""
    with path.open(encoding="utf-8", errors="replace") as stream:
        first = stream.readline()
        if not first.endswith("\n"):
            raise BlockingIOError  # Still being written; look again next time.
        try:
            meta = json.loads(first)
        except json.JSONDecodeError:
            return None
        payload = meta.get("payload") or {}
        if meta.get("type") != "session_meta" or not payload.get("id") or not payload.get("cwd"):
            return None
        title = None
        for line in stream:
            if '"user' not in line:  # user_message events and user-role items
                continue
            try:
                item = json.loads(line).get("payload") or {}
            except json.JSONDecodeError:
                continue
            if item.get("type") == "user_message":
                text = str(item.get("message") or "").strip()
            elif item.get("type") == "message" and item.get("role") == "user":
                text = block_text(item.get("content")).strip()
                text = "" if CODEX_CONTEXT.match(text) else text
            else:
                continue
            if text:
                title = text
                break
    return {
        "agent": "codex",
        "id": str(payload["id"]),
        "cwd": str(Path(payload["cwd"]).resolve()),
        "title": title,
    }


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
    try:
        found = claude_details(path) if agent == "claude" else codex_details(path)
    except BlockingIOError:
        return None  # Not cached: the first line is still being written.
    except (OSError, ValueError):
        found = None
    with _lock:
        _details.pop(key, None)
        _details[key] = (current, found)
        while len(_details) > DETAIL_FILES:
            _details.pop(next(iter(_details)), None)
    return found


def session_files(root):
    """Candidate transcript files: Claude folders for the checkout and its subdirectories,
    and every Codex rollout (each is matched on its recorded cwd)."""
    # Claude names a project folder after its cwd, every other character a dash.
    folder = re.sub(r"[^A-Za-z0-9]", "-", str(root))
    for home in claude_accounts.homes():
        try:
            folders = [
                entry
                for entry in (home / "projects").iterdir()
                if entry.name == folder or entry.name.startswith(folder + "-")
            ]
        except OSError:
            folders = []
        for entry in folders:
            for path in entry.glob("*.jsonl"):
                yield "claude", path
    codex = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    for path in codex.glob("*/*/*/rollout-*.jsonl"):
        yield "codex", path


def sessions(path):
    """Agent sessions recorded in this checkout or below it, newest first."""
    root = Path(path).resolve()
    listed = []
    for agent, file in session_files(root):
        found = details(file, agent)
        if (
            not found
            or not SESSION.match(found["id"])
            or not Path(found["cwd"]).is_relative_to(root)
        ):
            continue
        try:
            stat = file.stat()
        except OSError:
            continue
        title = (found["title"] or "").strip()
        listed.append(
            {
                "id": found["id"],
                "agent": agent,
                "updated": stat.st_mtime,
                "size": stat.st_size,
                "title": title.splitlines()[0][:160] if title else None,
                "_file": file,
            }
        )
        if agent == "claude":
            home = claude_accounts.transcript_home(file)
            account = claude_accounts.account_name(home) if home else None
            if home and (account is not None or home != claude_accounts.current_home()):
                listed[-1].update(
                    claude_config_dir=str(home),
                    claude_config_env=claude_accounts.config_environment(home),
                    claude_account=account,
                )
    listed.sort(key=lambda s: s["updated"], reverse=True)
    return listed[:MAX_SESSIONS]


class Transcript:
    """A session's conversation, parsed once and then only as its file grows."""

    def __init__(self, agent):
        self.agent = agent
        self.lock = threading.Lock()
        self.reset(None)

    def reset(self, inode):
        self.inode = inode
        self.offset = 0
        self.entries: list[dict] = []
        self.tools: dict[str, dict] = {}
        # Codex versions that record typed prompts as events also echo them as items.
        self.prompts: dict[str, set[str]] = {"event": set(), "item": set()}

    def update(self, path):
        stat = path.stat()
        # A replaced or truncated file is read again from the start.
        if stat.st_ino != self.inode or stat.st_size < self.offset:
            self.reset(stat.st_ino)
        with path.open("rb") as stream:
            stream.seek(self.offset)
            pending = b""
            while True:
                chunk = stream.read(CHUNK)
                if not chunk:
                    break
                pending += chunk
                end = pending.rfind(b"\n")
                if end < 0:
                    continue
                # Only complete lines: a line still being written is read next time.
                for line in pending[: end + 1].split(b"\n"):
                    if line.strip():
                        try:
                            item = json.loads(line)
                        except ValueError:
                            continue
                        self.feed(item)
                self.offset += end + 1
                pending = pending[end + 1 :]

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
        self.tools[key or ""] = tool
        self.entries.append(tool)

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
                    tool = self.tools.get(block.get("tool_use_id") or "")
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
            tool = self.tools.get(payload.get("call_id") or "")
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
            other.discard(text)
            return
        self.prompts[source].add(text)
        self.entries.append(
            {"role": "user", "kind": "prompt", "text": clip(text, MAX_TEXT), "time": when}
        )


def transcript(path, session=None, before=None, after=None):
    """One page of a session's conversation.

    The newest page by default; ``before`` pages back from an index, and ``after``
    returns what follows an index, so an open viewer fetches only what is new.
    """
    listed = sessions(path)
    if session is None:
        if not listed:
            return {"sessions": [], "session": None, "entries": [], "start": 0, "total": 0}
        chosen = listed[0]
    else:
        if not isinstance(session, str) or not SESSION.match(session):
            raise ValueError("Choose a listed session")
        chosen = next((s for s in listed if s["id"] == session), None)
        if chosen is None:
            raise ValueError("That session is not recorded for this workspace")
    key = str(chosen["_file"])
    with _lock:
        parser = _parsers.pop(key, None) or Transcript(chosen["agent"])
        _parsers[key] = parser
        while len(_parsers) > PARSED_FILES:
            _parsers.pop(next(iter(_parsers)), None)
    # A first parse of a large file takes a while; it holds only this transcript's lock.
    with parser.lock:
        parser.update(chosen["_file"])
        entries = parser.entries
        total = len(entries)
        if after is not None and 0 <= after <= total and total - after <= PAGE:
            start, end = after, total
        else:
            end = total if before is None else max(0, min(before, total))
            start = max(0, end - PAGE)
        page = [dict(e) for e in entries[start:end]]
    return {
        "sessions": [{k: v for k, v in s.items() if k != "_file"} for s in listed],
        "session": {k: v for k, v in chosen.items() if k != "_file"},
        "entries": page,
        "start": start,
        "total": total,
    }
