"""Registry of worker machines and the one place that builds herdr and ssh argv for them.

``<home>/machines.json`` lists the machines other than this one. No registry, or a
``None`` machine, means local: every helper then behaves exactly as the direct call
it replaces. Remote paths (``src``, ``cwd``) are strings for the remote shell and are
never resolved or expanded on this machine. Pass ones that may start with ``~``
(``python``, ``src``, ``babysit_root``) as argv elements through ``remote_path``.
"""

import functools
import hashlib
import ipaddress
import json
import os
import re
import shlex
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

import owned_process

ID = re.compile(r"[a-z][a-z0-9-]{0,23}")
# At most one @, and a leading letter or digit on the user and the host, keeps the
# target from ever being read as an ssh option.
SSH_TARGET = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9._:-]*")
WORKSPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
CAPABILITY = re.compile(r"[a-z0-9][a-z0-9:._-]{0,63}")
COMMAND = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
OSES = {"darwin", "linux"}
FIELDS = {
    "id",
    "label",
    "ssh",
    "os",
    "src",
    "babysit_root",
    "python",
    "max_agents",
    "labels",
    "collie_url",
    "enabled",
    "tailnet_ip",
}
REQUIRED = {"id", "label", "ssh", "os"}
# sshd's MaxSessions defaults to 10; stay well below it on one multiplexed connection.
CHANNELS = 4
# macOS sun_path is 104 bytes including the NUL; ssh hashes %C to 40 hex characters
# and appends a 17-character random suffix while it creates the master socket.
SOCKET_LIMIT = 103 - 1 - 40 - 17


class Machine(NamedTuple):
    id: str
    label: str
    ssh: str
    os: str
    src: str
    babysit_root: str
    python: str
    max_agents: int
    labels: tuple[str, ...]
    collie_url: str | None
    enabled: bool
    # Where its registrations reach the dashboard from, as `tailscale serve` reports it.
    tailnet_ip: str | None = None


class RemotePath(str):
    """A path for the remote machine: ``~`` expands to the remote home, not this one."""


# Keyed by ssh target (ControlPath multiplexes per target, not per machine id). The
# limit is per process; several supervisor processes each get their own.
_channels: dict[str, threading.BoundedSemaphore] = {}
_channels_lock = threading.Lock()


def _collie_url(value):
    # The dashboard puts it in links: an http(s) URL with a host, and nothing that could
    # leave an attribute or start markup.
    if not isinstance(value, str) or len(value) > 512 or not value.isprintable():
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return (
        parts.scheme in {"http", "https"}
        and bool(parts.hostname)
        and not any(c in value for c in " \"'<>`\\")
    )


def _ip(value):
    try:
        return isinstance(value, str) and str(ipaddress.ip_address(value)) == value
    except ValueError:
        return False


def _remote_path(value):
    return (
        isinstance(value, str)
        and (value == "~" or value.startswith(("/", "~/")))
        and not any(c in value for c in "\0\n\r")
        and len(value) <= 1024
    )


def remote_path(path) -> RemotePath:
    """Mark ``path`` (absolute or ``~``-relative) as a remote path for ``remote_argv``/``run``."""
    if not _remote_path(path):
        raise ValueError("Remote path must be absolute or start with ~")
    return RemotePath(path)


