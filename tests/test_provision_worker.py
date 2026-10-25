"""Worker provisioning script: parsing and dry runs only; nothing touches a machine.

Every tool the script could change a system or reach the network with is a fake that
records its call, and the dry runs must leave that record empty.
"""

import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "provision_worker.sh"
TOOLS = (
    "apt-get apparmor_parser curl dockerd-rootless-setuptool.sh git gpg install ln "
    "loginctl npm sshd sudo systemctl tar useradd usermod"
).split()


@pytest.fixture
def run(tmp_path):
    fakes = tmp_path / "bin"
    fakes.mkdir()
    calls = tmp_path / "calls"
    for tool in TOOLS:
        fake = fakes / tool
        fake.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{calls}"\nexit 97\n')
        fake.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()

    def run(*args, **env):
        done = subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env={"PATH": f"{fakes}:/usr/bin:/bin", "HOME": str(home), **env},
            timeout=30,
        )
        assert not calls.exists(), calls.read_text()
        assert not any(home.iterdir())
        return done

    return run


def test_script_parses():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_system_dry_run_plans_every_step_without_changing_anything(run):
    done = run("--dry-run")
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert "useradd --create-home --shell /bin/bash --user-group babysit" in out
    for package in ("bubblewrap", "uidmap", "socat", "build-essential", "jq", "python3"):
        assert re.search(rf"apt-get install .*\b{package}\b", out)
    assert re.search(
        r"apt-get install .*\bgh docker-ce .*docker-ce-rootless-extras .*claude-code", out
    )
    # Every apt repository and download is pinned to a fingerprint or a checksum.
    assert out.count("would trust https://") == 3
    assert re.search(r"node-v[\d.]+-linux-x64\.tar\.xz and verify sha256 [0-9a-f]{64}", out)
    assert re.search(r"uv-x86_64-unknown-linux-gnu\.tar\.gz and verify sha256 [0-9a-f]{64}", out)
    assert re.search(r"herdr-linux-x86_64 and verify sha256 [0-9a-f]{64}", out)
    assert "60-babysit-worker.conf" in out
    assert "systemctl disable --now docker.service docker.socket" in out
    assert "dockerd-rootless-setuptool.sh install" in out
    assert "bubblewrap >= 0.10.0" in out
    # Nothing is piped from the network into a shell.
    assert not re.search(r"\|\s*(sudo\s+)?(ba)?sh\b", SCRIPT.read_text())


def test_system_dry_run_follows_architecture_and_options(run):
    done = run("--dry-run", ARCH="arm64", INSTALL_CLAUDE="0", BABYSIT_USER="worker")
    assert done.returncode == 0, done.stderr
    assert "linux-arm64.tar.xz" in done.stdout
    assert "uv-aarch64-unknown-linux-gnu" in done.stdout
    assert "herdr-linux-aarch64" in done.stdout
    assert "claude-code" not in done.stdout
    assert "--user-group worker" in done.stdout


def test_user_step_dry_run_pins_the_checkout(run):
    done = run("--user-step", "--dry-run", "--sha", "0123abc", "--repo", "/fixture/repo")
    assert done.returncode == 0, done.stderr
    assert "git clone /fixture/repo" in done.stdout
    assert "checkout --quiet --detach 0123abc" in done.stdout
    assert ".cache/babysit-sandbox" in done.stdout
    assert "codex login --device-auth" in done.stdout
    assert "claude-account login <name>" in done.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["--user-step", "--dry-run"],
        ["--user-step", "--dry-run", "--sha", "main; rm -rf ~"],
        ["--frobnicate"],
        ["--dry-run", "--sha"],
    ],
)
def test_misuse_exits_2(run, args):
    assert run(*args).returncode == 2


def test_unknown_architecture_is_refused(run):
    assert run("--dry-run", ARCH="riscv64").returncode == 2
