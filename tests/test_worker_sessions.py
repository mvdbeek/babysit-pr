"""Naming a hand-started Codex's session for a worker's herdr, against a fake /proc."""

import json
import os
import uuid

import pytest
import worker_sessions
from test_worker_broker import fake_herdr, fake_process, hold, let_go


def process(proc, pid, parent=1, comm="codex", argv=("codex",), cwd=None, nspid=None, **env):
    """A process of this user: its parent, command line, directory and environment."""
    fake_process(proc, pid, nspid=nspid, comm=comm)
    with (proc / str(pid) / "status").open("a") as status:
        status.write(f"PPid:\t{parent}\n")
    (proc / str(pid) / "cmdline").write_bytes(b"\0".join(map(os.fsencode, argv)) + b"\0")
    (proc / str(pid) / "environ").write_bytes(
        b"\0".join(f"{name}={value}".encode() for name, value in {"USER": "babysit", **env}.items())
    )
    if cwd is not None:
        (proc / str(pid) / "cwd").symlink_to(cwd)


def pane(name):
    return {"HERDR_ENV": "1", "HERDR_PANE_ID": name}


def rollout(home, cwd, written, source="cli"):
    """A conversation recorded in ``cwd``, last written at ``written``: (ID, its rollout)."""
    sid = str(uuid.uuid4())
    folder = home / "sessions" / "2026" / "11" / "15"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-11-15T09-00-00-{sid}.jsonl"
    meta = {"type": "session_meta", "payload": {"id": sid, "cwd": str(cwd), "source": source}}
    path.write_text(json.dumps(meta) + "\n")
    os.utime(path, (written, written))
    return sid, str(path.resolve())


@pytest.fixture
def worker(tmp_path):
    """A worker's /proc, its Codex home and two checkouts. Codex's background server
    (40) was started by the Codex of some earlier pane and outlived it."""
    proc, home = tmp_path / "proc", tmp_path / ".codex"
    trees = [tmp_path / "one", tmp_path / "two"]
    for tree in trees:
        tree.mkdir()
    proc.mkdir()
    (proc / "self").mkdir()  # Entries that are not processes are passed over.
    process(proc, 40, argv=("codex", "app-server", "--listen", "unix://"), **pane("wGone:p1"))

    def found():
        return worker_sessions.sessions(proc, home)

    return {"proc": proc, "home": home, "trees": trees, "found": found}


def test_a_codex_that_writes_its_own_rollout_runs_that_session(worker):
    proc, home, (one, two), found = (worker[key] for key in ("proc", "home", "trees", "found"))
    # npm's codex (node) runs the native one, as `codex --no-daemon` typed in a pane does.
    process(proc, 20, comm="node", argv=("node", "codex", "--no-daemon"), cwd=one, **pane("wA:p1"))
    process(proc, 21, parent=20, argv=("codex", "--no-daemon"), cwd=one, **pane("wA:p1"))
    assert found() == {}  # Nothing written yet.
    sid, path = rollout(home, two, 100)  # Wherever it was recorded: it is the one written.
    hold(proc, 21, 30, path)
    # The server's newer conversation in this directory is some other Codex's.
    hold(proc, 40, 30, rollout(home, one, 900)[1])
    assert found() == {"wA:p1": (21, sid, path)}
    # Read, as /resume's picker reads one, is not run; two written at once are not told apart.
    other = rollout(home, one, 200)[1]
    hold(proc, 21, 31, other, flags="02100000")
    assert found() == {"wA:p1": (21, sid, path)}
    hold(proc, 21, 32, other)
    assert found() == {}


def test_a_codex_of_the_shared_server_gets_the_rollout_last_written_for_its_directory(worker):
    proc, home, (one, two), found = (worker[key] for key in ("proc", "home", "trees", "found"))
    process(proc, 20, parent=5, argv=("codex", "resume", "x"), cwd=one, **pane("wA:p1"))
    assert found() == {}  # The server writes nothing recorded here.
    elsewhere = rollout(home, two, 900)
    hold(proc, 40, 30, elsewhere[1])
    assert found() == {}
    old, new = rollout(home, one, 100), rollout(home, one, 200)
    hold(proc, 40, 31, old[1])
    assert found() == {"wA:p1": (20, *old)}
    # After a /new the server writes another one here, and may keep the old one open.
    hold(proc, 40, 32, new[1])
    assert found() == {"wA:p1": (20, *new)}
    # A subagent's rollout is not a conversation, however lately it was written.
    hold(proc, 40, 33, rollout(home, one, 800, {"subagent": "review"})[1])
    assert found() == {"wA:p1": (20, *new)}
    # Nor is one the server of another Codex home writes.
    apart = home.parent / "apart"
    process(proc, 41, argv=("codex", "app-server"), CODEX_HOME=str(apart))
    hold(proc, 41, 30, rollout(apart, one, 950)[1])
    assert found() == {"wA:p1": (20, *new)}
    # Written at the same instant, neither is known to be the later one.
    os.utime(old[1], (200, 200))
    assert found() == {}
    os.utime(old[1], (100, 100))
    # A Codex in the other directory is named by that directory's.
    process(proc, 22, parent=6, cwd=two, **pane("wB:p1"))
    assert found() == {"wA:p1": (20, *new), "wB:p1": (22, *elsewhere)}
    # A second Codex of the server in one directory: which runs which is not known,
    # whether herdr holds it in a pane or not.
    process(proc, 23, parent=7, cwd=one)
    assert found() == {"wB:p1": (22, *elsewhere)}


