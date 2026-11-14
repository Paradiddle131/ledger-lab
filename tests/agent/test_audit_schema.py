"""The audit record's shape and its tamper evidence, on invented example records.

The examples (`audit_examples.yaml`) are one answer, one proposal and its approval, chained from the
first record of a log; each test below breaks one of them once and expects the schema, the
catalogue check or the chain check to refuse it.
"""

import copy
import json
from collections.abc import Callable
from typing import Any

import pytest
import yaml
from conftest import REPO
from jsonschema import Draft202012Validator

from agent.audit import GENESIS, ChainBroken, record_digest, seal, verify_chain

SCHEMA = REPO / "agent/audit.schema.json"
EXAMPLES = REPO / "tests/agent/audit_examples.yaml"
CLEAR_VALUE = "Chicago paid 1234.50"


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.fixture
def examples() -> dict[str, Any]:
    return yaml.safe_load(EXAMPLES.read_text())


def errors(validator: Draft202012Validator, record: dict[str, Any]) -> list[str]:
    return [e.message for e in validator.iter_errors(record)]


def at(value: Any, path: str) -> Any:
    for step in path.split("/"):
        value = value[int(step)] if isinstance(value, list) else value[step]
    return value


def personas_of(record: dict[str, Any]) -> dict[str, str]:
    """Each persona a record names, by the kind of persona its place takes."""
    named: dict[str, str] = {}
    if record["kind"] == "answer":
        named[record["asker"]["persona"]] = "human"
        for run in record["runs"]:
            named.setdefault(run["as"], "any")
    elif record["kind"] == "proposal":
        named[record["requester"]["persona"]] = "human"
        named[record["agent"]] = "agent"
    else:
        named[record["approver"]] = "human"
    return named


def unknown_personas(record: dict[str, Any], access: dict[str, Any]) -> list[str]:
    """The personas a record names that the catalogue lacks or holds as another kind."""
    kinds = {p["id"]: p["kind"] for p in access["personas"]}
    return sorted(
        persona
        for persona, kind in personas_of(record).items()
        if persona not in kinds or kind not in ("any", kinds[persona])
    )


def test_the_examples_validate_and_their_chain_verifies(
    validator: Draft202012Validator, examples: dict[str, Any]
) -> None:
    records = examples["records"]
    assert [r["kind"] for r in records] == ["answer", "proposal", "approval"]
    for record in records:
        assert errors(validator, record) == [], record["seq"]
    head = verify_chain(records)
    assert head == records[-1]["digest"]
    assert verify_chain(records[1:], prev=records[0]["digest"], seq=1) == head


def test_the_examples_name_only_catalogued_personas_sources_and_metrics(
    examples: dict[str, Any], access: dict[str, Any]
) -> None:
    for record in examples["records"]:
        assert unknown_personas(record, access) == [], record["seq"]
    sources = yaml.safe_load((REPO / "ingest/sources.yaml").read_text())["sources"]
    metrics = {
        m["name"]
        for path in (REPO / "semantics/metricflow").glob("*.yml")
        for m in yaml.safe_load(path.read_text()).get("metrics", [])
    }
    answer = examples["records"][0]
    assert {s["name"] for s in answer["data"]["sources"]} <= set(sources)
    assert {m["name"] for m in answer["metrics"]} <= metrics


def test_each_seal_opens_with_its_salt_and_value_and_the_approval_saw_the_draft(
    examples: dict[str, Any],
) -> None:
    records = examples["records"]
    for seq, fields in examples["sealed"].items():
        for path, (salt, value) in fields.items():
            assert at(records[seq], path) == seal(salt, value), f"{seq}: {path}"
    proposal, approval = records[1], records[2]
    assert approval["proposal"] == {"seq": proposal["seq"], "digest": proposal["digest"]}
    assert approval["seen"] == proposal["draft"]


def drop(path: str) -> Callable[[dict[str, Any]], None]:
    parent, _, last = path.rpartition("/")

    def change(record: dict[str, Any]) -> None:
        holder = at(record, parent) if parent else record
        del holder[last]

    return change


def put(path: str, value: Any) -> Callable[[dict[str, Any]], None]:
    parent, _, last = path.rpartition("/")

    def change(record: dict[str, Any]) -> None:
        holder = at(record, parent) if parent else record
        holder[last] = value

    return change


