"""The one place that names the lab's GCP project.

The project comes only from `LEDGER_LAB_GCP_PROJECT`. A session may also carry `GCP_PROJECT` and an
injected credential that belong to another lab. This module never uses that variable's value: it
reads only whether it is set, and while it is set there is no lab project, whatever
`LEDGER_LAB_GCP_PROJECT` says, since the credential the session's proxy injects is the other lab's.
`lab_project_or_none` then answers None and `lab_project` refuses with the reason, so no caller
makes a GCP call in such a session.
"""

import os
from collections.abc import Mapping

VARIABLE = "LEDGER_LAB_GCP_PROJECT"
FOREIGN = "GCP_PROJECT"


class NoLabProjectError(RuntimeError):
    """The lab has no GCP project of its own in this environment."""


def foreign_session(env: Mapping[str, str] | None = None) -> bool:
    """Whether the session carries another lab's `GCP_PROJECT`, and so its credential."""
    return bool((os.environ if env is None else env).get(FOREIGN, "").strip())


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
            f"{FOREIGN} is set: this session's GCP credential belongs to another lab, so no GCP "
            f"call is made, {VARIABLE} or not"
        )
    project = lab_project_or_none(env)
    if project is None:
        raise NoLabProjectError(f"{VARIABLE} is unset: the lab has no GCP project yet")
    return project
