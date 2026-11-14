"""The audit record's shape, chain, links and bound SQL, on invented example records.

The examples (`audit_examples.yaml`) are one answer, one proposal and its approval, chained from the
first record of a log; each test below breaks one of them once and expects the schema, the
catalogue check, the chain check, the link check or the SQL check to refuse it.
"""

import copy
import json
from collections.abc import Iterator
from typing import Any

import pytest
import yaml
from conftest import REPO
from jsonschema import Draft202012Validator

from agent.audit import (
    GENESIS,
    ChainBroken,
    LinkBroken,
    UnboundValue,
    canonical,
    check_bound,
    check_times,
    load_record,
    new_salt,
    record_digest,
    seal,
    sealed_result,
    verify_chain,
    verify_links,
)

SCHEMA = REPO / "agent/audit.schema.json"
EXAMPLES = REPO / "tests/agent/audit_examples.yaml"
CLEAR_VALUE = "Chicago paid 1234.50"
# The fields a record may leave out, by example record and path; every other field is required.
OPTIONAL = {
    (0, "model"),
    (0, "model/settings/temperature"),
    (0, "model/settings/max_tokens"),
    (0, "runs/0/query/parameters"),
    (1, "model"),
    (1, "model/settings/temperature"),
}
# The objects whose keys are open: a model's settings (their values are checked) and the build's
# dbt variables, which the commit's dbt_project.yml defines.
OPEN_OBJECTS = {"model/settings", "data/build/vars"}


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    doc = json.loads(SCHEMA.read_text())
    Draft202012Validator.check_schema(doc)
    return doc


