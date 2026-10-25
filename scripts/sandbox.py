"""Agent sandboxes outside the harness: grants in, a contained launch out.

Agents run with their own permission systems off (``--dangerously-skip-permissions``,
``--dangerously-bypass-approvals-and-sandbox``) inside an OS sandbox that babysit
starts, so supporting another harness only takes its launch argv. Backends:

- ``safehouse`` (macOS): Agent Safehouse. Headless repairs use the verified
  ``claude_safehouse.zsh`` launcher. Typed launches rely on the user's interactive
  ``safe``/``claude()``/``codex()`` shell functions, which read ``SAFE_ENABLE``,
  ``SAFEHOUSE_ADD_DIRS`` and ``SAFEHOUSE_ENV_PASS``; babysit only sets those.
- ``bwrap`` (Linux): ``babysit-bwrap``, a bubblewrap wrapper taking Safehouse's flags.
- ``none``: no sandbox, only for launches that explicitly opt out.

A caller may name the backend; otherwise ``$BABYSIT_SANDBOX`` may pin ``safehouse`` or
``bwrap`` (the tests pin Safehouse on every platform), and failing that it follows
``sys.platform``. The environment can never select ``none``: an agent sets the
environment of the launches it starts.
"""

import os
import shlex
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

SCRIPTS = Path(__file__).resolve().parent
BACKENDS = ("safehouse", "bwrap", "none")
# Launcher executables by file name, as they appear in a running agent's argv.
LAUNCHERS = {"safehouse": "safehouse", "babysit-bwrap": "bwrap"}
# Safehouse's names for the backend-neutral features; babysit-bwrap takes both.
SAFEHOUSE_FEATURES = {"docker": "docker", "playwright": "playwright-chrome"}
# The saved Claude permission mode of a job whose sandbox launcher owns permissions.
# Existing jobs store this value, so it keeps the name from before other backends.
LAUNCHER_PERMISSIONS = "safehouse"
# Repair marker, and git and gh failing fast instead of prompting in unattended repairs.
REPAIR_ENV = (
    "BABYSIT_PR_REPAIR",
    "GIT_TERMINAL_PROMPT",
    "GH_PROMPT_DISABLED",
    "GH_NO_UPDATE_NOTIFIER",
    "GCM_INTERACTIVE",
)


class Grants(NamedTuple):
    """What a sandboxed agent may use beyond its working tree and its own configuration."""

    add_dirs: tuple[str, ...] = ()  # writable directories
    env_pass: tuple[str, ...] = ()  # environment variable names passed through
    features: tuple[str, ...] = ()  # docker, playwright


def backend(name: str | None = None) -> str:
    if name is None:
        pinned = os.environ.get("BABYSIT_SANDBOX")
        if pinned:
            if pinned not in ("safehouse", "bwrap"):
                raise ValueError(f"BABYSIT_SANDBOX must be safehouse or bwrap, not {pinned!r}")
            return pinned
        name = {"darwin": "safehouse", "linux": "bwrap"}.get(sys.platform)
        if name is None:
            raise ValueError(f"No agent sandbox for {sys.platform}")
    elif name not in BACKENDS:
        raise ValueError(f"Unknown sandbox backend {name!r}")
    return name


def flags(grants: Grants, name: str) -> list[str]:
    """Safehouse's grant options, which babysit-bwrap accepts too."""
    if any(":" in path for path in grants.add_dirs):
        # Both launchers split their directory lists on colons.
        raise ValueError("A sandbox directory path contains ':'")
    unknown = set(grants.features) - set(SAFEHOUSE_FEATURES)
    if unknown:
        raise ValueError(f"Unknown sandbox feature {sorted(unknown)[0]!r}")
    words = []
    if grants.add_dirs:
        words.append("--add-dirs=" + ":".join(dict.fromkeys(grants.add_dirs)))
    if grants.env_pass:
        words.append("--env-pass=" + ",".join(grants.env_pass))
    if grants.features:
        names = [SAFEHOUSE_FEATURES[f] if name == "safehouse" else f for f in grants.features]
        words.append("--enable=" + ",".join(names))
    return words


NO_GRANTS = Grants()