def _machine(raw):
    if not isinstance(raw, dict):
        raise ValueError("Each machine must be a JSON object")
    if unknown := set(raw) - FIELDS:
        raise ValueError(f"Unknown machine keys: {', '.join(sorted(unknown))}")
    if missing := REQUIRED - set(raw):
        raise ValueError(f"Machine is missing {', '.join(sorted(missing))}")
    machine = Machine(
        id=raw["id"],
        label=raw["label"],
        ssh=raw["ssh"],
        os=raw["os"],
        src=raw.get("src", "~/src"),
        babysit_root=raw.get("babysit_root", "~/src/babysit-pr"),
        python=raw.get("python", "python3"),
        max_agents=raw.get("max_agents", 4),
        labels=raw.get("labels", []),
        collie_url=raw.get("collie_url"),
        enabled=raw.get("enabled", True),
        tailnet_ip=raw.get("tailnet_ip"),
    )
    name = machine.id if isinstance(machine.id, str) else "?"
    checks = [
        (isinstance(machine.id, str) and ID.fullmatch(machine.id), "id"),
        (machine.id not in {"local", "auto"}, "id (local and auto are reserved)"),
        (
            isinstance(machine.label, str)
            and 0 < len(machine.label) <= 64
            and machine.label.isprintable()
            and not machine.label.startswith("-"),
            "label",
        ),
        (
            isinstance(machine.ssh, str)
            and len(machine.ssh) <= 255
            and SSH_TARGET.fullmatch(machine.ssh),
            "ssh",
        ),
        (isinstance(machine.os, str) and machine.os in OSES, "os"),
        (_remote_path(machine.src), "src"),
        (_remote_path(machine.babysit_root), "babysit_root"),
        (
            isinstance(machine.python, str)
            and (COMMAND.fullmatch(machine.python) or _remote_path(machine.python)),
            "python",
        ),
        (
            type(machine.max_agents) is int and 1 <= machine.max_agents <= 64,
            "max_agents",
        ),
        (
            isinstance(machine.labels, list)
            and len(machine.labels) <= 32
            and all(isinstance(x, str) and CAPABILITY.fullmatch(x) for x in machine.labels),
            "labels",
        ),
        (machine.collie_url is None or _collie_url(machine.collie_url), "collie_url"),
        (isinstance(machine.enabled, bool), "enabled"),
        (machine.tailnet_ip is None or _ip(machine.tailnet_ip), "tailnet_ip"),
    ]
    for ok, field in checks:
        if not ok:
            raise ValueError(f"Invalid {field} for machine {name}")
    return machine._replace(labels=tuple(machine.labels))


def _repos(raw, ids):
    if not isinstance(raw, dict) or len(raw) > 500:
        raise ValueError("Configure repos as {owner/repo: {requires, prefer}}")
    repos = {}
    for slug, rule in raw.items():
        if not REPO.fullmatch(slug) or not isinstance(rule, dict):
            raise ValueError(f"Invalid repos entry {slug}")
        if unknown := set(rule) - {"requires", "prefer"}:
            raise ValueError(f"Unknown keys for {slug}: {', '.join(sorted(unknown))}")
        requires = rule.get("requires", [])
        if not isinstance(requires, list) or not all(
            isinstance(x, str) and CAPABILITY.fullmatch(x) for x in requires
        ):
            raise ValueError(f"Invalid requires for {slug}")
        prefer = rule.get("prefer")
        if prefer is not None and not isinstance(prefer, str):
            raise ValueError(f"Invalid prefer for {slug}")
        if prefer is not None and prefer not in ids:
            raise ValueError(f"{slug} prefers unknown machine {prefer}")
        repos[slug] = {"requires": requires, "prefer": prefer}
    return repos


def load_config(home):
    """Validated registry, None when absent. Raises ValueError when malformed.

    Ids, labels and ssh targets must be unique; aliases of one host (``user@host``
    and ``host``, or two names for one address) are not detected.
    """
    path = Path(home) / "machines.json"
    if not path.exists():
        return None
    if path.stat().st_size > 65536:
        raise ValueError("Machine registry exceeds size limit")
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("Machine registry must be a JSON object")
    if unknown := set(raw) - {"version", "machines", "repos"}:
        raise ValueError(f"Unknown registry keys: {', '.join(sorted(unknown))}")
    if type(raw.get("version")) is not int or raw["version"] != 1:
        raise ValueError("Machine registry version must be 1")
    entries = raw.get("machines", [])
    if not isinstance(entries, list) or len(entries) > 64:
        raise ValueError("Configure machines as a list of at most 64 entries")
    machines: dict[str, Machine] = {}
    labels = set()
    targets = set()
    for entry in entries:
        machine = _machine(entry)
        if machine.id in machines:
            raise ValueError(f"Duplicate machine id {machine.id}")
        if machine.label in labels:
            raise ValueError(f"Duplicate machine label {machine.label}")
        if machine.ssh in targets:
            raise ValueError(f"Duplicate machine ssh target {machine.ssh}")
        machines[machine.id] = machine
        labels.add(machine.label)
        targets.add(machine.ssh)
    return {"machines": machines, "repos": _repos(raw.get("repos", {}), machines)}


