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
config (Git LFS, say) and blanking the rest; Git skips an empty filter command. Git LFS
takes its extensions, custom transfer programs and object store from that same config,
so a repository that sets any of them is refused.
``remote.<name>.uploadpack`` is first-wins, so fetches pass ``--upload-pack`` instead.
A fetch names a GitHub URL (:func:`github_url`) and its refs, never a remote whose URL
the agent rewrote, and is refused while the repository rewrites URLs; only the gh
helper is asked, and only when the caller vouches for that repository. Only the https,
ssh and file transports are allowed, for a fetch alone (the user's own rewrites may
lead there), and any ``protocol.<name>.allow`` the repository adds is overridden, so no
``git-remote-<name>`` helper is started.

Which config applies depends on the worktree: ``config.worktree`` and ``includeIf``
conditions on the branch or git dir. So a worktree is checked out by adding it with
``--no-checkout`` and resetting inside it, where the listing sees what the reset will,
and removed with ``--force`` after a hardened status in it, since Git's own clean check
runs ``git status`` in that worktree with its config. Status and diffs never look
into a submodule's working tree, since its own config is never listed.
``core.worktree`` and ``core.bare`` are read from config files only, so a checkout's own
path is passed as ``--work-tree``.

Left open, and stated in references/supervisor.md: config rewritten between that
listing and the command itself, drivers in a submodule's own config, and helpers for the
allowed transports already on PATH during a fetch.

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
    # Both run Git inside a submodule, under the submodule's own config.
    "-c",
    "status.submoduleSummary=false",
    "-c",
    "diff.submodule=short",
    # Remote helpers are programs: no transport is allowed but those a fetch adds back.
    "-c",
    "protocol.allow=never",
    # Auto gc after a fetch would run gc.recentObjectsHook.
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.auto=false",
)
# GIT_NO_LAZY_FETCH stops a partial clone's promisor remote from being fetched on demand,
# whatever the inherited environment says. An empty GIT_PROXY_COMMAND takes precedence
# over core.gitProxy and means no proxy, unless the user's environment names one.
FORCED = {"GIT_NO_LAZY_FETCH": "1"}
ENVIRONMENT = {"GIT_PROXY_COMMAND": ""}
# Git ignores GIT_NO_LAZY_FETCH before this version.
LAZY_FETCH_SWITCH = (2, 44)
TOKENS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
# Subcommands that read working tree files through filters, or run a transport.
CONTENT = {"diff", "fetch", "reset", "status", "switch"}
# Their checkout or clean check would run in a worktree whose config was not listed.
WORKTREE = {"worktree add": "--no-checkout", "worktree remove": "--force"}
# A fetch names https://github.com, so these are reached only through URL rewrites in the
# user's own config (to ssh, or a local mirror); the repository's own are refused.
ALLOWED = {"https", "ssh", "file"}
TRANSPORTS = ("file", "git", "ssh", "http", "https", "ftp", "ftps", "ext")
FETCHING = [arg for name in sorted(ALLOWED) for arg in ("-c", f"protocol.{name}.allow=always")]
# Every other command reaches no remote at all, not even a promisor's missing object.
OFFLINE = [arg for name in TRANSPORTS for arg in ("-c", f"protocol.{name}.allow=never")]
# A host fetch names its repository by this URL, built from a host-side record of which
# repository is meant, and its refs explicitly: never a remote, whose URL the agent writes.
GITHUB_URL = re.compile(r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+)\.git")
GITHUB_REMOTE = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+?)(?:\.git)?/?"
)
# Config that would send that URL elsewhere: a rewrite, or a remote named by a URL,
# whose url, vcs or other settings Git applies to a fetch of that URL.
REDIRECT = re.compile(r"url\..+\.insteadof|remote\..*://.*")
# Subcommands that may need credentials; the rest run without the token.
NETWORK = {"fetch"}
DIFFS = {"diff", "log", "show"}
# Commands that would otherwise run Git in each submodule to see whether it changed.
SUBMODULES = {"diff", "log", "show", "status"}
TRUSTED = {"system", "global", "command"}
LISTING = ("config", "--list", "-z", "--show-scope")
FILTER = re.compile(r"filter\..+\.(clean|smudge|process|required)")
PROTOCOL = re.compile(r"protocol\.(.+)\.allow")
# Settings that let a proxy, another CA or another address read what is sent to GitHub,
# or keep its cookies in a file the agent reads.
TLS = re.compile(
    r"http\.(.+\.)?(sslverify|sslcainfo|sslcapath|sslbackend|proxy|cookiefile|savecookies"
    r"|curloptresolve)|remote\..+\.proxy"
)
GITHUB = "credential.https://github.com.helper"
# git-lfs, a filter the user's config keeps, reads these from the same config: programs it
# runs and the directory it writes objects to. Its endpoints get no credentials here.
LFS = re.compile(r"lfs\.(extension|customtransfer)\..+|lfs\.(standalonetransferagent|storage)")


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


