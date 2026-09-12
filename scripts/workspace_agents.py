"""Workspace picker choices; discovery never starts an agent or changes its defaults."""

import json
import os
import re
from pathlib import Path

MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}")
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def catalog():
    """Use Codex's local picker cache, and documented Claude Code model aliases.

    A missing/malformed cache leaves Default available. Cache schema is private,
    so accept only validated picker entries and never infer a default from order.
    """
    codex = []
    cache = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "models_cache.json"
    try:
        value = json.loads(cache.read_text())
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
    except (OSError, ValueError, AttributeError, TypeError):
        codex = []
    return {
        "codex": {
            "models": codex,
            "efforts": [e for e in EFFORTS if any(e in m["efforts"] for m in codex)],
        },
        "claude": {
            "models": [
                {"id": name, "efforts": CLAUDE_EFFORTS if name != "haiku" else []}
                for name in ("fable", "opus", "sonnet", "haiku")
            ],
            "efforts": CLAUDE_EFFORTS,
        },
    }


def validate(agent, model, effort):
    choices = catalog()[agent]
    selected = next((m for m in choices["models"] if m["id"] == model), None)
    if model and selected is None:
        raise ValueError("Select an available model for this agent; refresh workspace choices")
    levels = selected["efforts"] if selected else choices["efforts"]
    if effort and effort not in levels:
        raise ValueError("Select a supported reasoning effort for this agent and model")