def test_only_a_panes_own_unsandboxed_codex_is_named(worker):
    proc, home, (one, two), found = (worker[key] for key in ("proc", "home", "trees", "found"))
    sid, path = rollout(home, one, 100)
    # A sandboxed Codex has a PID of its own namespace; its broker reports it.
    process(proc, 20, nspid=3, cwd=one, **pane("wA:p1"))
    hold(proc, 20, 30, path)
    assert found() == {}
    # A pane outside herdr, or whose variable some other tool set, is not herdr's.
    process(proc, 21, cwd=one, HERDR_PANE_ID="wB:p1")
    hold(proc, 21, 30, path)
    assert found() == {}
    process(proc, 22, cwd=one, **pane("wC:p1"))
    hold(proc, 22, 30, path)
    assert found() == {"wC:p1": (22, sid, path)}
    # A Codex that the pane's Codex runs through a shell is its tool, not the pane's:
    # it neither takes the pane nor counts as a second Codex in it.
    process(proc, 24, parent=22, comm="bash", argv=("bash", "-c", "codex exec"), cwd=one)
    process(proc, 25, parent=24, argv=("codex", "exec"), cwd=one, **pane("wC:p1"))
    hold(proc, 25, 30, rollout(home, one, 300)[1])
    assert found() == {"wC:p1": (22, sid, path)}
    # So is one a conversation of the server runs.
    process(proc, 26, parent=40, argv=("codex", "exec"), cwd=two, **pane("wD:p1"))
    hold(proc, 26, 30, rollout(home, two, 300)[1])
    assert found() == {"wC:p1": (22, sid, path)}
    # Two Codex typed into one pane, one after a ctrl-z: neither is named.
    process(proc, 27, cwd=two, **pane("wC:p1"))
    assert found() == {}


def test_a_rollout_outside_the_codexs_own_home_is_not_reported(worker, tmp_path):
    proc, home, (one, _), found = (worker[key] for key in ("proc", "home", "trees", "found"))
    process(proc, 20, cwd=one, **pane("wA:p1"))
    sid, path = rollout(tmp_path / "another-home", one, 100)
    hold(proc, 20, 30, path)
    assert found() == {}
    # Nor one a link in its store leads to.
    link = home / "sessions" / "2026" / "11" / "15"
    link.mkdir(parents=True)
    (link / os.path.basename(path)).symlink_to(path)
    assert found() == {}


def test_a_codex_that_may_run_a_conversation_recorded_elsewhere_leaves_its_server_unnamed(worker):
    proc, home, (one, two), found = (worker[key] for key in ("proc", "home", "trees", "found"))
    process(proc, 20, cwd=one, **pane("wA:p1"))
    mine, other = rollout(home, one, 100), rollout(home, one, 200)
    hold(proc, 40, 30, mine[1])
    # A Codex typed plainly in another directory, idle at its first prompt, records
    # its conversation there: the one recorded here is not its.
    process(proc, 21, cwd=two, **pane("wB:p1"))
    assert found() == {"wA:p1": (20, *mine)}
    # One that resumed a conversation, or was given a directory, may run one recorded
    # here, which would pass for the latest of this directory.
    for pid, argv in enumerate(
        [("codex", "resume", "--last"), ("codex", "fork"), ("codex", "-C", str(one)),
         ("codex", f"--cd={one}"), ("node", "--cd", str(one))],
        start=22,
    ):  # fmt: skip
        process(proc, pid, argv=argv, cwd=two, **pane("wC:p1"))
        hold(proc, 40, 31, other[1])
        assert found() == {}, argv
        (proc / str(pid) / "status").unlink()
        assert found() == {"wA:p1": (20, *other)}
        let_go(proc, 40, 31)
    # Once the server writes one recorded where that Codex runs, each has its own.
    process(proc, 30, argv=("codex", "resume", "x"), cwd=two, **pane("wC:p1"))
    assert found() == {}
    there = rollout(home, two, 300)
    hold(proc, 40, 32, there[1])
    (proc / "21" / "status").unlink()
    assert found() == {"wA:p1": (20, *mine), "wC:p1": (30, *there)}


