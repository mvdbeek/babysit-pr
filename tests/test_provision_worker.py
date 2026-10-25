"""Worker provisioning script: parsing and dry runs only; nothing touches a machine.

Every tool the script could change a system or reach the network with is a fake that
records its call, and the dry runs must leave that record empty.
"""

import json
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
def root(tmp_path):
    """The machine the dry runs inspect: an empty /etc/apt and a dpkg that knows packages."""
    root = tmp_path / "root"
    (root / "etc/apt/sources.list.d").mkdir(parents=True)
    (root / "etc/apt/keyrings").mkdir()
    (root / "installed").write_text("")
    return root


@pytest.fixture
def run(tmp_path, root):
    fakes = tmp_path / "bin"
    fakes.mkdir()
    calls = tmp_path / "calls"
    for tool in TOOLS:
        fake = fakes / tool
        fake.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{calls}"\nexit 97\n')
        fake.chmod(0o755)
    # Read-only, so it answers instead of recording: lines of "package version".
    (fakes / "dpkg-query").write_text(
        "#!/bin/sh\nfor package; do :; done\n"
        f'version=$(awk -v p="$package" \'$1 == p {{ print $2 }}\' "{root / "installed"}")\n'
        '[ -n "$version" ] || exit 1\n'
        'case "$*" in *Status*) echo "install ok installed" ;; *) echo "$version" ;; esac\n'
    )
    (fakes / "dpkg-query").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()

    def run(*args, **env):
        done = subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{fakes}:/usr/bin:/bin",
                "HOME": str(home),
                "PROVISION_ROOT": str(root),
                **env,
            },
            timeout=30,
        )
        assert not calls.exists(), calls.read_text()
        assert not any(home.iterdir())
        return done

    return run


# git in the deployed copy never runs hooks, an fsmonitor or ext:: transports.
SAFE_GIT = "-c core.fsmonitor=false -c core.hooksPath=/dev/null -c protocol.ext.allow=never"


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
    # A deployed copy apart from ~/src, where agents work: babysit-bwrap protects it.
    app = "/.local/share/babysit-pr/app"
    assert re.search(rf"git {SAFE_GIT} clone /fixture/repo \S*{app}\n", done.stdout)
    assert re.search(rf"git {SAFE_GIT} -C \S*{app} fetch --quiet origin", done.stdout)
    sha = "0123abc" * 5 + "01234"
    assert re.search(rf"git {SAFE_GIT} -C \S*{app} checkout --quiet --detach {sha}", done.stdout)
    assert "src/babysit-pr" not in done.stdout
    assert ".cache/babysit-sandbox" in done.stdout
    # The mark that keeps the worker from registering watches of its own.
    assert re.search(r"would write \S*/\.config/babysit-pr/worker\.json \(mode 0600\)", done.stdout)
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
    home = tmp_path / "home"
    app = home / ".local/share/babysit-pr/app"
    (app / ".git").mkdir(parents=True)
    (app / "scripts").mkdir()
    for tool in ("bwrap", "docker", "gpg", "systemctl"):
        (fakes / tool).write_text(f"#!/bin/sh\necho rootless\nexit {status}\n")
    (app / "scripts" / "babysit-bwrap").write_text(f"#!/bin/sh\nexit {status}\n")
    (app / "scripts" / "babysit-bwrap").chmod(0o755)
    # The account's groups, not those of whoever runs the tests.
    (fakes / "id").write_text('#!/bin/sh\ncase "$1" in -u) echo 1000 ;; *) echo babysit ;; esac\n')
    # git answers the signing key and the identity; every other git call follows `works`.
    (fakes / "git").write_text(
        '#!/bin/sh\ncase "$*" in *user.signingkey*) echo KEYID-SECRET!; exit 0 ;; esac\n'
        f"exit {status}\n"
    )
    for fake in fakes.iterdir():
        fake.chmod(0o755)
    (home / ".gnupg").mkdir(parents=True)
    (home / ".gnupg/signing-subkey.pass").write_text("PASSPHRASE-SECRET\n")
    # gpg-agent forgets even a preset passphrase after max-cache-ttl, two hours by default.
    conf = "allow-preset-passphrase\n" + ("max-cache-ttl 31536000\n" if works else "")
    (home / ".gnupg/gpg-agent.conf").write_text(conf)
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
        assert "ok: gpg-agent keeps the preset passphrase" in output
        assert "ok: gpg signs with user.signingkey" in output
        assert "ok: a commit signed inside babysit-bwrap --enable=gpg verifies" in output
    else:
        assert done.returncode == 1
        assert "babysit-gpg-preset.service is not enabled" in output
        assert "max-cache-ttl" in output
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


