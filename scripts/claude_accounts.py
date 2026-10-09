#!/usr/bin/env python3
"""Named Claude logins, each with its own configuration and transcript store."""

import argparse
import json
import os
import re
import shlex
from pathlib import Path

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
# An explicitly selected subscription account must not inherit a different credential.
AUTH_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_PROFILE",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)


def default_home():
    return (Path.home() / ".claude").resolve()


def current_home():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", str(default_home()))).expanduser().resolve()


def account_home(name, *, create=False):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("Use an account name containing letters, digits, underscores or hyphens")
    path = default_home() if name == "default" else default_home() / "accounts" / name
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    elif not path.is_dir():
        raise ValueError(f"Unknown Claude account {name!r}; run claude-account login {name} first")
    # Symlinks can point at an existing custom config directory.
    return path.resolve()


def catalog():
    accounts = [{"id": "default", "label": "Default", "config_dir": str(default_home())}]
    try:
        entries = sorted((default_home() / "accounts").iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if entry.name != "default" and NAME.fullmatch(entry.name) and entry.is_dir():
            accounts.append(
                {"id": entry.name, "label": entry.name, "config_dir": str(entry.resolve())}
            )
    return accounts


def login(home):
    """Who a configuration is signed in as, from its global config; never reads credentials.

    Claude keeps ~/.claude's login in ~/.claude.json, and any other's inside that directory.
    """
    home = Path(home).resolve()
    path = Path.home() / ".claude.json" if home == default_home() else home / ".claude.json"
    try:
        value = json.loads(path.read_text())["oauthAccount"]
        return value["accountUuid"], value.get("organizationUuid")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def choices():
    """Accounts to offer; the default login is left out when a named one is the same login."""
    default, *named = catalog()
    same = login(default["config_dir"])
    logins = [login(a["config_dir"]) for a in named]
    if not same or same not in logins:
        return [default, *named]
    # That login stands in for jobs saved on the default one.
    stand_in = logins.index(same)
    return [{**a, "default": True} if i == stand_in else a for i, a in enumerate(named)]


def homes():
    return list(dict.fromkeys([current_home(), *(Path(a["config_dir"]) for a in catalog())]))


def account_name(path):
    return next((a["id"] for a in catalog() if Path(a["config_dir"]) == Path(path).resolve()), None)


def transcript_home(path):
    """Claude's top-level transcripts live directly in CONFIG/projects/PROJECT/."""
    path = Path(path).resolve()
    return path.parents[2] if path.parent.parent.name == "projects" else None


def validate(agent, name):
    if name:
        if agent != "claude":
            raise ValueError("A Claude account can only be selected for Claude")
        return account_home(name)
    return None


def config_environment(home):
    """Preserve the literal path: Claude hashes it for the macOS Keychain name."""
    home = Path(home).resolve()
    if home == current_home() and os.environ.get("CLAUDE_CONFIG_DIR"):
        return os.environ["CLAUDE_CONFIG_DIR"]
    return None if home == default_home() else str(home)


def environment(config_dir, *, subscription=False, base=None, config_env=False):
    env = dict(os.environ if base is None else base)
    if config_env is False:
        home = Path(config_dir).expanduser().resolve()
        config_env = None if home == default_home() else str(home)
    if config_env is None:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(config_env)
    if subscription:
        for name in AUTH_ENV:
            env.pop(name, None)
    return env


def shell_command(command, config_dir, *, subscription=False, config_env=False):
    """Retain the user's shell function, including its Safehouse containment.

    The prefix only sets the environment, so a ``command claude …`` from an unsandboxed
    cron job still bypasses the function, and ``SAFEHOUSE_ENV_PASS`` goes unused.
    """
    if config_env is False:
        home = Path(config_dir).resolve()
        config_env = None if home == default_home() else str(home)
    unset = list(AUTH_ENV) if subscription else []
    if config_env is None:
        unset.append("CLAUDE_CONFIG_DIR")
    else:
        path = shlex.quote(str(config_env))
        prefix = f"CLAUDE_CONFIG_DIR={path} SAFEHOUSE_ENV_PASS=CLAUDE_CONFIG_DIR${{SAFEHOUSE_ENV_PASS:+,$SAFEHOUSE_ENV_PASS}}"
        command = f"{prefix} {command}"
    if unset:
        command = f"(unset {' '.join(unset)}; {command})"
    return command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List local accounts; never reads credentials")
    for command in ("login", "status", "run"):
        child = sub.add_parser(command)
        child.add_argument("account", help="default, or a named account under ~/.claude/accounts")
        child.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == "list":
        for account in catalog():
            print(f"{account['id']}\t{account['config_dir']}")
        return
    try:
        home = account_home(args.account, create=args.command == "login")
    except ValueError as exc:
        parser.error(str(exc))
    extra = args.args[1:] if args.args[:1] == ["--"] else args.args
    words = {"login": ["auth", "login", "--claudeai"], "status": ["auth", "status"], "run": []}
    launcher = Path(__file__).resolve().with_name("claude_safehouse.zsh")
    os.execvpe(
        "/bin/zsh",
        ["/bin/zsh", str(launcher), *words[args.command], *extra],
        environment(home, subscription=True),
    )


if __name__ == "__main__":
    main()
