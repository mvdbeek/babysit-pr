"""Personal skills kept on workers as on this machine: the registry key, the bundle, the
worker's install and the launches that sync first, over fake homes and a fake ssh."""

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import threading
import time
from pathlib import Path

import agent_messages
import cron_agents
import machines
import pytest
import remote_probe
import remote_repair
import skill_sync
from test_new_tasks import request as new_request  # noqa: F401
from test_pr_workspaces import finish, local  # noqa: F401 (fixture)
from test_remote_cron import bwrap, git, host, rollout, short_tmp  # noqa: F401 (fixtures)
from test_remote_launch import WORKER, register, remote, remote_commands  # noqa: F401

CHONK = {"id": "chonk", "label": "chonk", "ssh": "babysit@chonk", "os": "linux"}


@pytest.fixture(autouse=True)
def fresh_sync(monkeypatch):
    """Each test packs and records afresh."""
    monkeypatch.setattr(skill_sync, "_bundles", {})
    monkeypatch.setattr(skill_sync, "_results", {})
    monkeypatch.setattr(skill_sync, "_syncing", {})


@pytest.fixture
def skills(tmp_path, monkeypatch):
    """This machine's skills folder: merge-forward with a script, and wt-task by a link."""
    source = tmp_path / "controller-skills"
    merge = source / "merge-forward"
    (merge / "scripts").mkdir(parents=True)
    (merge / "SKILL.md").write_text("---\nname: merge-forward\n---\nMerge.\n")
    (merge / "scripts" / "hop.sh").write_text("#!/bin/sh\necho hop\n")
    (merge / "scripts" / "hop.sh").chmod(0o755)
    (merge / "__pycache__").mkdir()
    (merge / "__pycache__" / "x.pyc").write_bytes(b"cache")
    managed = tmp_path / "dotfiles" / "wt-task"
    managed.mkdir(parents=True)
    (managed / "SKILL.md").write_text("---\nname: wt-task\n---\nHand off.\n")
    (source / "wt-task").symlink_to(managed)
    monkeypatch.setattr(skill_sync, "SOURCE", str(source))
    return source


@pytest.fixture
def worker_home(tmp_path, monkeypatch):
    """A worker's home: two Claude accounts, the default config and Codex's home."""
    home = tmp_path / "worker"
    for path in (".claude/accounts/oss", ".claude/accounts/psu", ".codex"):
        (home / path).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home


ROOTS = (".claude/skills", ".claude/accounts/oss/skills", ".claude/accounts/psu/skills")
ROOTS += (".codex/skills",)


def write(home, *entries, **fields):
    (home / "machines.json").write_text(
        json.dumps({"version": 1, "machines": [{**CHONK, **fields}, *entries]})
    )


def stage(home, data):
    bundles = home / ".local/state/babysit-pr/skill-bundles"
    bundles.mkdir(parents=True, exist_ok=True)
    path = bundles / f"{hashlib.sha256(data).hexdigest()[:16]}.tar"
    path.write_bytes(data)
    return os.path.realpath(path)


def install(home, data, sha=None):
    path = stage(home, data)
    answer = remote_probe._host_skills_install(path, sha or hashlib.sha256(data).hexdigest())
    return remote_probe.validate_skills(answer)


def status():
    return remote_probe.validate_skills(remote_probe._host_skills_status())


def manifest(home):
    return json.loads((home / ".local/state/babysit-pr/skills.json").read_text())


