"""Run the MetricFlow command line (`mf`) with every network call refused.

The command asks PyPI for a newer release of itself on every run (the `update_checker` package)
and offers no way to turn that off, so this runner replaces the check with none, then refuses
any connection or name lookup a run would make: it exits at once with `REFUSED`, so no library
can catch the refusal and carry on. A socket to a local file (a Unix socket) is allowed.

    python tests/semantics/offline_mf.py <mf arguments>
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
    os.write(2, f"offline_mf: refused a network call: {event} {args[1:]!r}\n".encode())
    os._exit(REFUSED)


def no_newer_release(*_: object, **__: object) -> None:
    return None


def main() -> None:
    sys.addaudithook(refuse_the_network)
    import update_checker

    setattr(update_checker.UpdateChecker, "check", no_newer_release)  # noqa: B010
    from dbt_metricflow.cli.main import cli

    cli(args=sys.argv[1:], prog_name="mf")


if __name__ == "__main__":
    main()
