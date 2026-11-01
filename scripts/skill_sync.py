"""This machine's personal agent skills, kept the same on the workers that name them.

A worker's ``skills`` in machines.json names skills in this user's chezmoi source:
each is the target ``~/.claude/skills/<name>`` or ``~/.codex/skills/<name>`` there, and
``chezmoi archive`` renders it (a chezmoi symlink entry is followed to its own target in
the source, never through the file system). The files are re-packed into one
deterministic tar of folders and regular files, whose SHA-256 is the bundle's version.
Before every launch on such a worker, and after the dashboard updates its babysit-pr,
the worker's manifest (probe op ``skills_status``) is compared with it; a differing hash
or a missing or changed copy there stages the bundle and installs it (``skills_install``)
into every skills folder there. The probe runs this machine's own copy of
``remote_probe.py``, so what installs is never the worker's.

The chezmoi source (``~/.local/share/chezmoi``) is read-only to agents here, unlike
``~/.claude`` and ``~/.codex``, which Safehouse may grant them: so a skill reaches
workers only once it is in that source, and nothing an agent writes to an applied copy
does.
"""

import hashlib
import io
import os
import posixpath
import pwd
import stat
import subprocess
import tarfile
import threading
import time
import uuid

import machines
import owned_process
import remote_probe

# chezmoi by absolute path: the dashboard runs under launchd with a minimal PATH.
CHEZMOI = os.environ.get("BABYSIT_CHEZMOI") or "/opt/homebrew/bin/chezmoi"
CHEZMOI_TIMEOUT = 30
# Where chezmoi's targets are: this user's home from the password database, not $HOME.
HOME = None
# The skills folders a skill's target may be in, under the home.
TARGETS = (".claude/skills", ".codex/skills")
# A link from one skill target to another is followed this many times at most.
MAX_LINKS = 2
# A launch burst reuses one packing; a skill changed meanwhile goes out after this.
CACHE_SECONDS = 30
STAGE = remote_probe.SKILL_BUNDLES
STATUS_TIMEOUT = 30
STAGE_TIMEOUT = 120
INSTALL_TIMEOUT = 120
# A staged bundle the install never took (it failed to start) goes with the next one.
STAGE_KEEP = 3600

_lock = threading.Lock()
_bundles: dict[tuple, tuple[float, bytes, str]] = {}
_syncing: dict[str, threading.Lock] = {}
# The last sync's outcome per worker id, for the machine listing.
_results: dict[str, dict] = {}


class NotManaged(ValueError):
    """chezmoi's source has no such target."""


def _home():
    return HOME or pwd.getpwuid(os.getuid()).pw_dir


