#!/usr/bin/env python3
"""A gpg-agent socket for one sandbox that passes on only what signing needs.

    gpg_agent_proxy.py LISTEN AGENT

``babysit-bwrap --enable=gpg`` starts this outside the sandbox and binds LISTEN where
gpg inside looks for its agent. gpg signs with ``RESET``, ``OPTION``, ``GETINFO
version``, ``HAVEKEY``, ``SIGKEY``, ``SETKEYDESC``, ``SETHASH`` and ``PKSIGN`` (logged
from a gpg 2.5 session, the same flow as gpg 2.2 and 2.4); those, ``KEYINFO`` and
``NOP``/``BYE`` reach AGENT. Everything else is refused, among it ``EXPORT_KEY``,
``DELETE_KEY``, ``GET_PASSPHRASE``, ``PRESET_PASSPHRASE``, ``CLEAR_PASSPHRASE``,
``KILLAGENT`` and ``UPDATESTARTUPTTY``. Options other than two harmless ones, and the
key description gpg gives a pinentry, are acknowledged and dropped: the sandbox never
points a pinentry at a terminal or display, never words its prompt, and never turns
on loopback pinentry, which would let it supply passphrases. So it can have the
unlocked key sign, but not take the key, its passphrase or the agent.

Prints ``ready`` once LISTEN accepts connections, then runs until its parent exits
(babysit-bwrap execs bwrap, so that is the sandbox), and removes LISTEN and its
directory.
"""

import contextlib
import os
import signal
import socket
import sys
import threading

LINE_MAX = 1000  # Assuan's line limit, newline included
FORBIDDEN = b"ERR 67109115 Forbidden <GPG Agent>\n"  # GPG_ERR_FORBIDDEN, as the agent says it
COMMANDS = {b"RESET", b"NOP", b"BYE", b"HAVEKEY", b"KEYINFO", b"SIGKEY", b"SETKEY"}
COMMANDS |= {b"SETHASH", b"PKSIGN"}
GETINFO = {b"version", b"cmd_has_option"}
OPTIONS = {b"agent-awareness", b"allow-pinentry-notify"}
# Answered OK without reaching the agent.
DROPPED = {b"SETKEYDESC", b"OPTION"}


class Closed(Exception):
    """A side hung up or broke the protocol; the connection ends."""


def read_line(stream):
    line = stream.readline(LINE_MAX)
    if not line.endswith(b"\n"):
        raise Closed  # end of stream, or a line longer than Assuan allows
    return line


def verdict(line):
    """``forward``, ``drop`` (answer OK) or ``refuse`` for one client command line."""
    words = line.rstrip(b"\n").replace(b"\t", b" ").split(b" ", 1)
    command, rest = words[0].upper(), (words[1] if len(words) > 1 else b"")
    if command == b"OPTION":
        name = rest.strip().removeprefix(b"--")
        for separator in b"= ":
            name = name.split(bytes([separator]), 1)[0]
        return "forward" if name in OPTIONS else "drop"
    if command == b"GETINFO":
        return "forward" if rest.split()[:1] and rest.split()[0] in GETINFO else "refuse"
    if command in DROPPED:
        return "drop"
    return "forward" if command in COMMANDS else "refuse"


def relay(agent_in, client, client_in, agent):
    """Passes the agent's answer to one command on, with any inquiry it makes."""
    while True:
        line = read_line(agent_in)
        client.sendall(line)
        if line == b"OK\n" or line.startswith((b"OK ", b"ERR ")) or line == b"ERR\n":
            return
        if line.startswith((b"INQUIRE ", b"INQUIRE\n")):
            # Only data and its end go back; the agent asked for them.
            while True:
                answer = read_line(client_in)
                if not answer.startswith(b"D ") and answer not in (b"END\n", b"CAN\n"):
                    raise Closed
                agent.sendall(answer)
                if not answer.startswith(b"D "):
                    break


def serve(client, upstream):
    with client, socket.socket(socket.AF_UNIX) as agent:
        try:
            agent.connect(upstream)
            agent_in, client_in = agent.makefile("rb"), client.makefile("rb")
            relay(agent_in, client, client_in, agent)  # the greeting
            while True:
                line = read_line(client_in)
                decision = verdict(line)
                if decision == "forward":
                    agent.sendall(line)
                    relay(agent_in, client, client_in, agent)
                    if line[:3].upper() == b"BYE":
                        return
                else:
                    client.sendall(b"OK\n" if decision == "drop" else FORBIDDEN)
        except (Closed, OSError):
            return


def main(argv):
    listen, upstream = argv
    # Out of the terminal's session: its signals and the terminal itself are not ours.
    with contextlib.suppress(OSError):
        os.setsid()
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    parent = os.getppid()
    server = socket.socket(socket.AF_UNIX)
    try:
        server.bind(listen)
        server.listen()
        server.settimeout(1)
        print("ready", flush=True)
        os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
        while os.getppid() == parent:
            try:
                client, _ = server.accept()
            except TimeoutError:
                continue
            client.settimeout(None)
            threading.Thread(target=serve, args=(client, upstream), daemon=True).start()
    finally:
        server.close()
        with contextlib.suppress(OSError):
            os.unlink(listen)
            os.rmdir(os.path.dirname(listen))


if __name__ == "__main__":
    main(sys.argv[1:])