def wrap(argv: Sequence[str], grants: Grants = NO_GRANTS, name: str | None = None) -> list[str]:
    """The argv that runs ``argv``, an agent with its own permissions off, in the sandbox."""
    name = backend(name)
    if name == "none":
        return list(argv)
    launcher = (
        str(Path.home() / ".local/bin/safehouse")
        if name == "safehouse"
        else str(SCRIPTS / "babysit-bwrap")
    )
    return [launcher, *flags(grants, name), "--", *argv]


def claude_launcher(name: str | None = None) -> list[str]:
    """The headless Claude repair launcher; Claude's own options follow it."""
    name = backend(name)
    if name == "safehouse":
        # Equivalent to wrap() with REPAIR_ENV and playwright, plus this user's verified
        # safe.env and Python IPC profile, which only the zsh launcher knows.
        return ["/bin/zsh", str(SCRIPTS / "claude_safehouse.zsh")]
    if name == "none":
        raise ValueError("Headless Claude repairs only run inside a sandbox")
    argv = ["claude", "--dangerously-skip-permissions"]
    return wrap(argv, Grants(env_pass=REPAIR_ENV, features=("playwright",)), name)


def launcher_backend(word: str) -> str | None:
    """The backend whose launcher executable ``word`` names, if any."""
    return LAUNCHERS.get(Path(word).name)


def typed_line(
    words: Sequence[str],
    grants: Grants,
    stage: Callable[[str], str],
    skip_permissions: str,
    name: str | None = None,
) -> str:
    """The shell line a multiplexer types to start agent ``words`` in the sandbox.

    ``skip_permissions`` is the agent's flag that turns its own permissions off. The
    directory list is staged through ``stage``, like the prompt, so the typed line does
    not grow with every path.
    """
    name = backend(name)
    if name == "none":
        # `command` skips the shell function, so its flag is added here instead.
        return shlex.join(["command", words[0], skip_permissions, *words[1:]])
    dirs = list(dict.fromkeys(grants.add_dirs))
    if name == "safehouse":
        # The shell's `safe` function adds the permission flag and runs Safehouse.
        command = shlex.join(words)
        if "docker" in grants.features:
            command = f"SAFE_ENABLE=docker {command}"
        if dirs:
            # Extends the list safe.env exports.
            listed = shlex.quote(stage(":".join(dirs)))
            command = f"SAFEHOUSE_ADD_DIRS=${{SAFEHOUSE_ADD_DIRS:+$SAFEHOUSE_ADD_DIRS:}}$(cat {listed}) {command}"
        return command
    options = flags(grants._replace(add_dirs=()), name)
    if dirs:
        options.append(f'"--add-dirs=$(cat {shlex.quote(stage(":".join(dirs)))})"')
    launch = [shlex.quote(str(SCRIPTS / "babysit-bwrap")), *options, "--"]
    return " ".join([*launch, shlex.join([words[0], skip_permissions, *words[1:]])])


def env_pass_prefix(names: Sequence[str], name: str | None = None) -> str | None:
    """A typed-line assignment that passes ``names`` into the sandbox, if one is needed.

    Safehouse's shell function forwards ``SAFEHOUSE_ENV_PASS``; babysit-bwrap already
    passes the agent configuration variables, and ``none`` keeps the whole environment.
    """
    if backend(name) != "safehouse":
        return None
    listed = ",".join(names)
    return f"SAFEHOUSE_ENV_PASS={listed}${{SAFEHOUSE_ENV_PASS:+,$SAFEHOUSE_ENV_PASS}}"


def docker_socket() -> Path:
    """The rootless Docker socket babysit-bwrap binds for ``--enable=docker``."""
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "docker.sock"


def inspect(pid: int, name: str | None = None) -> dict[str, bool | None]:
    """What a running agent can use, for example ``{"docker": True}``; None is unknown."""
    name = backend(name)
    if name == "safehouse":
        import agent_docker

        try:
            return {"docker": agent_docker.has_access(pid)}
        except ValueError:
            return {"docker": None}
    if name == "bwrap":
        # The agent's mount namespace holds the socket only when it was granted.
        try:
            os.stat(Path(f"/proc/{pid}/root") / docker_socket().relative_to("/"))
        except FileNotFoundError:
            return {"docker": False}
        except OSError:
            return {"docker": None}
        return {"docker": True}
    return {"docker": None}
