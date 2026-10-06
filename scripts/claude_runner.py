"""Claude session validation, bounded CLI invocation, and streamed results."""

import json
import uuid
from pathlib import Path

import claude_accounts


def config_home():
    return claude_accounts.current_home()


def _within(path, root):
    """A session may cd into a subdirectory of its worktree (e.g. lib/) without changing identity."""
    return path == root or root in path.parents


def session_info(session_id, cwd, transcript=None, config_dir=None):
    session_id = str(uuid.UUID(session_id))
    files = (
        [Path(transcript)]
        if transcript
        else [
            path
            for home in ([Path(config_dir)] if config_dir else claude_accounts.homes())
            for path in (home / "projects").glob(f"*/{session_id}.jsonl")
        ]
    )
    if len(files) != 1:
        raise ValueError("Supply --rollout: could not identify one saved Claude transcript")
    home = claude_accounts.transcript_home(files[0]) or Path(config_dir or config_home())
    if config_dir and home.resolve() != Path(config_dir).resolve():
        raise ValueError("Claude transcript belongs to a different account")
    seen = False
    model = None
    permission_mode = None
    with files[0].open() as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("type") not in {"user", "assistant"}:
                continue
            if item.get("isSidechain"):
                raise ValueError("Use the original Claude session, not a subagent transcript")
            if item.get("sessionId") != session_id:
                raise ValueError("Claude transcript session ID does not match")
            if not item.get("cwd") or not _within(Path(item["cwd"]).resolve(), Path(cwd).resolve()):
                raise ValueError("Claude transcript cwd does not match the worktree")
            seen = True
            candidate = (item.get("message") or {}).get("model")
            if candidate and candidate != "<synthetic>":
                model = candidate
            if item.get("permissionMode"):
                permission_mode = item["permissionMode"]
    if not seen or not model:
        raise ValueError("Cannot validate the Claude conversation and its model")
    account = claude_accounts.account_name(home)
    return {
        "session_id": session_id,
        "rollout": str(files[0].resolve()),
        "model": model,
        "claude_permission_mode": "plan" if permission_mode == "plan" else "dontAsk",
        "claude_config_dir": str(home.resolve()),
        "claude_config_env": (
            None
            if Path(config_dir or home).resolve() == claude_accounts.default_home()
            else str(home.resolve())
        )
        if config_dir
        else claude_accounts.config_environment(home),
        "claude_account": account,
        "claude_subscription": bool(config_dir) or account not in (None, "default"),
    }


def safehouse_command():
    return ["/bin/zsh", str(Path(__file__).resolve().with_name("claude_safehouse.zsh"))]


def command_for(job, schema):
    cmd = [
        *job["claude_command"],
        "--print",
        "--resume",
        job["session_id"],
        "--output-format",
        "stream-json",
        "--verbose",
        "--json-schema",
        json.dumps(schema),
    ]
    mode = job.get("claude_permission_mode", "dontAsk")
    if mode == "safehouse":
        if job["claude_command"] != safehouse_command():
            raise ValueError("Safehouse permission mode requires the verified Safehouse launcher")
    else:
        cmd += ["--permission-mode", mode]
    if job.get("model"):
        cmd += ["--model", job["model"]]
    return cmd


def display_event(event):
    message = event.get("message")
    if isinstance(message, str):
        yield message  # System events such as permission_denied use a string.
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        yield content
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            value = block.get("text", "")
            yield value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        elif block.get("type") == "tool_use":
            yield f"[{block.get('name', 'tool')}] {json.dumps(block.get('input', {}), ensure_ascii=False)}"
        elif block.get("type") == "tool_result":
            content = block.get("content", "")
            yield content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    if event.get("type") == "result":
        yield json.dumps(
            event.get("structured_output") or event.get("result") or event.get("errors") or {},
            ensure_ascii=False,
        )


def stream_output(stream, log, visible):
    for line in stream:
        log.write(line)
        log.flush()
        if not visible:
            continue
        try:
            event = json.loads(line)
            text = "\n".join(display_event(event)) if isinstance(event, dict) else str(event)
        except Exception:
            # Formatting must never stop draining the pipe or lose the final
            # result. Unknown event shapes remain available as raw JSON.
            text = line.decode("utf-8", errors="replace").rstrip()
        if text:
            try:
                print(text, flush=True)
            except (OSError, UnicodeError):
                pass


def read_result(path, session_id):
    results = []
    with path.open() as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                results.append(event)
    if len(results) != 1 or results[0].get("session_id") != session_id:
        raise RuntimeError("Claude returned no unambiguous result for the original session")
    result = results[0]
    if result.get("is_error") or result.get("subtype") != "success":
        raise RuntimeError(
            "Claude did not complete successfully: "
            + str(result.get("errors") or result.get("result") or result.get("subtype"))
        )
    if result.get("permission_denials"):
        return {
            "status": "blocked",
            "summary": "Claude encountered permission denials; review existing tool grants before resuming",
        }
    structured = result.get("structured_output")
    if (
        not isinstance(structured, dict)
        or structured.get("status") not in {"waiting", "blocked"}
        or not isinstance(structured.get("summary"), str)
    ):
        raise RuntimeError("Claude returned no valid structured continuation outcome")
    return structured
