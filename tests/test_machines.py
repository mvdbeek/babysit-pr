"""Machine registry validation and the herdr/ssh argv every remote call goes through."""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import machines
import owned_process
import pytest

MINI = {
    "id": "mini",
    "label": "Mac mini",
    "ssh": "mini.tail1234.ts.net",
    "os": "darwin",
    "src": "~/src",
    "babysit_root": "~/src/babysit-pr",
    "python": "python3",
    "max_agents": 4,
    "labels": ["docker", "safehouse", "claude-account:work"],
    "enabled": True,
}


@pytest.fixture(autouse=True)
def short_tmp(tmp_path, monkeypatch):
    """pytest's tmp_path is too deep for an ssh socket, so control sockets fall back to
    the temporary directory; point that at a fresh short one instead of the real /tmp.
    Requesting tmp_path first keeps pytest's own base directory where it was."""
    path = tempfile.mkdtemp(prefix="bs", dir="/tmp")
    monkeypatch.setattr(tempfile, "tempdir", path)
    machines.control_dir.cache_clear()
    yield Path(path)
    shutil.rmtree(path)


def write(home, *entries, **top):
    home.mkdir(parents=True, exist_ok=True)
    config = {"version": 1, "machines": list(entries), **top}
    (home / "machines.json").write_text(json.dumps(config))
    return home


def test_missing_registry_is_none_and_local_only(tmp_path):
    assert machines.load_config(tmp_path) is None
    assert machines.load(tmp_path) == {}
    assert machines.resolve(tmp_path, None) is None
    with pytest.raises(ValueError, match="Unknown machine mini"):
        machines.resolve(tmp_path, "mini")


def test_valid_registry_with_defaults_and_repos(tmp_path):
    write(
        tmp_path,
        MINI,
        {"id": "box", "label": "Linux box", "ssh": "babysit@box", "os": "linux"},
        repos={"galaxyproject/galaxy": {"requires": ["docker"], "prefer": "mini"}},
    )
    config = machines.load_config(tmp_path)
    assert config["repos"] == {"galaxyproject/galaxy": {"requires": ["docker"], "prefer": "mini"}}
    loaded = machines.load(tmp_path)
    assert loaded["mini"].labels == ("docker", "safehouse", "claude-account:work")
    assert loaded["box"] == machines.Machine(
        id="box",
        label="Linux box",
        ssh="babysit@box",
        os="linux",
        src="~/src",
        babysit_root="~/src/babysit-pr",
        python="python3",
        max_agents=4,
        labels=(),
        collie_url=None,
        enabled=True,
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"id": "Mini"}, "Invalid id"),
        ({"id": "1mini"}, "Invalid id"),
        ({"id": "m" * 25}, "Invalid id"),
        ({"id": "local"}, "local and auto are reserved"),
        ({"id": "auto"}, "local and auto are reserved"),
        ({"ssh": "-oProxyCommand=x"}, "Invalid ssh"),
        ({"ssh": "mini host"}, "Invalid ssh"),
        ({"ssh": "mini;id"}, "Invalid ssh"),
        ({"ssh": "a@-oProxyCommand=x"}, "Invalid ssh"),
        ({"ssh": "a@b@c"}, "Invalid ssh"),
        ({"ssh": "-a@b"}, "Invalid ssh"),
        ({"ssh": "a" * 256}, "Invalid ssh"),
        ({"os": ["linux"]}, "Invalid os"),
        ({"os": {"linux": 1}}, "Invalid os"),
        ({"os": "windows"}, "Invalid os"),
        ({"src": "src"}, "Invalid src"),
        ({"babysit_root": "~other/babysit-pr"}, "Invalid babysit_root"),
        ({"python": "python3 -c x"}, "Invalid python"),
        ({"max_agents": 0}, "Invalid max_agents"),
        ({"max_agents": 65}, "Invalid max_agents"),
        ({"max_agents": True}, "Invalid max_agents"),
        ({"labels": ["Docker"]}, "Invalid labels"),
        ({"label": ""}, "Invalid label"),
        ({"label": "-Mac"}, "Invalid label"),
        ({"label": "Mac\nmini"}, "Invalid label"),
        ({"collie_url": "javascript:alert(1)"}, "Invalid collie_url"),
        ({"collie_url": "https://"}, "Invalid collie_url"),
        ({"collie_url": 'https://mini/"onmouseover=x'}, "Invalid collie_url"),
        ({"collie_url": "https://mini/<b>"}, "Invalid collie_url"),
        ({"collie_url": "ftp://mini/"}, "Invalid collie_url"),
        ({"enabled": "yes"}, "Invalid enabled"),
        ({"hostname": "mini"}, "Unknown machine keys: hostname"),
    ],
)
def test_invalid_machine_fields(tmp_path, change, message):
    write(tmp_path, {**MINI, **change})
    with pytest.raises(ValueError, match=message):
        machines.load(tmp_path)


