"""The tool policy, deny by default: a listed read-only tool allowed, everything else denied.

The policy (`agent/policy/tools.cedar`) permits one tool, `run_metric_query`. Each test below asks
the seam once and expects a denial, except the ones that ask for that tool on a metric; the engine
failures are planted by replacing the engine's call or the policy's files.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import cedarpy
import pytest

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


def test_the_only_permit_names_the_read_only_tool() -> None:
    policies = json.loads(cedarpy.policies_to_json_str(POLICIES.read_text()))
    effects = [(p["effect"], p["action"]) for p in policies["staticPolicies"].values()]
    assert effects == [
        ("permit", {"op": "==", "entity": {"type": "Action", "id": READ_ONLY_TOOL}}),
    ]
    assert policies["templates"] == {}


def test_the_listed_read_only_tool_is_allowed(persona: str) -> None:
    decision = asks(persona)
    assert decision == Decision(allowed=True, policies=("run-metric-query",))
    assert decision


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