REFUSED = {
    "an answer whose run has no query": (0, drop("runs/0/query")),
    "an answer whose run has no query text": (0, drop("runs/0/query/text")),
    "an answered question with no run": (0, put("runs", [])),
    "an answered question citing no metric": (0, put("metrics", [])),
    "an answer with no asking persona": (0, drop("asker/persona")),
    "an answer with no asker": (0, drop("asker")),
    "a run with no identity it ran as": (0, drop("runs/0/as")),
    "a proposal with no requester persona": (1, drop("requester/persona")),
    "an approval with no approver": (2, drop("approver")),
    "an unknown field on the record": (0, put("reviewed_by", "auditor")),
    "an unknown field on a run": (0, put("runs/0/rows", 1)),
    "an unknown field on the asker": (0, put("asker/name", "Example Person")),
    "an unknown kind": (0, put("kind", "note")),
    "a clear answer in place of its seal": (0, put("answer", CLEAR_VALUE)),
    "a clear question in place of its seal": (0, put("question", "What did Chicago pay?")),
    "a clear result in place of its seal": (0, put("runs/0/result", {"rows": [["1234.50"]]})),
    "clear parameters in place of their seal": (0, put("runs/0/query/parameters", {"day": "x"})),
    "a clear draft in place of its seal": (1, put("draft", CLEAR_VALUE)),
    "an approval with no posting": (2, drop("posting")),
    "a rejection with a posting": (2, put("decision", "rejected")),
    "a time not in UTC": (0, put("at", "2026-01-15T11:00:05+01:00")),
}


@pytest.mark.parametrize("case", REFUSED)
def test_a_record_breaking_its_shape_is_refused(
    case: str, validator: Draft202012Validator, examples: dict[str, Any]
) -> None:
    position, change = REFUSED[case]
    record = copy.deepcopy(examples["records"][position])
    assert errors(validator, record) == []
    change(record)
    assert errors(validator, record) != [], case


def test_an_abstention_needs_no_run_but_its_seals(
    validator: Draft202012Validator, examples: dict[str, Any]
) -> None:
    record = copy.deepcopy(examples["records"][0])
    record.update(outcome="abstained", runs=[], metrics=[])
    assert errors(validator, record) == []
    del record["answer"]
    assert errors(validator, record) != []


@pytest.mark.parametrize(
    ("path", "named"),
    [
        ("asker/persona", "nobody-at-all"),
        ("runs/0/as", "nobody-at-all"),
        ("asker/persona", "proposer"),
    ],
)
def test_a_persona_the_catalogue_lacks_or_holds_as_another_kind_is_refused(
    path: str,
    named: str,
    validator: Draft202012Validator,
    examples: dict[str, Any],
    access: dict[str, Any],
) -> None:
    record = copy.deepcopy(examples["records"][0])
    put(path, named)(record)
    assert errors(validator, record) == []
    assert unknown_personas(record, access) == [named]


def test_a_proposal_drafted_by_a_human_persona_is_refused(
    examples: dict[str, Any], access: dict[str, Any]
) -> None:
    record = copy.deepcopy(examples["records"][1])
    record["agent"] = "approver"
    assert unknown_personas(record, access) == ["approver"]


def rechained(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The records with each digest recomputed, as a writer that rewrote them would leave them."""
    for record in records:
        record["digest"] = record_digest(record)
    return records


def test_a_broken_previous_digest_is_found(examples: dict[str, Any]) -> None:
    records = copy.deepcopy(examples["records"])
    records[1]["prev"] = records[1]["digest"]
    records[1]["digest"] = record_digest(records[1])
    with pytest.raises(ChainBroken, match="record 1: prev"):
        verify_chain(records)
    with pytest.raises(ChainBroken, match="record 0: prev"):
        verify_chain(examples["records"], prev="f" * 64)


def test_a_body_that_does_not_match_its_digest_is_found(examples: dict[str, Any]) -> None:
    records = copy.deepcopy(examples["records"])
    records[0]["runs"][0]["query"]["text"] += " or true"
    with pytest.raises(ChainBroken, match="record 0: the body"):
        verify_chain(records)


def test_a_record_removed_or_moved_inside_the_log_is_found(examples: dict[str, Any]) -> None:
    records = copy.deepcopy(examples["records"])
    with pytest.raises(ChainBroken, match="expected position 1"):
        verify_chain([records[0], records[2]])
    records[1]["seq"], records[2]["seq"] = 2, 1
    with pytest.raises(ChainBroken, match="record 1: prev"):
        verify_chain(rechained([records[0], records[2], records[1]]))


def test_a_whole_log_rewritten_from_the_first_record_still_verifies(
    examples: dict[str, Any],
) -> None:
    """What the chain does not prove: a writer of the whole log can rebuild it; only a head
    digest kept outside the log shows the change."""
    original_head = examples["records"][-1]["digest"]
    records = copy.deepcopy(examples["records"])
    records[0]["outcome"] = "abstained"
    prev = GENESIS
    for record in records:
        record["prev"] = prev
        if record["kind"] == "approval":
            record["proposal"]["digest"] = records[record["proposal"]["seq"]]["digest"]
        prev = record["digest"] = record_digest(record)
    assert verify_chain(records) != original_head


def test_a_seal_refuses_a_short_salt() -> None:
    assert seal("x" * 22, "value") != seal("y" * 22, "value")
    with pytest.raises(ValueError, match="at least 22"):
        seal("x" * 21, "value")