def test_missing_required_field(tmp_path):
    write(tmp_path, {k: v for k, v in MINI.items() if k != "ssh"})
    with pytest.raises(ValueError, match="missing ssh"):
        machines.load(tmp_path)


def test_duplicate_ids_labels_and_ssh_targets(tmp_path):
    write(tmp_path, MINI, {**MINI, "label": "Other"})
    with pytest.raises(ValueError, match="Duplicate machine id mini"):
        machines.load(tmp_path)
    write(tmp_path, MINI, {**MINI, "id": "other", "ssh": "other"})
    with pytest.raises(ValueError, match="Duplicate machine label Mac mini"):
        machines.load(tmp_path)
    write(tmp_path, MINI, {**MINI, "id": "other", "label": "Other"})
    with pytest.raises(ValueError, match="Duplicate machine ssh target mini.tail1234.ts.net"):
        machines.load(tmp_path)
    write(tmp_path, MINI, {**MINI, "id": "other", "label": "Other", "ssh": "me@mini"})
    assert sorted(machines.load(tmp_path)) == ["mini", "other"]  # aliases are not caught


@pytest.mark.parametrize(
    ("top", "message"),
    [
        ({"version": 2}, "version must be 1"),
        ({"version": True}, "version must be 1"),
        ({"version": 1.0}, "version must be 1"),
        ({"extra": True}, "Unknown registry keys: extra"),
        ({"machines": {"mini": MINI}}, "list of at most 64"),
        ({"repos": {"not-a-slug": {}}}, "Invalid repos entry"),
        ({"repos": {"a/b": {"prefer": "nowhere"}}}, "prefers unknown machine nowhere"),
        ({"repos": {"a/b": {"prefer": ["mini"]}}}, "Invalid prefer for a/b"),
        ({"repos": {"a/b": {"prefer": {"mini": 1}}}}, "Invalid prefer for a/b"),
        ({"repos": {"a/b": {"requires": "docker"}}}, "Invalid requires"),
        ({"repos": {"a/b": {"pin": "mini"}}}, "Unknown keys for a/b: pin"),
    ],
)
def test_invalid_registry(tmp_path, top, message):
    write(tmp_path, MINI, **top)
    with pytest.raises(ValueError, match=message):
        machines.load_config(tmp_path)


def test_malformed_and_oversize_files(tmp_path):
    path = tmp_path / "machines.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="JSON object"):
        machines.load(tmp_path)
    path.write_text("{")
    with pytest.raises(ValueError):
        machines.load(tmp_path)
    path.write_text(json.dumps({"version": 1, "machines": [], "pad": " " * 65536}))
    with pytest.raises(ValueError, match="size limit"):
        machines.load(tmp_path)


def test_resolve_disabled_only_refused_for_placement(tmp_path):
    write(tmp_path, {**MINI, "enabled": False})
    assert machines.resolve(tmp_path, "mini").label == "Mac mini"
    with pytest.raises(ValueError, match="disabled"):
        machines.resolve(tmp_path, "mini", placing=True)
    with pytest.raises(ValueError, match="Unknown machine local"):
        machines.resolve(tmp_path, "local")


def test_herdr_argv_local_is_unchanged_and_remote_uses_label(tmp_path):
    write(tmp_path, MINI)
    mini = machines.resolve(tmp_path, "mini")
    assert machines.herdr_argv(None, "workspace", "list") == ["herdr", "workspace", "list"]
    assert machines.herdr_argv(mini, "workspace", "list") == [
        "herdr",
        "--machine=Mac mini",
        "workspace",
        "list",
    ]


def unwrap(command):
    """The sh command line inside ``remote_argv``'s octal-escaped wrapper."""
    match = re.fullmatch(r"sh -c 'eval \"\$\(printf \"((?:\\[0-7]{3})*)\"\)\"'", command)
    assert match, command
    return bytes(int(x, 8) for x in match[1].split("\\")[1:]).decode()


