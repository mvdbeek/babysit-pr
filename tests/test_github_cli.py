"""GitHub CLI calls read the keyring once, never prompt, and stay bounded in number."""

import json
import os
import subprocess
import sys
import threading
import time

import github_cli
import owned_process
import pytest

TOKEN = "gho_0123456789abcdefghijklmnopqrstuvwxyz"


@pytest.fixture
def fresh(monkeypatch):
    """A process-fresh credential cache, without the fixture token in the environment."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    credentials = github_cli.Credentials()
    monkeypatch.setattr(github_cli, "CREDENTIALS", credentials)
    return credentials


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    """A gh stand-in recording argv and the environment of every call."""
    calls = tmp_path / "calls.jsonl"
    fake = tmp_path / "gh"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "keys = ['GH_TOKEN', 'GH_PROMPT_DISABLED', 'GH_NO_UPDATE_NOTIFIER', "
        "'GIT_TERMINAL_PROMPT', 'GCM_INTERACTIVE']\n"
        "with Path(os.environ['FAKE_GH_CALLS']).open('a') as f:\n"
        "    f.write(json.dumps({'args': args, 'env': {k: os.environ.get(k) for k in keys}}) + '\\n')\n"
        "if args[:2] == ['auth', 'token']:\n"
        "    print(os.environ.get('FAKE_GH_TOKEN', ''))\n"
        "    sys.exit(int(os.environ.get('FAKE_GH_TOKEN_STATUS', '0')))\n"
        "if os.environ.get('FAKE_GH_401'):\n"
        "    print('HTTP 401: Requires authentication', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "print(json.dumps({'login': 'user'}))\n"
    )
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_CALLS", str(calls))
    monkeypatch.setenv("FAKE_GH_TOKEN", TOKEN)

    def recorded():
        if not calls.exists():
            return []
        return [json.loads(line) for line in calls.read_text().splitlines()]

    return recorded


def test_token_is_read_once_and_handed_to_every_gh_process(fresh, fake_gh):
    for _ in range(3):
        result = github_cli.run(["gh", "api", "user"], text=True, timeout=10)
        assert result.returncode == 0
    calls = fake_gh()
    assert [call["args"] for call in calls] == [
        ["auth", "token", "--hostname", "github.com"],
        ["api", "user"],
        ["api", "user"],
        ["api", "user"],
    ]
    assert calls[0]["env"]["GH_TOKEN"] is None
    for call in calls[1:]:
        assert call["env"] == {
            "GH_TOKEN": TOKEN,
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
        }


def test_environment_token_skips_the_keyring_lookup(fresh, fake_gh, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    env = github_cli.environment({"HOME": "/nowhere"})
    assert env["GH_TOKEN"] == TOKEN
    assert env["HOME"] == "/nowhere"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert fake_gh() == []


@pytest.mark.parametrize("output", ["", '{"login": "not a token"}', "short"])
def test_unusable_lookup_output_is_not_exported_and_retried_after_backoff(
    fresh, fake_gh, monkeypatch, output
):
    monkeypatch.setenv("FAKE_GH_TOKEN", output)
    clock = [1000.0]
    monkeypatch.setattr(github_cli.time, "monotonic", lambda: clock[0])
    first = github_cli.run(["gh", "api", "user"], text=True, timeout=10)
    assert first.returncode == 1 and "not a token" in first.stderr
    github_cli.run(["gh", "api", "user"], text=True, timeout=10)
    assert [c["args"] for c in fake_gh()] == [["auth", "token", "--hostname", "github.com"]]
    clock[0] += github_cli.RETRY_AFTER + 1
    github_cli.run(["gh", "api", "user"], text=True, timeout=10)
    lookups = [c for c in fake_gh() if c["args"][:2] == ["auth", "token"]]
    assert len(lookups) == 2


def test_without_a_token_no_gh_process_starts_and_the_reason_is_reported(fresh, monkeypatch):
    started = []

    def hang(args, **kwargs):
        started.append(args)
        if args[:3] == ["gh", "auth", "token"]:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return subprocess.CompletedProcess(args, 0, "{}", "")

    monkeypatch.setattr(owned_process, "run", hang)
    result = github_cli.run(["gh", "api", "user"], text=True, timeout=10)
    assert result.returncode == 1
    assert "keyring may be stuck" in result.stderr
    assert "GH_TOKEN" in result.stderr and "--insecure-storage" in result.stderr
    raw = github_cli.run(["gh", "api", "user"], timeout=10)
    assert isinstance(raw.stderr, bytes) and b"keyring may be stuck" in raw.stderr
    with pytest.raises(github_cli.CredentialsUnavailable, match="insecure-storage"):
        with github_cli.command(["gh", "api", "user"]):
            pass
    assert started == [["gh", "auth", "token", "--hostname", "github.com"]]
    assert "GH_TOKEN" not in github_cli.environment()

    def missing(args, **kwargs):
        raise FileNotFoundError("gh")

    fresh._retry_at = 0.0
    monkeypatch.setattr(owned_process, "run", missing)
    assert fresh.token() is None
    assert fresh.reason == "gh not found on PATH"


def test_launchd_style_keyring_failure_names_gh_reason(fresh, fake_gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_TOKEN", "")
    monkeypatch.setenv("FAKE_GH_TOKEN_STATUS", "1")
    result = github_cli.run(["gh", "api", "user"], text=True, timeout=10)
    assert result.returncode == 1
    assert "gh auth token: exit status 1" in result.stderr
    assert [c["args"] for c in fake_gh()] == [["auth", "token", "--hostname", "github.com"]]


def test_rejected_token_is_forgotten_and_read_again(fresh, fake_gh, monkeypatch):
    github_cli.run(["gh", "api", "user"], text=True, timeout=10)
    monkeypatch.setenv("FAKE_GH_401", "1")
    result = github_cli.run(["gh", "api", "user"], text=True, timeout=10)
    assert result.returncode == 1
    monkeypatch.delenv("FAKE_GH_401")
    monkeypatch.setenv("FAKE_GH_TOKEN", TOKEN.replace("0123", "9876"))
    github_cli.run(["gh", "api", "user"], text=True, timeout=10)
    calls = fake_gh()
    lookups = [c for c in calls if c["args"][:2] == ["auth", "token"]]
    assert len(lookups) == 2
    assert calls[-1]["env"]["GH_TOKEN"] == TOKEN.replace("0123", "9876")


def test_token_is_refreshed_after_its_ttl(fresh, fake_gh, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(github_cli.time, "monotonic", lambda: clock[0])
    assert fresh.token() == TOKEN
    clock[0] += github_cli.TOKEN_TTL - 1
    assert fresh.token() == TOKEN
    clock[0] += 2
    assert fresh.token() == TOKEN
    assert len(fake_gh()) == 2


def test_unauthenticated_detects_text_and_bytes_only_on_failure():
    ok = subprocess.CompletedProcess([], 0, "", "HTTP 401")
    assert not github_cli.unauthenticated(ok)
    text = subprocess.CompletedProcess([], 1, "", "gh: Requires authentication (HTTP 401)")
    assert github_cli.unauthenticated(text)
    raw = subprocess.CompletedProcess([], 1, b'{"message": "Requires authentication"}', b"")
    assert github_cli.unauthenticated(raw)
    other = subprocess.CompletedProcess([], 1, "", "rate limit exceeded")
    assert not github_cli.unauthenticated(other)


def test_concurrent_gh_processes_are_capped(monkeypatch):
    active = []
    peak = []
    lock = threading.Lock()

    def slow(args, **kwargs):
        with lock:
            active.append(1)
            peak.append(len(active))
        time.sleep(0.05)
        with lock:
            active.pop()
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(owned_process, "run", slow)
    threads = [
        threading.Thread(
            target=github_cli.run, args=(["gh", "api", "user"],), kwargs={"timeout": 5}
        )
        for _ in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(peak) == 12
    assert max(peak) <= github_cli.MAX_CONCURRENT
    assert github_cli.SLOTS._value == github_cli.MAX_CONCURRENT


def test_streaming_command_gets_the_shared_environment_and_releases_its_slot(
    fresh, fake_gh, tmp_path
):
    with github_cli.command(["gh", "api", "user"], stdout=subprocess.PIPE, text=True) as proc:
        assert proc.stdout is not None
        assert json.loads(proc.stdout.read()) == {"login": "user"}
    assert github_cli.SLOTS._value == github_cli.MAX_CONCURRENT
    calls = fake_gh()
    assert calls[-1]["args"] == ["api", "user"]
    assert calls[-1]["env"]["GH_TOKEN"] == TOKEN
    assert calls[-1]["env"]["GH_PROMPT_DISABLED"] == "1"


def test_error_reporting_never_includes_the_child_environment(fresh, fake_gh, monkeypatch):
    monkeypatch.setenv("FAKE_GH_401", "1")
    result = github_cli.run(["gh", "api", "user"], text=True, timeout=10)
    assert TOKEN not in repr(result)
    assert TOKEN not in result.stderr