def load(home) -> dict[str, Machine]:
    """Registered machines by id; empty without a registry."""
    config = load_config(home)
    return config["machines"] if config else {}


def resolve(home, machine_id: str | None, *, placing=False) -> Machine | None:
    """The machine a record names; None for local.

    Disabled machines still resolve so pinned records keep working, but are refused
    for new placements.
    """
    if machine_id is None:
        return None
    machine = load(home).get(machine_id)
    if machine is None:
        raise ValueError(f"Unknown machine {machine_id}")
    if placing and not machine.enabled:
        raise ValueError(f"Machine {machine_id} is disabled")
    return machine


def herdr_argv(m: Machine | None, *args) -> list[str]:
    # herdr addresses saved machines by their label, not our id.
    # The = form (labels never start with -) keeps the label from reading as an option.
    return ["herdr", *args] if m is None else ["herdr", f"--machine={m.label}", *args]


def _private_dir(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ValueError(f"{path} must be a private directory owned by this user")
    return path


@functools.cache
def _control_dir(home: Path) -> Path:
    candidates = [home / "ssh"]
    # Deep homes fall back to the temporary directory; macOS's $TMPDIR is itself
    # long, so /tmp is the last resort. The digest keeps separate homes apart.
    name = f"babysit-ssh-{os.getuid()}-{hashlib.sha256(str(home).encode()).hexdigest()[:8]}"
    candidates += [Path(tempfile.gettempdir()) / name, Path("/tmp") / name]
    for path in candidates:
        if len(os.fsencode(path)) <= SOCKET_LIMIT:
            if path == home / "ssh":
                home.mkdir(mode=0o700, parents=True, exist_ok=True)
            return _private_dir(path)
    raise ValueError("No ssh control socket directory fits the socket path limit")


def control_dir(home) -> Path:
    """Directory for ssh master sockets, short enough for macOS's socket path limit.

    Checked once per home and process; ``control_dir.cache_clear()`` forces a recheck.
    """
    return _control_dir(Path(home))


control_dir.cache_clear = _control_dir.cache_clear  # type: ignore[attr-defined]


def ssh_options(home) -> list[str]:
    options = {
        "BatchMode": "yes",
        "ConnectTimeout": "5",
        "ServerAliveInterval": "10",
        "ControlMaster": "auto",
        "ControlPersist": "300",
        "ControlPath": f"{control_dir(home)}/%C",
        # Whatever the user's ssh config says, a compromised worker never reaches this
        # machine's ssh agent or ports.
        "ForwardAgent": "no",
        "ClearAllForwardings": "yes",
    }
    return [arg for key, value in options.items() for arg in ("-o", f"{key}={value}")]


def _quote_path(path):
    # "$HOME" rather than ~ so a quoted remainder still lands in the remote home.
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def remote_argv(home, m: Machine, argv, *, cwd=None) -> list[str]:
    """ssh argv running ``argv`` on ``m`` under ``sh -c``; ``RemotePath`` elements expand ``~`` remotely.

    sshd runs the command string through the worker's login shell, whose quoting rules
    differ (tcsh expands ``!`` and rejects newlines inside single quotes; fish treats
    ``\\'`` there as an escape). So that shell only ever sees a fixed wrapper around
    octal escapes, which sh's own printf turns back into the command line.
    """
    command = " ".join(
        _quote_path(x) if isinstance(x, RemotePath) else shlex.quote(x) for x in argv
    )
    if cwd is not None:
        if not _remote_path(cwd):
            raise ValueError("Remote working directory must be absolute or start with ~")
        command = f"cd -- {_quote_path(cwd)} && {command}"
    if "\0" in command:
        raise ValueError("Remote commands cannot contain NUL")
    escaped = "".join(f"\\{byte:03o}" for byte in command.encode())
    return ["ssh", *ssh_options(home), m.ssh, "--", f'sh -c \'eval "$(printf "{escaped}")"\'']


def _channel(m: Machine):
    with _channels_lock:
        return _channels.setdefault(m.ssh, threading.BoundedSemaphore(CHANNELS))


def run(home, m: Machine | None, argv, *, timeout, input=None, env=None, cwd=None, **kwargs):
    """owned_process.run on this machine, or over ssh on ``m``.

    Remote commands get the worker's own environment: ``env`` is refused rather than
    forwarding controller credentials. ssh exits 255 when the machine is unreachable.

    Other keyword arguments (``text``, ``pass_fds``) go to the local process.

    ``timeout`` covers waiting for an ssh channel plus the ssh client; exceeding it
    raises ``subprocess.TimeoutExpired``. Contract for remote callers: a timeout kills
    only the local ssh client. The remote process may keep running (there is no remote
    ``timeout`` dependency), so long-running remote work must carry its own lifeline.
    """
    if m is None:
        argv = [os.path.expanduser(x) if isinstance(x, RemotePath) else x for x in argv]
        return owned_process.run(argv, timeout=timeout, input=input, env=env, cwd=cwd, **kwargs)
    if env is not None:
        raise ValueError("Remote commands run with the worker's environment")
    started = time.monotonic()
    channel = _channel(m)
    if not channel.acquire(timeout=timeout):
        raise subprocess.TimeoutExpired(argv, timeout)
    # Without input, ssh must not read (and swallow) this process's own stdin.
    if input is None:
        kwargs.setdefault("stdin", subprocess.DEVNULL)
    try:
        remaining = None if timeout is None else max(0.0, timeout - (time.monotonic() - started))
        return owned_process.run(
            remote_argv(home, m, argv, cwd=cwd), timeout=remaining, input=input, **kwargs
        )
    finally:
        channel.release()


# A staged file's name: no path, no option, no hidden file.
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# Writes stdin to a new private file and prints its absolute path. The folder must be
# this user's own, writable by nobody else and not a symlink, before anything in it is
# removed or written. With ``keep``, older regular files in it are removed first.
PUT_FILE = (
    'umask 077 && mkdir -p -- "$1" || exit 1\n'
    'if [ -L "$1" ]; then echo "$1 is a symbolic link" >&2; exit 1; fi\n'
    'cd -P -- "$1" || exit 1\n'
    'if [ -z "$(find . -maxdepth 0 -user "$(id -u)" ! -perm -g+w ! -perm -o+w)" ]; then\n'
    '  echo "$1 is not a folder only this user can write" >&2; exit 1\n'
    "fi\n"
    'if [ -n "$3" ]; then find . -maxdepth 1 -type f -mmin "+$3" -exec rm -f {} + ; fi\n'
    'set -C && cat > "$2" && printf "%s/%s" "$(pwd -P)" "$2"'
)
# A terminal acts on these: never in a path a typed line will carry.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def put_file(home, m: Machine, directory, name, data: bytes, *, keep=None, timeout=30) -> str:
    """Write ``data`` to ``directory/name`` on ``m``: a new file only its owner reads.

    ``directory`` is a ``RemotePath`` (it may start with ``~``) and is made if missing.
    Returns the file's absolute path there; never overwrites one. ``keep`` (seconds)
    first removes regular files in the folder older than that.
    """
    if not isinstance(name, str) or not FILE_NAME.fullmatch(name):
        raise ValueError(f"Invalid staged file name {name!r}")
    if not isinstance(directory, RemotePath):
        directory = remote_path(directory)
    minutes = "" if keep is None else str(max(1, int(keep) // 60))
    result = run(
        home, m, ["sh", "-c", PUT_FILE, "sh", directory, name, minutes], timeout=timeout, input=data
    )
    path = result.stdout.decode(errors="replace")
    if result.returncode or not path.startswith("/") or CONTROL.search(path):
        detail = result.stderr.decode(errors="replace").strip()[-300:]
        raise ValueError(f"Could not stage {name} on {m.label}: {detail or 'no path returned'}")
    return path


def qualify(machine_id: str | None, wid: str) -> str:
    """API/DOM workspace id: ``w111`` locally, ``mini/w111`` on a worker."""
    if not WORKSPACE.fullmatch(wid):
        raise ValueError(f"Invalid workspace id {wid!r}")
    if machine_id is None:
        return wid
    if not ID.fullmatch(machine_id) or machine_id == "local":
        raise ValueError(f"Invalid machine id {machine_id!r}")
    return f"{machine_id}/{wid}"


def split(qid: str) -> tuple[str | None, str]:
    if "/" not in qid:
        return None, qualify(None, qid)
    machine_id, _, wid = qid.rpartition("/")
    qualify(machine_id, wid)
    return machine_id, wid