def test_remote_argv_quotes_hostile_arguments_and_paths(tmp_path, short_tmp):
    home = write(short_tmp / "home", MINI)
    mini = machines.resolve(home, "mini")
    argv = machines.remote_argv(
        home, mini, ["git", "log", "--format=%H", "a b; rm -rf /"], cwd="/w/it's $(id)"
    )
    assert argv[:-1] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=300",
        "-o",
        f"ControlPath={home}/ssh/%C",
        "mini.tail1234.ts.net",
        "--",
    ]
    assert unwrap(argv[-1]) == (
        "cd -- '/w/it'\"'\"'s $(id)' && git log --format=%H 'a b; rm -rf /'"
    )
    assert unwrap(machines.remote_argv(home, mini, ["ls"], cwd="~/src/my repo")[-1]) == (
        "cd -- \"$HOME\"/'src/my repo' && ls"
    )
    assert unwrap(
        machines.remote_argv(home, mini, [machines.remote_path("~/b in/x"), "~/y"])[-1]
    ) == ("\"$HOME\"/'b in/x' '~/y'")
    with pytest.raises(ValueError, match="Remote path"):
        machines.remote_path("rel/x")
    with pytest.raises(ValueError, match="absolute"):
        machines.remote_argv(home, mini, ["ls"], cwd="src")
    with pytest.raises(ValueError, match="NUL"):
        machines.remote_argv(home, mini, ["echo", "a\0b"])


