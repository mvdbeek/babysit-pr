"""Credential grants for Safehouse agents, in a temporary auth home."""

import json

import host_credentials as grants
import pytest

SID = "8f2c6f0e-3b7a-4d34-9a8f-2b8d6c1e9a10"


@pytest.fixture
def auth(tmp_path):
    home = tmp_path / "auth"
    home.mkdir(mode=0o700)
    return home


def stage(auth, minutes=5, now=1000.0):
    return grants.stage(auth, b"secret", minutes, "w1:p1", SID, clock=lambda: now)


def test_a_grant_is_a_private_read_only_file_with_metadata_beside_it(auth):
    path = stage(auth)
    assert path.read_bytes() == b"secret" and path.stat().st_mode & 0o777 == 0o400
    assert path.parent.stat().st_mode & 0o777 == 0o700
    meta = json.loads((path.parent.parent / f"{path.parent.name}.json").read_text())
    assert meta == {"expires": 1300.0, "pane": "w1:p1", "session": SID}
    # The metadata is outside the directory the agent is granted.
    assert [p.name for p in path.parent.iterdir()] == ["credential"]


def test_sweep_removes_a_grant_when_it_expires(auth):
    path = stage(auth)
    assert grants.sweep(auth, clock=lambda: 1299.0) == 1 and path.exists()
    assert grants.sweep(auth, clock=lambda: 1300.0) == 0
    assert not list(grants.root(auth).iterdir())


@pytest.mark.parametrize("current, kept", [(SID, True), (None, True), ("", True), ("other", False)])
def test_sweep_removes_a_grant_once_its_pane_runs_another_conversation(auth, current, kept):
    path = stage(auth)
    panes = []

    def session_of(pane):
        panes.append(pane)
        return current

    assert grants.sweep(auth, session_of, clock=lambda: 1000.0) == int(kept)
    assert path.exists() is kept and panes == ["w1:p1"]


def test_sweep_removes_leftovers_it_cannot_expire(auth):
    kept = stage(auth)
    root = grants.root(auth)
    (root / ("a" * 32)).mkdir()
    (root / f"{'b' * 32}.json").write_text("not json")
    (root / ("b" * 32)).mkdir()
    (root / "unrelated").mkdir()
    assert grants.sweep(auth, clock=lambda: 1000.0) == 1
    assert sorted(p.name for p in root.iterdir()) == sorted(
        [kept.parent.name, f"{kept.parent.name}.json", "unrelated"]
    )


def test_launcher_words_of_a_grant_are_recognised():
    word = f"--add-dirs-ro=/Users/me/.config/babysit-pr-auth/credential-grants/{'c' * 32}"
    assert grants.GRANTED.fullmatch(word)
    assert not grants.GRANTED.fullmatch("--add-dirs-ro=/opt/data")
    assert not grants.GRANTED.fullmatch(f"--add-dirs={word.split('=', 1)[1]}")
