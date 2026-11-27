"""Diff and transcript views over temporary repositories and session stores."""

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
import remote_probe
import workspace_viewer as wv


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A checkout two commits past origin/main, with uncommitted and untracked work."""
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    path = tmp_path / "work"
    path.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.test")
    (path / "keep.txt").write_text("one\ntwo\nthree\n")
    (path / "old.txt").write_text("moved content\n" * 5)
    (path / "gone.txt").write_text("bye\n")
    git("add", ".")
    git("commit", "-m", "base")
    git("update-ref", "refs/remotes/upstream/release_1.0", "HEAD")
    (path / "keep.txt").write_text("one\ntwo\nthree\nfour\n")
    git("commit", "-am", "on main")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("update-ref", "refs/remotes/origin/topic-of-someone-else", "HEAD")
    git("switch", "-c", "feature")
    git("mv", "old.txt", "new.txt")
    git("rm", "-q", "gone.txt")
    (path / "image.bin").write_bytes(b"\x00\x01\x02binary")
    git("add", ".")
    git("commit", "-m", "Rename and add a binary")
    git("commit", "--allow-empty", "-m", "Second commit")
    (path / "keep.txt").write_text("ONE\ntwo\nthree\nfour\n")
    (path / "notes.txt").write_text("untracked\n")
    return path, git


def test_branch_diff_uses_the_nearest_base_and_includes_uncommitted_work(repo):
    path, _ = repo
    value = wv.diff(path)
    assert value["base"] == "origin/main"
    assert [b["ref"] for b in value["bases"]] == ["origin/main", "upstream/release_1.0"]
    assert value["bases"][0]["ahead"] == 2
    assert [c["subject"] for c in value["commits"]] == ["Second commit", "Rename and add a binary"]
    files = {f["path"]: f for f in value["files"]}
    assert files["new.txt"]["status"] == "renamed" and files["new.txt"]["old_path"] == "old.txt"
    assert files["gone.txt"]["status"] == "deleted" and files["gone.txt"]["removed"] == 1
    assert files["image.bin"]["status"] == "added" and files["image.bin"]["binary"]
    assert files["keep.txt"]["status"] == "modified"
    assert "-one" in files["keep.txt"]["lines"] and "+ONE" in files["keep.txt"]["lines"]
    assert value["untracked"] == ["notes.txt"]
    assert value["added"] == sum(f["added"] for f in value["files"])


def test_an_older_base_can_be_chosen_but_not_an_arbitrary_ref(repo):
    path, _ = repo
    value = wv.diff(path, base="upstream/release_1.0")
    assert value["base"] == "upstream/release_1.0" and len(value["commits"]) == 3
    assert "+four" in next(f for f in value["files"] if f["path"] == "keep.txt")["lines"]
    for base in ("origin/topic-of-someone-else", "HEAD~1", "--output=/tmp/x"):
        with pytest.raises(ValueError, match="listed base"):
            wv.diff(path, base=base)


def test_commit_bodies_preserve_paragraphs_and_do_not_split_commit_records(repo):
    path, git = repo
    body = "Explain the change.\n\n- First detail\n- Second detail\n\nRefs: https://example.com/1"
    git("commit", "--allow-empty", "-m", "With details", "-m", body)
    git("commit", "--allow-empty", "-m", "Latest", "-m", "Another body.")
    commits = wv.diff(path)["commits"]
    assert [c["subject"] for c in commits] == [
        "Latest",
        "With details",
        "Second commit",
        "Rename and add a binary",
    ]
    assert [c["body"] for c in commits] == ["Another body.", body, "", ""]
    assert commits[0]["sha"] == git("rev-parse", "HEAD")[:12]
    assert all(c["author"] == "Fixture" and c["time"] > 0 for c in commits)


def test_one_commit_shows_only_its_own_changes(repo):
    path, git = repo
    (path / "keep.txt").write_text("ONE\ntwo\nthree\nfour\n")
    git("commit", "-am", "Shout")
    (path / "keep.txt").write_text("ONE\ntwo\nthree\nFOUR\n")
    shout, rename = git("rev-parse", "HEAD"), git("rev-parse", "HEAD~2")
    value = wv.diff(path, commit=shout[:12])
    assert value["commit"] == shout[:12] and value["scope"] == "branch"
    # The whole branch stays listed, to step through; the uncommitted FOUR is left out.
    assert [c["subject"] for c in value["commits"]][0] == "Shout" and len(value["commits"]) == 3
    assert [f["path"] for f in value["files"]] == ["keep.txt"]
    assert value["files"][0]["lines"] == [
        "@@ -1,4 +1,4 @@",
        "-one",
        "+ONE",
        " two",
        " three",
        " four",
    ]
    assert (value["added"], value["removed"], value["untracked"]) == (1, 1, [])
    # A full ID names the same commit; an earlier one shows its rename, not later edits.
    value = wv.diff(path, commit=rename)
    assert value["commit"] == rename[:12]
    assert sorted(f["path"] for f in value["files"]) == ["gone.txt", "image.bin", "new.txt"]


def test_a_commit_that_is_not_listed_shows_all_changes(repo):
    path, git = repo
    everything = wv.diff(path)
    # A commit of the base, or one rewritten since, is never handed to Git.
    for commit in (git("rev-parse", "origin/main"), "0123456789ab"):
        value = wv.diff(path, commit=commit)
        assert "no longer on this branch" in value["note"] and value["commit"] is None
        assert value["files"] == everything["files"] and value["untracked"] == ["notes.txt"]
    for commit in ("HEAD", "--output=/tmp/x", "abc", "ABCDEF123456", ""):
        with pytest.raises(ValueError, match="commit ID"):
            wv.diff(path, commit=commit)
    value = wv.diff(path, "uncommitted", commit="0123456789ab")
    assert value["commit"] is None and value["note"] is None


def test_merge_and_root_commits_are_compared_with_their_first_parent(repo):
    path, git = repo
    git("stash", "-q")
    git("switch", "-q", "--orphan", "island")
    (path / "island.txt").write_text("alone\n")
    git("add", "island.txt")
    git("commit", "-q", "-m", "Root of an island")
    root = git("rev-parse", "HEAD")
    git("switch", "-q", "feature")
    git("merge", "-q", "--allow-unrelated-histories", "-m", "Merge the island", "island")
    merge = git("rev-parse", "HEAD")
    for commit in (root, merge):
        value = wv.diff(path, commit=commit)
        assert value["commit"] == commit[:12]
        assert [(f["path"], f["status"], f["lines"][1:]) for f in value["files"]] == [
            ("island.txt", "added", ["+alone"])
        ]
        # A worker's hardened Git reads them the same way.
        assert remote_probe.diff(remote_probe.host_output, str(path), commit=commit) == value


def test_uncommitted_diff_compares_with_head_only(repo):
    path, _ = repo
    value = wv.diff(path, "uncommitted")
    assert value["base"] is None and value["commits"] == [] and value["bases"] == []
    assert [f["path"] for f in value["files"]] == ["keep.txt"]
    with pytest.raises(ValueError, match="branch or uncommitted"):
        wv.diff(path, "everything")


def test_a_checkout_without_remote_bases_shows_uncommitted_changes(repo):
    path, git = repo
    for ref in ("origin/main", "upstream/release_1.0"):
        git("update-ref", "-d", f"refs/remotes/{ref}")
    value = wv.diff(path)
    assert value["scope"] == "uncommitted" and "No base branch" in value["note"]
    assert [f["path"] for f in value["files"]] == ["keep.txt"]
    with pytest.raises(ValueError, match="listed base"):
        wv.diff(path, base="origin/main")


def test_a_tie_goes_to_the_base_that_moved_on_least(repo):
    path, git = repo
    # upstream/dev contains origin/main and one commit more: both are 2 behind HEAD's work.
    git("switch", "-q", "-c", "later", "origin/main")
    git("commit", "-q", "--allow-empty", "-m", "on dev")
    git("update-ref", "refs/remotes/upstream/dev", "HEAD")
    git("switch", "-q", "feature")
    found = wv.bases(path)
    assert [b["ref"] for b in found[:2]] == ["origin/main", "upstream/dev"]
    assert found[0]["ahead"] == found[1]["ahead"] == 2


def test_git_runs_without_the_github_token(repo, monkeypatch):
    path, _ = repo
    monkeypatch.setenv("GH_TOKEN", "gho_secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    assert "GH_TOKEN" not in wv.local_environment()
    assert "GITHUB_TOKEN" not in wv.local_environment()
    assert wv.local_environment()["GIT_TERMINAL_PROMPT"] == "0"


def test_large_diffs_are_cut_short(repo, monkeypatch):
    path, _ = repo
    (path / "keep.txt").write_text("".join(f"line {n}\n" for n in range(5000)))
    monkeypatch.setattr(wv, "MAX_FILE_LINES", 10)
    value = wv.diff(path, "uncommitted")
    keep = value["files"][0]
    assert len(keep["lines"]) == 10 and keep["truncated"]
    monkeypatch.setattr(wv, "MAX_DIFF", 200)
    value = wv.diff(path, "uncommitted")
    assert value["truncated"] and value["files"][-1]["truncated"]


def test_git_failures_are_reported(tmp_path):
    with pytest.raises(ValueError):
        wv.git_output(tmp_path, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="longer than"):
        wv.git_output(tmp_path, "--version", timeout=0)


def test_split_patch_reads_mode_only_changes():
    patch = "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n"
    assert wv.split_patch(patch, False) == [
        {
            "path": "run.sh",
            "old_path": None,
            "status": "modified",
            "binary": False,
            "added": 0,
            "removed": 0,
            "lines": [],
            "truncated": False,
        }
    ]


# Transcripts -------------------------------------------------------------------


def write_jsonl(path, *items):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in items) + '{"partial')


SID = "8f2c6f0e-3b7a-4d34-9a8f-2b8d6c1e9a10"
CODEX_SID = "01a1064c-4c81-7232-ad79-a967ee51fac2"


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(wv, "_details", {})
    checkout = tmp_path / "wt" / "repo.x_1"
    checkout.mkdir(parents=True)
    folder = (
        tmp_path / "claude" / "projects" / "".join(c if c.isalnum() else "-" for c in str(checkout))
    )
    common = {"sessionId": SID, "cwd": str(checkout)}
    write_jsonl(
        folder / f"{SID}.jsonl",
        {"type": "user", "isMeta": True, **common, "message": {"content": "context"}},
        {"type": "user", **common, "message": {"content": "Fix the crash"}, "timestamp": "t1"},
        {
            "type": "assistant",
            **common,
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "Looking."},
                    {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}},
                ]
            },
        },
        {"type": "user", "isSidechain": True, **common, "message": {"content": "subagent"}},
        {
            "type": "user",
            **common,
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu1",
                        "content": [{"type": "text", "text": "a.py"}],
                        "is_error": True,
                    }
                ]
            },
        },
        {
            "type": "assistant",
            **common,
            "message": {"content": [{"type": "text", "text": "Done."}]},
        },
    )
    # Another directory's session never shows up here.
    other = folder.parent / "elsewhere"
    write_jsonl(other / f"{SID[:-1]}1.jsonl", {"type": "user", "cwd": "/elsewhere"})
    day = tmp_path / "codex" / "sessions" / "2026" / "10" / "04"
    write_jsonl(
        day / f"rollout-a-{CODEX_SID}.jsonl",
        {"type": "session_meta", "payload": {"id": CODEX_SID, "cwd": str(checkout / "lib")}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "# AGENTS.md instructions\nrules"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Add a button"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "On it."}],
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "exec", "input": "ls", "call_id": "c1"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "c1",
                "output": [{"type": "input_text", "text": "app.js"}],
            },
        },
        {"type": "response_item", "payload": {"type": "reasoning", "summary": []}},
    )
    write_jsonl(
        day / "rollout-b.jsonl",
        {"type": "session_meta", "payload": {"id": "01a1064c-0000", "cwd": "/somewhere/else"}},
    )
    return checkout, folder, day


def test_sessions_list_both_agents_with_their_first_prompt(stores):
    checkout, _, _ = stores
    listed = wv.sessions(checkout)
    assert {(s["agent"], s["id"], s["title"]) for s in listed} == {
        ("claude", SID, "Fix the crash"),
        ("codex", CODEX_SID, "Add a button"),
    }
    assert all("_file" in s for s in listed)
    # Details are remembered per file version, not read again.
    assert any(CODEX_SID in key for key in wv._details)
    assert wv.sessions(checkout / "lib")[0]["agent"] == "codex"


def test_claude_transcript_pairs_tools_with_results(stores):
    checkout, _, _ = stores
    value = wv.transcript(checkout, SID)
    assert value["session"]["agent"] == "claude" and "_file" not in value["session"]
    assert all("_file" not in s for s in value["sessions"])
    assert [(e["role"], e.get("text") or e.get("name")) for e in value["entries"]] == [
        ("user", "Fix the crash"),
        ("assistant", "Looking."),
        ("tool", "Bash"),
        ("assistant", "Done."),
    ]
    tool = value["entries"][2]
    assert (tool["brief"], tool["input"], tool["about"]) == ("ls", "ls", None)
    assert tool["output"] == "a.py" and tool["error"] is True


@pytest.mark.parametrize(
    "text,view",
    [
        ("Fix it<system-reminder>be careful</system-reminder>", ("prompt", "Fix it")),
        ("<system-reminder>only context</system-reminder>", None),
        (
            "<command-message>merge-forward</command-message>\n"
            "<command-name>/merge-forward</command-name>\n"
            "<command-args>release_25.1 into dev</command-args>",
            ("command", "/merge-forward release_25.1 into dev"),
        ),
        ("<command-name>/exit</command-name><command-args></command-args>", ("command", "/exit")),
        ("<bash-input> gh pr merge 6</bash-input>", ("command", "! gh pr merge 6")),
        (
            "<bash-stdout>merged</bash-stdout><bash-stderr>warning</bash-stderr>",
            ("output", "merged\nwarning"),
        ),
        ("<local-command-stdout></local-command-stdout>", None),
        ("<local-command-caveat>Caveat: not a request</local-command-caveat>", None),
        (
            "<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n"
            '<summary>Background command "tox" completed (exit code 0)</summary>\n'
            "</task-notification>\n<system-reminder>not user input</system-reminder>",
            ("notification", 'Background command "tox" completed (exit code 0)'),
        ),
        (
            "<task-notification><status>killed</status></task-notification>",
            ("notification", "Background task killed"),
        ),
        ("<img src=x> is broken", ("prompt", "<img src=x> is broken")),
    ],
)
def test_claude_user_turns_read_as_prompts_commands_output_or_notifications(text, view):
    assert wv.claude_prompt(text) == view


@pytest.mark.parametrize(
    "value,view",
    [
        (
            {"command": "git status\ngit log", "description": "Show state"},
            ("Show state", "git status", "git status\ngit log"),
        ),
        (
            {"file_path": "/a.py", "old_string": "x = 1\ny = 2", "new_string": "x = 3"},
            (None, "/a.py", "/a.py\n\nnew_string: x = 3\n\nold_string:\nx = 1\ny = 2"),
        ),
        ({"task_id": "b1", "limit": 3}, (None, "task_id: b1", "task_id: b1\nlimit: 3")),
        ({}, (None, None, "")),
        # Codex: JSON arguments, an argv command, and the exec tool's script.
        (
            '{"command": ["bash", "-lc", "rg foo"], "workdir": "/w"}',
            (None, "rg foo", "rg foo\n\nworkdir: /w"),
        ),
        (
            {"command": ["git", "commit", "-m", "a b"]},
            (None, "git commit -m 'a b'", "git commit -m 'a b'"),
        ),
        (
            'text(await tools.exec_command({cmd:"pwd; ls \\"x\\"",max_output_tokens:10}));',
            (
                None,
                'pwd; ls "x"',
                'text(await tools.exec_command({cmd:"pwd; ls \\"x\\"",max_output_tokens:10}));',
            ),
        ),
        (
            "*** Begin Patch\n*** Update File: a.py",
            (None, "*** Begin Patch", "*** Begin Patch\n*** Update File: a.py"),
        ),
    ],
)
def test_tool_input_is_written_out_with_its_description_and_main_argument(value, view):
    assert wv.tool_view(value) == view


def test_a_long_prompt_full_of_unclosed_tags_is_classified_quickly():
    text = "<x>" + "<br> text " * 10000
    start = time.monotonic()
    assert wv.claude_prompt(text) == ("prompt", text.strip())
    assert time.monotonic() - start < 0.5
    # Non-string argv elements read as JSON, not Python reprs.
    assert wv.tool_view({"command": ["run", {"a": 1}]})[1] == "run '{\"a\": 1}'"


def test_a_session_named_only_by_commands_takes_its_first_command(stores):
    checkout, _, _ = stores
    folder = (
        checkout.parent.parent
        / "claude"
        / "projects"
        / "".join(c if c.isalnum() else "-" for c in str(checkout))
    )
    sid = "c0ffee00-0000-4000-8000-000000000001"
    write_jsonl(
        folder / f"{sid}.jsonl",
        {
            "type": "user",
            "sessionId": sid,
            "cwd": str(checkout),
            "message": {
                "content": "<task-notification><summary>done</summary></task-notification>"
            },
        },
        {
            "type": "user",
            "sessionId": sid,
            "cwd": str(checkout),
            "message": {
                "content": "<command-name>/babysit-pr</command-name><command-args>9</command-args>"
            },
        },
    )
    titles = {s["id"]: s["title"] for s in wv.sessions(checkout)}
    assert titles[sid] == "/babysit-pr 9"


def test_codex_transcript_skips_injected_context_and_reasoning(stores):
    checkout, _, _ = stores
    value = wv.transcript(checkout, CODEX_SID)
    assert [(e["role"], e.get("text") or e.get("name")) for e in value["entries"]] == [
        ("user", "Add a button"),
        ("assistant", "On it."),
        ("tool", "exec"),
    ]
    assert value["entries"][2]["output"] == "app.js"


def test_codex_typed_prompt_events_replace_their_echoes(stores):
    checkout, _, day = stores
    path = next(day.glob(f"*{CODEX_SID}*"))
    lines = path.read_text().splitlines()[:-1]
    event = {"type": "event_msg", "payload": {"type": "user_message", "message": "Add a button"}}
    path.write_text("\n".join(lines + [json.dumps(event)]) + "\n")
    entries = wv.transcript(checkout, CODEX_SID)["entries"]
    assert [e["role"] for e in entries].count("user") == 1


def test_transcript_defaults_to_the_newest_session_and_pages_backwards(stores, monkeypatch):
    checkout, _, _ = stores
    newest = wv.transcript(checkout)
    assert newest["session"]["id"] == newest["sessions"][0]["id"]
    monkeypatch.setattr(wv, "PAGE", 2)
    last = wv.transcript(checkout, SID)
    assert (last["start"], last["total"], len(last["entries"])) == (2, 4, 2)
    first = wv.transcript(checkout, SID, before=last["start"])
    assert first["start"] == 0 and [e["text"] for e in first["entries"]][0] == "Fix the crash"


def test_transcript_prefers_a_recorded_session_over_the_newest(stores):
    checkout, _, _ = stores
    listed = wv.transcript(checkout)["sessions"]
    assert len(listed) > 1
    older = listed[-1]["id"]
    assert wv.transcript(checkout, prefer=older)["session"]["id"] == older
    # One not recorded here falls back to the newest.
    assert wv.transcript(checkout, prefer=f"{SID[:-1]}1")["session"]["id"] == listed[0]["id"]


def test_transcript_refuses_unlisted_sessions(stores, tmp_path):
    checkout, _, _ = stores
    for session in ("../../etc/passwd", f"{SID[:-1]}1", "x"):
        with pytest.raises(ValueError):
            wv.transcript(checkout, session)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert wv.transcript(empty) == {
        "sessions": [],
        "session": None,
        "entries": [],
        "start": 0,
        "first": 0,
        "total": 0,
    }


PARENT = "5b0d3c1e-8a2f-4c6d-9e1b-7f3a2c4d5e6f"


def subagent(folder, session, agent, *items, meta=None):
    """A Claude subagent transcript of ``session``, recorded in project ``folder``."""
    file = folder / session / "subagents" / f"agent-{agent}.jsonl"
    write_jsonl(file, *items)
    if meta:
        file.with_name(f"agent-{agent}.meta.json").write_text(json.dumps(meta))
    return file


def side_call(cwd, name, value):
    content = [{"type": "tool_use", "id": f"t-{name}", "name": name, "input": value}]
    return {"type": "assistant", "isSidechain": True, "cwd": cwd, "message": {"content": content}}


def test_a_fork_that_worked_here_is_listed_and_read_though_its_session_is_elsewhere(
    stores, tmp_path, monkeypatch
):
    checkout, folder, _ = stores
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(wv, "_scanned", {})
    monkeypatch.setattr(wv, "_subagents", {})
    elsewhere = folder.parent / "-elsewhere"
    fork = subagent(
        elsewhere,
        PARENT,
        "a1b2c3d4e5f6",
        {"type": "fork-context-ref", "parentSessionId": PARENT},
        {"type": "user", "isSidechain": True, "cwd": "/elsewhere", "message": {"content": "Go"}},
        side_call("/elsewhere", "Edit", {"file_path": f"{checkout}/a.py", "old_string": "a"}),
        {
            "type": "assistant",
            "isSidechain": True,
            "cwd": "/elsewhere",
            "message": {"content": [{"type": "text", "text": "Pushed the fix."}]},
        },
        meta={"agentType": "fork", "description": "Fix it in the other checkout"},
    )
    # Commands that only read it, a sibling checkout, and a subagent of a session recorded
    # here (listed already) are not listed.
    reader = subagent(
        elsewhere,
        PARENT,
        "b0",
        side_call("/elsewhere", "Bash", {"command": f"cd {checkout}; git log"}),
    )
    subagent(
        elsewhere,
        PARENT,
        "c0",
        side_call("/elsewhere", "Bash", {"command": f"cd {checkout}-old && git commit -m x"}),
    )
    subagent(
        folder, SID, "d0", side_call(str(checkout), "Write", {"file_path": f"{checkout}/b.py"})
    )
    # Its shell spells the checkout from ~, and commits there.
    tilde = f"~/{checkout.relative_to(tmp_path)}"
    committer = subagent(
        elsewhere,
        PARENT,
        "e0",
        side_call("/elsewhere", "Bash", {"command": f"cd {tilde} && git commit -qm x"}),
    )
    for file, when in ((committer, 1_700_000_000), (fork, time.time() + 60)):
        os.utime(file, (when, when))

    listed = wv.forks(checkout)
    assert [
        (r["id"], r["fork_of"], r["recorded_in"], r["subagent"], r["title"]) for r in listed
    ] == [
        ("agent-a1b2c3d4e5f6", PARENT, "/elsewhere", "fork", "Fix it in the other checkout"),
        ("agent-e0", PARENT, "/elsewhere", None, None),
    ]
    # Shown, but a session recorded here is still what the view opens on.
    value = wv.transcript(checkout)
    assert value["sessions"][0]["id"] == "agent-a1b2c3d4e5f6"
    assert value["session"]["id"] == value["sessions"][1]["id"] in {SID, CODEX_SID}
    value = wv.transcript(checkout, "agent-a1b2c3d4e5f6")
    assert {k: value["session"][k] for k in ("fork_of", "recorded_in", "subagent")} == {
        "fork_of": PARENT,
        "recorded_in": "/elsewhere",
        "subagent": "fork",
    }
    assert "_file" not in value["session"]
    assert [(e["role"], e.get("text") or e.get("name")) for e in value["entries"]] == [
        ("user", "Go"),
        ("tool", "Edit"),
        ("assistant", "Pushed the fix."),
    ]
    assert [h["session"] for h in wv.search(checkout, "pushed the fix")["results"]] == [
        "agent-a1b2c3d4e5f6"
    ]
    # Read, never resumed: Claude resumes its session, not the fork.
    assert "agent-a1b2c3d4e5f6" not in {s["id"] for s in wv.resumable(checkout)}
    # A file is read on from where the last listing stopped, and listed once it commits.
    assert wv._scanned[(str(reader), str(checkout.resolve()))][2] is False
    reader.write_text(
        reader.read_text().replace('{"partial', "")
        + json.dumps(side_call("/elsewhere", "Bash", {"command": f"git -C {checkout} push"}))
        + "\n"
    )
    assert "agent-b0" in {r["id"] for r in wv.forks(checkout)}


def test_a_running_agents_session_is_read_wherever_it_was_recorded(stores, tmp_path):
    """An agent started in a pane that had left the checkout records its session under
    that directory; the workspace it runs in shows it all the same."""
    checkout, folder, day = stores
    away, codex_away, nowhere = f"{SID[:-1]}2", f"{CODEX_SID[:-1]}3", f"{SID[:-1]}4"
    common = {"sessionId": away, "cwd": "/elsewhere"}
    write_jsonl(
        folder.parent / "-elsewhere" / f"{away}.jsonl",
        {"type": "user", **common, "message": {"content": "Profile the scripts"}},
        {"type": "assistant", **common, "message": {"content": [{"type": "text", "text": "Ok."}]}},
    )
    write_jsonl(
        day / f"rollout-c-{codex_away}.jsonl",
        {"type": "session_meta", "payload": {"id": codex_away, "cwd": "/elsewhere"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Port it"}},
    )
    recorded = [s["id"] for s in wv.sessions(checkout)]
    assert away not in recorded and codex_away not in recorded

    def unasked():
        raise AssertionError("a recorded session needs no look at the agents")

    assert wv.transcript(checkout, SID, running=unasked)["session"]["id"] == SID
    assert wv.transcript(checkout, running=unasked)["session"]["id"] in recorded
    for session, agent, prompt in [
        (away, "claude", "Profile the scripts"),
        (codex_away, "codex", "Port it"),
    ]:
        with pytest.raises(ValueError, match="not recorded"):
            wv.transcript(checkout, session)
        # Some other session's agent does not open it either.
        with pytest.raises(ValueError, match="not recorded"):
            wv.transcript(checkout, session, running=lambda: {SID})
        value = wv.transcript(checkout, session, running=lambda session=session: {session})
        assert (value["session"]["id"], value["session"]["agent"]) == (session, agent)
        assert value["session"]["title"] == prompt and value["entries"][0]["text"] == prompt
        # Listed with the checkout's own, so the picker shows what is open.
        assert sorted(s["id"] for s in value["sessions"]) == sorted([*recorded, session])
        assert all("_file" not in s for s in value["sessions"])
    # A session herdr names that no store holds stays unread, as does a malformed one.
    with pytest.raises(ValueError, match="not recorded"):
        wv.transcript(checkout, nowhere, running=lambda: {nowhere})
    with pytest.raises(ValueError, match="listed session"):
        wv.transcript(checkout, "../*", running=lambda: {"../*"})


def test_long_text_is_clipped():
    assert wv.clip("x" * 30, 10).startswith("x" * 10) and "20 more" in wv.clip("x" * 30, 10)
    assert wv.clip({"a": 1}, 100) == '{"a": 1}'


def test_a_growing_transcript_is_read_only_from_where_it_stopped(stores, monkeypatch):
    checkout, folder, _ = stores
    fed = []
    real = wv.Transcript.feed
    monkeypatch.setattr(
        wv.Transcript, "feed", lambda self, item: fed.append(item) or real(self, item)
    )
    first = wv.transcript(checkout, SID)
    count = len(fed)
    wv.transcript(checkout, SID)
    assert len(fed) == count
    transcript_file = folder / f"{SID}.jsonl"
    # Complete the partial last line as something else, then add a prompt.
    with transcript_file.open("a") as stream:
        stream.write(
            '": 1}\n' + json.dumps({"type": "user", "message": {"content": "More"}}) + "\n"
        )
    value = wv.transcript(checkout, SID, after=first["total"])
    assert value["start"] == first["total"] and [e["text"] for e in value["entries"]] == ["More"]
    assert len(fed) == count + 2
    # A rewritten file is read again from the start.
    transcript_file.write_text(
        json.dumps({"type": "user", "cwd": str(checkout), "message": {"content": "New"}}) + "\n"
    )
    assert [e["text"] for e in wv.transcript(checkout, SID)["entries"]] == ["New"]


def test_a_reopened_store_reads_on_from_where_it_stopped(stores, monkeypatch):
    """After a restart only new bytes are parsed, and an open tool call still gets its
    result, though its entry was saved before."""
    checkout, folder, _ = stores
    fed = []
    real = wv.Transcript.feed
    monkeypatch.setattr(
        wv.Transcript, "feed", lambda self, item: fed.append(item) or real(self, item)
    )
    path = folder / f"{SID}.jsonl"
    common = {"sessionId": SID, "cwd": str(checkout)}
    call = {"type": "tool_use", "id": "late", "name": "Bash", "input": {"command": "make"}}
    path.write_text(
        json.dumps({"type": "user", **common, "message": {"content": "Build it"}})
        + "\n"
        + json.dumps({"type": "assistant", **common, "message": {"content": [call]}})
        + "\n"
    )
    first = wv.transcript(checkout, SID)
    assert first["entries"][-1]["output"] is None
    # A restarted dashboard: no caches, the same store file.
    monkeypatch.setattr(wv, "_store", None)
    monkeypatch.setattr(wv, "_details", {})
    fed.clear()
    assert wv.transcript(checkout, SID)["entries"] == first["entries"]
    assert fed == []
    result = {"type": "tool_result", "tool_use_id": "late", "content": "built"}
    with path.open("a") as stream:
        stream.write(
            json.dumps({"type": "user", **common, "message": {"content": [result]}}) + "\n"
        )
    monkeypatch.setattr(wv, "_store", None)
    value = wv.transcript(checkout, SID, after=0)
    assert len(fed) == 1 and value["entries"][-1]["output"] == "built"
    hits = wv.search(checkout, "built")["results"]
    assert [(hit["session"], hit["index"]) for hit in hits] == [(SID, 1)]
    # A store kept by another parser version is parsed again.
    monkeypatch.setattr(wv, "PARSER", wv.PARSER + 1)
    fed.clear()
    assert wv.transcript(checkout, SID)["entries"] == value["entries"]
    assert len(fed) == 3


def test_after_beyond_a_page_falls_back_to_the_newest_page(stores, monkeypatch):
    checkout, _, _ = stores
    monkeypatch.setattr(wv, "PAGE", 1)
    value = wv.transcript(checkout, SID, after=0)
    assert (value["start"], len(value["entries"])) == (3, 1)


def test_claude_sessions_in_subdirectories_and_command_wrappers(stores):
    checkout, folder, _ = stores
    sub = folder.parent / (folder.name + "-lib")
    other = "2f2c6f0e-3b7a-4d34-9a8f-2b8d6c1e9a10"
    common = {"sessionId": other, "cwd": str(checkout / "lib")}
    write_jsonl(
        sub / f"{other}.jsonl",
        {"type": "user", **common, "message": {"content": "<command-name>/clear</command-name>"}},
        {"type": "user", **common, "isCompactSummary": True, "message": {"content": "Summary"}},
        {"type": "user", **common, "message": {"content": "Real prompt"}},
    )
    listed = {s["id"]: s for s in wv.sessions(checkout)}
    assert listed[other]["title"] == "Real prompt"
    # A sibling directory sharing the prefix is not a subdirectory.
    sibling = folder.parent / (folder.name + "2")
    stranger = "3f2c6f0e-3b7a-4d34-9a8f-2b8d6c1e9a10"
    write_jsonl(sibling / f"{stranger}.jsonl", {"type": "user", "cwd": str(checkout) + "2"})
    assert stranger not in {s["id"] for s in wv.sessions(checkout)}


def test_codex_failures_are_marked(stores):
    checkout, _, day = stores
    path = next(day.glob(f"*{CODEX_SID}*"))
    lines = path.read_text().splitlines()[:-1]
    failed = {
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": "c1",
            "output": '{"exit_code":127,"output":"pnpm: not found"}',
        },
    }
    path.write_text("\n".join(lines + [json.dumps(failed)]) + "\n")
    tool = next(e for e in wv.transcript(checkout, CODEX_SID)["entries"] if e["role"] == "tool")
    assert tool["error"] and "pnpm" in tool["output"]


@pytest.mark.parametrize(
    ("output", "failed"),
    [
        ([{"type": "input_text", "text": "Script failed\nboom"}], True),
        ([{"type": "input_text", "text": "Script error\nbad"}], True),
        # The runner's verdict wins over an inner command's exit code.
        ([{"type": "input_text", "text": 'Script completed\n{"exit_code":1}'}], False),
        ("x" * 5000 + '{"exit_code":3}', True),
    ],
)
def test_codex_failures_follow_the_script_verdict(stores, output, failed):
    checkout, _, day = stores
    path = next(day.glob(f"*{CODEX_SID}*"))
    lines = path.read_text().splitlines()[:-1]
    result = {
        "type": "response_item",
        "payload": {"type": "custom_tool_call_output", "call_id": "c1", "output": output},
    }
    path.write_text("\n".join(lines + [json.dumps(result)]) + "\n")
    tool = next(e for e in wv.transcript(checkout, CODEX_SID)["entries"] if e["role"] == "tool")
    assert tool["error"] is failed


def test_a_codex_turn_that_ended_in_an_error_is_shown(stores):
    checkout, _, day = stores
    path = next(day.glob(f"*{CODEX_SID}*"))
    lines = path.read_text().splitlines()[:-1]
    message = "This content was flagged for possible biological risk."
    ended = [
        {
            "type": "event_msg",
            "timestamp": "t9",
            "payload": {
                "type": "task_complete",
                "last_agent_message": None,
                "error": {"message": message, "codex_error_info": "bio_policy"},
            },
        },
        # A turn that finished adds nothing: its reply is recorded as a message.
        {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "Ok"}},
    ]
    path.write_text("\n".join(lines + [json.dumps(item) for item in ended]) + "\n")
    entries = wv.transcript(checkout, CODEX_SID)["entries"]
    assert [e for e in entries if e["role"] == "user"][1:] == [
        {"role": "user", "kind": "error", "text": message, "code": "bio_policy", "time": "t9"}
    ]
    # Found by its message and by its code.
    for query in ("biological", "bio_policy"):
        hits = wv.search(checkout, query)["results"]
        assert [(hit["kind"], hit["index"]) for hit in hits] == [("error", len(entries) - 1)]


def test_codex_prompts_keep_one_copy_whichever_comes_first(tmp_path):
    parser = wv.Transcript("codex")
    item = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "A"}],
        },
    }
    event = {"type": "event_msg", "payload": {"type": "user_message", "message": "A"}}
    for record in (
        item,
        event,
        event,
        item,
        {**event, "payload": {"type": "user_message", "message": "B"}},
    ):
        parser.feed(record)
    assert [e["text"] for e in parser.entries] == ["A", "A", "B"]


def test_undecodable_session_files_are_skipped(stores):
    checkout, folder, _ = stores
    broken = "4f2c6f0e-3b7a-4d34-9a8f-2b8d6c1e9a10"
    (folder / f"{broken}.jsonl").write_bytes(
        json.dumps({"type": "user", "cwd": str(checkout)}).encode() + b"\n\xe2\x80"
    )
    assert broken in {s["id"] for s in wv.sessions(checkout)}
    assert len(wv.sessions(checkout)) == 3


def test_a_rollout_still_writing_its_first_line_is_skipped(stores):
    checkout, _, day = stores
    (day / "rollout-c.jsonl").write_text('{"type": "session_meta", "payload": {"id"')
    assert len(wv.sessions(checkout)) == 2
    assert not any("rollout-c" in key for key in wv._details)


def test_paths_are_shown_unquoted(repo):
    path, git = repo
    (path / "café menu.txt").write_text("x\n")
    git("add", ".")
    value = wv.diff(path, "uncommitted")
    assert "café menu.txt" in [f["path"] for f in value["files"]]


def test_question_tools_carry_their_questions_and_claudes_answers(stores):
    checkout, _, day = stores
    folder = (
        checkout.parent.parent
        / "claude"
        / "projects"
        / "".join(c if c.isalnum() else "-" for c in str(checkout))
    )
    sid = "c0ffee00-0000-4000-8000-000000000002"
    common = {"sessionId": sid, "cwd": str(checkout)}
    questions = [
        {
            "question": "Ship it?",
            "header": "Ship",
            "multiSelect": False,
            "options": [{"label": "Yes", "description": "Merge now"}, {"label": "No"}],
        }
    ]
    write_jsonl(
        folder / f"{sid}.jsonl",
        {"type": "user", **common, "message": {"content": "Go"}},
        {
            "type": "assistant",
            **common,
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tq",
                        "name": "AskUserQuestion",
                        "input": {"questions": questions},
                    }
                ]
            },
        },
        {
            "type": "user",
            **common,
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "tq", "content": "answered"}]
            },
            "toolUseResult": {"answers": {"Ship it?": "Yes", "bad": 3}},
        },
    )
    tool = wv.transcript(checkout, sid)["entries"][-1]
    assert tool["id"] == "tq" and tool["answers"] == {"Ship it?": "Yes"}
    assert tool["questions"] == [
        {
            "question": "Ship it?",
            "header": "Ship",
            "multi": False,
            "options": [
                {"label": "Yes", "description": "Merge now"},
                {"label": "No", "description": None},
            ],
        }
    ]
    previews = wv.question_list(
        {
            "questions": [
                {
                    "question": "Choose",
                    "options": [
                        {"label": "A", "markdown": "**First** preview"},
                        {"label": "B", "preview": "Second preview"},
                    ],
                }
            ]
        }
    )
    assert [option["preview"] for option in previews[0]["options"]] == [
        "**First** preview",
        "Second preview",
    ]
    # Codex's request names a question its title and lists bare option strings.
    codex = wv.question_list('{"questions":[{"title":"Which?","options":["A","B"]}, 5]}')
    assert codex == [
        {
            "question": "Which?",
            "header": None,
            "multi": False,
            "options": [{"label": "A", "description": None}, {"label": "B", "description": None}],
        }
    ]
    assert wv.question_list({"questions": "no"}) is None and wv.question_list("not json") is None


# Bounds on what a transcript can make the parser hold or do ------------------------


def prompt_line(text):
    return (json.dumps({"type": "user", "message": {"content": text}}) + "\n").encode()


# A listing entry for a transcript the test feeds through its own ``read``.
LISTED = {"id": SID, "agent": "claude", "title": None, "updated": 0, "size": 0, "_file": "f"}


def test_claude_codes_recaps_and_notices_between_turns_are_shown():
    def system(subtype, content, **more):
        record = {"type": "system", "subtype": subtype, "content": content, "timestamp": "t"}
        return (json.dumps(record | more) + "\n").encode()

    recap = "Hop 26.1→26.2 conflicts are resolved. Running the checks next."
    parser = wv.Transcript("claude")
    parser.consume(
        system("away_summary", recap)
        + system("compact_boundary", "Conversation compacted")
        + system("scheduled_task_fire", "Claude resuming /loop wakeup (Nov 21 9:29am)")
        + system("informational", "Usage limit reached · set to continue automatically at 3am")
        # Bookkeeping the terminal does not show, and subagents' records, stay out.
        + system("turn_duration", None, durationMs=5)
        + system("stop_hook_summary", "")
        + system("informational", "   ")
        + system("compact_boundary", "Conversation compacted", isSidechain=True)
    )
    assert parser.entries == [
        {"role": "assistant", "kind": "summary", "text": recap, "time": "t"},
        {"role": "user", "kind": "notice", "text": "Conversation compacted", "time": "t"},
        {
            "role": "user",
            "kind": "notice",
            "text": "Claude resuming /loop wakeup (Nov 21 9:29am)",
            "time": "t",
        },
        {
            "role": "user",
            "kind": "notice",
            "text": "Usage limit reached · set to continue automatically at 3am",
            "time": "t",
        },
    ]


def test_a_long_transcript_keeps_its_newest_entries_and_whole_transcript_indices(monkeypatch):
    monkeypatch.setattr(wv, "MAX_ENTRIES", 10)
    tool = {"type": "tool_use", "id": "old", "name": "Bash", "input": {"command": "ls"}}
    chunks = [
        (json.dumps({"type": "assistant", "message": {"content": [tool]}}) + "\n").encode(),
        b"".join(prompt_line(f"p{i}") for i in range(24)),
    ]
    parsers = []

    def read(parser, file, save):
        parsers.append(parser)
        while chunks:
            parser.consume(chunks.pop(0))
            save()
        return True

    def page(**args):
        return wv.transcript_page(
            [LISTED],
            lambda file: ("w", file),
            read,
            SID,
            args.get("before"),
            args.get("after"),
            None,
            args.get("around"),
            args.get("later"),
        )

    newest = page()
    # 25 entries: past 10, the oldest go with a tenth (one) more, and their tool calls.
    assert (newest["start"], newest["first"], newest["total"]) == (16, 16, 25)
    assert "old" not in parsers[0].tools
    assert [e["text"] for e in newest["entries"]] == [f"p{i}" for i in range(15, 24)]
    assert page(after=23)["entries"][0]["text"] == "p22" and page(after=23)["start"] == 23
    # Nothing before the first kept entry: an older position answers from there.
    assert page(before=5)["entries"] == [] and page(before=5)["start"] == 16
    assert page(after=3)["start"] == 16
    monkeypatch.setattr(wv, "PAGE", 4)
    around = page(around=20)
    assert (around["start"], [e["text"] for e in around["entries"]]) == (
        18,
        ["p17", "p18", "p19", "p20"],
    )
    later = page(later=22)
    assert (later["start"], [e["text"] for e in later["entries"]]) == (22, ["p21", "p22", "p23"])
    assert page(later=3)["start"] == 16


def test_lines_that_are_not_objects_or_nest_too_deeply_are_passed_over():
    parser = wv.Transcript("claude")
    deep = b"[" * 200000 + b"]" * 200000 + b"\n"
    used = parser.consume(deep + b"[1, 2]\n" + b'"text"\n' + prompt_line("after"))
    assert [e["text"] for e in parser.entries] == ["after"]
    assert used == parser.offset and parser.offset > len(deep)
    codex = wv.Transcript("codex")
    codex.consume(b'{"payload": ' * 100000 + b"\n")
    assert codex.entries == [] and codex.offset > 0


def test_a_line_too_long_to_parse_is_skipped_once(tmp_path, monkeypatch):
    monkeypatch.setattr(wv, "MAX_LINE", 1000)
    monkeypatch.setattr(wv, "CHUNK", 64)
    path = tmp_path / "t.jsonl"
    path.write_bytes(prompt_line("before") + prompt_line("x" * 5000) + prompt_line("after"))
    parser = wv.Transcript("claude")
    parser.update(path)
    texts = [e["text"] for e in parser.entries]
    assert texts[0] == "before" and texts[2] == "after" and "too long" in texts[1]
    assert parser.offset == path.stat().st_size and not parser.skipping
    # Still being written: the long line is passed over as it grows, never held whole.
    with path.open("ab") as stream:
        stream.write(b'{"type": "user", "message": {"content": "' + b"y" * 3000)
    parser.update(path)
    assert parser.skipping and parser.held == 0 and parser.offset == path.stat().st_size
    with path.open("ab") as stream:
        stream.write(b"y" * 3000 + b'"}}\n' + prompt_line("done"))
    parser.update(path)
    assert parser.entries[-1]["text"] == "done"
    assert sum("too long" in e["text"] for e in parser.entries) == 2


def test_a_busy_transcript_answers_from_its_last_complete_read(monkeypatch):
    listed = [LISTED]
    reads = []

    def read(parser, file, save):
        reads.append(file)
        parser.consume(prompt_line(f"r{len(reads)}"))
        return True

    def page():
        return wv.transcript_page(listed, lambda file: ("w", file), read, SID, None, None, None)

    busy = wv._reading.setdefault(("w", "f"), threading.Lock())
    with busy:
        with pytest.raises(ValueError, match="still being read"):
            page()
    assert reads == []
    assert [e["text"] for e in page()["entries"]] == ["r1"]
    with busy:
        started = time.monotonic()
        value = page()
    assert time.monotonic() - started < 5
    assert [e["text"] for e in value["entries"]] == ["r1"] and len(reads) == 1


def test_a_transcript_saved_by_another_reader_meanwhile_is_not_overwritten(monkeypatch):
    listed = [LISTED]

    def read(parser, file, save):
        parser.consume(prompt_line("mine"))
        # Another process read the same file and saved first.
        row = wv.store().row("w", "f")
        wv.store().save(
            row["id"],
            row["generation"],
            restarted=False,
            added=[(0, json.dumps({"role": "user", "text": "theirs"}), "theirs")],
            changed=[],
            place=(None, 9, False, 1, 0, "{}"),
            ready=True,
        )
        return True

    with pytest.raises(ValueError, match="read elsewhere"):
        wv.transcript_page(listed, lambda file: ("w", file), read, SID, None, None, None)
    assert wv.store().page(wv.store().row("w", "f")["id"], 0, 5) == [
        {"role": "user", "text": "theirs"}
    ]


def test_only_lines_that_can_be_an_object_are_parsed(monkeypatch):
    parsed = []
    loads = wv.json.loads

    def spy(line, *args, **kwargs):
        parsed.append(line[:1])
        return loads(line, *args, **kwargs)

    monkeypatch.setattr(wv.json, "loads", spy)
    parser = wv.Transcript("claude")
    parser.consume(b"[" * 100000 + b"\n" + b'"text"\n' + b"  " + prompt_line("after"))
    assert [e["text"] for e in parser.entries] == ["after"]
    assert parsed == [b" "]


# Searching a checkout's sessions -------------------------------------------------


def test_search_finds_entries_in_every_session_of_the_checkout_only(stores, tmp_path):
    checkout, folder, _ = stores
    stranger = "5f2c6f0e-3b7a-4d34-9a8f-2b8d6c1e9a10"
    write_jsonl(
        folder.parent / "elsewhere-repo" / f"{stranger}.jsonl",
        {"type": "user", "cwd": str(tmp_path / "other"), "message": {"content": "A button"}},
    )
    found = wv.search(checkout, "a.py")
    assert (found["total"], found["searched"], found["behind"]) == (1, 2, 0)
    [hit] = found["results"]
    assert (hit["session"], hit["role"], hit["name"], hit["index"]) == (SID, "tool", "Bash", 2)
    assert {"text": "a.py", "match": True} in hit["snippet"]
    [button] = wv.search(checkout, "  BUTTON ")["results"]
    assert (button["session"], button["kind"], button["agent"]) == (CODEX_SID, "prompt", "codex")
    assert wv.search(checkout, "  BUTTON ")["query"] == "BUTTON"
    # Every word must match; a word too short to match alone makes the query one phrase.
    assert wv.search(checkout, "fix crash")["total"] == 1
    assert wv.search(checkout, "fix button")["total"] == 0
    assert [h["session"] for h in wv.search(checkout, "on it")["results"]] == [CODEX_SID]
    for bad, reason in (("ab", "at least 3"), ("", "Type something"), ("x" * 201, "at most")):
        with pytest.raises(ValueError, match=reason):
            wv.search(checkout, bad)


def test_search_reads_unopened_sessions_within_its_budget_and_says_what_it_skipped(
    stores, monkeypatch
):
    checkout, _, _ = stores
    monkeypatch.setattr(wv, "SEARCH_BUDGET", 0)
    nothing = wv.search(checkout, "Done")
    assert (nothing["results"], nothing["searched"], nothing["behind"]) == ([], 0, 2)
    monkeypatch.setattr(wv, "SEARCH_BUDGET", 20)
    # A session another request is still reading is searched as far as it was saved:
    # not at all, before its first save.
    codex = next(s["_file"] for s in wv.sessions(checkout) if s["id"] == CODEX_SID)
    with wv._reading.setdefault(("", str(codex)), threading.Lock()):
        value = wv.search(checkout, "Done")
    assert (value["total"], value["searched"], value["behind"]) == (1, 1, 1)
    assert wv.search(checkout, "Done")["behind"] == 0


# Kept once the file is gone ----------------------------------------------------------


def texts(value):
    return [entry.get("text") or entry.get("name") for entry in value["entries"]]


def test_a_session_whose_file_is_gone_is_still_listed_shown_and_searched(stores, tmp_path):
    checkout, folder, _ = stores
    opened = wv.transcript(checkout, SID)
    assert "archived" not in opened["session"]
    # Claude deletes a session file a month after it last changed.
    (folder / f"{SID}.jsonl").unlink()
    assert [s["id"] for s in wv.sessions(checkout)] == [CODEX_SID]
    value = wv.transcript(checkout, SID)
    assert value["session"] == {**opened["session"], "archived": True}
    assert texts(value) == texts(opened) == ["Fix the crash", "Looking.", "Bash", "Done."]
    assert {s["id"]: s.get("archived") for s in value["sessions"]} == {SID: True, CODEX_SID: None}
    assert all("_file" not in s and "_cwd" not in s for s in value["sessions"])
    assert wv.transcript(checkout, SID, before=2)["entries"] == opened["entries"][:2]
    # The one never opened was not kept, and is gone with its file.
    (tmp_path / "codex").rename(tmp_path / "codex-gone")
    assert [s["id"] for s in wv.transcript(checkout)["sessions"]] == [SID]
    found = wv.search(checkout, "a.py")
    assert (found["total"], found["searched"], found["behind"]) == (1, 1, 0)
    assert found["results"][0]["session"] == SID
    # Its worktree removed as well, the checkout's path still finds it; another's does not.
    shutil.rmtree(checkout)
    assert texts(wv.transcript(checkout)) == texts(opened)
    assert wv.transcript(checkout.with_name("repo"))["sessions"] == []
    with pytest.raises(ValueError, match="not recorded"):
        wv.transcript(checkout.with_name("repo"), SID)


def test_a_kept_transcript_outlives_a_parser_change_once_its_file_is_gone(stores, monkeypatch):
    checkout, folder, _ = stores
    opened = wv.transcript(checkout, SID)

    def as_another_parser_left_it():
        with sqlite3.connect(wv.store().path) as db:
            db.execute("UPDATE entries SET data = replace(data, 'Done.', 'Old.')")
        monkeypatch.setattr(wv, "PARSER", wv.PARSER + 1)

    # While the file is there it is parsed again, from its start.
    as_another_parser_left_it()
    assert texts(wv.transcript(checkout, SID)) == texts(opened)
    (folder / f"{SID}.jsonl").unlink()
    as_another_parser_left_it()
    assert texts(wv.transcript(checkout, SID)) == [*texts(opened)[:-1], "Old."]


def test_a_reread_cut_short_leaves_the_whole_transcript_as_the_record(stores, monkeypatch):
    checkout, folder, _ = stores
    file = folder / f"{SID}.jsonl"
    opened = wv.transcript(checkout, SID)
    assert len(opened["entries"]) == 4
    # The parser changed, and the read from the start fails after its first lines.
    monkeypatch.setattr(wv, "PARSER", wv.PARSER + 1)
    lines = file.read_bytes().split(b"\n")

    def cut_short(parser, path, save):
        parser.position(file.stat().st_ino, file.stat().st_size)
        parser.absorb(b"\n".join(lines[:2]) + b"\n")
        save()
        raise OSError("The worker went away")

    monkeypatch.setattr(wv, "local_read", cut_short)
    with pytest.raises(OSError):
        wv.transcript(checkout, SID)
    assert wv.store().row("", str(file))["saved"] == 1
    # Then the file is gone: what the last whole read kept is shown and searched.
    file.unlink()
    value = wv.transcript(checkout, SID)
    assert value["session"]["archived"] and value["entries"] == opened["entries"]
    assert (value["start"], value["first"], value["total"]) == (0, 0, 4)
    assert wv.transcript(checkout, SID, before=2)["entries"] == opened["entries"][:2]
    [hit] = wv.search(checkout, "Done")["results"]
    assert (hit["session"], hit["index"]) == (SID, 3)


def test_the_archive_keeps_sessions_nobody_opened_and_reads_only_what_grew(
    stores, monkeypatch, tmp_path
):
    checkout, folder, day = stores
    # Every Claude account's store is swept: none but the test's own may be found.
    monkeypatch.setenv("HOME", str(tmp_path))
    # A file that ends in an unfinished line is read again for its end; these do not.
    for file in (*folder.parent.glob("*/*.jsonl"), *day.glob("*.jsonl")):
        file.write_text(file.read_text().removesuffix('{"partial'))
    read = []
    real = wv.local_read
    monkeypatch.setattr(
        wv, "local_read", lambda parser, file, save: read.append(file) or real(parser, file, save)
    )
    # Every session of this machine's stores, the other checkouts' too.
    assert wv.archive_local() == 4 and len(read) == 4
    assert wv.archive_local() == 0 and len(read) == 4
    with (folder / f"{SID}.jsonl").open("a") as stream:
        stream.write(json.dumps({"type": "user", "message": {"content": "And now?"}}) + "\n")
    assert wv.archive_local() == 1 and read[4:] == [str(folder / f"{SID}.jsonl")]
    # Kept unindexed, until a search needs them.
    with sqlite3.connect(wv.store().path) as db:
        assert db.execute("SELECT count(*) FROM entries WHERE indexed").fetchone() == (0,)
    shutil.rmtree(folder)
    shutil.rmtree(day)
    value = wv.transcript(checkout)
    assert [(s["id"], s["archived"], s["title"]) for s in value["sessions"]] == [
        (SID, True, "Fix the crash"),
        (CODEX_SID, True, "Add a button"),
    ]
    assert texts(value) == ["Fix the crash", "Looking.", "Bash", "Done.", "And now?"]
    assert texts(wv.transcript(checkout, CODEX_SID)) == ["Add a button", "On it.", "exec"]
    assert wv.search(checkout, "button")["results"][0]["session"] == CODEX_SID
    assert wv.transcript(checkout / "lib")["sessions"][0]["id"] == CODEX_SID


def test_an_archived_session_s_file_is_written_back_as_it_was_kept(stores, monkeypatch, tmp_path):
    checkout, folder, day = stores
    monkeypatch.setenv("HOME", str(tmp_path))
    file, rollout = folder / f"{SID}.jsonl", day / f"rollout-a-{CODEX_SID}.jsonl"
    whole = file.read_bytes().removesuffix(b'{"partial')
    codex = rollout.read_bytes().removesuffix(b'{"partial')
    rollout.write_bytes(codex)
    opened = wv.transcript(checkout, SID)
    # A session only opened here has a transcript, but no copy of its file yet.
    assert wv.store().row("", str(file))["raw_size"] == 0
    wv.archive_local()
    assert wv.store().row("", str(file))["raw_size"] == len(whole) + len('{"partial')
    assert all("restorable" not in s for s in wv.resumable(checkout))
    file.unlink()
    shutil.rmtree(day.parents[1])
    listed = {s["id"]: s for s in wv.resumable(checkout)}
    assert {(s["archived"], s["restorable"]) for s in listed.values()} == {(True, True)}
    assert wv.transcript(checkout, SID)["session"]["restorable"] is True
    # Written back up to its last whole line, as a file of its owner's alone.
    assert wv.restore(listed[SID]) == file and file.read_bytes() == whole
    assert file.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in folder.iterdir()] == [file.name]
    assert wv.restore(listed[CODEX_SID]) == rollout and rollout.read_bytes() == codex
    # Sessions again as any other, and nothing of them is parsed a second time.
    read = []
    with monkeypatch.context() as patch:
        patch.setattr(wv.Transcript, "feed", lambda self, item: read.append(item))
        assert [s.get("archived") for s in wv.sessions(checkout)] == [None, None]
        value = wv.transcript(checkout, SID)
        assert texts(value) == texts(opened) and "archived" not in value["session"]
        wv.archive_local()
    assert read == []
    # Resumed, it grows: kept on, and written back with what it gained.
    with file.open("a") as stream:
        stream.write(json.dumps({"type": "user", "message": {"content": "And now?"}}) + "\n")
    grown = file.read_bytes()
    wv.archive_local()
    file.unlink()
    (again,) = [s for s in wv.resumable(checkout) if s["id"] == SID]
    wv.restore(again)
    assert file.read_bytes() == grown
    assert texts(wv.transcript(checkout, SID))[-1] == "And now?"
    # One written back without its unfinished last line is kept again from its start,
    # never on from where the copy with that line ended.
    with file.open("a") as stream:
        stream.write('{"partial')
    wv.archive_local()
    file.unlink()
    wv.restore(again)
    assert file.read_bytes() == grown
    row = wv.store().row("", str(file))
    # Also when the file then grows to just the length of that copy.
    with file.open("a") as stream:
        stream.write('{"a": 1}\n')
    assert file.stat().st_size == row["raw_size"]
    wv.archive_local()
    assert b"".join(wv.store().kept(row["id"])) == file.read_bytes()
    with file.open("a") as stream:
        stream.write(json.dumps({"type": "user", "message": {"content": "Once more"}}) + "\n")
    grown = file.read_bytes()
    wv.archive_local()
    row = wv.store().row("", str(file))
    assert b"".join(wv.store().kept(row["id"])) == grown
    assert texts(wv.transcript(checkout, SID))[-2:] == ["And now?", "Once more"]
    # Never over a file that is there.
    file.write_bytes(b"its own\n")
    assert wv.restore(again) == file and file.read_bytes() == b"its own\n"
    # A copy that cannot be kept on (a piece of it is damaged) is made again.
    file.unlink()
    wv.restore(again)
    row = wv.store().row("", str(file))
    assert wv.store().kept_files("")[str(file)] == [row["id"], file.stat().st_ino, len(grown)]
    with sqlite3.connect(wv.store().path) as db:
        db.execute("UPDATE raw SET data=x'00' WHERE transcript=?", (row["id"],))
    with file.open("a") as stream:
        stream.write(json.dumps({"type": "user", "message": {"content": "Still?"}}) + "\n")
    wv.archive_local()
    assert b"".join(wv.store().kept(row["id"])) == file.read_bytes()
    with sqlite3.connect(wv.store().path) as db:
        db.execute("DELETE FROM raw WHERE transcript=?", (row["id"],))
    with file.open("a") as stream:
        stream.write(json.dumps({"type": "user", "message": {"content": "And yet?"}}) + "\n")
    wv.archive_local()
    assert b"".join(wv.store().kept(row["id"])) == file.read_bytes()


def test_a_copy_that_is_behind_its_file_is_not_offered_to_resume_from(
    stores, monkeypatch, tmp_path
):
    checkout, folder, _ = stores
    monkeypatch.setenv("HOME", str(tmp_path))
    file = folder / f"{SID}.jsonl"
    file.write_bytes(file.read_bytes().removesuffix(b'{"partial'))
    wv.archive_local()
    kept = file.stat().st_size
    # Grown past what is kept as it is: its transcript is read on, its copy is not.
    monkeypatch.setattr(wv, "RAW_MAX", kept)
    with file.open("a") as stream:
        stream.write(json.dumps({"type": "user", "message": {"content": "And now?"}}) + "\n")
    wv.archive_local()
    row = wv.store().row("", str(file))
    assert (row["raw_size"], row["size"]) == (kept, file.stat().st_size)
    file.unlink()
    value = wv.transcript(checkout, SID)
    assert texts(value)[-1] == "And now?" and value["session"]["archived"]
    assert "restorable" not in value["session"]
    assert SID not in [s["id"] for s in wv.resumable(checkout)]
    # Nor one whose agent's store is no longer one of this machine's.
    monkeypatch.setattr(wv, "RAW_MAX", 1 << 30)
    with sqlite3.connect(wv.store().path) as db:
        db.execute("UPDATE transcripts SET size=raw_size WHERE id=?", (row["id"],))
    assert SID in [s["id"] for s in wv.resumable(checkout)]
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "another"))
    assert SID not in [s["id"] for s in wv.resumable(checkout)]


def test_a_session_is_written_back_only_from_a_whole_copy_into_an_agent_s_store(
    stores, monkeypatch, tmp_path
):
    checkout, folder, _ = stores
    monkeypatch.setenv("HOME", str(tmp_path))
    file = folder / f"{SID}.jsonl"
    wv.transcript(checkout, SID)
    session = next(s for s in wv.sessions(checkout) if s["id"] == SID)
    file.unlink()
    # Opened, never swept: its transcript shows, but there is no file to resume it from.
    assert [s["id"] for s in wv.resumable(checkout)] == [CODEX_SID]
    assert "restorable" not in wv.transcript(checkout, SID)["session"]
    with pytest.raises(ValueError, match="No copy of that session's file was kept"):
        wv.restore(session)
    row = wv.store().row("", str(file))
    wv.store().keep(row["id"], 1, 0, [b"no line end"])
    with pytest.raises(ValueError, match="is not whole"):
        wv.restore(session)
    wv.store().keep(row["id"], 1, 0, [b'{"type": "user"}\n'])
    with sqlite3.connect(wv.store().path) as db:
        db.execute("UPDATE raw SET data=x'00' WHERE transcript=?", (row["id"],))
    with pytest.raises(ValueError, match="cannot be read"):
        wv.restore(session)
    assert not file.exists() and list(folder.iterdir()) == []
    # A row naming a file outside the agents' stores writes nothing anywhere.
    for agent, path in [
        ("claude", tmp_path / "elsewhere" / "x" / f"{SID}.jsonl"),
        ("claude", folder.parent / ".." / f"{SID}.jsonl"),
        ("claude", folder / "notes.txt"),
        ("codex", tmp_path / "codex" / "sessions" / f"rollout-{SID}.jsonl"),
        ("codex", folder / f"{SID}.jsonl"),
    ]:
        kept = wv.store().open("", str(path), agent, wv.PARSER)
        wv.store().keep(kept["id"], 1, 0, [b'{"type": "user"}\n'])
        with pytest.raises(ValueError, match="session store that session was in is gone"):
            wv.restore({"agent": agent, "_file": str(path)})
        assert not path.exists()


def test_the_archive_passes_over_a_session_it_cannot_read_but_not_a_silent_worker(monkeypatch):
    def session(name, size=5):
        return {
            "id": f"session-{name}",
            "agent": "claude",
            "title": None,
            "updated": 1.0,
            "size": size,
            "_file": f"/s/{name}.jsonl",
            "_cwd": "/wt/repo",
        }

    def read(parser, file, save):
        if "broken" in file:
            raise ValueError("The transcript changed while it was read; refresh")
        if "silent" in file:
            raise remote_probe.Unreachable("Mac mini did not answer within 60 seconds")
        parser.reset(1)
        parser.absorb(prompt_line(file))
        parser.offset = 5  # As far as the listing says the file goes.
        return True

    listed = [session("one"), session("broken"), session("two")]
    assert wv.archive(listed, "mini", read) == 2
    assert [s["id"] for s in wv.with_kept([], "mini", "/wt/repo")] == ["session-one", "session-two"]
    # Read to their end, they are passed over until they grow.
    assert wv.archive(listed, "mini", read) == 0
    assert wv.archive([session("one", size=9), session("two")], "mini", read) == 1
    with pytest.raises(remote_probe.Unreachable):
        wv.archive([session("silent"), session("three")], "mini", read)
    assert len(wv.with_kept([], "mini", "/wt/repo")) == 2


def test_a_search_names_the_sessions_it_had_no_time_to_index(stores, monkeypatch, tmp_path):
    checkout, _, _ = stores
    monkeypatch.setenv("HOME", str(tmp_path))
    assert wv.archive_local() == 4
    # The archive leaves its entries unindexed: a search with no time left finds none.
    monkeypatch.setattr(wv, "SEARCH_BUDGET", 0)
    nothing = wv.search(checkout, "Done")
    assert (nothing["total"], nothing["searched"], nothing["behind"]) == (0, 2, 2)
    monkeypatch.setattr(wv, "SEARCH_BUDGET", 20)
    found = wv.search(checkout, "Done")
    assert (found["total"], found["searched"], found["behind"]) == (1, 2, 0)


def test_a_transcript_lists_a_bounded_number_of_kept_sessions_newest_first(stores, monkeypatch):
    checkout, folder, _ = stores
    kept = wv.store()

    def keep(file, session, updated, text):
        row = kept.open("", file, "claude", wv.PARSER)
        kept.describe(row, session, str(checkout), None, updated, 1)
        entry = {"role": "user", "kind": "prompt", "text": text, "time": None}
        kept.save(
            row["id"],
            row["generation"],
            restarted=False,
            added=[(0, json.dumps(entry), text)],
            changed=[],
            place=(1, 1, False, 1, 0, "{}"),
            ready=True,
        )

    for number in range(6):
        keep(f"/gone/{number}.jsonl", f"gone-session-{number}", float(number), f"Prompt {number}")
    monkeypatch.setattr(wv, "KEPT_SESSIONS", 5)
    listed = wv.transcript(checkout)["sessions"]
    # The two with files, which are newest, then the newest three of the six kept.
    assert [s["id"] for s in listed[2:]] == [f"gone-session-{n}" for n in (5, 4, 3)]
    assert {s["id"] for s in listed[:2]} == {SID, CODEX_SID}
    assert texts(wv.transcript(checkout, "gone-session-4")) == ["Prompt 4"]
    # One kept under another file is the same session: listed once, from its file.
    keep("/moved/elsewhere.jsonl", SID, time.time() + 60, "Its older copy")
    again = wv.transcript(checkout, SID)
    assert [s["id"] for s in again["sessions"]].count(SID) == 1
    assert texts(again)[0] == "Fix the crash" and "archived" not in again["session"]


def test_the_archiver_sweeps_this_machine_then_each_workers_checkouts(monkeypatch, tmp_path):
    calls = []
    mini, box = SimpleNamespace(id="mini"), SimpleNamespace(id="box")

    def remote(home, machine, path, stop):
        calls.append((machine.id, path))
        if machine.id == "box":
            raise remote_probe.Unreachable("Box could not be reached")
        if path == "/b":
            raise ValueError("Not at the top of a Git checkout")

    monkeypatch.setattr(wv, "archive_local", lambda stop: calls.append("local"))
    monkeypatch.setattr(wv, "archive_remote", remote)
    monkeypatch.setattr(wv, "ARCHIVE_FAILURES", 2)
    checkouts = [(box, "/a"), (mini, "/b"), (box, "/c"), (box, "/d"), (mini, "/e")]
    archiver = wv.Archiver(tmp_path, lambda: checkouts)
    archiver.sweep()
    # A worker that keeps giving no answer is left for the next sweep; one that only
    # lost a checkout is asked for its others.
    assert calls == ["local", ("box", "/a"), ("mini", "/b"), ("box", "/c"), ("mini", "/e")]
    # Started, it sweeps after its first wait and stops when closed.
    monkeypatch.setattr(wv, "ARCHIVE_FIRST", 0)
    monkeypatch.setattr(wv, "ARCHIVE_EVERY", 60)
    del calls[:]
    archiver.start()
    for _ in range(200):
        if len(calls) == 5:
            break
        time.sleep(0.01)
    archiver.close()
    archiver.worker.join(5)
    assert len(calls) == 5 and not archiver.worker.is_alive()