def _chezmoi(state):
    """chezmoi's command line up to its targets, pinned to this user's source.

    ``~/.config/chezmoi`` is left out: its config could name another source or add
    hooks, so chezmoi gets an empty one the dashboard keeps in ``<state>/chezmoi``
    (private, with chezmoi's state and cache), and the source ``~/.local/share/chezmoi``
    under this user's home from the password database.
    """
    if not os.path.isabs(CHEZMOI):
        raise ValueError(f"chezmoi must be named by an absolute path, not {CHEZMOI}")
    folder = os.path.join(os.path.abspath(state), "chezmoi")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    config = os.path.join(folder, "chezmoi.toml")
    try:
        os.close(os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    except FileExistsError:
        pass
    info = os.lstat(config)
    if not stat.S_ISREG(info.st_mode) or info.st_size or info.st_uid != os.getuid():
        raise ValueError(f"{config} must be an empty file of this user's")
    return [
        CHEZMOI,
        f"--config={config}",
        "--config-format=toml",
        f"--source={posixpath.join(_home(), '.local/share/chezmoi')}",
        f"--persistent-state={os.path.join(folder, 'chezmoistate.boltdb')}",
        f"--cache={os.path.join(folder, 'cache')}",
        "archive",
        "--format=tar",
        "--no-tty",
        "--no-pager",
        "--refresh-externals=never",
        "--",
    ]


def _archive(command, target):
    """``chezmoi archive`` of one target (``command`` from ``_chezmoi``), as tar members
    with their contents.

    chezmoi runs by absolute path, with stdin closed, a fixed environment (this user's
    home and the system PATH, nothing from the dashboard's), no TTY, no pager, externals
    not refreshed and a deadline: a template needing a password manager fails, and so
    does the sync.
    """
    argv = [*command, target]
    environment = {"HOME": _home(), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"}
    try:
        done = owned_process.run(
            argv,
            timeout=CHEZMOI_TIMEOUT,
            max_output=remote_probe.MAX_SKILL_BUNDLE,
            stdin=subprocess.DEVNULL,
            env=environment,
            cwd="/",
        )
    except subprocess.TimeoutExpired:
        raise ValueError(
            f"chezmoi did not render {target} within {CHEZMOI_TIMEOUT} seconds"
        ) from None
    except owned_process.OutputLimitExceeded:
        raise ValueError(
            f"chezmoi's {target} exceeds {remote_probe.MAX_SKILL_BUNDLE} bytes"
        ) from None
    except OSError as exc:
        raise ValueError(f"Cannot run chezmoi at {CHEZMOI}: {exc.strerror or exc}") from None
    said = remote_probe.CONTROL.sub(" ", done.stderr.decode(errors="replace")).strip()[-300:]
    if done.returncode and "not managed" in said:
        raise NotManaged(target)
    if done.returncode:
        raise ValueError(f"chezmoi could not render {target}: {said or f'exit {done.returncode}'}")
    try:
        with tarfile.open(fileobj=io.BytesIO(done.stdout), mode="r:") as archive:
            members = []
            for member in archive.getmembers():
                stream = archive.extractfile(member) if member.isreg() else None
                members.append((member, stream.read() if stream else b""))
    except (tarfile.TarError, EOFError) as exc:
        raise ValueError(f"chezmoi gave no tar for {target}: {exc}") from None
    return members


def _read_skill(command, name):
    """Skill ``name`` from the chezmoi source as tar entries; ValueError naming why not."""
    home = _home()
    for folder in TARGETS:
        target = posixpath.join(home, folder, name)
        try:
            return _rendered(command, name, target, _archive(command, target), MAX_LINKS)
        except NotManaged:
            continue
    raise ValueError(
        f"{name} is not in your chezmoi source (as ~/.claude/skills/{name} or "
        f"~/.codex/skills/{name}); add it with chezmoi add"
    )


def _rendered(command, name, target, members, links):
    """Skill ``name`` from chezmoi's archive of ``target``: its folders and regular files,
    a link to another skill target followed through chezmoi again; ValueError otherwise."""
    home = _home()
    prefix = posixpath.relpath(target, home)
    shown = f"~/{prefix}"
    if len(members) == 1 and members[0][0].name == prefix and members[0][0].issym():
        link = members[0][0].linkname
        path = posixpath.normpath(posixpath.join(posixpath.dirname(target), link))
        if not links or not any(path.startswith(f"{home}/{f}/") for f in TARGETS):
            raise ValueError(
                f"{shown} links to {link}, which is not a skill in your chezmoi source"
            )
        try:
            return _rendered(command, name, path, _archive(command, path), links - 1)
        except NotManaged:
            raise ValueError(
                f"{shown} links to {link}, which is not in your chezmoi source"
            ) from None
    entries = [(name, True, False, b"")]
    budget = {"entries": remote_probe.MAX_SKILL_ENTRIES, "bytes": remote_probe.MAX_SKILL_BYTES}
    for member, content in members:
        if member.name == prefix:
            if not member.isdir():
                raise ValueError(f"{shown} is not a folder in your chezmoi source")
            continue
        if not member.name.startswith(prefix + "/"):
            raise ValueError(f"chezmoi's archive of {shown} holds {member.name!r}")
        path = member.name[len(prefix) + 1 :]
        if set(path.split("/")) & remote_probe.SKILL_IGNORED:
            continue  # As the worker skips them when it reads its copies.
        label = f"{shown}/{path}"
        budget["entries"] -= 1
        if budget["entries"] < 0:
            raise ValueError(f"{shown} holds more than {remote_probe.MAX_SKILL_ENTRIES} entries")
        if member.issym():
            raise ValueError(f"{label} is a symbolic link; skills are synced without links")
        if member.isdir():
            entries.append((f"{name}/{path}", True, False, b""))
            continue
        if not member.isreg():
            raise ValueError(f"{label} is neither a folder nor a regular file")
        budget["bytes"] -= len(content)
        if budget["bytes"] < 0:
            raise ValueError(f"{shown} exceeds {remote_probe.MAX_SKILL_BYTES} bytes")
        entries.append((f"{name}/{path}", False, bool(member.mode & 0o100), content))
    if (f"{name}/SKILL.md", False) not in {(path, is_dir) for path, is_dir, *_ in entries}:
        raise ValueError(f"{shown} is not a skill folder with a SKILL.md")
    return entries


def pack(names, state):
    """The bundle of skills ``names`` and its SHA-256; ValueError naming what is wrong.

    Entries are sorted (each folder before its contents), owned by nobody, dated 1970
    and keep only the executable bit, so the same skills always give the same bytes.
    """
    command = _chezmoi(state)
    entries = []
    for name in sorted(names):
        entries += _read_skill(command, name)
    entries.sort(key=lambda entry: entry[0].split("/"))
    if len(entries) > remote_probe.MAX_SKILL_ENTRIES:
        raise ValueError(f"The skills hold more than {remote_probe.MAX_SKILL_ENTRIES} entries")
    if sum(len(data) for *_, data in entries) > remote_probe.MAX_SKILL_BYTES:
        raise ValueError(f"The skills exceed {remote_probe.MAX_SKILL_BYTES} bytes")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path, is_dir, executable, data in entries:
            info = tarfile.TarInfo(path)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if is_dir:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                archive.addfile(info)
            else:
                info.size, info.mode = len(data), 0o755 if executable else 0o644
                archive.addfile(info, io.BytesIO(data))
    data = buffer.getvalue()
    # Refused here exactly as the worker would refuse it (a control character in a name).
    remote_probe.skill_entries(data)
    if len(data) > remote_probe.MAX_SKILL_BUNDLE:
        raise ValueError(f"The skill bundle exceeds {remote_probe.MAX_SKILL_BUNDLE} bytes")
    return data, hashlib.sha256(data).hexdigest()


def bundle(names, state, now=time.monotonic):
    """``pack(names, state)``, reused for ``CACHE_SECONDS``."""
    key = (CHEZMOI, _home(), tuple(sorted(names)))
    with _lock:
        cached = _bundles.get(key)
        if cached and now() - cached[0] < CACHE_SECONDS:
            return cached[1], cached[2]
    data, digest = pack(names, state)
    with _lock:
        _bundles[key] = (now(), data, digest)
    return data, digest


def root_key(agent, account=None):
    """The worker's skills folder a launch reads (``remote_probe.SKILL_ROOT``)."""
    if agent == "codex":
        return "codex"
    return "claude" if account in (None, "", "default") else f"claude:{account}"


def _probe(home, m, op, timeout, **args):
    try:
        return remote_probe.call(home, m, op, timeout, **args)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{m.label} did not answer within {timeout} seconds") from exc
    except OSError as exc:
        raise ValueError(f"{m.label} could not be reached: {exc}") from exc


def ensure(home, m, agent=None, account=None):
    """Bring worker ``m``'s skills to this machine's; its answer, None without ``skills``.

    Raises ValueError, naming the worker, when the sync fails, or when it leaves the
    skills folder the launch reads (``agent`` and Claude ``account``) without the
    bundle; another folder it could not write is only reported. Callers decide whether
    that stops the launch.
    """
    if m is None or m.skills is None:
        return None
    own = None if agent is None else root_key(agent, account)
    with _lock:
        syncing = _syncing.setdefault(m.id, threading.Lock())
    with syncing:
        try:
            data, digest = bundle(m.skills, home)
            state = _probe(home, m, "skills_status", STATUS_TIMEOUT)
            # A folder that cannot be opened or made is not helped by installing again.
            if state["bundle"] != digest or state["stale"]:
                staged = machines.put_file(
                    home,
                    m,
                    machines.remote_path(STAGE),
                    f"{digest[:16]}-{uuid.uuid4().hex[:8]}.tar",
                    data,
                    keep=STAGE_KEEP,
                    timeout=STAGE_TIMEOUT,
                )
                state = _probe(
                    home, m, "skills_install", INSTALL_TIMEOUT, path=staged, sha256=digest
                )
            if state["bundle"] != digest:
                raise ValueError("the bundle did not install")
            if own is not None and own not in state["roots"]:
                raise ValueError(f"it has no skills folder for {own}")
            if own is not None and state["roots"][own]:
                raise ValueError(state["roots"][own])
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            error = f"Cannot sync skills to {m.label}: {exc}"
            with _lock:
                _results[m.id] = {"error": error, "checked_at": time.time()}
            raise ValueError(error) from exc
        with _lock:
            _results[m.id] = {**state, "error": None, "checked_at": time.time()}
        return state


def status(m, home):
    """Worker ``m``'s skills as last seen, for the machine listing; None without ``skills``.

    ``state`` is ``in_sync`` (its last answer has this machine's current bundle in every
    skills folder), ``partial`` (some folders lack it: ``problems`` says why),
    ``out_of_date``, ``error`` (the last sync failed, or the skills cannot be packed
    here) or ``unknown`` (not checked since the dashboard started).
    """
    if m.skills is None:
        return None
    with _lock:
        last = dict(_results.get(m.id) or {})
    problems = {key: said for key, said in (last.get("roots") or {}).items() if said}
    shown = {
        "state": "unknown",
        "error": last.get("error"),
        "problems": problems,
        "skipped": last.get("skipped", []),
        "checked_at": last.get("checked_at"),
    }
    try:
        _, digest = bundle(m.skills, home)
    except (OSError, ValueError) as exc:
        return {**shown, "state": "error", "error": f"Cannot pack skills here: {exc}"}
    if last.get("error"):
        shown["state"] = "error"
    elif last:
        current = last.get("bundle") == digest
        shown["state"] = "out_of_date" if not current else "partial" if problems else "in_sync"
    return shown
