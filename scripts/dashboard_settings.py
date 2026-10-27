"""Page settings the dashboard keeps for the person: task prompts, the main reviewer, and
how many agents this machine takes before Auto places launches on workers.

The task a Handle button prefills, and which agent a review starts with, used to be
fixed in the page's code: changing a word meant editing a file in the checkout, which
a phone over the tailnet does not have. Both are stored here, in ``dashboard-settings``
under the watcher's home, and the page keeps the wording itself. Only what the page
must agree with the server about lives here: which tasks exist, and which agents review.
"""

import json
import os
import threading
from pathlib import Path

SETTINGS = "dashboard-settings.json"
# The prefilled tasks the page offers; its own copy holds each one's default wording.
TASKS = ("prReview", "issue", "ci", "review")
AGENTS = ("codex", "claude")
# Where a prompt names the pull request or issue it is about; the page substitutes it.
URL = "{url}"
# A prompt is a paragraph or two. The textarea that writes it allows far more, so a
# rejection here says the text is too long rather than silently keeping part of it.
LIMIT = 8000
DEFAULT_REVIEWER = "codex"
# Agents this machine runs before Auto prefers a worker; it still takes a launch no
# worker can. Only matters with a machine registry.
DEFAULT_LOCAL_MAX_AGENTS = 20
LOCAL_MAX_AGENTS_LIMIT = 200

# Saves read, change and rewrite the file; concurrent requests take turns.
_lock = threading.Lock()


def stored(home):
    """The settings file as written, or {} when it is missing or unreadable."""
    try:
        value = json.loads((Path(home) / SETTINGS).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def local_max_agents(value):
    return type(value) is int and 1 <= value <= LOCAL_MAX_AGENTS_LIMIT


def settings(home):
    """``{"task_prompts": {task: text}, "reviewer": agent, "local_max_agents": n}``;
    unknown or malformed entries dropped."""
    value = stored(home)
    prompts = value.get("task_prompts")
    if not isinstance(prompts, dict):
        prompts = {}
    reviewer = value.get("reviewer")
    capacity = value.get("local_max_agents")
    return {
        "task_prompts": {
            task: text
            for task, text in prompts.items()
            if task in TASKS and isinstance(text, str) and text.strip()
        },
        "reviewer": reviewer if reviewer in AGENTS else DEFAULT_REVIEWER,
        "local_max_agents": capacity if local_max_agents(capacity) else DEFAULT_LOCAL_MAX_AGENTS,
    }


def save(home, request):
    """Change one setting; returns all of them.

    ``{"task": task, "prompt": text}`` replaces a task's wording, and an empty prompt
    restores the page's own. ``{"reviewer": agent}`` sets which agent a review starts
    with, the other one being offered as a second opinion. ``{"local_max_agents": n}``
    sets how many agents this machine runs before Auto prefers a worker.
    """
    changes = {"task", "reviewer", "local_max_agents"}
    if not isinstance(request, dict) or set(request) - {*changes, "prompt"}:
        raise ValueError("Unknown setting")
    if len(changes & set(request)) != 1:
        raise ValueError("Change one setting at a time")
    with _lock:
        value = settings(home)
        if "local_max_agents" in request:
            if not local_max_agents(request["local_max_agents"]):
                raise ValueError(
                    f"Choose a whole number of agents from 1 to {LOCAL_MAX_AGENTS_LIMIT}"
                )
            value["local_max_agents"] = request["local_max_agents"]
        elif "reviewer" in request:
            if request["reviewer"] not in AGENTS:
                raise ValueError("Choose Codex or Claude as the main reviewer")
            value["reviewer"] = request["reviewer"]
        else:
            task, prompt = request["task"], request.get("prompt", "")
            if task not in TASKS:
                raise ValueError("Unknown task")
            if not isinstance(prompt, str):
                raise ValueError("Write the prompt as text")
            if not prompt.strip():
                value["task_prompts"].pop(task, None)
            elif len(prompt) > LIMIT:
                raise ValueError(f"Keep the prompt under {LIMIT} characters")
            elif URL not in prompt:
                raise ValueError(f"Include {URL} where the link belongs")
            else:
                value["task_prompts"][task] = prompt
        write(home, value)
    return value


def write(home, value):
    path = Path(home) / SETTINGS
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