def tar(*members):
    """A tar of ``(TarInfo fields, content)`` pairs, for bundles no packing would make."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for fields, content in members:
            info = tarfile.TarInfo(fields.pop("name"))
            for key, value in fields.items():
                setattr(info, key, value)
            if content is not None:
                info.size = len(content)
            archive.addfile(info, None if content is None else io.BytesIO(content))
    return buffer.getvalue()


def folder(name):
    return {"name": name, "type": tarfile.DIRTYPE, "mode": 0o755}, None


def file(name, content=b"x\n", mode=0o644):
    return {"name": name, "mode": mode}, content


# The registry -------------------------------------------------------------------------


def test_a_worker_names_the_skills_it_keeps_in_sync(tmp_path):
    write(tmp_path)
    assert machines.load(tmp_path)["chonk"].skills is None
    write(tmp_path, skills=["merge-forward", "wt-task", "x.y_z"])
    assert machines.load(tmp_path)["chonk"].skills == ("merge-forward", "wt-task", "x.y_z")
    # An empty list keeps nothing: what an earlier sync installed is removed.
    write(tmp_path, skills=[])
    assert machines.load(tmp_path)["chonk"].skills == ()


@pytest.mark.parametrize(
    "skills",
    [
        "merge-forward",
        ["synced"],
        ["babysit-pr"],
        ["wt-task", "wt-task"],
        [".hidden"],
        ["-x"],
        ["a/b"],
        ["..", "x"],
        ["tab\tname"],
        ["n" * 65],
        [7],
        [f"s{i}" for i in range(machines.MAX_SKILLS + 1)],
    ],
)
def test_skill_names_a_worker_cannot_take_are_refused(tmp_path, skills):
    write(tmp_path, skills=skills)
    with pytest.raises(
        ValueError,
        match=r"Invalid skills \(unique names, at most 64, not babysit-pr or synced\) for machine chonk",
    ):
        machines.load(tmp_path)


# The bundle ---------------------------------------------------------------------------


def test_the_bundle_is_the_same_bytes_for_the_same_skills(skills):
    data, digest = skill_sync.pack(["wt-task", "merge-forward"])
    assert digest == hashlib.sha256(data).hexdigest()
    # Times, owners and the read bits never change it.
    os.utime(skills / "merge-forward" / "SKILL.md", (1, 1))
    (skills / "merge-forward" / "SKILL.md").chmod(0o600)
    assert skill_sync.pack(["merge-forward", "wt-task"]) == (data, digest)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = archive.getmembers()
    assert [m.name for m in members] == [
        "merge-forward",
        "merge-forward/SKILL.md",
        "merge-forward/scripts",
        "merge-forward/scripts/hop.sh",
        "wt-task",
        "wt-task/SKILL.md",
    ]
    assert {(m.mtime, m.uid, m.gid, m.uname, m.gname) for m in members} == {(0, 0, 0, "", "")}
    assert [m.mode for m in members if m.isfile()] == [0o644, 0o755, 0o644]
    # The executable bit and the content do.
    (skills / "merge-forward" / "scripts" / "hop.sh").chmod(0o644)
    assert skill_sync.pack(["merge-forward", "wt-task"])[1] != digest


def test_a_launch_burst_packs_once(skills):
    clock = [100.0]
    first = skill_sync.bundle(["merge-forward"], now=lambda: clock[0])
    (skills / "merge-forward" / "SKILL.md").write_text("changed\n")
    assert skill_sync.bundle(["merge-forward"], now=lambda: clock[0]) == first
    clock[0] += skill_sync.CACHE_SECONDS
    assert skill_sync.bundle(["merge-forward"], now=lambda: clock[0]) != first


def test_skills_holding_anything_but_folders_and_files_are_refused(skills, tmp_path):
    with pytest.raises(ValueError, match="No skill gone"):
        skill_sync.pack(["gone"])
    (skills / "plain").mkdir()
    with pytest.raises(ValueError, match="not a skill folder with a SKILL.md"):
        skill_sync.pack(["plain"])
    (skills / "plain" / "SKILL.md").symlink_to(skills / "merge-forward" / "SKILL.md")
    with pytest.raises(ValueError, match="plain/SKILL.md is a symbolic link"):
        skill_sync.pack(["plain"])
    (skills / "merge-forward" / "secret").symlink_to(tmp_path / "secret")
    with pytest.raises(ValueError, match="merge-forward/secret is a symbolic link"):
        skill_sync.pack(["merge-forward"])
    (skills / "merge-forward" / "secret").unlink()
    # A FIFO is refused, never waited on.
    os.mkfifo(skills / "merge-forward" / "pipe")
    with pytest.raises(ValueError, match="merge-forward/pipe is neither a folder nor a regular"):
        skill_sync.pack(["merge-forward"])
    (skills / "merge-forward" / "pipe").unlink()
    # A hard link could name any file of this user's.
    token = tmp_path / "token"
    token.write_text("secret\n")
    os.link(token, skills / "merge-forward" / "token")
    with pytest.raises(ValueError, match="merge-forward/token has other names"):
        skill_sync.pack(["merge-forward"])
    (skills / "merge-forward" / "token").unlink()
    (skills / "merge-forward" / "bad\x1bname").write_text("x")
    with pytest.raises(ValueError, match="Refusing skill bundle entry"):
        skill_sync.pack(["merge-forward"])


def test_a_skill_folder_swapped_after_it_was_resolved_is_refused(skills, monkeypatch):
    monkeypatch.setattr(skill_sync, "_fd_path", lambda fd: "/Users/someone/.ssh")
    with pytest.raises(ValueError, match="merge-forward moved while it was read"):
        skill_sync.pack(["merge-forward"])


def test_the_folder_packed_is_the_one_its_path_names(skills):
    # The real path, as the kernel reports the descriptor: nothing moved, nothing refused.
    fd = os.open(os.path.realpath(skills / "wt-task"), os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert skill_sync._fd_path(fd) == os.path.realpath(skills / "wt-task")
    finally:
        os.close(fd)


def test_skills_over_the_size_limit_are_refused_before_they_are_read(skills, monkeypatch):
    monkeypatch.setattr(remote_probe, "MAX_SKILL_BYTES", 64)
    (skills / "merge-forward" / "big").write_bytes(b"x" * 65)
    read = []
    real_open = os.open

    def opened(path, *args, **kwargs):
        read.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", opened)
    with pytest.raises(ValueError, match="merge-forward exceeds 64 bytes"):
        skill_sync.pack(["merge-forward"])
    assert "big" not in read
    monkeypatch.setattr(os, "open", real_open)
    (skills / "merge-forward" / "big").unlink()
    monkeypatch.setattr(remote_probe, "MAX_SKILL_ENTRIES", 3)
    with pytest.raises(ValueError, match="more than 3 entries"):
        skill_sync.pack(["merge-forward"])


@pytest.mark.parametrize(
    ("members", "message"),
    [
        (
            [folder("s"), file("s/SKILL.md"), ({"name": "s/l", "type": tarfile.SYMTYPE}, None)],
            "not a folder or file",
        ),
        (
            [folder("s"), file("s/SKILL.md"), ({"name": "s/h", "type": tarfile.LNKTYPE}, None)],
            "not a folder or file",
        ),
        (
            [folder("s"), file("s/SKILL.md"), ({"name": "s/d", "type": tarfile.CHRTYPE}, None)],
            "not a folder or file",
        ),
        ([folder("s"), file("s/SKILL.md"), file("/etc/passwd")], "Refusing skill bundle entry"),
        ([folder("s"), file("s/SKILL.md"), file("s/../../x")], "Refusing skill bundle entry"),
        ([folder("s"), file("s/SKILL.md"), file("s/./x")], "Refusing skill bundle entry"),
        ([folder("s"), file("s/SKILL.md"), file("s/x\ny")], "Refusing skill bundle entry"),
        ([folder("s"), file("s/SKILL.md"), file("s/SKILL.md")], "appears twice"),
        ([folder("s"), file("s/sub/x"), file("s/SKILL.md")], "comes before its folder"),
        ([file("s/SKILL.md")], "comes before its folder"),
        ([file("loose")], "not a skill folder"),
        ([folder("synced"), file("synced/SKILL.md")], "not a skill folder"),
        ([folder("Synced"), file("Synced/SKILL.md")], "not a skill folder"),
        ([folder("BABYSIT-PR"), file("BABYSIT-PR/SKILL.md")], "not a skill folder"),
        ([folder("s"), file("s/notes.md")], "Skill s has no SKILL.md"),
        ([folder("s"), folder("s/SKILL.md")], "Skill s has no SKILL.md"),
    ],
)
def test_a_bundle_no_packing_makes_is_refused_whole(members, message):
    with pytest.raises(ValueError, match=message):
        remote_probe.skill_entries(tar(*members))


def test_a_bundle_that_is_no_tar_is_refused():
    with pytest.raises(ValueError, match="not a tar archive"):
        remote_probe.skill_entries(b"\x1f\x8b gzip, not a plain tar" * 40)


# The worker's install -----------------------------------------------------------------

ROOT_KEYS = {"claude": None, "claude:oss": None, "claude:psu": None, "codex": None}


def test_a_worker_gets_the_skills_in_every_skills_folder(skills, worker_home):
    data, digest = skill_sync.pack(["merge-forward", "wt-task"])
    assert status() == {"bundle": None, "roots": ROOT_KEYS, "skipped": []}
    assert install(worker_home, data) == {"bundle": digest, "roots": ROOT_KEYS, "skipped": []}
    for root in ROOTS:
        copy = worker_home / root
        assert stat.S_IMODE(copy.stat().st_mode) == 0o700
        assert (copy / "merge-forward/SKILL.md").read_text().endswith("Merge.\n")
        assert (copy / "wt-task/SKILL.md").read_text().endswith("Hand off.\n")
        # Real folders: a link would lead outside the sandbox's view.
        assert not (copy / "wt-task").is_symlink()
        assert os.access(copy / "merge-forward/scripts/hop.sh", os.X_OK)
        assert not os.access(copy / "merge-forward/SKILL.md", os.X_OK)
        assert not (copy / "merge-forward/__pycache__").exists()
        assert not [p for p in copy.iterdir() if p.name.startswith(".")]
    recorded = manifest(worker_home)
    assert recorded["bundle"] == digest
    assert set(recorded["skills"]) == {"merge-forward", "wt-task"}
    assert {Path(root).relative_to(worker_home.resolve()) for root in recorded["roots"]} == {
        Path(root) for root in ROOTS
    }
    # The staged bundle is taken.
    assert not list((worker_home / ".local/state/babysit-pr/skill-bundles").iterdir())


def test_codex_home_is_where_codex_reads_its_skills(skills, worker_home, monkeypatch):
    codex = worker_home / "codex-home"
    codex.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex))
    data, _ = skill_sync.pack(["wt-task"])
    assert install(worker_home, data)["roots"]["codex"] is None
    assert (codex / "skills/wt-task/SKILL.md").exists()
    assert not (worker_home / ".codex/skills").exists()


def test_a_changed_or_missing_copy_is_stale_and_put_back(skills, worker_home):
    data, digest = skill_sync.pack(["merge-forward"])
    install(worker_home, data)
    hop = worker_home / ".claude/accounts/oss/skills/merge-forward/scripts/hop.sh"
    hop.write_text("#!/bin/sh\necho tampered\n")
    (worker_home / ".codex/skills/merge-forward/SKILL.md").chmod(0o700)
    roots = status()["roots"]
    assert roots["claude:oss"].endswith("oss/skills/merge-forward was changed")
    assert roots["codex"].endswith(".codex/skills/merge-forward was changed")
    assert roots["claude"] is None and roots["claude:psu"] is None
    # A new account has none yet.
    (worker_home / ".claude/accounts/work").mkdir()
    assert status()["roots"]["claude:work"].endswith("work/skills/merge-forward is missing")
    answer = install(worker_home, data)
    assert answer["bundle"] == digest and not any(answer["roots"].values())
    assert hop.read_text() == "#!/bin/sh\necho hop\n"
    assert (worker_home / ".claude/accounts/work/skills/merge-forward/SKILL.md").exists()


def test_a_skill_the_sync_did_not_make_is_left_alone(skills, worker_home):
    own = worker_home / ".claude/accounts/psu/skills/wt-task"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("mine\n")
    elsewhere = worker_home / "elsewhere"
    elsewhere.mkdir()
    (worker_home / ".codex/skills").mkdir()
    (worker_home / ".codex/skills/wt-task").symlink_to(elsewhere)
    data, digest = skill_sync.pack(["merge-forward", "wt-task"])
    answer = install(worker_home, data)
    assert answer["bundle"] == digest and not any(answer["roots"].values())
    real = worker_home.resolve()
    assert sorted(answer["skipped"]) == [
        f"{real}/.claude/accounts/psu/skills/wt-task is not babysit's copy; left alone",
        f"{real}/.codex/skills/wt-task is not babysit's copy; left alone",
    ]
    assert (own / "SKILL.md").read_text() == "mine\n"
    assert (worker_home / ".codex/skills/wt-task").is_symlink() and not list(elsewhere.iterdir())
    assert (worker_home / ".codex/skills/merge-forward/SKILL.md").exists()
    # Reported, not a problem: a launch is not held up by it.
    assert not any(status()["roots"].values()) and len(status()["skipped"]) == 2
    # Dropping the skill removes only the copies the sync made.
    data, _ = skill_sync.pack(["merge-forward"])
    install(worker_home, data)
    assert (own / "SKILL.md").read_text() == "mine\n"
    assert (worker_home / ".codex/skills/wt-task").is_symlink()
    assert not (worker_home / ".claude/accounts/oss/skills/wt-task").exists()
    assert not (worker_home / ".claude/skills/wt-task").exists()
    data, _ = skill_sync.pack([])
    assert not any(install(worker_home, data)["roots"].values())
    assert not (worker_home / ".claude/skills/merge-forward").exists()
    assert manifest(worker_home)["roots"] == {}


def test_linked_skills_folders_and_accounts_are_never_followed(skills, worker_home):
    target = worker_home / "target"
    target.mkdir()
    (worker_home / ".claude/accounts/oss/skills").symlink_to(target)
    account = worker_home / "account"
    account.mkdir()
    (worker_home / ".claude/accounts/linked").symlink_to(account)
    data, digest = skill_sync.pack(["merge-forward"])
    answer = install(worker_home, data)
    accounts = (worker_home / ".claude/accounts").resolve()
    assert answer["bundle"] == digest
    assert answer["roots"]["claude:oss"] == (
        f"{accounts}/oss/skills is a symbolic link or no folder; left alone"
    )
    assert "claude:linked" not in answer["roots"]
    assert answer["skipped"] == [f"{accounts}/linked is a symbolic link; its skills are left alone"]
    assert not list(target.iterdir()) and not list(account.iterdir())
    assert (worker_home / ".claude/accounts/psu/skills/merge-forward/SKILL.md").exists()


def test_an_unwritable_skills_folder_says_why(skills, worker_home):
    psu = worker_home / ".claude/accounts/psu"
    psu.chmod(0o500)
    try:
        data, digest = skill_sync.pack(["merge-forward"])
        answer = install(worker_home, data)
        assert answer["bundle"] == digest
        assert (
            answer["roots"]["claude:psu"]
            == f"Cannot open {psu.resolve()}/skills: Permission denied"
        )
        assert answer["roots"]["claude:oss"] is None
    finally:
        psu.chmod(0o700)


def test_a_sync_interrupted_midway_is_cleaned_up_by_the_next(skills, worker_home):
    data, digest = skill_sync.pack(["merge-forward"])
    leftover = worker_home / ".codex/skills/.babysit-skill-0123"
    (leftover / "half").mkdir(parents=True)
    assert install(worker_home, data)["bundle"] == digest
    assert not leftover.exists()


def test_an_install_cut_short_before_its_manifest_is_finished_by_the_next(
    skills, worker_home, monkeypatch
):
    data, _ = skill_sync.pack(["merge-forward"])
    writes = []
    real = remote_probe._write_skill_manifest

    def cut(manifest):
        writes.append(manifest)
        if len(writes) == 2:
            raise OSError("killed")  # After the swaps, before the bundle is recorded.
        real(manifest)

    monkeypatch.setattr(remote_probe, "_write_skill_manifest", cut)
    with pytest.raises(OSError, match="killed"):
        install(worker_home, data)
    monkeypatch.setattr(remote_probe, "_write_skill_manifest", real)
    # Every name it touched is recorded, and no bundle, so the next launch installs again.
    assert manifest(worker_home)["bundle"] is None
    assert (worker_home / ".codex/skills/merge-forward/SKILL.md").exists()
    (skills / "merge-forward" / "SKILL.md").write_text("changed\n")
    data, digest = skill_sync.pack(["merge-forward"])
    answer = install(worker_home, data)
    assert answer["bundle"] == digest and not any(answer["roots"].values())
    assert answer["skipped"] == []
    for root in ROOTS:
        assert (worker_home / root / "merge-forward/SKILL.md").read_text() == "changed\n"


def test_an_unrecorded_copy_of_the_bundle_is_adopted(skills, worker_home):
    data, digest = skill_sync.pack(["merge-forward"])
    install(worker_home, data)
    (worker_home / ".local/state/babysit-pr/skills.json").unlink()
    remote_probe._write_skill_manifest(
        {"version": 1, "bundle": digest, "skills": manifest_digests(data), "roots": {}}
    )
    roots = status()["roots"]
    assert all(said.endswith("merge-forward is not recorded yet") for said in roots.values())
    assert not any(install(worker_home, data)["roots"].values())
    assert len(manifest(worker_home)["roots"]) == len(ROOTS)


def manifest_digests(data):
    skills = remote_probe.skill_entries(data)
    return {name: remote_probe.skill_digest(entries) for name, entries in skills.items()}


def test_concurrent_installs_take_turns(skills, worker_home):
    first, _ = skill_sync.pack(["merge-forward"])
    second, digest = skill_sync.pack(["merge-forward", "wt-task"])
    paths = [stage(worker_home, first), stage(worker_home, second)]
    errors = []

    def run(path, data):
        try:
            remote_probe._host_skills_install(path, hashlib.sha256(data).hexdigest())
        except Exception as exc:  # noqa: BLE001 (reported below)
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(paths[0], first)),
        threading.Thread(target=run, args=(paths[1], second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    # Whichever went last, its bundle is whole everywhere and nothing is left half done.
    state = status()
    assert state["bundle"] in {hashlib.sha256(first).hexdigest(), digest}
    assert not any(state["roots"].values())
    for root in ROOTS:
        assert not [p for p in (worker_home / root).iterdir() if p.name.startswith(".")]


def test_manifest_records_of_folders_gone_are_dropped(skills, worker_home):
    data, _ = skill_sync.pack(["merge-forward"])
    install(worker_home, data)
    shutil.rmtree(worker_home / ".claude/accounts/psu")
    install(worker_home, data)
    assert not [root for root in manifest(worker_home)["roots"] if "/psu/" in root]


def test_a_bundle_is_taken_only_from_its_staging_folder_and_by_its_hash(skills, worker_home):
    data, digest = skill_sync.pack(["merge-forward"])
    with pytest.raises(ValueError, match="does not match its SHA-256"):
        install(worker_home, data, sha="0" * 64)
    # Removed even so: a staged bundle runs once.
    assert not list((worker_home / ".local/state/babysit-pr/skill-bundles").iterdir())
    outside = worker_home / "bundle.tar"
    outside.write_bytes(data)
    with pytest.raises(ValueError, match="taken only from"):
        remote_probe._host_skills_install(str(outside), digest)
    assert outside.exists()
    with pytest.raises(ValueError, match="Expected the bundle's SHA-256"):
        remote_probe._host_skills_install(stage(worker_home, data), "main")
    bad = tar(folder("s"), file("s/SKILL.md"), ({"name": "s/l", "type": tarfile.SYMTYPE}, None))
    with pytest.raises(ValueError, match="not a folder or file"):
        install(worker_home, bad)
    assert not (worker_home / ".claude/skills").exists()


def test_a_damaged_manifest_stops_the_sync(skills, worker_home):
    data, _ = skill_sync.pack(["merge-forward"])
    install(worker_home, data)
    (worker_home / ".local/state/babysit-pr/skills.json").write_text('{"skills": []}')
    with pytest.raises(ValueError, match="not a skills manifest babysit wrote"):
        status()
    with pytest.raises(ValueError, match="not a skills manifest babysit wrote"):
        install(worker_home, data)


@pytest.mark.parametrize(
    "answer",
    [
        {"bundle": "main", "roots": {}, "skipped": []},
        {"bundle": None, "roots": {"codex": "a\x1b[2Jb"}, "skipped": []},
        {"bundle": None, "roots": {"claude:../x": None}, "skipped": []},
        {"bundle": None, "roots": {"elsewhere": None}, "skipped": []},
        {"bundle": None, "roots": {}, "skipped": ["x"] * 33},
        {"bundle": None, "roots": {}},
    ],
)
def test_a_malformed_skills_answer_is_refused(answer):
    with pytest.raises(ValueError, match="Malformed probe answer"):
        remote_probe.validate_skills(answer)


# Syncing from the controller ----------------------------------------------------------


@pytest.fixture
def in_process(tmp_path, worker_home, monkeypatch):
    """Probe ops and staging run in this process, against the worker's home."""
    calls = []

    def call(home, m, op, timeout, **args):
        calls.append(op)
        return remote_probe.VALIDATORS[op](remote_probe.OPS[op](**args))

    def put_file(home, m, directory, name, data, *, keep=None, timeout=30):
        calls.append("put_file")
        path = Path(os.path.expanduser(directory)) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return os.path.realpath(path)

    monkeypatch.setattr(remote_probe, "call", call)
    monkeypatch.setattr(machines, "put_file", put_file)
    controller = tmp_path / "controller"
    controller.mkdir()
    return controller, calls