@pytest.mark.skipif(os.geteuid() == 0, reason="the user step refuses root")
def test_user_step_moves_path_from_the_old_clone_to_the_deployed_copy(tmp_path):
    fakes = tmp_path / "bin"
    fakes.mkdir()
    calls = tmp_path / "calls"
    for tool in ("git", "npm", "bwrap", "docker", "id"):
        answer = "echo babysit" if tool == "id" else ""
        (fakes / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> "{calls}"\n{answer}\n')
        (fakes / tool).chmod(0o755)
    home = tmp_path / "home"
    (home / ".local/share/babysit-pr/app/.git").mkdir(parents=True)
    # What the first provisioning of a worker wrote.
    old = 'export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$HOME/src/babysit-pr/scripts:$PATH"'
    (home / ".bashrc").write_text(f"# babysit-pr worker PATH\n{old}\n\n# Ubuntu's own\n")
    marker = home / ".config/babysit-pr/worker.json"
    marker.parent.mkdir(parents=True)
    # What --controller recorded: a rerun keeps it.
    marker.write_text(json.dumps({"version": 1, "controller": "https://c.test", "token": "t"}))
    done = subprocess.run(
        ["bash", str(SCRIPT), "--user-step", "--sha", "0123abc" * 5 + "01234"],
        capture_output=True,
        text=True,
        env={"PATH": f"{fakes}:/usr/bin:/bin", "HOME": str(home)},
        timeout=30,
    )
    bashrc = (home / ".bashrc").read_text()
    assert "src/babysit-pr" not in bashrc, done.stdout + done.stderr
    assert "$HOME/.local/share/babysit-pr/app/scripts:$PATH" in bashrc
    assert bashrc.count("# babysit-pr worker PATH") == 1 and "# Ubuntu's own" in bashrc
    assert json.loads(marker.read_text()) == {
        "version": 1,
        "app": str(home / ".local/share/babysit-pr/app"),
        "controller": "https://c.test",
        "token": "t",
    }
    assert marker.stat().st_mode & 0o777 == 0o600
    assert all(
        SAFE_GIT in line for line in calls.read_text().splitlines() if line.startswith("git -")
    )


def install_line(out):
    """The packages of the last apt-get install the dry run planned."""
    lines = [line for line in out.splitlines() if "would run: apt-get install" in line]
    return lines[-1].split("--no-install-recommends")[1].split()


DOCKER_SOURCES = """Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
"""


def test_existing_deb822_docker_and_gh_repositories_are_reused(run, root):
    # Docker's current instructions, and an operator's GitHub CLI source.
    (root / "etc/apt/sources.list.d/docker.sources").write_text(DOCKER_SOURCES)
    (root / "etc/apt/keyrings/docker.asc").write_text("operator key")
    (root / "etc/apt/sources.list.d/github-cli.list").write_text(
        "deb [signed-by=/etc/apt/keyrings/github.gpg] https://cli.github.com/packages stable main\n"
    )
    done = run("--dry-run")
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert "reusing the https://download.docker.com/linux/ubuntu repository" in out
    assert "reusing the https://cli.github.com/packages repository" in out
    # A second entry with another Signed-By would break apt for the whole machine.
    assert "would trust https://download.docker.com" not in out
    assert "would trust https://cli.github.com" not in out
    assert out.count("would trust https://") == 1  # Claude Code's
    assert {"docker-ce", "docker-ce-rootless-extras", "gh"} <= set(install_line(out))


def test_a_repository_this_script_added_gives_way_to_the_operators(run, root):
    lists = root / "etc/apt/sources.list.d"
    (lists / "docker.sources").write_text(DOCKER_SOURCES)
    (lists / "docker.list").write_text(
        "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] "
        "https://download.docker.com/linux/ubuntu noble stable\n"
    )
    done = run("--dry-run")
    assert done.returncode == 0, done.stderr
    assert f"would run: rm -f {lists / 'docker.list'}" in done.stdout
    # Never a keyring: one this script did not write may be another source's Signed-By.
    assert not re.search(r"\brm\b[^\n]*keyrings", SCRIPT.read_text())


def test_installed_docker_is_never_upgraded_or_restarted(run, root):
    (root / "etc/apt/sources.list.d/docker.sources").write_text(DOCKER_SOURCES)
    (root / "installed").write_text(
        "docker-ce 5:27.3.1-1~ubuntu.24.04~noble\ndocker-ce-cli 5:27.3.1-1~ubuntu.24.04~noble\n"
        "containerd.io 1.7.22-1\ngit 1:2.43.0-1ubuntu7\ncurl 8.5.0-2ubuntu10\n"
    )
    done = run("--dry-run")
    assert done.returncode == 0, done.stderr
    # Only what rootless needs, at the running daemon's own version.
    docker = [p for p in install_line(done.stdout) if p.startswith(("docker", "containerd"))]
    assert docker == ["docker-ce-rootless-extras=5:27.3.1-1~ubuntu.24.04~noble"]
    base = [line for line in done.stdout.splitlines() if "apt-get install" in line][0]
    assert " git " not in base and " curl " not in base and " bubblewrap " in base


def test_installed_distro_docker_only_gets_rootlesskit(run, root):
    (root / "installed").write_text("docker.io 26.1.3-0ubuntu1\ncontainerd 1.7.12-0ubuntu4\n")
    done = run("--dry-run")
    assert done.returncode == 0, done.stderr
    assert "download.docker.com" not in done.stdout
    packages = install_line(done.stdout)
    assert "rootlesskit" in packages
    assert not {"docker.io", "containerd", "docker-ce"} & set(packages)


@pytest.mark.skipif(os.geteuid() == 0, reason="as root --check inspects the host instead")
@pytest.mark.parametrize("group", ["incus-admin", "lxd", "libvirt", "disk", "docker", "sudo"])
def test_checks_flag_root_equivalent_groups(tmp_path, group):
    fakes = tmp_path / "bin"
    fakes.mkdir()
    (fakes / "id").write_text(
        f'#!/bin/sh\ncase "$1" in -un) echo babysit ;; -nG) echo "babysit {group} kvm" ;; '
        "*) echo 1000 ;; esac\n"
    )
    (fakes / "id").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    done = subprocess.run(
        ["bash", str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        env={"PATH": f"{fakes}:/usr/bin:/bin", "HOME": str(home)},
        timeout=30,
    )
    assert done.returncode == 1
    assert f"PROBLEM: babysit is in the {group} group" in done.stderr
    # Device access rather than root, but still more than an agent account needs.
    assert "WARNING: babysit is in the kvm group" in done.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="the user step refuses root")
def test_controller_step_records_the_token_without_showing_it(tmp_path):
    home = tmp_path / "home"
    marker = home / ".config/babysit-pr/worker.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"version": 1, "app": "/app"}))
    token = "s3cr3t-" + "x" * 36
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home)}

    def controller(*args, stdin=token + "\n"):
        return subprocess.run(
            ["bash", str(SCRIPT), "--controller", *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    done = controller("https://babysitter.example.ts.net/")
    assert done.returncode == 0, done.stdout + done.stderr
    assert token not in done.stdout + done.stderr
    assert json.loads(marker.read_text()) == {
        "version": 1,
        "app": "/app",
        "controller": "https://babysitter.example.ts.net",
        "token": token,
    }
    assert marker.stat().st_mode & 0o777 == 0o600
    for url in ("ftp://x", "https://u:p@x", "https://x/?q=1", ""):
        assert controller(url).returncode == 2
    assert controller("https://x", stdin="").returncode == 2
    assert controller("https://x", stdin="short\n").returncode == 2
    assert json.loads(marker.read_text())["token"] == token


@pytest.mark.skipif(os.geteuid() == 0, reason="as root --check inspects the host instead")
def test_user_check_reports_claude_accounts_that_would_not_start_unattended(tmp_path):
    fakes = tmp_path / "bin"
    fakes.mkdir()
    for tool in ("bwrap", "docker", "gpg", "systemctl", "git"):
        (fakes / tool).write_text("#!/bin/sh\necho rootless\nexit 0\n")
    (fakes / "id").write_text('#!/bin/sh\ncase "$1" in -u) echo 1000 ;; *) echo babysit ;; esac\n')
    for fake in fakes.iterdir():
        fake.chmod(0o755)
    home = tmp_path / "home"
    accounts = home / ".claude/accounts"
    login = {"oauthAccount": {"accountUuid": "SECRET-UUID", "emailAddress": "a@example.com"}}
    for name, config, settings in [
        (
            "ready",
            {**login, "hasCompletedOnboarding": True},
            {"skipDangerousModePermissionPrompt": True},
        ),
        ("fresh", login, {}),
        ("signedout", {}, {}),
    ]:
        (accounts / name).mkdir(parents=True)
        (accounts / name / ".claude.json").write_text(json.dumps(config))
        (accounts / name / "settings.json").write_text(json.dumps(settings))
    done = subprocess.run(
        ["bash", str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        env={"PATH": f"{fakes}:/usr/bin:/bin", "HOME": str(home)},
        timeout=30,
    )
    output = done.stdout + done.stderr
    assert "SECRET" not in output
    assert "ok: Claude account ready is signed in and starts unattended" in output
    assert re.search(
        r"Claude account fresh would stop at Claude's first-run screens \(onboarding permission-prompt\)",
        output,
    )
    assert "Claude account signedout is not signed in; run claude-account login signedout" in output
    assert done.returncode == 1
