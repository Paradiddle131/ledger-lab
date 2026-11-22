"""The lab's project comes only from its own variable, never from the session's foreign one, and
not at all while the session carries the foreign one."""

import gcp_project
import pytest
from gcp_project import NoLabProjectError, foreign_session, lab_project, lab_project_or_none


def test_the_project_comes_from_the_lab_variable() -> None:
    assert lab_project({"LEDGER_LAB_GCP_PROJECT": "the-lab"}) == "the-lab"


@pytest.mark.parametrize("foreign", ["another-lab", " another-lab "])
def test_a_foreign_project_beside_the_lab_one_leaves_no_lab_project(foreign: str) -> None:
    """The session's credential is the foreign lab's, so the lab variable is not used either."""
    env = {"LEDGER_LAB_GCP_PROJECT": "the-lab", "GCP_PROJECT": foreign}
    assert foreign_session(env)
    assert lab_project_or_none(env) is None
    with pytest.raises(NoLabProjectError, match="may belong to another lab") as refused:
        lab_project(env)
    assert "the-lab" not in str(refused.value)
    assert "another-lab" not in str(refused.value)


def test_an_empty_foreign_variable_is_no_foreign_session() -> None:
    env = {"LEDGER_LAB_GCP_PROJECT": "the-lab", "GCP_PROJECT": ""}
    assert not foreign_session(env)
    assert lab_project(env) == "the-lab"


def test_a_foreign_variable_of_spaces_is_still_a_foreign_session() -> None:
    """Fails closed, as the shell check `[ -z "${GCP_PROJECT:-}" ]` does."""
    env = {"LEDGER_LAB_GCP_PROJECT": "the-lab", "GCP_PROJECT": " "}
    assert foreign_session(env)
    assert lab_project_or_none(env) is None


def test_only_the_foreign_project_set_raises_and_says_why() -> None:
    with pytest.raises(NoLabProjectError, match="may belong to another lab"):
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


@pytest.mark.parametrize(
    ("env", "word"),
    [
        ({"LEDGER_LAB_GCP_PROJECT": "the-lab"}, "own"),
        ({}, "elsewhere"),
        ({"LEDGER_LAB_GCP_PROJECT": " "}, "elsewhere"),
        ({"LEDGER_LAB_GCP_PROJECT": "the-lab", "GCP_PROJECT": "another-lab"}, "elsewhere"),
    ],
)
def test_run_as_a_script_it_prints_one_word_and_no_project(
    env: dict[str, str], word: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gcp_project.main(env) == 0
    assert capsys.readouterr().out == f"{word}\n"