def test_a_worker_is_synced_only_when_its_skills_differ(skills, worker_home, in_process):
    controller, calls = in_process
    write(controller, skills=["merge-forward", "wt-task"])
    m = machines.load(controller)["chonk"]
    assert skill_sync.status(m)["state"] == "unknown"
    _, digest = skill_sync.bundle(m.skills)
    assert skill_sync.ensure(controller, m, "claude", "oss")["bundle"] == digest
    assert calls == ["skills_status", "put_file", "skills_install"]
    assert skill_sync.status(m)["state"] == "in_sync"
    calls.clear()
    skill_sync.ensure(controller, m, "codex")
    assert calls == ["skills_status"]
    # A skill edited here goes out once the packing is due again.
    (skills / "merge-forward" / "SKILL.md").write_text("new\n")
    skill_sync._bundles.clear()
    assert skill_sync.status(m)["state"] == "out_of_date"
    skill_sync.ensure(controller, m)
    assert (worker_home / ".codex/skills/merge-forward/SKILL.md").read_text() == "new\n"
    # Without the key nothing is asked of the worker.
    calls.clear()
    plain = m._replace(skills=None)
    assert skill_sync.ensure(controller, plain) is None and skill_sync.status(plain) is None
    assert calls == []


def test_only_the_launchs_own_unwritable_folder_stops_it(skills, worker_home, in_process):
    controller, _ = in_process
    write(controller, skills=["merge-forward"])
    m = machines.load(controller)["chonk"]
    psu = worker_home / ".claude/accounts/psu"
    psu.chmod(0o500)
    try:
        # Another account's folder is reported, not fatal.
        skill_sync.ensure(controller, m, "claude", "oss")
        shown = skill_sync.status(m)
        assert shown["state"] == "partial"
        assert shown["problems"] == {
            "claude:psu": f"Cannot open {psu.resolve()}/skills: Permission denied"
        }
        with pytest.raises(
            ValueError,
            match="^Cannot sync skills to chonk: Cannot open .*psu/skills: Permission denied$",
        ):
            skill_sync.ensure(controller, m, "claude", "psu")
        with pytest.raises(ValueError, match="has no skills folder for claude:gone"):
            skill_sync.ensure(controller, m, "claude", "gone")
    finally:
        psu.chmod(0o700)


