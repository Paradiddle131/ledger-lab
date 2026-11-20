"""The tool policy, deny by default: a listed read-only tool allowed to listed personas only.

The policy (`agent/policy/tools.cedar`) permits one tool, `run_metric_query`, to the personas the
catalogue (`access/personas.yaml`) gives a read or aggregate grant on a data product holding figures.
The tests check the policy file against the schema and the catalogue, and ask the seam for allowed
and denied requests; the engine failures are planted by replacing the engine's call or the policy's
files.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cedarpy
import pytest
from conftest import load_access

from agent.policy import tools
from agent.policy.tools import (
    POLICIES,
    SCHEMA,
    Decision,
    Entity,
    PolicyInvalid,
    ToolPolicy,
    authorize,
    default_policy,
)

READ_ONLY_TOOL = "run_metric_query"
METRIC = Entity("Metric", "spend_against_award")
LEDGER = Entity("Ledger", "chicago")
CONTEXT = {"session": "session-1"}
# The data products that hold no figure: a grant on them alone does not let a persona run a metric.
NO_FIGURES = {"metric-definitions", "audit-log"}
# The data products that hold figures; with NO_FIGURES, the catalogue's products, so a new product
# fails a test until it is placed in one of the two.
FIGURES = {"payments", "ledger", "group-totals"}


def metric_readers(catalogue: Mapping[str, Any]) -> set[str]:
    """The personas holding a read or aggregate grant on a data product that holds figures."""
    return {
        grant["persona"]
        for grant in catalogue["grants"]
        if grant["access"] in {"read", "aggregate"} and grant["product"] in FIGURES
    }


CATALOGUE = load_access()
READERS = sorted(metric_readers(CATALOGUE))
NOT_READERS = sorted({p["id"] for p in CATALOGUE["personas"]} - set(READERS))


def permitted_personas(policy: Mapping[str, Any]) -> list[str]:
    """The persona ids of a permit's `when { principal in [...] }` condition, in file order."""
    (condition,) = policy["conditions"]
    assert condition["kind"] == "when"
    membership = condition["body"]["in"]
    assert membership["left"] == {"Var": "principal"}
    entities = [item["Value"]["__entity"] for item in membership["right"]["Set"]]
    assert {entity["type"] for entity in entities} == {"Persona"}
    return [entity["id"] for entity in entities]


@pytest.fixture(autouse=True)
def fresh_policy() -> Any:
    """Each test loads the policy from its files, so a planted file does not outlive its test."""
    default_policy.cache_clear()
    yield
    default_policy.cache_clear()


def asks(persona: str, action: str = READ_ONLY_TOOL, **kwargs: Any) -> Decision:
    request: dict[str, Any] = {
        "principal": Entity("Persona", persona),
        "action": action,
        "resource": METRIC,
        "context": CONTEXT,
    }
    request.update(kwargs)
    return authorize(**request)


def denied(decision: Decision) -> bool:
    return decision.allowed is False and not decision and bool(decision.reasons)


def test_the_policy_validates_against_the_schema() -> None:
    result = cedarpy.validate_policies(POLICIES.read_text(), SCHEMA.read_text())
    assert result.validation_passed, [str(error) for error in result.errors]


def test_the_data_products_are_split_into_figures_and_none() -> None:
    products = {product["id"] for product in CATALOGUE["data_products"]}
    assert products == FIGURES | NO_FIGURES
    assert not FIGURES & NO_FIGURES


def test_the_only_permit_is_the_read_only_tool_for_the_catalogues_metric_readers() -> None:
    policies = json.loads(cedarpy.policies_to_json_str(POLICIES.read_text()))
    assert policies["templates"] == {}
    assert policies["templateLinks"] == []
    (policy,) = policies["staticPolicies"].values()
    personas = permitted_personas(policy)
    assert len(personas) == len(set(personas))
    assert sorted(personas) == READERS
    assert {key: value for key, value in policy.items() if key != "conditions"} == {
        "effect": "permit",
        "principal": {"op": "is", "entity_type": "Persona"},
        "action": {"op": "==", "entity": {"type": "Action", "id": READ_ONLY_TOOL}},
        "resource": {"op": "is", "entity_type": "Metric"},
        "annotations": {"id": "run-metric-query"},
    }


def test_the_personas_without_a_figure_grant_are_the_operator_and_the_proposer() -> None:
    assert NOT_READERS == ["agent-operator", "proposer"]


@pytest.mark.parametrize("persona", READERS)
def test_the_listed_read_only_tool_is_allowed_to_a_metric_reader(persona: str) -> None:
    decision = asks(persona)
    assert decision == Decision(allowed=True, policies=("run-metric-query",))
    assert decision


@pytest.mark.parametrize(
    "persona", [*NOT_READERS, "no-such-persona", "", "Auditor", "auditor ", "*"]
)
def test_the_listed_read_only_tool_is_denied_to_anyone_else(persona: str) -> None:
    decision = asks(persona)
    assert denied(decision)
    assert decision.policies == ()
    assert decision.reasons == ("no policy permits run_metric_query (Deny)",)


def test_a_declared_tool_no_policy_permits_is_denied(persona: str) -> None:
    decision = asks(persona, "propose_entry", resource=LEDGER)
    assert denied(decision)
    assert decision.policies == ()
    assert "no policy permits propose_entry" in decision.reasons[0]


