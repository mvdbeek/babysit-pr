"""Updating a worker's babysit-pr to this machine's commit, over a stubbed ssh."""

import json
import subprocess
import threading
import time
from functools import partial
from pathlib import Path

import machines
import pr_workspaces as pw
import pytest
import remote_probe
import worker_updates

APP = Path(pw.__file__).resolve().parent.parent
HEAD = subprocess.check_output(["git", "-C", str(APP), "rev-parse", "HEAD"], text=True).strip()
CHONK = {
    "id": "chonk",
    "label": "chonk",
    "ssh": "babysit@chonk",
    "os": "linux",
    "babysit_root": "/home/babysit/.local/share/babysit-pr/app",
}


@pytest.fixture
def worker(tmp_path, monkeypatch):
    """The registry, the ssh commands run and the commit the worker reports."""
    (tmp_path / "machines.json").write_text(json.dumps({"version": 1, "machines": [CHONK]}))
    ran, reported, release = [], {"head": "0" * 40}, threading.Event()
    release.set()
    local = machines.run

    def run(home, m, argv, *, timeout, input=None, **kwargs):
        if m is None:
            return local(home, m, argv, timeout=timeout, input=input, **kwargs)
        release.wait(10)
        ran.append((m.id, argv, input))
        reported["head"] = argv[-1]
        return subprocess.CompletedProcess(argv, 0, b"==> Done\n", b"")

    monkeypatch.setattr(machines, "run", run)
    monkeypatch.setattr(remote_probe, "call", lambda home, m, op, timeout, **args: dict(reported))
    updates = worker_updates.WorkerUpdates(tmp_path, partial(pw.git, APP))
    return updates, ran, reported, release


def wait(updates, machine_id="chonk"):
    deadline = time.monotonic() + 10
    while updates.snapshot()[machine_id]["running"]:
        assert time.monotonic() < deadline, "the update never finished"
        time.sleep(0.01)
    return updates.snapshot()[machine_id]


def test_the_user_step_of_this_commit_runs_on_the_worker(worker):
    updates, ran, _, _ = worker
    started = updates.update("chonk")
    assert started["sha"] == HEAD and started["ok"] is None
    done = wait(updates)
    assert done["ok"] is True and done["error"] is None and not done["running"]
    [(machine_id, argv, script)] = ran
    assert machine_id == "chonk"
    assert argv == ["timeout", "570", "bash", "-s", "--", "--user-step", "--sha", HEAD]
    shown = subprocess.check_output(
        ["git", "-C", str(APP), "show", f"{HEAD}:scripts/provision_worker.sh"]
    )
    assert script == shown.rstrip(b"\n")


def test_a_worker_still_on_another_commit_reports_the_output(worker, monkeypatch):
    updates, _, reported, _ = worker
    local = machines.run
    monkeypatch.setattr(
        machines,
        "run",
        lambda home, m, argv, **kwargs: (
            local(home, m, argv, **kwargs)
            if m is None
            else subprocess.CompletedProcess(
                argv, 1, b"", b"  PROBLEM: app has local changes; leaving it alone\n"
            )
        ),
    )
    updates.update("chonk")
    done = wait(updates)
    assert done["ok"] is False
    assert done["error"].endswith("PROBLEM: app has local changes; leaving it alone")
    assert reported["head"] == "0" * 40


def test_an_unreachable_worker_is_reported(worker, monkeypatch):
    updates, *_ = worker

    local = machines.run

    def unreachable(home, m, argv, **kwargs):
        if m is None:
            return local(home, m, argv, **kwargs)
        raise subprocess.TimeoutExpired("ssh", 600)

    monkeypatch.setattr(machines, "run", unreachable)
    updates.update("chonk")
    assert wait(updates)["error"] == "chonk did not finish within 600 seconds"


def test_one_update_per_worker_at_a_time(worker):
    updates, ran, _, release = worker
    release.clear()
    updates.update("chonk")
    with pytest.raises(ValueError, match="chonk is already being updated"):
        updates.update("chonk")
    release.set()
    assert wait(updates)["ok"] is True and len(ran) == 1


@pytest.mark.parametrize(
    "changes",
    [{"os": "darwin"}, {"babysit_root": "/home/babysit/src/babysit-pr"}],
)
def test_a_copy_provisioning_does_not_manage_is_refused(worker, tmp_path, changes):
    updates, ran, _, _ = worker
    entry = {**CHONK, **changes}
    (tmp_path / "machines.json").write_text(json.dumps({"version": 1, "machines": [entry]}))
    with pytest.raises(ValueError, match="update its babysit-pr at .* by hand"):
        updates.update("chonk")
    assert not ran and not updates.snapshot()


def test_unknown_machines_are_refused(worker):
    updates, *_ = worker
    with pytest.raises(ValueError, match="Unknown machine box"):
        updates.update("box")
    with pytest.raises(ValueError, match="Name the worker"):
        updates.update(None)
