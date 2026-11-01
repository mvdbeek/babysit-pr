"""This machine's personal agent skills, kept the same on the workers that name them.

A worker's ``skills`` in machines.json names skills under this user's
``~/.claude/skills`` (a top-level link is followed, as chezmoi makes them). They are
packed into one deterministic tar of folders and regular files, whose SHA-256 is the
bundle's version. Before every launch on such a worker, and after the dashboard updates
its babysit-pr, the worker's manifest (probe op ``skills_status``) is compared with it;
a differing hash or a missing or changed copy there stages the bundle and installs it
(``skills_install``) into every skills folder there. The probe runs this machine's own
copy of ``remote_probe.py``, so what installs is never the worker's.
"""

import hashlib
import io
import subprocess
import tarfile
import threading
import time
import uuid
from pathlib import Path

import machines
import remote_probe

# Where this machine's skills are; tests point it elsewhere.
SOURCE = "~/.claude/skills"
# A launch burst reuses one packing; a skill edited meanwhile goes out after this.
CACHE_SECONDS = 30
# Left out of a bundle: caches and metadata that change without the skill changing.
IGNORED = {".DS_Store", "__pycache__", ".git"}
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


def _entries(name, top):
    """Skill ``name`` (at ``top``, its real folder) as tar entries, parents first."""
    found = [(name, True, False, b"")]

    def walk(folder, prefix):
        for child in sorted(folder.iterdir(), key=lambda path: path.name):
            if child.name in IGNORED:
                continue
            shown = f"{prefix}/{child.name}"
            info = child.lstat()
            if child.is_symlink():
                raise ValueError(f"{shown} is a symbolic link; skills are synced without links")
            if child.is_dir():
                found.append((shown, True, False, b""))
                walk(child, shown)
            elif child.is_file():
                found.append((shown, False, bool(info.st_mode & 0o100), child.read_bytes()))
            else:
                raise ValueError(f"{shown} is neither a folder nor a regular file")

    walk(top, name)
    return found


def pack(names, source=None):
    """The bundle of skills ``names`` and its SHA-256; ValueError naming what is wrong.

    Entries are sorted, owned by nobody, dated 1970 and keep only the executable bit, so
    the same skills always give the same bytes.
    """
    source = Path(source or SOURCE).expanduser()
    entries = []
    for name in sorted(names):
        try:
            top = (source / name).resolve(strict=True)
        except OSError:
            raise ValueError(f"No skill {name} in {source}") from None
        manual = top / "SKILL.md"
        if not top.is_dir() or manual.is_symlink() or not manual.is_file():
            raise ValueError(f"{source / name} is not a skill folder with a SKILL.md")
        entries += _entries(name, top)
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


def bundle(names, now=time.monotonic):
    """``pack(names)``, reused for ``CACHE_SECONDS``."""
    key = (str(Path(SOURCE).expanduser()), tuple(sorted(names)))
    with _lock:
        cached = _bundles.get(key)
        if cached and now() - cached[0] < CACHE_SECONDS:
            return cached[1], cached[2]
    data, digest = pack(names)
    with _lock:
        _bundles[key] = (now(), data, digest)
    return data, digest


def _probe(home, m, op, timeout, **args):
    try:
        return remote_probe.call(home, m, op, timeout, **args)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{m.label} did not answer within {timeout} seconds") from exc
    except OSError as exc:
        raise ValueError(f"{m.label} could not be reached: {exc}") from exc


def ensure(home, m):
    """Bring worker ``m``'s skills to this machine's; its answer, None without ``skills``.

    Raises ValueError, naming the worker, when it cannot: a launch then fails rather
    than run an agent whose skills are missing or stale.
    """
    if m is None or m.skills is None:
        return None
    with _lock:
        syncing = _syncing.setdefault(m.id, threading.Lock())
    with syncing:
        try:
            data, digest = bundle(m.skills)
            state = _probe(home, m, "skills_status", STATUS_TIMEOUT)
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
                if state["bundle"] != digest or state["stale"]:
                    raise ValueError("; ".join(state["stale"][:3]) or "the bundle did not install")
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            error = f"Cannot sync skills to {m.label}: {exc}"
            with _lock:
                _results[m.id] = {"error": error, "checked_at": time.time()}
            raise ValueError(error) from exc
        with _lock:
            _results[m.id] = {**state, "error": None, "checked_at": time.time()}
        return state


def status(m):
    """Worker ``m``'s skills as last seen, for the machine listing; None without ``skills``.

    ``state`` is ``in_sync`` (its last answer has this machine's current bundle),
    ``out_of_date``, ``error`` (the last sync failed, or the skills cannot be packed
    here) or ``unknown`` (not checked since the dashboard started).
    """
    if m.skills is None:
        return None
    with _lock:
        last = dict(_results.get(m.id) or {})
    shown = {
        "state": "unknown",
        "error": last.get("error"),
        "skipped": last.get("skipped", []),
        "checked_at": last.get("checked_at"),
    }
    try:
        _, digest = bundle(m.skills)
    except (OSError, ValueError) as exc:
        return {**shown, "state": "error", "error": f"Cannot pack skills here: {exc}"}
    if last.get("error"):
        shown["state"] = "error"
    elif last:
        current = last.get("bundle") == digest and not last.get("stale")
        shown["state"] = "in_sync" if current else "out_of_date"
    return shown
