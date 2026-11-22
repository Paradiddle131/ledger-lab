"""The one place that names the lab's GCP project.

The project comes only from `LEDGER_LAB_GCP_PROJECT`. A session may also carry `GCP_PROJECT` and an
injected credential that belong to another lab. This module never uses that variable's value: it
reads only whether it is non-empty, and while it is there is no lab project, whatever
`LEDGER_LAB_GCP_PROJECT` says, since the credential the session's proxy injects may be the other
lab's.
`lab_project_or_none` then answers None and `lab_project` refuses with the reason, so no caller of
this module makes a GCP call in such a session; the dbt profile applies the same rule on its own.
"""

import os
from collections.abc import Mapping

VARIABLE = "LEDGER_LAB_GCP_PROJECT"
FOREIGN = "GCP_PROJECT"


class NoLabProjectError(RuntimeError):
    """The lab has no GCP project of its own in this environment."""


def foreign_session(env: Mapping[str, str] | None = None) -> bool:
    """Whether the session carries another lab's `GCP_PROJECT`, and so may carry its
    credential: any non-empty value, spaces included, as the autopilot profile's shell check reads
    it."""
    return bool((os.environ if env is None else env).get(FOREIGN, ""))


def lab_project_or_none(env: Mapping[str, str] | None = None) -> str | None:
    """The lab's project id, or None while the owner has not set one or the session is another
    lab's."""
    env = os.environ if env is None else env
    if foreign_session(env):
        return None
    return env.get(VARIABLE, "").strip() or None


def lab_project(env: Mapping[str, str] | None = None) -> str:
    """The lab's project id; raises when it is unset or the session is another lab's, saying
    which."""
    env = os.environ if env is None else env
    if foreign_session(env):
        raise NoLabProjectError(
            f"{FOREIGN} is set: this session's GCP credential may belong to another lab, so no GCP "
            f"call is made, {VARIABLE} or not"
        )
    project = lab_project_or_none(env)
    if project is None:
        raise NoLabProjectError(f"{VARIABLE} is unset: the lab has no GCP project yet")
    return project


def main(env: Mapping[str, str] | None = None) -> int:
    """Print `own` in the lab's own environment and `elsewhere` otherwise, never the project id;
    the Makefile's `test` target reads the word to decide whether to read the cost around pytest."""
    print("own" if lab_project_or_none(env) is not None else "elsewhere")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
