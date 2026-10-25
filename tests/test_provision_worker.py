"""Worker provisioning script: parsing and dry runs only; nothing touches a machine.

Every tool the script could change a system or reach the network with is a fake that
records its call, and the dry runs must leave that record empty.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "provision_worker.sh"
TOOLS = (
    "apt-get apparmor_parser curl dockerd-rootless-setuptool.sh git gpg install ln "
    "loginctl npm runuser sshd sudo systemctl tar useradd usermod"
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
    # The machine's rootful docker is left alone.
    assert "docker.service" not in out
    assert "containerd.service" not in out
    assert "dockerd-rootless-setuptool.sh install" in out
    assert "bubblewrap >= 0.10.0" in out
    # Archives never keep their uid: extracted unprivileged-style, installed as root.
    assert re.search(r"tar -xJf .*--no-same-owner --no-same-permissions", out)
    assert re.search(r"tar -xzf .*--no-same-owner --no-same-permissions", out)
    assert (
        "install -o root -g root -m 0755 /tmp/provision-worker.XXXXXX/uv/uv /usr/local/bin/uv"
        in out
    )
    assert "would record /usr/local/share/babysit-provision/uv-" in out
    assert "systemctl try-reload-or-restart ssh.service" in out
    assert "would run: sshd -t" in out
    # Apt keys: only the pinned fingerprint is exported, to a binary keyring.
    assert out.count("gpg --export only that key to /etc/apt/keyrings/") == 3
    assert "/etc/apt/keyrings/docker.gpg" in out
    assert "/etc/apt/keyrings/claude-code.asc" not in out
    # Nothing is piped from the network into a shell.
    assert not re.search(r"\|\s*(sudo\s+)?(ba)?sh\b", SCRIPT.read_text())


def test_authorized_keys_are_written_as_the_user(run):
    done = run("--dry-run", BABYSIT_SSH_KEY="ssh-ed25519 AAAA test")
    assert done.returncode == 0, done.stderr
    assert "runuser -u babysit -- sh -c" in done.stdout
    text = SCRIPT.read_text()
    assert "umask 077" in text
    assert '[ -L "$p" ]' in text
    assert '--export "$fpr"' in text


def test_docker_source_selection(run):
    done = run("--dry-run", DOCKER_SOURCE="docker.io")
    assert done.returncode == 0, done.stderr
    assert re.search(r"apt-get install .*\bdocker\.io rootlesskit\b", done.stdout)
    assert "download.docker.com" not in done.stdout
    assert run("--dry-run", DOCKER_SOURCE="podman").returncode == 2


def test_unconfined_bwrap_needs_explicit_acknowledgement(run):
    assert run("--dry-run", BWRAP_APPARMOR="unconfined").returncode == 2
    done = run("--dry-run", BWRAP_APPARMOR="unconfined", BWRAP_APPARMOR_I_UNDERSTAND="1")
    assert done.returncode == 0, done.stderr


def test_system_dry_run_follows_architecture_and_options(run):
    done = run("--dry-run", ARCH="arm64", INSTALL_CLAUDE="0", BABYSIT_USER="worker")
    assert done.returncode == 0, done.stderr
    assert "linux-arm64.tar.xz" in done.stdout
    assert "uv-aarch64-unknown-linux-gnu" in done.stdout
    assert "herdr-linux-aarch64" in done.stdout
    assert "claude-code" not in done.stdout
    assert "--user-group worker" in done.stdout


def test_user_step_dry_run_pins_the_checkout(run):
    done = run(
        "--user-step", "--dry-run", "--sha", "0123abc" * 5 + "01234", "--repo", "/fixture/repo"
    )
    assert done.returncode == 0, done.stderr
    assert "git clone /fixture/repo" in done.stdout
    assert "checkout --quiet --detach " + "0123abc" * 5 + "01234" in done.stdout
    assert ".cache/babysit-sandbox" in done.stdout
    assert "codex login --device-auth" in done.stdout
    assert "claude-account login <name>" in done.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["--user-step", "--dry-run"],
        ["--user-step", "--dry-run", "--sha", "main; rm -rf ~"],
        ["--user-step", "--dry-run", "--sha", "0123abc"],
        ["--frobnicate"],
        ["--dry-run", "--sha"],
    ],
)
def test_misuse_exits_2(run, args):
    assert run(*args).returncode == 2


def test_unknown_architecture_is_refused(run):
    assert run("--dry-run", ARCH="riscv64").returncode == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="as root --check inspects the host instead")
@pytest.mark.parametrize("works", [True, False])
def test_user_check_verifies_signing_without_printing_secrets(tmp_path, works):
    fakes = tmp_path / "bin"
    fakes.mkdir()
    status = 0 if works else 1
    for tool in ("bwrap", "docker", "gpg", "systemctl", "babysit-bwrap"):
        (fakes / tool).write_text(f"#!/bin/sh\necho rootless\nexit {status}\n")
    # git answers the signing key and the identity; every other git call follows `works`.
    (fakes / "git").write_text(
        '#!/bin/sh\ncase "$*" in *user.signingkey*) echo KEYID-SECRET!; exit 0 ;; esac\n'
        f"exit {status}\n"
    )
    for fake in fakes.iterdir():
        fake.chmod(0o755)
    home = tmp_path / "home"
    (home / ".gnupg").mkdir(parents=True)
    (home / ".gnupg/signing-subkey.pass").write_text("PASSPHRASE-SECRET\n")
    done = subprocess.run(
        ["bash", str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        env={"PATH": f"{fakes}:/usr/bin:/bin", "HOME": str(home)},
        timeout=30,
    )
    output = done.stdout + done.stderr
    assert "SECRET" not in output
    if works:
        assert done.returncode == 0, output
        assert "ok: babysit-gpg-preset.service is enabled" in output
        assert "ok: gpg signs with user.signingkey" in output
        assert "ok: a commit signed inside babysit-bwrap --enable=gpg verifies" in output
    else:
        assert done.returncode == 1
        assert "babysit-gpg-preset.service is not enabled" in output
        assert "gpg cannot sign" in output
        assert "cannot commit signed inside" in output
    (home / ".gnupg/signing-subkey.pass").unlink()
    done = subprocess.run(
        ["bash", str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        env={"PATH": f"{fakes}:/usr/bin:/bin", "HOME": str(home)},
        timeout=30,
    )
    assert "commit signing not set up" in done.stdout
