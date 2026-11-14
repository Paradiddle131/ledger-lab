"""The refusal of every network call, as an audit hook a process installs on itself.

A connection to a host or a name lookup ends the process at once with `REFUSED`, so no library
can catch the refusal and carry on. A socket to a local file (a Unix socket) is allowed.
Importing this module installs nothing; `sitecustomize.py` beside it and `offline_mf.py` do.
"""

import os
import socket

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
