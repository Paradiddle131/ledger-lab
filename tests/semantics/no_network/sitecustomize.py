"""Refuse every network call a Python process makes, from its start.

Python imports `sitecustomize` at start-up from its import path, so a process started with this
folder on `PYTHONPATH` (as every MetricFlow run in the tests is) installs the hook before any
code of its own runs: a connection to a host or a name lookup ends the process at once with
`REFUSED`, so no library can catch the refusal and carry on. A socket to a local file (a Unix
socket) is allowed.
"""

import os
import socket
import sys

REFUSED = 97
NETWORK_EVENTS = frozenset(
    {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.sendto"}
)


def refuse_the_network(event: str, args: tuple[object, ...]) -> None:
    if event not in NETWORK_EVENTS:
        return
    if event == "socket.connect" and getattr(args[0], "family", None) == socket.AF_UNIX:
        return
    where = tuple(a for a in args if not isinstance(a, socket.socket))
    os.write(2, f"no_network: refused a network call: {event} {where!r}\n".encode())
    os._exit(REFUSED)


sys.addaudithook(refuse_the_network)