def test_a_sync_that_cannot_happen_says_why(skills, worker_home, in_process, monkeypatch):
    controller, _ = in_process
    write(controller, skills=["gone"])
    m = machines.load(controller)["chonk"]
    with pytest.raises(ValueError, match="^Cannot sync skills to chonk: No skill gone in"):
        skill_sync.ensure(controller, m)
    shown = skill_sync.status(m)
    assert shown["state"] == "error" and "No skill gone" in shown["error"]

    def unreachable(home, m, op, timeout, **args):
        raise subprocess.TimeoutExpired(["ssh"], timeout)

    monkeypatch.setattr(remote_probe, "call", unreachable)
    m = m._replace(skills=("merge-forward",))
    with pytest.raises(ValueError, match="chonk did not answer within 30 seconds"):
        skill_sync.ensure(controller, m)
    assert skill_sync.status(m)["state"] == "error"


def test_a_worker_cron_run_syncs_skills_before_anything_else(tmp_path, monkeypatch):
    write(tmp_path, skills=["merge-forward"])
    asked = []

    def ensure(home, m, agent=None, account=None):
        asked.append((m.id, agent, account))
        raise ValueError("Cannot sync skills to chonk: no")

    monkeypatch.setattr(skill_sync, "ensure", ensure)
    monkeypatch.setattr(cron_agents, "probe", lambda *a, **k: pytest.fail("probed"))
    job = {"machine": "chonk", "agent": "claude", "claude_account": "oss"}
    with pytest.raises(ValueError, match="Cannot sync skills to chonk"):
        cron_agents.start(job, 1, tmp_path, tmp_path)
    assert asked == [("chonk", "claude", "oss")]


