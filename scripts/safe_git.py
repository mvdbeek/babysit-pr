"""Host-side Git in checkouts a sandboxed agent can write.

Agents run with their worktree and the repository's common Git directory writable, so
the repository's config, its hooks and ``info/attributes`` are theirs to edit. Git run
on the host in such a checkout must not execute what they planted there, and must not
hand them the GitHub token. Every host Git call in a checkout goes through
:func:`prepare`, which returns the argv and environment to run it with.

Command-line ``-c`` settings are read after every config file, so for single-valued
keys they win over the repository's own; that closes the fixed-name vectors (fsmonitor,
hooks, ssh and askpass programs, alternate refs command) and clears every credential
helper. Pagers, external diffs and textconv have whole-command switches. Filter drivers
have no such switch: their names come from the repository, so commands that read
working tree files first list the effective config and neutralise each
``filter.<driver>`` the repository defines, keeping a definition from the user's own
config (Git LFS, say) and blanking the rest; Git skips an empty filter command.
``remote.<name>.uploadpack`` is first-wins, so fetches pass ``--upload-pack`` instead.

Left open, and stated in references/supervisor.md: config rewritten between that
listing and the command itself, drivers in a submodule's own config, remote helpers
already installed on PATH, and lazy fetches on Git older than 2.44.

The system and global config are trusted and still apply: the sandbox cannot write
them. Only the standard library is used, since ``wt`` imports this too.
"""

import os
import re
from collections.abc import Callable, Mapping, Sequence

OPTIONS = (
    # pager.<command> names a program, run whenever stdout is a terminal.
    "--no-pager",
    "--no-optional-locks",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.sshCommand=ssh",
    "-c",
    "core.askPass=",
    "-c",
    "core.alternateRefsCommand=",
    # An empty helper clears every helper configured before it, URL-scoped ones too.
    "-c",
    "credential.helper=",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "log.showSignature=false",
    "-c",
    "submodule.recurse=false",
    "-c",
    "fetch.recurseSubmodules=false",
    # Auto gc after a fetch would run gc.recentObjectsHook.
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.auto=false",
)
# GIT_NO_LAZY_FETCH stops a partial clone's promisor remote from being fetched on demand.
# An empty GIT_PROXY_COMMAND takes precedence over core.gitProxy and means no proxy.
ENVIRONMENT = {"GIT_NO_LAZY_FETCH": "1", "GIT_PROXY_COMMAND": ""}
TOKENS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
# Subcommands that read working tree files through filters, or run a transport.
CONTENT = {"diff", "fetch", "status", "switch", "worktree add", "worktree remove"}
# Subcommands that may need credentials; the rest run without the token.
NETWORK = {"fetch"}
DIFFS = {"diff", "log", "show"}
TRUSTED = {"system", "global", "command"}
LISTING = ("config", "--list", "-z", "--show-scope")
FILTER = re.compile(r"filter\..+\.(clean|smudge|process|required)")
HELPER = re.compile(r"credential\.(.+\.)?helper")
# Settings that let a proxy or another CA read what is sent to GitHub.
TLS = re.compile(r"http\.(.+\.)?(sslverify|sslcainfo|sslcapath|sslbackend|proxy)|remote\..+\.proxy")
GITHUB = "credential.https://github.com.helper"


def subcommand(args: Sequence[str]) -> int:
    """Index of the subcommand in ``args``, past global options and ``-c`` pairs."""
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 2 if args[index] in {"-c", "-C"} else 1
    return index


def entries(listing: str) -> list[tuple[str, str, str]]:
    """``(scope, key, value)`` from ``git config --list -z --show-scope``."""
    fields = listing.split("\0")
    found = []
    for scope, item in zip(fields[::2], fields[1::2], strict=False):
        key, _, value = item.partition("\n")
        found.append((scope, key, value))
    return found


def overrides(config: Sequence[tuple[str, str, str]], credentials: bool) -> list[str]:
    """``-c`` settings that undo the repository's own drivers and programs."""
    trusted = [(key, value) for scope, key, value in config if scope in TRUSTED]
    planted = [key for scope, key, _ in config if scope not in TRUSTED]
    options: list[str] = []
    for key in dict.fromkeys(k for k in planted if FILTER.fullmatch(k)):
        if "=" in key:
            raise ValueError(f"Cannot neutralise the repository's {key!r}; Git was not run")
        values = [value for name, value in trusted if name == key]
        default = "false" if key.endswith(".required") else ""
        options += ["-c", f"{key}={values[-1] if values else default}"]
    ssh = [value for name, value in trusted if name == "core.sshcommand"]
    if ssh:
        options += ["-c", f"core.sshCommand={ssh[-1]}"]
    if credentials:
        weakened = [key for key in planted if TLS.fullmatch(key)]
        if weakened:
            raise ValueError(
                f"The repository's own {weakened[0]} would expose GitHub credentials; "
                "Git was not run"
            )
        # The user's helpers serve other hosts; GitHub gets the gh helper alone, which
        # hands over the token in GH_TOKEN when the environment carries one.
        options += [
            arg
            for key, value in trusted
            if HELPER.fullmatch(key) and "=" not in key
            for arg in ("-c", f"{key}={value}")
        ]
        options += ["-c", f"{GITHUB}=", "-c", f"{GITHUB}=!gh auth git-credential"]
    return options


def read_config(argv: list[str], env: Mapping[str, str]) -> str:
    import subprocess  # Only when needed: wt keeps its imports cheap.

    result = subprocess.run(
        argv, env=dict(env), capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL
    )
    if result.returncode:
        raise ValueError(result.stderr.strip()[-400:] or "git config failed")
    return result.stdout


def prepare(
    path: str,
    *args: str,
    env: Mapping[str, str] | None = None,
    read: Callable[[list[str], Mapping[str, str]], str] = read_config,
) -> tuple[list[str], dict[str, str]]:
    """The hardened argv and environment for ``git -C path *args`` on the host.

    ``env`` is the base environment; a GitHub token in it reaches only a fetch.
    ``read`` runs the config listing for commands that need it.
    """
    argv = list(args)
    index = subcommand(argv)
    name = argv[index] if index < len(argv) else ""
    credentials = name in NETWORK
    environment = dict(os.environ if env is None else env)
    if not credentials:
        for token in TOKENS:
            environment.pop(token, None)
    for key, value in ENVIRONMENT.items():
        environment.setdefault(key, value)
    extra: list[str] = []
    if name in CONTENT or " ".join(argv[index : index + 2]) in CONTENT:
        # Listing config runs nothing: no index, hooks or filters, and no pager here.
        listing = read(["git", "--no-pager", "-C", path, *LISTING], environment)
        extra = overrides(entries(listing), credentials)
    flags = {
        "fetch": ["--upload-pack=git-upload-pack"],
        **{diff: ["--no-ext-diff", "--no-textconv"] for diff in DIFFS},
    }.get(name, [])
    argv[index + 1 : index + 1] = flags
    return ["git", "-C", path, *OPTIONS, *extra, *argv], environment
