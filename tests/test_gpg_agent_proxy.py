"""The gpg-agent proxy babysit-bwrap puts between a sandbox and the user's agent.

A fake agent records what reaches it; only the gated test at the end uses real gpg,
with its own throwaway homes and agent, never the user's.
"""

import contextlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

PROXY = Path(__file__).resolve().parent.parent / "scripts" / "gpg_agent_proxy.py"
GREETING = b"OK Pleased to meet you\n"
SIGNATURE = b"D (7:sig-val(5:eddsa))\n"


class FakeAgent:
    """Answers every command OK, signs after a pinentry notice, and records the lines."""

    def __init__(self, path):
        self.lines = []
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(path))
        self.server.listen()
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        with contextlib.suppress(OSError):
            while True:
                connection, _ = self.server.accept()
                threading.Thread(target=self.serve, args=(connection,), daemon=True).start()

    def serve(self, connection):
        # The proxy hangs up on clients that break the protocol.
        with connection, connection.makefile("rb") as lines, contextlib.suppress(OSError):
            connection.sendall(GREETING)
            for line in lines:
                self.lines.append(line)
                if line.startswith(b"GETINFO version"):
                    connection.sendall(b"D 2.4.4\nOK\n")
                elif line == b"PKSIGN\n":
                    connection.sendall(b"S PROGRESS x\nINQUIRE PINENTRY_LAUNCHED 42\n")
                    self.lines.append(next(lines, b""))  # the client's END
                    connection.sendall(SIGNATURE + b"OK\n")
                else:
                    connection.sendall(b"OK\n")


