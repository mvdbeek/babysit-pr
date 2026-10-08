"""Workspace picker choices; discovery never starts an agent or changes its defaults."""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import claude_accounts

MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}")
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
VERSION = re.compile(r"\d+\.\d+\.\d+[0-9A-Za-z.+-]*")
# Package managers may keep an executable's timestamp across upgrades, so a version
# found for an unchanged file is still rechecked after this many seconds.
VERSION_RECHECK = 600
REMEMBERED = "codex-models.json"
# Reasoning efforts a launch left on Default uses: a global one and one per repository.
DEFAULTS = "effort-defaults.json"
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

_version_lock = threading.Lock()
# Saves read, change and rewrite the defaults file; concurrent requests take turns.
_defaults_lock = threading.Lock()
_version: dict = {}


def codex_version():
    """The version printed by the first ``codex`` on PATH, or None when unknown.

    The launch types ``codex`` into the pane's shell, so this is the CLI that will
    most likely run. Only ``codex --version`` is executed, at most once per file
    identity and recheck interval.
    """
    path = shutil.which("codex")
    if not path:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (os.path.realpath(path), stat.st_ino, stat.st_size, stat.st_mtime_ns)
    with _version_lock:
        if _version.get("key") == key and time.monotonic() < _version["until"]:
            return _version["value"]
        try:
            output = subprocess.run(
                [path, "--version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            output = ""
        match = VERSION.search(output)
        value = match[0] if match else None
        _version.update(key=key, until=time.monotonic() + VERSION_RECHECK, value=value)
        return value


def codex_models(value):
    """Validated picker entries from a parsed Codex model cache."""
    codex = []
    for model in value.get("models", []):
        if not isinstance(model, dict):
            continue
        slug = model.get("slug")
        if (
            model.get("visibility") != "list"
            or not isinstance(slug, str)
            or not MODEL.fullmatch(slug)
            or not isinstance(model.get("supported_reasoning_levels"), list)
        ):
            continue
        levels = [
            level.get("effort")
            for level in model["supported_reasoning_levels"]
            if isinstance(level, dict)
        ]
        codex.append({"id": slug, "efforts": [e for e in EFFORTS if e in levels]})
    return codex


def remembered(home, version):
    """The list remembered for ``version``, or for whichever CLI was last seen if None."""
    try:
        value = json.loads((Path(home) / REMEMBERED).read_text())
        if version is None or value["client_version"] == version:
            return [
                {"id": m["id"], "efforts": [e for e in EFFORTS if e in m["efforts"]]}
                for m in value["models"]
                if isinstance(m["id"], str) and MODEL.fullmatch(m["id"])
            ]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def remember(home, version, models):
    if remembered(home, version) == models:
        return
    path = Path(home) / REMEMBERED
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    try:
        temporary.write_text(json.dumps({"client_version": version, "models": models}))
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)


def catalog(home=None):
    """Use Codex's local picker cache, and documented Claude Code model aliases.

    A missing/malformed cache leaves Default available. Cache schema is private,
    so accept only validated picker entries and never infer a default from order.

    Codex fetches its catalog per client version, and every Codex on the machine
    (an auto-updated app-server daemon too) rewrites the same cache. Entries are
    offered only when the cache was written by the ``codex`` the launch will run;
    otherwise the last matching list remembered under ``home`` stands in, so a
    model only a newer Codex accepts is never offered to an older CLI. When the
    CLI's version cannot be read, a remembered list is still preferred to the cache.
    """
    codex, note = [], None
    cache = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "models_cache.json"
    try:
        value = json.loads(cache.read_text())
        written_by = value.get("client_version")
        installed = codex_version() if isinstance(written_by, str) else None
        if installed is None and home and isinstance(written_by, str):
            codex = remembered(home, None) or codex_models(value)
        elif installed is None or written_by == installed:
            codex = codex_models(value)
            if home and installed and codex:
                remember(home, installed, codex)
        else:
            codex = (home and remembered(home, installed)) or []
            if not codex:
                note = (
                    f"Codex model choices were cached by Codex {written_by}, but `codex` "
                    f"is {installed}. Start or update `codex` to list its models."
                )
    except (OSError, ValueError, AttributeError, TypeError):
        codex = []
    return {
        "codex": {
            "models": codex,
            "efforts": [e for e in EFFORTS if any(e in m["efforts"] for m in codex)],
            **({"note": note} if note else {}),
        },
        "claude": {
            "accounts": [{"id": a["id"], "label": a["label"]} for a in claude_accounts.catalog()],
            "models": [
                {"id": name, "efforts": CLAUDE_EFFORTS if name != "haiku" else []}
                for name in ("fable", "opus", "sonnet", "haiku")
            ],
            "efforts": CLAUDE_EFFORTS,
        },
    }


def validate(agent, model, effort, home=None):
    choices = catalog(home)[agent]
    selected = next((m for m in choices["models"] if m["id"] == model), None)
    if model and selected is None:
        raise ValueError("Select an available model for this agent; refresh workspace choices")
    levels = selected["efforts"] if selected else choices["efforts"]
    if effort and effort not in levels:
        raise ValueError("Select a supported reasoning effort for this agent and model")


def effort_defaults(home):
    """``{"effort": level or None, "repos": {"owner/name": level}}``, keys lower-case.

    A missing or malformed file means no defaults; unknown levels are dropped.
    """
    try:
        value = json.loads((Path(home) / DEFAULTS).read_text())
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    repos = value.get("repos")
    if not isinstance(repos, dict):
        repos = {}
    return {
        "effort": value.get("effort") if value.get("effort") in EFFORTS else None,
        "repos": {
            slug.lower(): level
            for slug, level in repos.items()
            if isinstance(slug, str) and REPO.fullmatch(slug) and level in EFFORTS
        },
    }


def save_effort_default(home, request):
    """Set one default, or clear it with an empty effort; returns all defaults.

    ``{"effort": level}`` is the global default, ``{"repo": "owner/name", ...}`` the
    repository's, which a launch there prefers.
    """
    if not isinstance(request, dict) or set(request) - {"repo", "effort"}:
        raise ValueError("Unknown effort default field")
    effort = request.get("effort") or ""
    if effort and effort not in EFFORTS:
        raise ValueError("Select a known reasoning effort")
    repo = request.get("repo")
    if repo is not None and not (isinstance(repo, str) and REPO.fullmatch(repo)):
        raise ValueError("Name the repository as owner/name")
    with _defaults_lock:
        defaults = effort_defaults(home)
        if repo is None:
            defaults["effort"] = effort or None
        elif effort:
            defaults["repos"][repo.lower()] = effort
        else:
            defaults["repos"].pop(repo.lower(), None)
        path = Path(home) / DEFAULTS
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
        try:
            temporary.write_text(json.dumps(defaults, indent=2, sort_keys=True))
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return defaults


def default_effort(home, repo, agent, model=""):
    """The effort for a launch in ``repo`` left on Default, or "" for the agent's own.

    The repository's default wins over the global one; a default the agent and model
    do not support is skipped rather than failing the launch.
    """
    defaults = effort_defaults(home)
    choices = catalog(home)[agent]
    selected = next((m for m in choices["models"] if m["id"] == model), None)
    levels = selected["efforts"] if selected else choices["efforts"]
    for level in (defaults["repos"].get((repo or "").lower()), defaults["effort"]):
        if level and level in levels:
            return level
    return ""
