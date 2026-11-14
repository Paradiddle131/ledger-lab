"""The lab's project comes only from its own variable, never from the session's foreign one."""

import pytest
from gcp_project import NoLabProjectError, lab_project, lab_project_or_none


def test_the_project_comes_from_the_lab_variable() -> None:
    assert lab_project({"LEDGER_LAB_GCP_PROJECT": "the-lab"}) == "the-lab"


def test_the_lab_variable_wins_over_the_foreign_one() -> None:
    env = {"LEDGER_LAB_GCP_PROJECT": "the-lab", "GCP_PROJECT": "another-lab"}
    assert lab_project(env) == "the-lab"


def test_only_the_foreign_project_set_raises_and_says_why() -> None:
    with pytest.raises(NoLabProjectError, match="belongs to another lab"):
        lab_project({"GCP_PROJECT": "another-lab"})


@pytest.mark.parametrize(
    "env", [{}, {"LEDGER_LAB_GCP_PROJECT": ""}, {"LEDGER_LAB_GCP_PROJECT": " "}]
)
def test_no_lab_project_raises(env: dict[str, str]) -> None:
    with pytest.raises(NoLabProjectError, match="unset"):
        lab_project(env)
    assert lab_project_or_none(env) is None


def test_the_foreign_project_is_never_returned() -> None:
    assert lab_project_or_none({"GCP_PROJECT": "another-lab"}) is None