def test_a_resume_on_a_worker_warns_when_its_skills_cannot_sync(tmp_path, monkeypatch):
    write(tmp_path, skills=["merge-forward"])
    m = machines.load(tmp_path)["chonk"]
    session = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    chosen = {"id": session, "agent": "codex", "_cwd": "/w"}
    monkeypatch.setattr(agent_messages, "recorded", lambda root, home, machine: [chosen])

    def ensure(home, machine, agent=None, account=None):
        raise ValueError(f"Cannot sync skills to {machine.label}: no")

    class Resumed(Exception):
        pass

    def remote_resume(*args):
        raise Resumed  # It goes ahead.

    monkeypatch.setattr(skill_sync, "ensure", ensure)
    monkeypatch.setattr(agent_messages, "remote_resume", remote_resume)
    with pytest.raises(Resumed):
        agent_messages.resume("w1", session, "go on", tmp_path, root="/w", machine=m)
    shown = agent_messages.warned({"sent": True, "warning": "Not confirmed yet."}, "Skills: no.")
    assert shown["warning"] == "Skills: no. Not confirmed yet."
    assert agent_messages.warned({"warning": None}, None) == {"warning": None}


def test_a_repair_on_a_worker_syncs_skills_and_goes_ahead(tmp_path, monkeypatch):
    write(tmp_path, skills=["merge-forward"])
    asked = []

    def ensure(home, m, agent=None, account=None):
        asked.append((agent, account))
        raise ValueError("Cannot sync skills to chonk: no")

    class Staged(Exception):
        pass

    def put_file(*args, **kwargs):
        raise Staged

    monkeypatch.setattr(skill_sync, "ensure", ensure)
    monkeypatch.setattr(remote_repair, "check_version", lambda home, worker: None)
    monkeypatch.setattr(machines, "put_file", put_file)
    folder = tmp_path / "run"
    folder.mkdir()
    job = {
        "machine": "chonk",
        "agent": "claude",
        "claude_config_dir": "/home/babysit/.claude/accounts/psu",
    }
    with pytest.raises(Staged):
        remote_repair.run(tmp_path, job, folder, {"head_branch": "b", "head_sha": "0" * 40}, "go")
    assert asked == [("claude", "psu")]
    assert "Cannot sync skills to chonk: no" in (folder / "worker.log").read_text()
    assert remote_repair.config_account("/home/babysit/.claude") is None