@pytest.mark.parametrize("action", ["fetch_url", "run_sql", "", 'run_metric_query" || true'])
def test_an_unlisted_tool_is_denied(action: str) -> None:
    decision = asks("auditor", action)
    assert denied(decision)
    assert "does not exist in the supplied schema" in decision.reasons[0]


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"principal": Entity("Robot", "auditor")}, "not declared in the schema"),
        ({"principal": Entity("Metric", "auditor")}, "is not valid for"),
        ({"resource": Entity("Ledger", "chicago")}, "is not valid for"),
        ({"resource": Entity("Action", READ_ONLY_TOOL)}, "not declared in the schema"),
        ({"context": {}}, "expected the record to have an attribute `session`"),
        ({"context": {"session": 1}}, "is not valid for"),
        ({"context": {"session": "s", "on_behalf_of": "agent"}}, "should not exist"),
    ],
)
def test_a_request_the_schema_refuses_is_denied(change: dict[str, Any], says: str) -> None:
    decision = asks("auditor", **change)
    assert denied(decision)
    assert says in decision.reasons[0]


@pytest.mark.parametrize(
    "change",
    [
        {"principal": 'Persona::"auditor"'},
        {"principal": None},
        {"principal": Entity("Persona", 7)},  # type: ignore[arg-type]
        {"action": Entity("Action", READ_ONLY_TOOL)},
        {"action": None},
        {"resource": {"type": "Metric", "id": "spend_against_award"}},
        {"context": None},
        {"context": [("session", "s")]},
        {"context": {"session": object()}},
    ],
)
def test_an_argument_of_the_wrong_type_is_denied(change: dict[str, Any]) -> None:
    decision = asks("auditor", **change)
    assert denied(decision)
    assert decision.reasons[0].startswith("the engine raised")


def raises(*_: Any, **__: Any) -> Any:
    raise RuntimeError("the engine failed")


def answers(response: Mapping[str, Any]) -> Any:
    def is_authorized(*_: Any, **__: Any) -> cedarpy.AuthzResult:
        return cedarpy.AuthzResult(dict(response))

    return is_authorized


@pytest.mark.parametrize(
    ("engine", "says"),
    [
        (raises, "the engine raised RuntimeError"),
        (answers({"decision": "Maybe"}), "the engine raised KeyError"),
        (answers({}), "the engine raised KeyError"),
        (
            answers({"decision": "Allow", "diagnostics": {"reason": ["policy0"], "errors": ["x"]}}),
            "the engine allowed with errors",
        ),
        (answers({"decision": "Allow", "diagnostics": {}}), "no policy as its reason"),
        (answers({"decision": "NoDecision", "diagnostics": {}}), "(NoDecision)"),
    ],
)
def test_an_engine_failure_or_odd_answer_is_denied(
    monkeypatch: pytest.MonkeyPatch, engine: Any, says: str
) -> None:
    monkeypatch.setattr(cedarpy, "is_authorized", engine)
    decision = asks("auditor")
    assert denied(decision)
    assert says in " ".join(decision.reasons)


def test_a_forbid_overrides_the_permit() -> None:
    policies = (
        POLICIES.read_text()
        + '\n@id("no-auditor")\nforbid (principal == Persona::"auditor", action, resource);\n'
    )
    policy = ToolPolicy(policies, SCHEMA.read_text())
    decision = policy.authorize(Entity("Persona", "auditor"), READ_ONLY_TOOL, METRIC, CONTEXT)
    assert denied(decision)
    assert decision.policies == ("no-auditor",)


@pytest.mark.parametrize(
    "policies",
    [
        "permit (principal, action, resource",
        'permit (principal, action == Action::"fetch_url", resource);',
        'permit (principal is Robot, action == Action::"run_metric_query", resource);',
    ],
)
def test_policies_that_do_not_validate_deny_every_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, policies: str
) -> None:
    planted = tmp_path / "tools.cedar"
    planted.write_text(policies)
    with pytest.raises(PolicyInvalid):
        ToolPolicy.from_files(planted)
    monkeypatch.setattr(tools, "POLICIES", planted)
    decision = asks("auditor")
    assert denied(decision)
    assert decision.reasons[0].startswith("the tool policy did not load: PolicyInvalid")


def test_a_missing_policy_file_denies_every_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tools, "POLICIES", tmp_path / "absent.cedar")
    decision = asks("auditor")
    assert denied(decision)
    assert "FileNotFoundError" in decision.reasons[0]


def test_an_empty_policy_set_denies_the_read_only_tool(tmp_path: Path) -> None:
    empty = tmp_path / "tools.cedar"
    empty.write_text("")
    policy = ToolPolicy.from_files(empty)
    decision = policy.authorize(Entity("Persona", "auditor"), READ_ONLY_TOOL, METRIC, CONTEXT)
    assert denied(decision)
    assert decision.policies == ()


def test_a_schema_that_does_not_parse_denies_every_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    planted = tmp_path / "tools.cedarschema"
    planted.write_text("entity Persona; action run_metric_query appliesTo {")
    monkeypatch.setattr(tools, "SCHEMA", planted)
    decision = asks("auditor")
    assert denied(decision)
    assert decision.reasons[0].startswith("the tool policy did not load")
