"""The policy tags on the clear payments copy (`access/policy_tags.py`), over a stand-in for
`gcloud` and `bq`: each tagged column of access/columns.yaml gets its tag's resource name, every
other column is left as it is, the masked copy is not touched, and a missing taxonomy, tag or
column stops before any schema is written. Live, as each human persona (`bigquery_persona`): a
persona without the clear reader role on a tag is refused that column of the clear copy, even where
it may read the table's other columns, both through each view of its purposes over the copy
(`access/purpose_views.py`), which is where a persona reads the copy, and on the copy itself.
"""

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from access import policy_tags
from access.bigquery import PersonaQueryError, PersonaSession
from access.policy_tags import TAXONOMY, tag
from access.purpose_views import views
from access.row_policies import RowPolicyError
from tests.conftest import load_aggregates, load_purposes

PROJECT = "the-lab"
WHERE = "us-central1"
TAXONOMY_NAME = f"projects/{PROJECT}/locations/{WHERE}/taxonomies/1"


def schema(names: Sequence[str]) -> list[dict[str, Any]]:
    return [{"name": n, "type": "STRING", "mode": "NULLABLE"} for n in names]


class Tools:
    def __init__(
        self,
        columns: dict[str, Any],
        taxonomies: int = 1,
        drop_tag: str | None = None,
        drop_column: str | None = None,
    ) -> None:
        self.tags = [t["id"] for t in columns["tags"] if t["id"] != drop_tag]
        self.taxonomies = taxonomies
        tagged = [c.split(".", 1)[1] for c in columns["products"]["payments"]["columns"]]
        self.columns = [c for c in ["payment_key", "entity_id", *tagged] if c != drop_column]
        self.written: dict[str, list[dict[str, Any]]] = {}
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> str:
        self.commands.append(list(command))
        if command[0] == "gcloud":
            if "policy-tags" in command:
                return json.dumps(
                    [
                        {"displayName": t, "name": f"{TAXONOMY_NAME}/policyTags/{t}"}
                        for t in self.tags
                    ]
                )
            return json.dumps(
                [{"displayName": TAXONOMY, "name": TAXONOMY_NAME}] * self.taxonomies
                + [{"displayName": "another", "name": "x/taxonomies/2"}]
            )
        if "show" in command:
            return json.dumps(schema(self.columns))
        if "update" in command:
            self.written[command[-2]] = json.loads(Path(command[-1]).read_text())
            return ""
        raise AssertionError(command)


def test_each_tagged_column_of_the_clear_copy_gets_its_tag(
    access: dict[str, Any], columns: dict[str, Any]
) -> None:
    tools = Tools(columns)
    tag(access, columns, PROJECT, WHERE, tools)
    assert list(tools.written) == [f"{PROJECT}:payments.payments"]
    want = {c.split(".", 1)[1]: t for c, t in columns["products"]["payments"]["columns"].items()}
    for field in tools.written[f"{PROJECT}:payments.payments"]:
        if field["name"] in want:
            assert field["policyTags"] == {
                "names": [f"{TAXONOMY_NAME}/policyTags/{want[field['name']]}"]
            }
        else:
            assert "policyTags" not in field
            assert field == {"name": field["name"], "type": "STRING", "mode": "NULLABLE"}


FAILS: dict[str, Callable[[dict[str, Any]], Tools]] = {
    "no taxonomy": lambda columns: Tools(columns, taxonomies=0),
    "two taxonomies": lambda columns: Tools(columns, taxonomies=2),
    "a tag missing": lambda columns: Tools(columns, drop_tag="individual-id"),
    "a column missing": lambda columns: Tools(columns, drop_column="vendor_person_key"),
}


@pytest.mark.parametrize("name", list(FAILS))
def test_a_taxonomy_tag_or_column_missing_writes_no_schema(
    access: dict[str, Any], columns: dict[str, Any], name: str
) -> None:
    tools = FAILS[name](columns)
    with pytest.raises(RowPolicyError):
        tag(access, columns, PROJECT, WHERE, tools)
    assert tools.written == {}


def test_the_command_refuses_a_session_without_the_lab_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_gcp(command: Sequence[str]) -> str:
        raise AssertionError("a GCP call was made")

    monkeypatch.setattr(policy_tags, "run_tool", no_gcp)
    assert policy_tags.main(["tag"], {}) == 1
    assert policy_tags.main(["tag"], {"LEDGER_LAB_GCP_PROJECT": PROJECT, "GCP_PROJECT": "x"}) == 1


@pytest.mark.bigquery_persona
def test_a_persona_without_a_tags_clear_role_is_refused_its_column_of_the_clear_copy(
    as_persona_on_bigquery: Callable[[str], PersonaSession],
    access: dict[str, Any],
    columns: dict[str, Any],
    human: str,
) -> None:
    from gcp_project import lab_project

    project = lab_project()
    tagged = columns["products"]["payments"]["columns"]
    clear = {t["id"]: set(t["clear"]) for t in columns["tags"]}
    purposes = load_purposes()
    mine = {p["id"] for p in purposes["purposes"] if human in p["personas"]}
    # The views of the persona's purposes over the clear copy: where it reads the copy's rows.
    over_clear = [
        v
        for v in views(access, columns, purposes, load_aggregates())
        if v.purpose in mine and (v.source_dataset, v.source) == ("payments", "payments")
    ]
    session = as_persona_on_bigquery(human)
    for column, tag_id in tagged.items():
        name = column.split(".", 1)[1]
        places = [f"{v.dataset}.{v.name}" for v in over_clear if name in v.columns]
        if human in clear[tag_id]:
            if not places:
                pytest.fail("no view of the clear reader's purposes lists a tagged column")
            for place in places:
                session.query(f"SELECT {name} FROM `{project}.{place}` LIMIT 1")  # noqa: S608
            continue
        for place in [*places, "payments.payments"]:
            with pytest.raises(PersonaQueryError) as refused:
                session.query(f"SELECT {name} FROM `{project}.{place}` LIMIT 1")  # noqa: S608
            if refused.value.status != 403:
                pytest.fail("a tagged column was not refused to a persona without its clear role")
    for view in over_clear:
        session.query(f"SELECT entity_id FROM `{project}.{view.dataset}.{view.name}` LIMIT 1")  # noqa: S608
