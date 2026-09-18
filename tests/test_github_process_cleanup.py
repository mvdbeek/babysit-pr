"""GitHub polling must reap credential helpers even when gh itself exits."""

import contextlib
import os
import signal
import sys
import time

import gh_pr_watch
import pr_ci
import pr_ci_logs
import pr_overview
import pytest
import upstream_tests


@pytest.mark.parametrize("caller", ["overview", "watch", "ci", "job", "log", "upstream"])
def test_github_poll_reaps_abandoned_credential_helper(tmp_path, monkeypatch, caller):
    pidfile = tmp_path / "credential-helper.pid"
    fake = tmp_path / "gh"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "Path(os.environ['FAKE_CREDENTIAL_PID']).write_text(str(child.pid))\n"
        "print('keychain timeout', file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CREDENTIAL_PID", str(pidfile))
    pid = None
    try:
        if caller == "overview":
            with pytest.raises(ValueError, match="keychain timeout"):
                pr_overview.github_page("is:pr")
        elif caller == "watch":
            with pytest.raises(gh_pr_watch.GhCommandError, match="keychain timeout"):
                gh_pr_watch.gh_text(["api", "user"])
        else:
            with pytest.raises(ValueError):
                if caller == "ci":
                    pr_ci.graphql("query", {})
                elif caller == "job":
                    pr_ci_logs.read_job({"repo": "test/repo", "job_id": 1})
                elif caller == "log":
                    pr_ci_logs.download_log({"repo": "test/repo", "job_id": 1})
                else:
                    upstream_tests.gh_bytes("repos/test/repo")
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        pytest.fail("GitHub poll left its credential helper running after gh exited")
    finally:
        if pid is None and pidfile.exists():
            pid = int(pidfile.read_text())
        if pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