HOSTILE = [
    "!",
    "!!",
    "a!b",
    "!$",
    "line\nbreak",
    "\n",
    "trailing\n",
    "'",
    "\\'';touch pwned;\\",
    "\\",
    "\\\\",
    '"$HOME"',
    "$(touch pwned)",
    "`touch pwned`",
    "~",
    "%s%n",
    "*",
    "{a,b}",
    "a\tb c",
    "",
    "-n",
    "é ✓",
]


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "dash", "ksh", "tcsh", "csh", "fish"])
def test_remote_command_survives_any_login_shell(tmp_path, short_tmp, shell):
    """sshd hands the command string to the worker's login shell as ``$SHELL -c``."""
    path = shutil.which(shell)
    if path is None:
        pytest.skip(f"{shell} is not installed")
    home = write(short_tmp / "home", MINI)
    mini = machines.resolve(home, "mini")
    remote_home = tmp_path / "remote"
    work = remote_home / "it's !x"
    work.mkdir(parents=True)
    echo = [
        sys.executable,
        "-c",
        "import json,os,sys; print(json.dumps([os.getcwd(), *sys.argv[1:]]))",
    ]
    command = machines.remote_argv(home, mini, [*echo, *HOSTILE], cwd="~/it's !x")[-1]
    assert set(command) <= set("sh -c'eval\"$(printf\\01234567)")
    result = subprocess.run(
        [path, "-c", command],
        cwd=tmp_path,
        env={"HOME": str(remote_home), "PATH": os.environ["PATH"], "LC_ALL": "en_US.UTF-8"},
        capture_output=True,
        input=b"",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [str(work.resolve()), *HOSTILE]
    assert not list(tmp_path.rglob("pwned"))


def test_control_path_directory_is_private_and_falls_back_when_too_long(tmp_path, short_tmp):
    short = short_tmp / "home"
    assert machines.control_dir(short) == short / "ssh"
    assert stat.S_IMODE((short / "ssh").stat().st_mode) == 0o700

    deep = tmp_path / ("d" * 60)
    fallback = machines.control_dir(deep)
    assert fallback.parent == short_tmp
    assert fallback.name.startswith(f"babysit-ssh-{os.getuid()}-")
    assert len(os.fsencode(fallback)) <= machines.SOCKET_LIMIT
    assert not deep.exists()
    assert machines.control_dir(deep) == fallback
    assert machines.control_dir(tmp_path / ("e" * 60)) != fallback

    fallback.chmod(0o755)
    assert machines.control_dir(deep) == fallback  # checked once per process
    machines.control_dir.cache_clear()
    with pytest.raises(ValueError, match="private directory"):
        machines.control_dir(deep)


def test_run_local_delegates_to_owned_process(tmp_path, monkeypatch):
    calls = []
    real = owned_process.run

    def record(args, **kwargs):
        calls.append((args, kwargs))
        return real(args, **kwargs)

    monkeypatch.setattr(owned_process, "run", record)
    result = machines.run(tmp_path, None, ["cat"], timeout=5, input=b"hi")
    assert (result.returncode, result.stdout) == (0, b"hi")
    assert calls == [(["cat"], {"timeout": 5, "input": b"hi", "env": None, "cwd": None})]
    assert not (tmp_path / "ssh").exists()


def test_run_remote_executes_in_fake_host_home(tmp_path, fake_tools, short_tmp):
    home = write(short_tmp / "home", MINI)
    mini = machines.resolve(home, "mini")
    host = tmp_path / "hosts" / "mini.tail1234.ts.net"
    hostile = host / "src" / "it's $(touch pwned)"
    hostile.mkdir(parents=True)

    result = machines.run(
        home, mini, ["sh", "-c", 'pwd; echo "$HOME"; cat'], timeout=10, input=b"stdin"
    )
    lines = result.stdout.decode().splitlines()
    assert result.returncode == 0
    assert [os.path.realpath(x) for x in lines[:2]] == [str(host.resolve())] * 2
    assert lines[2] == "stdin"

    result = machines.run(home, mini, ["pwd"], timeout=10, cwd="~/src/it's $(touch pwned)")
    assert os.path.realpath(result.stdout.decode().strip()) == str(hostile.resolve())
    assert not list(tmp_path.rglob("pwned"))
    recorded = json.loads(fake_tools.read_text())["ssh"]
    assert recorded[-1]["target"] == "mini.tail1234.ts.net"
    assert unwrap(recorded[-1]["command"]) == (
        "cd -- \"$HOME\"/'src/it'\"'\"'s $(touch pwned)' && pwd"
    )

    with pytest.raises(ValueError, match="worker's environment"):
        machines.run(home, mini, ["env"], timeout=10, env={"GH_TOKEN": "secret"})


def test_run_remote_never_reads_the_controller_stdin(tmp_path, fake_tools, short_tmp):
    home = write(short_tmp / "home", MINI)
    script = (
        "import machines,sys; m=machines.resolve(sys.argv[1],'mini');"
        "r=machines.run(sys.argv[1],m,['cat'],timeout=10);"
        "r2=machines.run(sys.argv[1],m,['cat'],timeout=10,input=b'given');"
        "print(repr((r.stdout,r2.stdout)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(home)],
        input=b"controller stdin",
        capture_output=True,
        timeout=30,
        env={**os.environ, "PYTHONPATH": str(Path(machines.__file__).parent)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode().strip() == repr((b"", b"given"))


def test_run_expands_remote_paths_on_the_host_or_locally(
    tmp_path, fake_tools, short_tmp, monkeypatch
):
    home = write(short_tmp / "home", MINI)
    mini = machines.resolve(home, "mini")
    script = "#!/bin/sh\necho ran-$1\n"
    local_home = tmp_path / "me"
    host = tmp_path / "hosts" / "mini.tail1234.ts.net"
    for base in (local_home, host):
        (base / "my bin").mkdir(parents=True)
        tool = base / "my bin" / "x"
        tool.write_text(script)
        tool.chmod(0o755)
    monkeypatch.setenv("HOME", str(local_home))
    tool = machines.remote_path("~/my bin/x")
    assert machines.run(home, mini, [tool, "r"], timeout=10).stdout == b"ran-r\n"
    assert machines.run(home, None, [tool, "l"], timeout=10).stdout == b"ran-l\n"


def test_run_remote_unreachable_surfaces_ssh_failure(fake_tools, short_tmp, monkeypatch):
    home = write(short_tmp / "home", MINI)
    monkeypatch.setenv("FAKE_SSH_DOWN", "mini.tail1234.ts.net")
    result = machines.run(home, machines.resolve(home, "mini"), ["true"], timeout=10)
    assert result.returncode == 255
    assert b"Connection refused" in result.stderr


def test_fake_herdr_keeps_separate_state_per_machine(tmp_path, fake_tools):
    write(tmp_path, MINI, {**MINI, "id": "box", "label": "Linux box", "ssh": "box"})
    opened = []
    for machine in (None, *machines.load(tmp_path).values()):
        argv = machines.herdr_argv(
            machine, "worktree", "open", "--cwd", "/repo", "--path", "/repo/wt"
        )
        output = subprocess.run(argv, check=True, capture_output=True, text=True).stdout
        opened.append(json.loads(output)["result"]["root_pane"]["pane_id"])
    assert opened == ["w1:p1", "w1:p1", "w1:p1"]

    state = json.loads(fake_tools.read_text())
    assert [w["workspace_id"] for w in state["workspaces"]] == ["w1"]
    assert sorted(state["machines"]) == ["Linux box", "Mac mini"]
    assert state["machines"]["Mac mini"]["calls"] == [
        ["worktree", "open", "--cwd", "/repo", "--path", "/repo/wt"]
    ]
    assert state["calls"][0] == ["worktree", "open", "--cwd", "/repo", "--path", "/repo/wt"]
    assert state["calls"][1][:2] == ["--machine=Mac mini", "worktree"]


def test_remote_channels_are_limited_per_ssh_target(tmp_path, short_tmp, monkeypatch):
    home = write(short_tmp / "home", MINI, {**MINI, "id": "box", "label": "Box", "ssh": "box"})
    loaded = machines.load(home)
    monkeypatch.setattr(machines, "_channels", {})
    release = threading.Event()
    lock = threading.Lock()
    active: dict[str, int] = {"mini.tail1234.ts.net": 0, "box": 0}
    peak = dict(active)

    def blocking(args, **kwargs):
        target = args[-3]
        with lock:
            active[target] += 1
            peak[target] = max(peak[target], active[target])
        release.wait(10)
        with lock:
            active[target] -= 1
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(owned_process, "run", blocking)
    threads = [
        threading.Thread(
            target=machines.run, args=(home, loaded[m], ["true"]), kwargs={"timeout": 5}
        )
        for m in ["mini"] * 6 + ["box"]
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 5
    while sum(active.values()) < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    assert active == {"mini.tail1234.ts.net": machines.CHANNELS, "box": 1}
    release.set()
    for thread in threads:
        thread.join(5)
    assert peak == {"mini.tail1234.ts.net": machines.CHANNELS, "box": 1}


def test_remote_channel_wait_respects_timeout(short_tmp, monkeypatch):
    home = write(short_tmp / "home", MINI)
    mini = machines.resolve(home, "mini")
    monkeypatch.setattr(machines, "_channels", {})
    semaphore = machines._channel(mini)
    for _ in range(machines.CHANNELS):
        semaphore.acquire()
    monkeypatch.setattr(owned_process, "run", lambda *a, **k: pytest.fail("ran without a channel"))
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        machines.run(home, mini, ["true"], timeout=0.2)
    assert time.monotonic() - started < 5

    semaphore.release()
    seen = []
    monkeypatch.setattr(owned_process, "run", lambda args, **kw: seen.append(kw["timeout"]) or "ok")
    assert machines.run(home, mini, ["true"], timeout=30) == "ok"
    assert 0 < seen[0] <= 30


def test_remote_channel_without_timeout_waits_for_a_free_channel(short_tmp, monkeypatch):
    home = write(short_tmp / "home", MINI)
    mini = machines.resolve(home, "mini")
    monkeypatch.setattr(machines, "_channels", {})
    semaphore = machines._channel(mini)
    for _ in range(machines.CHANNELS):
        semaphore.acquire()
    monkeypatch.setattr(owned_process, "run", lambda args, **kw: "ok")
    threading.Timer(0.2, semaphore.release).start()
    assert machines.run(home, mini, ["true"], timeout=None) == "ok"


@pytest.mark.parametrize(
    ("machine_id", "wid", "qid"), [(None, "w111", "w111"), ("mini", "w1", "mini/w1")]
)
def test_qualify_split_round_trip(machine_id, wid, qid):
    assert machines.qualify(machine_id, wid) == qid
    assert machines.split(qid) == (machine_id, wid)


@pytest.mark.parametrize(
    "qid", ["", "/w1", "mini/", "local/w1", "Mini/w1", "a/b/w1", "w1:p1", "-w1"]
)
def test_split_rejects_malformed(qid):
    with pytest.raises(ValueError):
        machines.split(qid)


def test_wrappers_drive_herdr_here_and_git_over_ssh(tmp_path, fake_tools, short_tmp):
    import herdr_handoff
    import pr_workspaces

    home = write(short_tmp / "home", MINI)
    mini = machines.resolve(home, "mini")
    assert pr_workspaces.herdr("workspace", "list") == {"workspaces": []}
    assert pr_workspaces.herdr("workspace", "list", machine=mini) == {"workspaces": []}
    assert json.loads(herdr_handoff.herdr("agent", "list", machine=mini)) == {
        "result": {"agents": []}
    }
    state = json.loads(fake_tools.read_text())
    assert state["calls"] == [
        ["workspace", "list"],
        ["--machine=Mac mini", "workspace", "list"],
        ["--machine=Mac mini", "agent", "list"],
    ]
    assert "ssh" not in state  # herdr reaches the worker itself

    clone = tmp_path / "hosts" / "mini.tail1234.ts.net" / "src" / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(clone)], check=True)
    head = pr_workspaces.git(clone, "symbolic-ref", "HEAD", machine=mini, home=home)
    assert head == "refs/heads/main"
    [call] = json.loads(fake_tools.read_text())["ssh"]
    assert call["target"] == "mini.tail1234.ts.net"
    assert unwrap(call["command"]) == f"git -C {clone} symbolic-ref HEAD"