@pytest.fixture
def proxy():
    # Unix socket paths are short; tmp_path can exceed the limit on macOS.
    with tempfile.TemporaryDirectory(dir="/tmp") as runtime:
        listen = Path(runtime) / "proxy" / "S.gpg-agent"
        listen.parent.mkdir(mode=0o700)
        agent = FakeAgent(Path(runtime) / "agent")
        process = subprocess.Popen(
            [sys.executable, "-I", str(PROXY), str(listen), str(Path(runtime) / "agent")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
        )
        try:
            assert process.stdout.readline() == b"ready\n"
            yield listen, agent
        finally:
            process.terminate()
            process.wait(timeout=10)
            agent.server.close()


def session(listen):
    client = socket.socket(socket.AF_UNIX)
    client.connect(str(listen))
    lines = client.makefile("rb")
    assert lines.readline() == GREETING

    def ask(line, count=1):
        client.sendall(line)
        return [lines.readline() for _ in range(count)]

    return client, lines, ask


def test_proxy_forwards_what_gpg_sends_to_sign(proxy):
    listen, agent = proxy
    client, lines, ask = session(listen)
    with client:
        # The exact session gpg 2.5 ran to sign a commit, through a logging proxy.
        assert ask(b"RESET\n") == [b"OK\n"]
        for option in (b"ttyname=/dev/pts/3", b"ttytype=xterm", b"display=:0", b"lc-ctype=C"):
            assert ask(b"OPTION " + option + b"\n") == [b"OK\n"]
        assert ask(b"GETINFO version\n", 2) == [b"D 2.4.4\n", b"OK\n"]
        assert ask(b"OPTION allow-pinentry-notify\n") == [b"OK\n"]
        assert ask(b"OPTION agent-awareness=2.1.0\n") == [b"OK\n"]
        assert ask(b"HAVEKEY --list=1000\n") == [b"OK\n"]
        assert ask(b"SIGKEY E6F800F64A2C2D2990C4AD993B28EE3ACCCE8F8D\n") == [b"OK\n"]
        assert ask(b"SETKEYDESC Please+enter+the+passphrase\n") == [b"OK\n"]
        assert ask(b"SETHASH 10 8280016253282F29\n") == [b"OK\n"]
        assert ask(b"PKSIGN\n", 2) == [b"S PROGRESS x\n", b"INQUIRE PINENTRY_LAUNCHED 42\n"]
        client.sendall(b"END\n")
        assert [lines.readline(), lines.readline()] == [SIGNATURE, b"OK\n"]
    assert agent.lines == [
        b"RESET\n",
        b"GETINFO version\n",
        b"OPTION allow-pinentry-notify\n",
        b"OPTION agent-awareness=2.1.0\n",
        b"HAVEKEY --list=1000\n",
        b"SIGKEY E6F800F64A2C2D2990C4AD993B28EE3ACCCE8F8D\n",
        b"SETHASH 10 8280016253282F29\n",
        b"PKSIGN\n",
        b"END\n",
    ]


@pytest.mark.parametrize(
    "line",
    [
        b"EXPORT_KEY --openpgp E6F800F64A2C2D2990C4AD993B28EE3ACCCE8F8D",
        b"export_key E6F800F64A2C2D2990C4AD993B28EE3ACCCE8F8D",  # commands ignore case
        b"DELETE_KEY --force E6F800F64A2C2D2990C4AD993B28EE3ACCCE8F8D",
        b"GET_PASSPHRASE --data x X X Phish",
        b"PRESET_PASSPHRASE E6F8 -1 70617373",
        b"CLEAR_PASSPHRASE --mode=normal E6F8",
        b"KILLAGENT",
        b"RELOADAGENT",
        b"UPDATESTARTUPTTY",
        b"GET_CONFIRMATION yes",
        b"PKDECRYPT",
        b"IMPORT_KEY",
        b"PASSWD E6F8",
        b"SCD SERIALNO",
        b"GETINFO socket_name",
        b"GETINFO",
        b"D 1234",
        b"\tKILLAGENT",
        b"KILLAGENT\r",
    ],
)
def test_proxy_refuses_everything_else(proxy, line):
    listen, agent = proxy
    client, _, ask = session(listen)
    with client:
        assert ask(line + b"\n") == [b"ERR 67109115 Forbidden <GPG Agent>\n"]
        assert ask(b"NOP\n") == [b"OK\n"]  # and the session goes on
    assert agent.lines == [b"NOP\n"]


@pytest.mark.parametrize(
    "option",
    [
        b"pinentry-mode=loopback",  # the client would supply passphrases
        b"--pinentry-mode loopback",
        b"ttyname=/dev/pts/0",  # an operator's terminal
        b"TTYNAME=/dev/pts/0",
        b"putenv=GPG_TTY=/dev/pts/0",
        b"xauthority=/tmp/x",
        b"lc-messages=C",
        b"agent-awareness-x=1",
    ],
)
def test_proxy_drops_options_that_steer_the_agent(proxy, option):
    listen, agent = proxy
    client, _, ask = session(listen)
    with client:
        assert ask(b"OPTION " + option + b"\n") == [b"OK\n"]
        assert ask(b"BYE\n") == [b"OK\n"]
    assert agent.lines == [b"BYE\n"]


def test_proxy_ends_a_connection_that_breaks_the_protocol(proxy):
    listen, agent = proxy
    client, lines, ask = session(listen)
    with client:
        ask(b"PKSIGN\n", 2)
        # Only data and its end answer an inquiry, never another command.
        client.sendall(b"DELETE_KEY --force E6F8\n")
        assert lines.readline() == b""
    client, lines, _ = session(listen)
    with client:
        client.sendall(b"N" * 2000 + b"\n")
        assert lines.readline() == b""
    assert b"DELETE_KEY --force E6F8\n" not in agent.lines
    assert not any(line.startswith(b"NNN") for line in agent.lines)


def test_proxy_exits_with_its_parent_and_removes_its_socket():
    with tempfile.TemporaryDirectory(dir="/tmp") as runtime:
        listen = Path(runtime) / "proxy" / "S.gpg-agent"
        listen.parent.mkdir(mode=0o700)
        script = 'exec 3< <(exec "$0" -I "$1" "$2" "$3" </dev/null); read -r line <&3; echo "$line"'
        argv = ["bash", "-c", script, sys.executable, str(PROXY), str(listen), "/nowhere"]
        assert subprocess.run(argv, capture_output=True, text=True).stdout == "ready\n"
        for _ in range(100):
            if not listen.parent.exists():
                break
            time.sleep(0.1)
        assert not listen.parent.exists()


def _gpg_tools():
    return all(shutil.which(tool) for tool in ("gpg", "gpg-agent", "gpgconf", "gpg-connect-agent"))


@pytest.mark.skipif(not _gpg_tools(), reason="needs GnuPG")
def test_real_gpg_signs_through_the_proxy_and_cannot_delete_the_key():
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="gp") as runtime:
        owner, inside = Path(runtime) / "o", Path(runtime) / "i"
        for home in (owner, inside):
            home.mkdir(mode=0o700)
        env = {"PATH": os.environ["PATH"], "HOME": runtime, "LANG": "C"}

        def gpg(home, *args, **kwargs):
            return subprocess.run(
                [*args[:1], "--homedir", str(home), *args[1:]],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                **kwargs,
            )

        uid = "Proxy Test <proxy@example.invalid>"
        made = gpg(
            owner,
            "gpg",
            "--batch",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--quick-gen-key",
            uid,
            "ed25519",
            "sign",
            "never",
        )
        try:
            assert made.returncode == 0, made.stderr
            for name in ("pubring.kbx", "trustdb.gpg"):
                shutil.copy(owner / name, inside / name)
            (inside / "gpg.conf").write_text("no-autostart\nuse-agent\n")
            upstream = gpg(owner, "gpgconf", "--list-dirs", "agent-socket").stdout.strip()
            listen = Path(gpg(inside, "gpgconf", "--list-dirs", "agent-socket").stdout.strip())
            listen.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            process = subprocess.Popen(
                [sys.executable, "-I", str(PROXY), str(listen), upstream],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
            )
            try:
                assert process.stdout.readline() == b"ready\n"
                signed = gpg(inside, "gpg", "--batch", "-bsau", uid, input="hello\n")
                assert signed.returncode == 0, signed.stderr
                (inside / "sig").write_text(signed.stdout)
                checked = gpg(inside, "gpg", "--verify", str(inside / "sig"), "-", input="hello\n")
                assert "Good signature" in checked.stderr
                keygrip = next(
                    line.split(":")[9]
                    for line in gpg(
                        owner, "gpg", "--with-colons", "--with-keygrip", "-K"
                    ).stdout.splitlines()
                    if line.startswith("grp:")
                )
                deleted = gpg(
                    inside,
                    "gpg-connect-agent",
                    "--no-autostart",
                    f"DELETE_KEY --force {keygrip}",
                    "/bye",
                )
                assert "Forbidden" in deleted.stdout + deleted.stderr
                assert (owner / "private-keys-v1.d" / f"{keygrip}.key").exists()
            finally:
                process.terminate()
                process.wait(timeout=10)
        finally:
            gpg(owner, "gpgconf", "--kill", "gpg-agent")