def github_url(slug: str) -> str:
    """The URL a host fetch of the GitHub repository ``owner/repo`` names."""
    url = f"https://github.com/{slug}.git"
    if not GITHUB_URL.fullmatch(url) or slug.split("/")[1] in {".", ".."}:
        raise ValueError(f"Not a GitHub repository: {slug!r}")
    return url


def github_slug(url: str) -> str | None:
    """``owner/repo`` of a configured GitHub remote URL, None for anything else.

    The same spellings as ``remote_probe.remote_slug``, which must stay self-contained.
    """
    match = GITHUB_REMOTE.fullmatch(url.strip())
    if not match or match[1].split("/")[1] in {".", ".."}:
        return None
    return match[1].lower()


_version: list[tuple[int, ...]] = []


def git_version() -> tuple[int, ...]:
    """This machine's Git version, asked once; (0,) when it cannot be read."""
    if not _version:
        import subprocess

        try:
            out = subprocess.run(
                ["git", "--version"], capture_output=True, text=True, timeout=15
            ).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        match = re.search(r"(\d+)\.(\d+)", out)
        _version.append((int(match[1]), int(match[2])) if match else (0,))
    return _version[0]


def overrides(
    config: Sequence[tuple[str, str, str]], credentials: bool, network: bool | None = None
) -> list[str]:
    """``-c`` settings that undo the repository's own drivers and programs.

    ``network`` is a fetch, and ``credentials`` whether it gets GitHub's token.
    """
    network = credentials if network is None else network
    trusted = [(key, value) for scope, key, value in config if scope in TRUSTED]
    planted = [key for scope, key, _ in config if scope not in TRUSTED]
    options: list[str] = []
    lfs = [key for key in planted if LFS.fullmatch(key)]
    if lfs:
        raise ValueError(f"Git LFS would take the repository's own {lfs[0]}; Git was not run")
    for key in dict.fromkeys(k for k in planted if FILTER.fullmatch(k)):
        if "=" in key:
            raise ValueError(f"Cannot neutralise the repository's {key!r}; Git was not run")
        values = [value for name, value in trusted if name == key]
        default = "false" if key.endswith(".required") else ""
        options += ["-c", f"{key}={values[-1] if values else default}"]
    for key in dict.fromkeys(k for k in planted if PROTOCOL.fullmatch(k)):
        name = key.split(".", 1)[1].rsplit(".", 1)[0]
        if "=" in key:
            raise ValueError(f"Cannot neutralise the repository's {key!r}; Git was not run")
        allowed = network and name in ALLOWED
        options += ["-c", f"{key}={'always' if allowed else 'never'}"]
    ssh = [value for name, value in trusted if name == "core.sshcommand"]
    if ssh:
        options += ["-c", f"core.sshCommand={ssh[-1]}"]
    redirected = [key for key in planted if network and REDIRECT.fullmatch(key)]
    if redirected:
        raise ValueError(f"The repository's own {redirected[0]} redirects a fetch; Git was not run")
    if credentials:
        weakened = [key for key in planted if TLS.fullmatch(key)]
        if weakened:
            raise ValueError(
                f"The repository's own {weakened[0]} would expose GitHub credentials; "
                "Git was not run"
            )
        # GitHub gets the gh helper alone, which hands over the token in GH_TOKEN when the
        # environment carries one; no other helper is asked.
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
    credentials: bool = True,
) -> tuple[list[str], dict[str, str]]:
    """The hardened argv and environment for ``git -C path *args`` on the host.

    ``env`` is the base environment; a GitHub token in it reaches only a fetch, and only
    with ``credentials``: a fetch of a repository no host record names goes without.
    A fetch names ``github_url(slug)`` and its refspecs. ``read`` runs the config
    listing for commands that need it.
    """
    argv = list(args)
    index = subcommand(argv)
    name = argv[index] if index < len(argv) else ""
    network = name in NETWORK
    credentials = network and credentials
    if network:
        operands = [arg for arg in argv[index + 1 :] if not arg.startswith("-")]
        if (
            len(operands) < 2
            or not GITHUB_URL.fullmatch(operands[0])
            or not github_slug(operands[0])
        ):
            raise ValueError(
                "A host fetch names a GitHub repository URL and its refs, never a remote or "
                "another URL; Git was not run"
            )
    environment = dict(os.environ if env is None else env)
    if not credentials:
        for token in TOKENS:
            environment.pop(token, None)
        if network:
            environment.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
    for key, value in ENVIRONMENT.items():
        environment.setdefault(key, value)
    environment.update(FORCED)
    pair = " ".join(argv[index : index + 2])
    if pair in WORKTREE and WORKTREE[pair] not in argv[index + 2 :]:
        raise ValueError(f"git {pair} needs {WORKTREE[pair]} on the host; Git was not run")
    extra: list[str] = []
    # Where Git ignores GIT_NO_LAZY_FETCH, any command may fetch a missing object, so the
    # repository's own protocol permissions are overridden for every one.
    if name in CONTENT or (not network and git_version() < LAZY_FETCH_SWITCH):
        # Listing config runs nothing: no index, hooks or filters, and no pager here.
        listing = read(["git", "--no-pager", "-C", path, *LISTING], environment)
        extra = overrides(entries(listing), credentials, network)
    flags = {
        # FETCH_HEAD is opened without refusing a planted symlink; nothing here reads it.
        "fetch": ["--upload-pack=git-upload-pack", "--no-write-fetch-head"],
        **{diff: ["--no-ext-diff", "--no-textconv"] for diff in DIFFS},
    }.get(name, [])
    if name in SUBMODULES:
        # Never their working trees; a new commit checked out in one still shows.
        flags = [*flags, "--ignore-submodules=dirty"]
    argv[index + 1 : index + 1] = flags
    # core.worktree in config the agent writes would point Git at another directory, and
    # core.bare at none; a checkout with its own .git is its own work tree.
    if os.path.lexists(os.path.join(path, ".git")):
        argv[:0] = [f"--work-tree={os.path.abspath(path)}"]
    transports = FETCHING if network else OFFLINE
    return ["git", "-C", path, *OPTIONS, *transports, *extra, *argv], environment


