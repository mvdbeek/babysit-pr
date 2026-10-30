"""A worker's deployed babysit-pr moved to this machine's commit, on request.

Launches on a worker refuse one that runs another babysit-pr commit. The dashboard
offers this update where it reports that: the user step of ``provision_worker.sh`` at
this machine's commit, over the worker's ssh, as references/worker-setup.md runs it by
hand. It is offered only for the deployed copy that script manages, on Linux; the
worker's commit is read back afterwards, so a checkout the script left alone (local
changes) is reported rather than taken as updated.
"""

import subprocess
import threading
import time
from pathlib import Path

import machines
import remote_probe

DEPLOY_TIMEOUT = 600
# The worker stops the script itself this long before the ssh client gives up, so a
# timed-out update is not still running there when the next one starts.
REMOTE_GRACE = 30
VERSION_TIMEOUT = 30
ERROR_CHARS = 2000
# Where provision_worker.sh --user-step deploys, relative to the worker's home.
DEPLOYED = ".local/share/babysit-pr/app"


def updatable(m):
    """Whether provision_worker.sh manages the babysit-pr this worker runs."""
    root = m.babysit_root.rstrip("/")
    return m.os == "linux" and root.startswith(("/", "~")) and root.endswith(f"/{DEPLOYED}")


class WorkerUpdates:
    """One update per worker at a time, each on its own thread; the last result is kept."""

    def __init__(self, home, git, now=time.time):
        self.home = Path(home)
        # Git in this machine's babysit-pr: its output, ValueError on failure.
        self.git = git
        self.now = now
        self.lock = threading.Lock()
        self.updates: dict[str, dict] = {}

    def snapshot(self):
        with self.lock:
            return {key: dict(value) for key, value in self.updates.items()}

    def update(self, machine_id):
        """Deploy this machine's commit on the worker; ValueError if it cannot be offered."""
        if not isinstance(machine_id, str):
            raise ValueError("Name the worker to update")
        m = machines.resolve(self.home, machine_id)
        if m is None:
            raise ValueError("This machine runs its own babysit-pr")
        if not updatable(m):
            raise ValueError(
                f"{m.label} does not run the copy provision_worker.sh deploys; update its "
                f"babysit-pr at {m.babysit_root} by hand"
            )
        sha = self.git("rev-parse", "HEAD")
        # The script of the commit deployed, not whatever the working tree holds.
        script = self.git("show", f"{sha}:scripts/provision_worker.sh")
        with self.lock:
            if self.updates.get(m.id, {}).get("running"):
                raise ValueError(f"{m.label} is already being updated")
            self.updates[m.id] = {
                "running": True,
                "sha": sha,
                "ok": None,
                "error": None,
                "finished_at": None,
            }
            threading.Thread(target=self.deploy, args=(m, sha, script), daemon=True).start()
            return dict(self.updates[m.id])

    def deploy(self, m, sha, script):
        try:
            done = machines.run(
                self.home,
                m,
                ["timeout", str(DEPLOY_TIMEOUT - REMOTE_GRACE), "bash", "-s", "--"]
                + ["--user-step", "--sha", sha],
                input=script.encode(),
                timeout=DEPLOY_TIMEOUT,
            )
            output = (done.stdout + done.stderr).decode(errors="replace").strip()
            deployed = remote_probe.call(
                self.home, m, "version", VERSION_TIMEOUT, babysit_root=m.babysit_root
            )["head"]
            # The script also reports problems unrelated to the deploy (a signed-out
            # account): only the commit read back decides.
            ok = deployed == sha
            error = None if ok else (output[-ERROR_CHARS:] or f"{m.label} still runs {deployed}")
        except subprocess.TimeoutExpired:
            ok, error = False, f"{m.label} did not finish within {DEPLOY_TIMEOUT} seconds"
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            ok, error = False, f"Cannot update {m.label}: {exc}"
        with self.lock:
            self.updates[m.id] = {
                "running": False,
                "sha": sha,
                "ok": ok,
                "error": error,
                "finished_at": self.now(),
            }