def test_what_sandboxed_agents_write_leaves_at_most_a_pane_unnamed(worker, tmp_path):
    proc, home, (one, two), found = (worker[key] for key in ("proc", "home", "trees", "found"))
    process(proc, 20, cwd=one, **pane("wA:p1"))
    process(proc, 21, cwd=two, **pane("wB:p1"))
    mine, theirs = rollout(home, one, 100), rollout(home, two, 100)
    hold(proc, 40, 30, mine[1])
    hold(proc, 40, 31, theirs[1])
    named = {"wA:p1": (20, *mine), "wB:p1": (21, *theirs)}
    assert found() == named
    # A process names itself with any bytes, in a sandbox too.
    process(proc, 50, nspid=7, comm="sleep")
    (proc / "50" / "status").write_bytes(b"Name:\t\xff\xfe\nNSpid:\t50\t7\nPPid:\t1\n")
    process(proc, 51, comm="bash")
    (proc / "51" / "status").write_bytes(b"\xff" * 100)
    assert found() == named
    # The store is theirs to write: a rollout the server holds may record any directory.
    for cwd in (f"{one}\0", "relative", "/" + "a/../" * 100_000, 7, None):
        hostile = rollout(home, one, 900)
        meta = {"type": "session_meta", "payload": {"id": hostile[0], "cwd": cwd}}
        with open(hostile[1], "w") as stream:
            stream.write(json.dumps(meta) + "\n")
        os.utime(hostile[1], (900, 900))
        hold(proc, 40, 40, hostile[1])
        assert found() == named, cwd
        let_go(proc, 40, 40)
    # A last write dated ahead of the clock says nothing about which came last.
    ahead = rollout(home, one, 5000)
    hold(proc, 40, 41, ahead[1])
    assert worker_sessions.sessions(proc, home, now=1000) == {"wB:p1": (21, *theirs)}
    assert worker_sessions.sessions(proc, home, now=4990) == {**named, "wA:p1": (20, *ahead)}


def test_herdr_is_told_each_panes_session_and_again_when_it_changes(worker, tmp_path, monkeypatch):
    proc, home, (one, _) = (worker[key] for key in ("proc", "home", "trees"))
    monkeypatch.setattr(worker_sessions.Path, "home", lambda: tmp_path)
    process(proc, 20, cwd=one, **pane("wA:p1"))
    first, second = rollout(home, one, 100), rollout(home, one, 200)
    hold(proc, 40, 30, first[1])
    # herdr refuses the first report: it has not detected the agent yet.
    seen = tmp_path / "seen"
    calls = fake_herdr(tmp_path, monkeypatch, f'[ -e "{seen}" ] || {{ : > "{seen}"; exit 1; }}')
    rounds = iter(range(8))

    def running():
        index = next(rounds, None)
        if index == 4:  # A /new, and its first message.
            let_go(proc, 40, 30)
            hold(proc, 40, 31, second[1])
        if index == 6:  # The Codex exits and another resumes the same session.
            process(proc, 21, cwd=one, **pane("wA:p1"))
            (proc / "20" / "status").unlink()
        return index is not None

    worker_sessions.report(proc, running, poll=0)
    reports = [line.split() for line in calls.read_text().splitlines()]
    # Refused, told, told the new session, then told again for the new process.
    assert [args[5:] for args in reports] == [
        [f"--agent-session-id={sid}", f"--agent-session-path={path}", "wA:p1"]
        for sid, path in (first, first, second, second)
    ]
    for args in reports:
        assert args[:4] == ["pane", "report-agent-session", "--source=herdr:codex", "--agent=codex"]


def test_print_shows_one_look_and_tells_herdr_nothing(worker, tmp_path, monkeypatch, capsys):
    proc, home, (one, _) = (worker[key] for key in ("proc", "home", "trees"))
    process(proc, 20, cwd=one, **pane("wA:p1"))
    sid, path = rollout(home, one, 100)
    hold(proc, 40, 30, path)
    calls = fake_herdr(tmp_path, monkeypatch, "true")
    look = worker_sessions.sessions
    monkeypatch.setattr(worker_sessions, "sessions", lambda: look(proc, home))
    assert worker_sessions.main(["--print"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "wA:p1": {"pid": 20, "session": sid, "transcript": path}
    }
    assert not calls.exists()
    assert worker_sessions.main(["--frobnicate"]) == 2