def common_dir(checkout: str) -> str:
    """The Git common dir of ``checkout``, read from its layout rather than asked of Git.

    ``rev-parse --git-common-dir`` follows ``commondir`` files, which an agent can write
    in its worktree's admin dir or the clone's own ``.git``, so a host that grants that
    answer to a sandbox grants whatever directory the agent named. Here a clone's real
    ``.git`` directory is its own common dir, and a linked worktree's ``.git`` file must
    point at ``<common>/worktrees/<id>``, whose ``gitdir`` registers this very checkout.
    """
    dot = os.path.join(checkout, ".git")

    def refuse(reason: str) -> ValueError:
        return ValueError(f"The Git common dir of {checkout} cannot be verified: {reason}")

    if os.path.islink(dot):
        raise refuse(".git is a symbolic link")
    if os.path.isdir(dot):
        common = os.path.realpath(dot)
        if os.path.lexists(os.path.join(common, "commondir")):
            raise refuse(f"{common}/commondir names another common dir")
    else:
        try:
            with open(dot) as handle:
                pointer = handle.read(4096)
        except OSError as exc:
            raise refuse(f"no .git directory or file ({exc.strerror})") from exc
        if not pointer.startswith("gitdir: "):
            raise refuse(".git is not a gitdir file")
        admin = os.path.realpath(os.path.join(checkout, pointer[len("gitdir: ") :].strip()))
        worktrees = os.path.dirname(admin)
        common = os.path.dirname(worktrees)
        if os.path.basename(worktrees) != "worktrees" or os.path.basename(common) != ".git":
            raise refuse(f"{admin} is not a worktree of a clone's .git")
        try:
            with open(os.path.join(admin, "gitdir")) as handle:
                registered = handle.read(4096).strip()
        except OSError as exc:
            raise refuse(f"{admin} does not register this checkout ({exc.strerror})") from exc
        if os.path.realpath(os.path.join(admin, registered)) != os.path.realpath(dot):
            raise refuse(f"{admin} does not register this checkout")
        try:
            with open(os.path.join(admin, "commondir")) as handle:
                named = handle.read(4096).strip()
        except OSError as exc:
            raise refuse(f"{admin}/commondir is unreadable ({exc.strerror})") from exc
        named = os.path.realpath(os.path.join(admin, named))
        if named != common:
            raise refuse(f"{admin}/commondir names {named}, not {common}")
        if os.path.lexists(os.path.join(common, "commondir")):
            raise refuse(f"{common}/commondir names another common dir")
    if not os.path.isdir(os.path.join(common, "objects")):
        raise refuse(f"{common} holds no objects")
    return common


def writable_config(common: str) -> None:
    """Refuse a config write that would follow a symbolic link out of the common dir.

    Git writes ``config`` through a lock file that follows the link, so a host write
    would land wherever the agent pointed it.
    """
    if os.path.islink(os.path.join(common, "config")):
        raise ValueError(f"{common}/config is a symbolic link; the host will not write it")