@pytest.fixture(scope="module")
def validator(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(schema)


@pytest.fixture
def examples() -> dict[str, Any]:
    return yaml.safe_load(EXAMPLES.read_text())


def example(position: int) -> dict[str, Any]:
    return yaml.safe_load(EXAMPLES.read_text())["records"][position]


def errors(validator: Draft202012Validator, record: dict[str, Any]) -> list[str]:
    return [e.message for e in validator.iter_errors(record)]


def at(value: Any, path: str) -> Any:
    for step in path.split("/"):
        value = value[int(step)] if isinstance(value, list) else value[step]
    return value


def put(record: dict[str, Any], path: str, value: Any) -> None:
    parent, _, last = path.rpartition("/")
    (at(record, parent) if parent else record)[last] = value


def drop(record: dict[str, Any], path: str) -> None:
    parent, _, last = path.rpartition("/")
    del (at(record, parent) if parent else record)[last]


def objects(value: Any, path: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    """Every object inside a record, with its path, the record itself first."""
    if isinstance(value, dict):
        yield path, value
        for key, item in value.items():
            yield from objects(item, f"{path}/{key}".lstrip("/"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from objects(item, f"{path}/{index}")


def fields() -> list[tuple[int, str]]:
    records = yaml.safe_load(EXAMPLES.read_text())["records"]
    return [
        (position, f"{path}/{key}".lstrip("/"))
        for position, record in enumerate(records)
        for path, obj in objects(record)
        for key in obj
    ]


def rechain(records: list[dict[str, Any]], start: int = 0) -> list[dict[str, Any]]:
    """The records from `start` on relinked and re-digested, as a writer rewriting them would."""
    prev = records[start - 1]["digest"] if start else GENESIS
    for record in records[start:]:
        record["prev"] = prev
        if record["kind"] == "approval":
            record["proposal"]["digest"] = records[record["proposal"]["seq"]]["digest"]
        prev = record["digest"] = record_digest(record)
    return records


def personas_of(record: dict[str, Any]) -> dict[str, str]:
    """Each persona a record names, by the kind of persona its place takes."""
    named: dict[str, str] = {}
    if record["kind"] == "answer":
        if "persona" in record["asker"]:
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


# The examples


def test_the_examples_validate_their_chain_and_links_verify_and_their_sql_is_bound(
    validator: Draft202012Validator, examples: dict[str, Any]
) -> None:
    records = examples["records"]
    assert [r["kind"] for r in records] == ["answer", "proposal", "approval"]
    for record in records:
        assert errors(validator, record) == [], record["seq"]
        check_times(record)
        for run in record.get("runs", []):
            check_bound(run["query"]["text"], run["engine"])
    head = verify_chain(records)
    assert head == records[-1]["digest"]
    assert verify_chain(records[1:], prev=records[0]["digest"], seq=1) == head
    verify_links(records)
    assert [load_record(canonical(r).decode()) for r in records] == records


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


def test_each_seal_opens_with_its_salt_and_value(examples: dict[str, Any]) -> None:
    records = examples["records"]
    for seq, sealed in examples["sealed"].items():
        for path, (salt, value) in sealed.items():
            assert at(records[seq], path) == seal(salt, value), f"{seq}: {path}"


# The shape


@pytest.mark.parametrize(("position", "path"), fields())
def test_every_field_but_the_optional_ones_is_required(
    position: int, path: str, validator: Draft202012Validator
) -> None:
    record = example(position)
    drop(record, path)
    if (position, path) in OPTIONAL:
        assert errors(validator, record) == []
    else:
        assert errors(validator, record) != [], path


def test_the_required_top_fields_are_the_schemas_lists(
    schema: dict[str, Any], validator: Draft202012Validator, examples: dict[str, Any]
) -> None:
    for record in examples["records"]:
        refused = set()
        for key in record:
            changed = copy.deepcopy(record)
            del changed[key]
            if errors(validator, changed):
                refused.add(key)
        listed = set(schema["required"]) | set(schema["$defs"][record["kind"]]["required"])
        # An approved decision also requires its posting.
        assert refused == listed | ({"posting"} if record["kind"] == "approval" else set())


@pytest.mark.parametrize(
    ("position", "path"),
    [
        (position, path)
        for position, record in enumerate(yaml.safe_load(EXAMPLES.read_text())["records"])
        for path, _ in objects(record)
        if path not in OPEN_OBJECTS
    ],
)
def test_an_unknown_field_is_refused_at_every_closed_object(
    position: int, path: str, validator: Draft202012Validator
) -> None:
    record = example(position)
    (at(record, path) if path else record)["unknown_field"] = "x"
    assert errors(validator, record) != []


WRONG = [
    (0, "seq", -1),
    (0, "seq", True),
    (0, "seq", 1.5),
    (0, "prev", "0" * 63),
    (0, "digest", "A" * 64),
    (0, "kind", "note"),
    (0, "at", "2026-01-15T11:00:05+01:00"),
    (0, "at", "2026-01-15 10:00:05Z"),
    (0, "asker/persona", "Accountant Chicago"),
    (0, "asker/session", "a session with spaces"),
    (0, "asker/turn", 0),
    (0, "agent", "another-agent"),
    (0, "question", "What did Chicago pay?"),
    (0, "release/commit", "c" * 39),
    (0, "model/name", "a model, with a sentence"),
    (0, "model/settings/temperature", "a sentence that could carry a value"),
    (0, "model/settings/system prompt", "x"),
    (0, "outcome", "guessed"),
    (0, "reason", "policy"),
    (0, "data/build/id", "build id with spaces"),
    (0, "data/build/commit", "short"),
    (0, "data/build/target", "prod"),
    (0, "data/build/vars/source_rows", "some"),
    (0, "data/sources", []),
    (0, "data/sources/0/name", "Chicago Payments"),
    (0, "data/sources/0/sha256", "not a digest"),
    (0, "metrics/0/name", "Spend Against Award"),
    (0, "metrics/0/version", "version one"),
    (0, "metrics", []),
    (0, "runs", []),
    (0, "runs/0/query/text", ""),
    (0, "runs/0/query/parameters", {"day": "x"}),
    (0, "runs/0/as", "Nobody"),
    (0, "runs/0/engine", "postgres"),
    (0, "runs/0/job", "a job reference with spaces"),
    (0, "runs/0/result", {"rows": [["1234.50"]]}),
    (0, "answer", CLEAR_VALUE),
    (1, "agent", "Proposer"),
    (1, "draft", CLEAR_VALUE),
    (1, "requester/persona", "x"),
    (2, "proposal/seq", -1),
    (2, "proposal/digest", "1"),
    (2, "approver", "A"),
    (2, "seen", CLEAR_VALUE),
    (2, "decision", "maybe"),
    (2, "decision", "rejected"),
    (2, "posting", "posted to the ledger"),
]


@pytest.mark.parametrize(("position", "path", "value"), WRONG)
def test_a_wrong_value_is_refused(
    position: int, path: str, value: Any, validator: Draft202012Validator
) -> None:
    record = example(position)
    assert errors(validator, record) == []
    put(record, path, value)
    assert errors(validator, record) != [], path


def test_a_refused_or_stopped_turn_states_its_reason_and_only_no_identity_leaves_out_the_persona(
    validator: Draft202012Validator,
) -> None:
    record = example(0)
    record.update(outcome="abstained", runs=[], metrics=[])
    assert errors(validator, record) == []
    record.update(outcome="refused", reason="policy")
    assert errors(validator, record) == []
    record.update(outcome="stopped", reason="model-call-cap")
    assert errors(validator, record) == []
    record.update(outcome="stopped", reason="policy")
    assert errors(validator, record) != []
    del record["reason"]
    assert errors(validator, record) != []
    record.update(outcome="refused", reason="no-identity")
    assert errors(validator, record) != [], "a turn with no identity names a persona"
    del record["asker"]["persona"]
    assert errors(validator, record) == []
    record["runs"] = example(0)["runs"]
    assert errors(validator, record) != [], "a turn with no identity ran a query"
    record.update(runs=[], outcome="refused", reason="policy")
    assert errors(validator, record) != [], "only a turn with no identity has no persona"


@pytest.mark.parametrize(
    ("path", "named"),
    [
        ("asker/persona", "nobody-at-all"),
        ("runs/0/as", "nobody-at-all"),
        ("asker/persona", "proposer"),
    ],
)
def test_a_persona_the_catalogue_lacks_or_holds_as_another_kind_is_refused(
    path: str, named: str, validator: Draft202012Validator, access: dict[str, Any]
) -> None:
    record = example(0)
    put(record, path, named)
    assert errors(validator, record) == []
    assert unknown_personas(record, access) == [named]


def test_a_proposal_drafted_by_a_human_persona_is_refused(access: dict[str, Any]) -> None:
    record = example(1)
    record["agent"] = "approver"
    assert unknown_personas(record, access) == ["approver"]


# Times


@pytest.mark.parametrize(
    "time", ["2026-13-15T10:00:05Z", "2026-01-15T25:00:05Z", "2026-02-30T10:00:05Z"]
)
@pytest.mark.parametrize("path", ["at", "data/build/at", "data/sources/0/landed_at", "runs/0/at"])
def test_a_time_that_is_no_real_instant_is_refused(
    time: str, path: str, validator: Draft202012Validator
) -> None:
    record = example(0)
    put(record, path, time)
    record["digest"] = record_digest(record)
    assert errors(validator, record) == [], "the pattern alone lets it through"
    with pytest.raises(ChainBroken, match="not a real instant"):
        verify_chain([record])


# The canonical form


def test_the_canonical_form_is_rfc_8785s() -> None:
    """Two of the standard's own examples (RFC 8785, sections 3.2.2 and 3.2.3)."""
    values = {
        "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 0.000000000000000000000000001],
        "string": '\u20ac$\u000f\nA\'B"\\\\"/',
        "literals": [None, True, False],
    }
    assert (
        canonical(values)
        == (
            r'{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
            r'"string":"€$\u000f\nA'
            "'"
            r'B\"\\\\\"/"}'
        ).encode()
    )
    keys = json.loads(
        r'{"€": "Euro Sign", "\r": "Carriage Return", "דּ": "Hebrew Letter Dalet With'
        r' Dagesh", "1": "One", "😀": "Emoji: Grinning Face", "\u0080": "Control",'
        r' "ö": "Latin Small Letter O With Diaeresis"}'
    )
    order = [k for k in json.loads(canonical(keys))]
    assert order == ["\r", "1", "\u0080", "ö", "€", "\U0001f600", "דּ"]
    assert order != sorted(keys), "sorted keys by code point is not the standard's order"


# The chain


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


def test_a_record_removed_or_moved_without_rewriting_what_follows_is_found(
    examples: dict[str, Any],
) -> None:
    records = copy.deepcopy(examples["records"])
    with pytest.raises(ChainBroken, match="expected position 1"):
        verify_chain([records[0], records[2]])
    records[1]["seq"], records[2]["seq"] = 2, 1
    for record in records:
        record["digest"] = record_digest(record)
    with pytest.raises(ChainBroken, match="record 1: prev"):
        verify_chain([records[0], records[2], records[1]])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("seq", True, "not an integer"),
        ("seq", "0", "not an integer"),
        ("prev", None, "prev is not"),
        ("digest", "X", "digest is not"),
        ("at", "yesterday", "not an RFC 3339"),
    ],
)
def test_a_malformed_header_is_a_broken_chain(
    field: str, value: Any, message: str, examples: dict[str, Any]
) -> None:
    record = copy.deepcopy(examples["records"][0])
    record[field] = value
    with pytest.raises(ChainBroken, match=message):
        verify_chain([record])
    del record[field]
    with pytest.raises(ChainBroken, match=f"no {field}"):
        verify_chain([record])


def test_a_log_line_with_a_duplicate_key_or_a_non_finite_number_is_refused(
    examples: dict[str, Any],
) -> None:
    line = canonical(examples["records"][0]).decode()
    assert load_record(line) == examples["records"][0]
    with pytest.raises(ChainBroken, match="duplicate key"):
        load_record(line[:-1] + ',"outcome":"abstained"}')
    with pytest.raises(ChainBroken, match="non-finite"):
        load_record('{"seq": NaN}')
    with pytest.raises(ChainBroken, match="not an object"):
        load_record("[]")


def test_whoever_writes_the_log_can_rewrite_it_from_any_record_on_add_to_it_or_cut_its_tail(
    examples: dict[str, Any],
) -> None:
    """What the chain does not show: each of these verifies, and only a head digest kept outside
    the writer's reach tells the result from the log as it was."""
    original = examples["records"]
    head = original[-1]["digest"]
    edited = copy.deepcopy(original)
    edited[1]["inputs"] = edited[1]["draft"]
    assert verify_chain(rechain(edited, start=1)) != head
    relinked = copy.deepcopy([original[0], original[2]])
    relinked[1]["seq"] = 1
    assert verify_chain(rechain(relinked, start=1)) != head
    assert verify_chain(original[:-1]) != head
    forged = copy.deepcopy(original[2])
    forged.update(seq=3, decision="rejected")
    del forged["posting"]
    appended = rechain([*copy.deepcopy(original), forged], start=3)
    assert verify_chain(appended) != head
    with pytest.raises(LinkBroken, match="a second decision"):
        verify_links(appended)


# The links


def broken_link(change: str) -> list[dict[str, Any]]:
    records = yaml.safe_load(EXAMPLES.read_text())["records"]
    approval = records[2]
    if change == "a record not in the log":
        approval["proposal"]["seq"] = 5
    elif change == "a later record":
        later = {**copy.deepcopy(records[1]), "seq": 3}
        records.append(later)
        approval["proposal"] = {"seq": 3, "digest": later["digest"]}
    elif change == "an answer":
        approval["proposal"] = {"seq": 0, "digest": records[0]["digest"]}
    elif change == "another digest":
        approval["proposal"]["digest"] = "f" * 64
    elif change == "another draft":
        approval["seen"] = "f" * 64
    elif change == "the requester":
        approval["approver"] = records[1]["requester"]["persona"]
    elif change == "before the proposal":
        approval["at"] = "2026-01-15T10:59:59Z"
    elif change == "a second decision":
        records.append({**copy.deepcopy(approval), "seq": 3})
    return records


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("a record not in the log", "no earlier record"),
        ("a later record", "no earlier record"),
        ("an answer", "not a proposal"),
        ("another digest", "digest does not match"),
        ("another draft", "did not see"),
        ("the requester", "their own proposal"),
        ("before the proposal", "before the proposal"),
        ("a second decision", "a second decision"),
    ],
)
def test_an_approval_that_does_not_hold_against_its_proposal_is_refused(
    change: str, message: str
) -> None:
    verify_links(broken_link("none"))
    with pytest.raises(LinkBroken, match=message):
        verify_links(broken_link(change))