def test_a_review_on_a_worker_brings_its_skills_first(remote, skills):  # noqa: F811
    manager, pr, state, worker = remote
    register(manager.home, skills=["merge-forward", "wt-task"])
    (worker / ".claude/accounts/oss").mkdir(parents=True)
    manager.action({"id": pr["id"], "action": "handle", "task": "Review it", "machine": "mini"})
    op = finish(manager, pr["id"])
    assert op["status"] == "complete", op
    assert "skills_warning" not in op
    for root in (".claude/skills", ".claude/accounts/oss/skills", ".codex/skills"):
        assert (worker / root / "wt-task" / "SKILL.md").read_text().endswith("Hand off.\n")
    machine = next(m for m in manager.machine_list() if m["id"] == "mini")
    assert machine["skills"]["state"] == "in_sync"
    # Skills went before the agent was typed.
    commands = remote_commands(state)
    staged = next(i for i, c in enumerate(commands) if "skill-bundles" in c)
    assert staged < next(i for i, c in enumerate(commands) if "wt.py" in c)


def test_a_review_a_person_starts_warns_when_its_skills_cannot_sync(remote, skills):  # noqa: F811
    manager, pr, state, worker = remote
    register(manager.home, skills=["gone"])
    manager.action({"id": pr["id"], "action": "handle", "task": "Review it", "machine": "mini"})
    op = finish(manager, pr["id"])
    assert op["status"] == "complete", op
    assert op["skills_warning"].startswith("Cannot sync skills to Mac mini: No skill gone in")
    machine = next(m for m in manager.machine_list() if m["id"] == "mini")
    assert machine["skills"]["state"] == "error"


def test_a_scheduled_review_whose_skills_cannot_sync_never_starts(remote, skills):  # noqa: F811
    manager, pr, state, worker = remote
    register(manager.home, skills=["gone"])
    manager.overview.value["synced_at"] = time.time()
    start = time.time() + 3600
    request = {"id": pr["id"], "action": "handle", "task": "Later", "machine": "mini"}
    manager.action({**request, "start_at": start})
    manager.run_due(now=start)
    op = finish(manager, pr["id"])
    assert op["status"] == "failed"
    assert op["message"].startswith("Cannot sync skills to Mac mini: No skill gone in")
    assert not [c for c in remote_commands(state) if "wt.py" in c]
