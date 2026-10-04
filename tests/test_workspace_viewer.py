"""Diff and transcript views over temporary repositories and session stores."""

import json
import subprocess

import pytest
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
    monkeypatch.setattr(wv, "_parsers", {})
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
    assert json.loads(tool["input"]) == {"command": "ls"}
    assert tool["output"] == "a.py" and tool["error"] is True


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
        "total": 0,
    }


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
    monkeypatch.setattr(wv, "PARSED_FILES", 0)
    wv.transcript(checkout, CODEX_SID)
    assert wv._parsers == {}


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