# Bound SQL


@pytest.mark.parametrize(
    ("sql", "engine"),
    [
        ("select count(*) from payments where entity_id = 'chicago'", "duckdb"),
        ("select count(*) from payments where amount > 25000", "duckdb"),
        ("select count(*) from payments where entity_id = '1'", "duckdb"),
        ("select count(*) from payments where payment_date = date '2025-01-01'", "duckdb"),
        ("select count(*) from payments where amount > 0.5", "bigquery"),
        ("select count(*) from payments limit 10", "bigquery"),
        ("select count(*) from payments where", "duckdb"),
    ],
)
def test_sql_carrying_a_value_in_clear_is_refused(sql: str, engine: str) -> None:
    with pytest.raises(UnboundValue):
        check_bound(sql, engine)


@pytest.mark.parametrize(
    ("sql", "engine"),
    [
        ("select count(*) from payments where entity_id = $entity and amount > 0", "duckdb"),
        ("select count(*) from payments where entity_id = ? limit 1", "duckdb"),
        ("select count(*) from payments where entity_id = @entity and not is_repeat", "bigquery"),
    ],
)
def test_sql_with_every_value_bound_passes(sql: str, engine: str) -> None:
    check_bound(sql, engine)


# Seals and results


def test_a_seal_takes_only_a_base64url_salt_of_128_bits() -> None:
    salts = {new_salt() for _ in range(3)}
    assert len(salts) == 3
    for salt in salts:
        assert seal(salt, "value") != seal(new_salt(), "value")
    for bad in ("x" * 21, " " * 22, "a salt with spaces in it"):
        with pytest.raises(ValueError, match="at least 22 base64url"):
            seal(bad, "value")


def test_a_result_seals_alike_in_any_row_order() -> None:
    rows = [["b", "2"], ["a", None], ["a", "1"]]
    salt = new_salt()
    assert seal(salt, sealed_result(["k", "v"], rows)) == seal(
        salt, sealed_result(["k", "v"], list(reversed(rows)))
    )
    assert sealed_result(["k", "v"], rows) != sealed_result(["v", "k"], rows)
