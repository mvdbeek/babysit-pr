"""Dashboard credential profiles and delivery to a worker through SSH stdin."""

import json
import os
import re
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

import machines
import worker_credentials

PROFILES = "credential-profiles.json"
# A part may hold an email address, as in accounts/example.org/user+ci@example.org.
ENTRY_PART = r"[A-Za-z0-9_][A-Za-z0-9_.@+-]*"
ENTRY = re.compile(rf"{ENTRY_PART}(?:/{ENTRY_PART})*")
ENTRY_CHARACTER = re.compile(r"[A-Za-z0-9_.@+/-]")
FORMATS = ("password", "entry")
_lock = threading.Lock()


class CredentialFailed(ValueError):
    """A sanitized failure safe to show in the dashboard."""


def profiles(home: Path) -> dict[str, dict[str, str]]:
    try:
        saved = json.loads((home / PROFILES).read_text())
        if not isinstance(saved, dict) or any(
            not isinstance(profile, dict)
            or set(profile) - {"name", "entry", "format"}
            or not {"name", "entry"} <= set(profile)
            or not isinstance(profile["name"], str)
            or not isinstance(profile["entry"], str)
            or not ENTRY.fullmatch(profile["entry"])
            or profile.get("format", "password") not in FORMATS
            for profile in saved.values()
        ):
            raise ValueError
        return saved
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise CredentialFailed("Could not read credential profiles") from None


def update(home: Path, request: dict) -> dict[str, dict[str, str]]:
    with _lock:
        saved = profiles(home)
        if set(request) == {"remove"} and isinstance(request["remove"], str):
            if saved.pop(request["remove"], None) is None:
                raise ValueError("That credential profile no longer exists")
        elif set(request) in ({"name", "entry"}, {"name", "entry", "format"}):
            name, entry = request["name"], request["entry"]
            if not isinstance(name, str) or not (1 <= len(name.strip()) <= 80):
                raise ValueError("Enter a profile name of up to 80 characters")
            if not isinstance(entry, str) or not (1 <= len(entry) <= 255):
                raise ValueError("Enter a gopass entry name of up to 255 characters")
            if not ENTRY.fullmatch(entry):
                raise ValueError(entry_problem(entry))
            kind = request.get("format", "password")
            if kind not in FORMATS:
                raise ValueError("Choose password line or entire entry")
            saved[uuid.uuid4().hex] = {"name": name.strip(), "entry": entry, "format": kind}
        else:
            raise ValueError("Supply a profile name and gopass entry, or a profile to remove")
        temporary = None
        try:
            # The auth home is private to the host. Replace the metadata atomically.
            with tempfile.NamedTemporaryFile(dir=home, mode="w", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(saved, handle)
            os.replace(temporary, home / PROFILES)
        except OSError:
            raise CredentialFailed("Could not save credential profiles") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return saved


def entry_problem(entry: str) -> str:
    bad = next((c for c in entry if not ENTRY_CHARACTER.fullmatch(c)), None)
    if bad is not None:
        shown = repr(bad) if not bad.isprintable() or bad.isspace() else f"“{bad}”"
        return f"A gopass entry name cannot contain {shown}"
    return (
        "Each part of a gopass entry name must start with a letter, digit or _, as in galaxy/main"
    )


def read(profile: dict[str, str]) -> bytes:
    env = os.environ.copy()
    for name in ("GOPASS_DEBUG", "GOPASS_DEBUG_LOG", "GOPASS_DEBUG_VERBOSE"):
        env.pop(name, None)
    password = profile.get("format", "password") == "password"
    flags = ["--password"] if password else ["--unsafe", "--noparsing"]
    try:
        entries = subprocess.run(
            ["gopass", "list", "--flat"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
        ).stdout.splitlines()
        if profile["entry"].encode() not in entries:
            raise CredentialFailed("The named gopass entry does not exist on the dashboard host")
        result = subprocess.run(
            ["gopass", "show", *flags, "--alsoclip=false", "--nosync", "--", profile["entry"]],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=True,
        )
    except FileNotFoundError:
        raise CredentialFailed(
            "Install gopass on the dashboard host and make it available on PATH"
        ) from None
    except (OSError, subprocess.SubprocessError):
        raise CredentialFailed("Could not read the gopass entry on the dashboard host") from None
    data = result.stdout.rstrip(b"\r\n") if password else result.stdout
    if not 0 < len(data) <= worker_credentials.MAX_SECRET or (password and b"\n" in data):
        raise CredentialFailed("The gopass entry is empty, has the wrong format, or exceeds 64 KiB")
    return data


def worker_call(home, machine, action, pane, session, grant, minutes, data=None):
    python = machine.python
    if python.startswith(("/", "~")):
        python = machines.remote_path(python)
    script = machines.remote_path(f"{machine.babysit_root}/scripts/worker_credentials.py")
    try:
        result = machines.run(
            home,
            machine,
            [python, "-I", "-S", script, action, pane, session, grant, str(minutes)],
            input=data,
            timeout=12,
            max_output=1024,
        )
        if result.returncode == 0 and json.loads(result.stdout) == {"ok": True}:
            return
    except subprocess.TimeoutExpired:
        raise CredentialFailed(
            "The worker did not confirm the credential grant operation"
        ) from None
    except (OSError, ValueError, subprocess.SubprocessError):
        raise CredentialFailed("Could not reach the worker’s credential service") from None
    # Neither stdout nor stderr from the credential command should be included in errors.
    raise CredentialFailed(worker_credentials.UNAVAILABLE)
