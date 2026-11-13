"""The one place that names the lab's GCP project.

The project comes only from `LEDGER_LAB_GCP_PROJECT`. A session may also carry `GCP_PROJECT` and an
injected credential that belong to another lab; this module never reads them, and refuses loudly
when they are the only project in sight so no caller falls back to them.
"""

import os
from collections.abc import Mapping

VARIABLE = "LEDGER_LAB_GCP_PROJECT"
FOREIGN = "GCP_PROJECT"


class NoLabProjectError(RuntimeError):
    """The lab has no GCP project of its own in this environment."""


def lab_project_or_none(env: Mapping[str, str] | None = None) -> str | None:
    """The lab's project id, or None while the owner has not set one."""
    value = (os.environ if env is None else env).get(VARIABLE, "").strip()
    return value or None


def lab_project(env: Mapping[str, str] | None = None) -> str:
    """The lab's project id; raises when it is unset, naming a foreign project if one is present."""
    env = os.environ if env is None else env
    project = lab_project_or_none(env)
    if project is not None:
        return project
    if env.get(FOREIGN, "").strip():
        raise NoLabProjectError(
            f"{VARIABLE} is unset; {FOREIGN} is set but belongs to another lab and is never used"
        )
    raise NoLabProjectError(f"{VARIABLE} is unset: the lab has no GCP project yet")
