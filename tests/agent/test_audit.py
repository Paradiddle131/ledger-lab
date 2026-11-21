"""The own agent's audit records, written by `agent.ask.ask` to an `agent.audit.FileLog`.

The first tests rebuild an answer from its record alone: they read the log file and the kept
values from disk, verify the chain up to the record, check the build the record names against the
checkout (its commit and the digests of the files it lists), build the warehouse again from the
record's target and variables, re-run each query as the record's persona with its kept bound values
and match the result to its seal, then ask the scripted model of the release again with the kept
question and those results and match its reply to the answer's seal. Each wrong record, value or
chain below must stop that rebuild.

The others break the log sink, or hand it a record it must refuse, and expect no answer, the log
as it was, and no value of the turn in the error; and they read every record a run writes for
values held in clear.
"""

import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import duckdb
import pytest
from conftest import REPO, SAMPLED_SOURCES, git_out
from jsonschema import Draft202012Validator

import agent.ask
import agent.audit
from access.compile import open_persona
from agent.ask import MAX_TOOL_CALLS, Audit, NoPersonaError, ask, load_context
from agent.audit import (
    GENESIS,
    AuditWriteFailed,
    ChainBroken,
    FileLog,
    RecordRefused,
    UnboundValue,
    bind,
    cell_text,
    new_salt,
    read_log,
    record_digest,
    seal,
    sealed_paths,
    sealed_result,
    verify_chain,
)
from agent.model import Citation, Reply, ToolCall, Turn, model_from_config

AsPersona = Callable[[str], duckdb.DuckDBPyConnection]
QUESTION = "What share of the City of Chicago's served payment rows repeat another served row?"
UNANSWERED = "Which vendor was paid the most?"
CHICAGO = "accountant-chicago"
UK = "accountant-uk-transport"
DENIED = "agent-operator"
SCHEMA = Draft202012Validator(json.loads((REPO / "agent/audit.schema.json").read_text()))
Warehouse = Callable[[Mapping[str, Any]], Path]


class NotRebuilt(AssertionError):
    """The record, its kept values or the build do not give back the answer."""


def need(holds: bool, message: str) -> None:
    if not holds:
        raise NotRebuilt(message)


def produce(as_persona: AsPersona, audit: Audit) -> None:
    """Four turns written to `audit.log`: no persona, a question no query answers, then the
    verified question asked by Chicago's accountant and by the UK accountant (records 2 and 3).
    Nothing they return is kept: the rebuild reads only the files."""
    with pytest.raises(ValueError, match="not a persona"):
        ask("nobody-at-all", QUESTION, as_persona=as_persona, audit=audit)
    ask(CHICAGO, UNANSWERED, as_persona=as_persona, audit=audit)
    ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit)
    ask(UK, QUESTION, as_persona=as_persona, audit=audit)


def run_as(connection: duckdb.DuckDBPyConnection, text: str, values: Mapping[str, Any]) -> Any:
    """The record's SQL on the persona's database, its unqualified names resolved in the schemas
    that database holds, as the release's tool resolves them."""
    cursor = connection.cursor()
    held = cursor.execute(
        "select distinct table_schema from information_schema.tables"
        " where table_catalog = current_database()"
    ).fetchall()
    cursor.execute(f"set search_path = '{','.join(sorted(s for (s,) in held))}'")
    result = cursor.execute(text, dict(values) or [])
    columns = tuple(column[0] for column in result.description or ())
    return columns, tuple(tuple(row) for row in result.fetchall())


def rebuild(
    log: Path,
    kept: Path,
    seq: int,
    rebuilt: Warehouse,
    catalogues: Mapping[str, Any],
    directory: Path,
) -> list[dict[str, Any]]:
    """Rebuild record `seq` of the log at `log` from it alone, with the kept values under `kept`;
    return each run's result as re-run. Raises `NotRebuilt` or `ChainBroken` when it fails."""
    records = read_log(log.read_bytes())
    need(seq < len(records), f"the log holds no record {seq}")
    verify_chain(records[: seq + 1])
    record = records[seq]
    need(not list(SCHEMA.iter_errors(record)), "the record breaks the schema")
    need(record["kind"] == "answer" and record["outcome"] == "answered", "not an answered turn")
    opened = json.loads((kept / f"{seq}.json").read_text())
    need(set(opened) == sealed_paths(record), "the kept values are not one per sealed field")
    for path, (salt, value) in opened.items():
        need(field(record, path) == seal(salt, value), f"the kept {path} does not open its seal")

    # The build the record names, against the checkout: the same commit, no change to the dbt
    # project since it, and each listed file's digest.
    head = git_out("rev-parse", "HEAD")
    build = record["data"]["build"]
    need(record["release"]["commit"] == head, "the release is not the checkout's commit")
    need(build["commit"] == head, "the build's commit is not the checkout's")
    changed = git_out("status", "--porcelain", "--untracked-files=all", "--", "models")
    need(changed == "", f"the dbt project differs from the build's commit: {changed}")
    for source in record["data"]["sources"]:
        digest = hashlib_of(REPO / SAMPLED_SOURCES[source["name"]])
        need(digest == source["sha256"], f"{source['name']}: not the file the build read")
    database = rebuilt(build)

    rerun: list[dict[str, Any]] = []
    for index, run in enumerate(record["runs"]):
        values = opened.get(f"runs/{index}/query/parameters", ("", {}))[1]
        (directory / f"run-{index}").mkdir(parents=True)
        connection = open_persona(
            catalogues["access"],
            catalogues["columns"],
            catalogues["aggregates"],
            catalogues["purposes"],
            run["as"],
            database,
            directory / f"run-{index}",
        )
        try:
            columns, rows = run_as(connection, run["query"]["text"], values)
        finally:
            connection.close()
        result = sealed_result(columns, ([cell_text(v) for v in row] for row in rows))
        salt = opened[f"runs/{index}/result"][0]
        need(seal(salt, result) == run["result"], f"run {index}: the result does not match")
        rerun.append({"columns": columns, "rows": rows, "sealed": result})

    # The answer: the release's scripted model asked the kept question again, shown the results
    # just re-run in the order the record lists them.
    question = opened["question"][1]
    queries = load_context()
    model = model_from_config({}, queries)
    results: list[Citation] = []
    while not isinstance(
        step := model.step(Turn(question, tuple(queries.values()), tuple(results))), Reply
    ):
        assert isinstance(step, ToolCall)
        index = len(results)
        need(index < len(record["runs"]), "the model calls a query the record has no run of")
        query = queries[step.query]
        text, values = bind(query.sql, "duckdb")
        run = record["runs"][index]
        need(text == run["query"]["text"], f"run {index}: not the query the model calls")
        kept_values = opened.get(f"runs/{index}/query/parameters", ("", {}))[1]
        need(values == kept_values, f"run {index}: not the values the query binds")
        found = rerun[index]
        results.append(
            Citation(query.id, query.metric, query.version, query.sql, *found_of(found), None)
        )
    need(len(results) == len(record["runs"]), "the record lists a run the model does not call")
    metrics = list(dict.fromkeys((c.metric, c.version) for c in results))
    need(record["metrics"] == [{"name": n, "version": v} for n, v in metrics], "other metrics")
    need(seal(opened["answer"][0], step.text) == record["answer"], "the answer does not match")
    return rerun


def found_of(found: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]:
    return found["columns"], found["rows"]


def hashlib_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def field(record: Mapping[str, Any], path: str) -> Any:
    value: Any = record
    for step in path.split("/"):
        value = value[int(step)] if isinstance(value, list) else value[step]
    return value


@pytest.fixture(scope="module")
def rebuilt(tmp_path_factory: pytest.TempPathFactory) -> Warehouse:
    """The warehouse built again, apart from the one the answers were made over, from a record's
    build: its target and its dbt variables, once per such pair in this module."""
    from tests.models.dbt_build import built

    made: dict[str, Path] = {}

    def make(build: Mapping[str, Any]) -> Path:
        need(build["target"] == "local", "a build on a target this rebuild cannot run")
        need(set(build["vars"]) == {"source_rows"}, "dbt variables this rebuild does not pass")
        rows = build["vars"]["source_rows"]
        if rows not in made:
            made[rows] = built(tmp_path_factory.mktemp("rebuilt"), rows).database
        return made[rows]

    return make


@pytest.fixture
def catalogues(
    access: dict[str, Any],
    columns: dict[str, Any],
    aggregates: dict[str, Any],
    purposes: dict[str, Any],
) -> dict[str, Any]:
    return {"access": access, "columns": columns, "aggregates": aggregates, "purposes": purposes}


@pytest.fixture
def produced(as_persona: AsPersona, audit: Audit) -> FileLog:
    produce(as_persona, audit)
    assert isinstance(audit.log, FileLog)
    return audit.log


# Rebuilding an answer


def test_answer_rebuilt_from_its_audit_record(
    produced: FileLog, rebuilt: Warehouse, catalogues: dict[str, Any], tmp_path: Path
) -> None:
    """Records 2 and 3, Chicago's and the UK accountant's answers to one question, each rebuilt
    from the files alone: Chicago's re-run gives one share in [0, 1], the UK's the empty sum's
    null, each equal to the sealed result, and the model's reply equal to the sealed answer."""
    log, kept = produced.path, produced.kept
    (chicago,) = rebuild(log, kept, 2, rebuilt, catalogues, tmp_path / "chicago")
    (uk,) = rebuild(log, kept, 3, rebuilt, catalogues, tmp_path / "uk")
    ((share,),) = chicago["rows"]
    assert isinstance(share, float) and 0 <= share <= 1
    assert uk["rows"] == ((None,),)
    assert chicago["sealed"] != uk["sealed"]
    assert verify_chain(read_log(log.read_bytes())) == read_log(log.read_bytes())[-1]["digest"]


def rewrite(log: Path, change: Callable[[list[dict[str, Any]]], None], rechain: bool) -> None:
    """Change the log's records in place; with `rechain`, relink and re-digest them all, as a
    writer rewriting the log would."""
    records = read_log(log.read_bytes())
    change(records)
    if rechain:
        prev = GENESIS
        for record in records:
            record["prev"] = prev
            prev = record["digest"] = record_digest(record)
    log.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))


def reseal(kept: Path, seq: int, path: str, value: Any) -> Callable[[list[dict[str, Any]]], None]:
    """Replace a kept value and return the change that seals it into the record, as a writer
    making up both would."""
    opened = json.loads((kept / f"{seq}.json").read_text())
    salt = new_salt()
    opened[path] = [salt, value]
    (kept / f"{seq}.json").write_text(json.dumps(opened))

    def change(records: list[dict[str, Any]]) -> None:
        put(records[seq], path, seal(salt, value))

    return change


def put(record: dict[str, Any], path: str, value: Any) -> None:
    parent, _, last = path.rpartition("/")
    target = field(record, parent) if parent else record
    if isinstance(target, list):
        target[int(last)] = value
    else:
        target[last] = value


OTHER_QUERY = "duplicate-payment-amount-rate"


def wrong(case: str, log: Path, kept: Path) -> None:
    """Break record 2 or what is kept for it, one way per case."""
    if case == "the result's seal":
        rewrite(log, lambda r: put(r[2], "runs/0/result", seal(new_salt(), "x")), rechain=True)
    elif case == "the result with its seal":
        made_up = {"columns": ["duplicate_payment_rate"], "rows": [["0.5"]]}
        rewrite(log, reseal(kept, 2, "runs/0/result", made_up), rechain=True)
    elif case == "the query":
        text, _ = bind(load_context()[OTHER_QUERY].sql, "duckdb")
        rewrite(log, lambda r: put(r[2], "runs/0/query/text", text), rechain=True)
    elif case == "the persona it ran as":
        rewrite(log, lambda r: put(r[2], "runs/0/as", UK), rechain=True)
    elif case == "an earlier record":
        rewrite(log, lambda r: put(r[1], "outcome", "refused"), rechain=False)
    elif case == "the record itself":
        rewrite(log, lambda r: put(r[2], "metrics/0/version", 2), rechain=False)
    elif case == "the kept result":
        opened = json.loads((kept / "2.json").read_text())
        opened["runs/0/result"][1]["rows"] = [["0.5"]]
        (kept / "2.json").write_text(json.dumps(opened))
    elif case == "a kept value with its seal":
        change = reseal(kept, 2, "runs/0/query/parameters", {"v1": "uk"})
        rewrite(log, change, rechain=True)
    elif case == "the answer with its seal":
        rewrite(log, reseal(kept, 2, "answer", "Every payment repeats."), rechain=True)
    elif case == "the question with its seal":
        rewrite(log, reseal(kept, 2, "question", UNANSWERED), rechain=True)
    elif case == "the build's commit":
        rewrite(log, lambda r: put(r[2], "data/build/commit", "c" * 40), rechain=True)
    elif case == "a listed file's digest":
        rewrite(log, lambda r: put(r[2], "data/sources/0/sha256", "a" * 64), rechain=True)
    else:
        raise AssertionError(case)


@pytest.mark.parametrize(
    ("case", "error", "message"),
    [
        ("the result's seal", NotRebuilt, "kept runs/0/result does not open"),
        ("the result with its seal", NotRebuilt, "result does not match"),
        ("the query", NotRebuilt, "result does not match"),
        ("the persona it ran as", NotRebuilt, "result does not match"),
        ("an earlier record", ChainBroken, "record 1: the body"),
        ("the record itself", ChainBroken, "record 2: the body"),
        ("the kept result", NotRebuilt, "kept runs/0/result does not open"),
        ("a kept value with its seal", NotRebuilt, "result does not match"),
        ("the answer with its seal", NotRebuilt, "answer does not match"),
        ("the question with its seal", NotRebuilt, "the record lists a run the model does not"),
        ("the build's commit", NotRebuilt, "build's commit"),
        ("a listed file's digest", NotRebuilt, "not the file the build read"),
    ],
)
def test_a_wrong_record_kept_value_or_chain_is_not_rebuilt(
    case: str,
    error: type[Exception],
    message: str,
    produced: FileLog,
    rebuilt: Warehouse,
    catalogues: dict[str, Any],
    tmp_path: Path,
) -> None:
    rebuild(produced.path, produced.kept, 2, rebuilt, catalogues, tmp_path / "before")
    wrong(case, produced.path, produced.kept)
    with pytest.raises(error, match=message):
        rebuild(produced.path, produced.kept, 2, rebuilt, catalogues, tmp_path / "after")


# One record per turn, and what it holds in clear


class Looping:
    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        return (ToolCall("duplicate-payment-rate"),) * (MAX_TOOL_CALLS + 1)

    def step(self, turn: Turn) -> ToolCall | Reply:
        return ToolCall("duplicate-payment-rate")


class Unplanned:
    """Plans nothing, makes one call, then replies: the call is refused as unplanned."""

    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        return ()

    def step(self, turn: Turn) -> ToolCall | Reply:
        return Reply("none ran") if turn.results else ToolCall("duplicate-payment-rate")


class Wandering:
    """Plans nothing and keeps making calls, until the refusal limit stops it; past a hundred
    steps it raises, so a turn the limit fails to end fails the test instead of running on."""

    def __init__(self) -> None:
        self.steps = 0

    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        return ()

    def step(self, turn: Turn) -> ToolCall | Reply:
        self.steps += 1
        assert self.steps <= 100, "the turn did not end"
        return ToolCall("duplicate-payment-rate")


class DeniedThenUnplanned:
    """Plans the write the policy denies, makes it, then an unplanned call, then replies: nothing
    runs, and the policy's refusal names the turn's reason."""

    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        return (ToolCall("chicago", "propose_entry"),)

    def step(self, turn: Turn) -> ToolCall | Reply:
        calls = [ToolCall("chicago", "propose_entry"), ToolCall("duplicate-payment-rate")]
        return calls[len(turn.results)] if len(turn.results) < len(calls) else Reply("none ran")


class ShownThenFails:
    """Calls the verified query, then raises once it has seen the rows."""

    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        return (ToolCall("duplicate-payment-rate"),)

    def step(self, turn: Turn) -> ToolCall | Reply:
        if not turn.results:
            return ToolCall("duplicate-payment-rate")
        raise TimeoutError("the model's endpoint timed out")


def test_each_turn_writes_one_record_with_its_outcome(as_persona: AsPersona, audit: Audit) -> None:
    with pytest.raises(ValueError, match="persona"):
        ask(None, QUESTION, as_persona=as_persona, audit=audit)
    answers = [
        ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit),
        ask(CHICAGO, UNANSWERED, as_persona=as_persona, audit=audit),
        ask(DENIED, QUESTION, as_persona=as_persona, audit=audit),
        ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=Looping()),
        ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=Unplanned()),
        ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=Wandering()),
        ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=DeniedThenUnplanned()),
    ]
    with pytest.raises(TimeoutError):
        ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=ShownThenFails())
    assert isinstance(audit.log, FileLog)
    records = read_log(audit.log.path.read_bytes())
    verify_chain(records)
    assert [(r["outcome"], r.get("reason"), len(r["runs"])) for r in records] == [
        ("refused", "no-identity", 0),
        ("answered", None, 1),
        ("abstained", None, 0),
        ("refused", "policy", 0),
        ("stopped", "model-call-cap", MAX_TOOL_CALLS),
        ("refused", "unplanned", 0),
        ("stopped", "model-refusal-cap", 0),
        ("refused", "policy", 0),
        ("failed", "model-error", 1),
    ]
    for record in records:
        assert list(SCHEMA.iter_errors(record)) == [], record["seq"]
        opened = json.loads((audit.log.kept / f"{record['seq']}.json").read_text())
        assert set(opened) == sealed_paths(record)
        for path, (salt, value) in opened.items():
            assert field(record, path) == seal(salt, value)
    for answer, record in zip(answers, records[1:-1], strict=True):
        opened = json.loads((audit.log.kept / f"{record['seq']}.json").read_text())
        assert opened["question"][1] == answer.question
        assert opened["answer"][1] == answer.text
    assert records[1]["metrics"] == [{"name": "duplicate_payment_rate", "version": 1}]
    assert records[1]["asker"]["persona"] == CHICAGO and records[1]["runs"][0]["as"] == CHICAGO


def leaves(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        return [leaf for item in value.values() for leaf in leaves(item)]
    if isinstance(value, list | tuple):
        return [leaf for item in value for leaf in leaves(item)]
    return [value]


def test_the_log_holds_no_sealed_value_in_clear(produced: FileLog) -> None:
    """The question, the answer's text and every result cell appear nowhere in the log's bytes,
    and no bound value appears there as a JSON string; each is in the kept values instead."""
    text = produced.path.read_text()
    for record in read_log(produced.path.read_bytes()):
        opened = json.loads((produced.kept / f"{record['seq']}.json").read_text())
        for path, (_, value) in opened.items():
            # A result's column names are the SQL's own, which the record holds in clear.
            for leaf in leaves(value["rows"] if path.endswith("/result") else value):
                if leaf is None:
                    continue
                if path.endswith("/parameters"):
                    assert json.dumps(leaf) not in text, path
                else:
                    assert str(leaf) not in text, path


# Fail closed


class Broken:
    """A sink that fails with a message carrying the turn's values, as a real one might."""

    def __init__(self) -> None:
        self.seen: list[Mapping[str, Any]] = []

    def append(self, body: Mapping[str, Any], kept: Mapping[str, tuple[str, Any]]) -> Any:
        self.seen.append(kept)
        raise OSError(f"no space left writing {json.dumps({p: v for p, (_, v) in kept.items()})}")


@pytest.mark.parametrize(
    ("who", "question", "model"),
    [
        (CHICAGO, QUESTION, None),
        (CHICAGO, UNANSWERED, None),
        (DENIED, QUESTION, None),
        (CHICAGO, QUESTION, Looping()),
        (CHICAGO, QUESTION, Unplanned()),
        (CHICAGO, QUESTION, Wandering()),
        ("nobody-at-all", QUESTION, None),
        (CHICAGO, QUESTION, ShownThenFails()),
    ],
    ids=[
        "answered",
        "abstained",
        "policy",
        "stopped",
        "unplanned",
        "refusal-cap",
        "no-identity",
        "failed",
    ],
)
def test_a_broken_log_sink_returns_no_answer(
    who: str, question: str, model: Any, as_persona: AsPersona, audit: Audit
) -> None:
    sink = Broken()
    broken = Audit(sink, audit.release, audit.data)
    with pytest.raises(AuditWriteFailed) as raised:
        ask(who, question, as_persona=as_persona, audit=broken, model=model)
    (kept,) = sink.seen
    said = str(raised.value)
    assert "OSError" in said
    held = [str(leaf) for _, value in kept.values() for leaf in leaves(value) if leaf is not None]
    assert [leaf for leaf in held if len(leaf) > 3 and leaf in said] == []
    assert raised.value.__cause__ is None and raised.value.__context__ is None


def test_a_line_write_that_fails_part_way_leaves_the_log_as_it_was(
    as_persona: AsPersona, audit: Audit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Half the record's line reaches the file, then the write raises: the log is truncated back,
    its chain verifies, and the next turn takes the position the failed one would have."""
    assert isinstance(audit.log, FileLog)
    ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit)
    before = audit.log.path.read_bytes()
    real = agent.audit._write_all

    def torn(fd: int, data: bytes) -> None:
        if not data.endswith(b"\n"):
            return real(fd, data)  # the kept values' file, which is written in full
        real(fd, data[: len(data) // 2])
        raise OSError("the disk went away")

    monkeypatch.setattr(agent.audit, "_write_all", torn)
    with pytest.raises(AuditWriteFailed):
        ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit)
    assert audit.log.path.read_bytes() == before
    assert not (audit.log.kept / "1.json").exists()
    monkeypatch.setattr(agent.audit, "_write_all", real)
    ask(UK, QUESTION, as_persona=as_persona, audit=audit)
    records = read_log(audit.log.path.read_bytes())
    verify_chain(records)
    assert [r["asker"]["persona"] for r in records] == [CHICAGO, UK]
    opened = json.loads((audit.log.kept / "1.json").read_text())
    assert all(field(records[1], p) == seal(s, v) for p, (s, v) in opened.items())


def test_a_torn_log_is_refused(as_persona: AsPersona, audit: Audit) -> None:
    assert isinstance(audit.log, FileLog)
    ask(CHICAGO, UNANSWERED, as_persona=as_persona, audit=audit)
    with audit.log.path.open("ab") as log:
        log.write(b'{"seq":1')
    before = audit.log.path.read_bytes()
    with pytest.raises(AuditWriteFailed, match="ChainBroken"):
        ask(CHICAGO, UNANSWERED, as_persona=as_persona, audit=audit)
    assert audit.log.path.read_bytes() == before


def test_a_log_edited_in_place_is_refused(as_persona: AsPersona, audit: Audit) -> None:
    assert isinstance(audit.log, FileLog)
    ask(CHICAGO, UNANSWERED, as_persona=as_persona, audit=audit)
    rewrite(audit.log.path, lambda r: put(r[0], "outcome", "refused"), rechain=False)
    with pytest.raises(AuditWriteFailed, match="ChainBroken"):
        ask(CHICAGO, UNANSWERED, as_persona=as_persona, audit=audit)


def test_kept_values_that_cannot_be_written_leave_no_record(
    as_persona: AsPersona, audit: Audit
) -> None:
    assert isinstance(audit.log, FileLog)
    audit.log.kept.write_text("a file where the folder of kept values goes")
    with pytest.raises(AuditWriteFailed):
        ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit)
    assert audit.log.path.read_bytes() == b""


@pytest.mark.parametrize(
    ("release", "data"),
    [
        ("not a commit", None),
        (None, {"build": {}, "sources": []}),
        (None, {"note": "Chicago paid 1234.50"}),
    ],
    ids=["release", "build", "free-text"],
)
def test_a_record_the_schema_refuses_is_not_written_and_its_answer_not_given(
    release: str | None, data: Any, as_persona: AsPersona, audit: Audit
) -> None:
    assert isinstance(audit.log, FileLog)
    wrong_audit = Audit(audit.log, release or audit.release, data or audit.data)
    with pytest.raises(AuditWriteFailed, match="RecordRefused") as raised:
        ask(CHICAGO, QUESTION, as_persona=as_persona, audit=wrong_audit)
    assert "1234" not in str(raised.value)
    assert audit.log.path.read_bytes() == b""
    assert not audit.log.kept.exists()


def test_a_log_that_cannot_be_opened_returns_no_answer(as_persona: AsPersona, audit: Audit) -> None:
    assert isinstance(audit.log, FileLog)
    unopenable = Audit(FileLog(audit.log.path.parent, audit.log.kept), audit.release, audit.data)
    with pytest.raises(AuditWriteFailed, match="IsADirectoryError"):
        ask(CHICAGO, QUESTION, as_persona=as_persona, audit=unopenable)


def test_every_verified_query_bound_gives_the_rows_of_its_text(warehouse: Path) -> None:
    """The tool sends each verified query with its values lifted to parameters; on the sample
    warehouse each gives the rows its committed text gives, compared in any order."""
    with duckdb.connect(str(warehouse), read_only=True) as db:
        for query in load_context().values():
            text, values = bind(query.sql, "duckdb")
            assert sorted(map(repr, db.execute(text, values or []).fetchall())) == sorted(
                map(repr, db.execute(query.sql).fetchall())
            ), query.id


# What a failed append leaves behind

# An invented marker asked inside the question, so the clear values of the turn can be searched for.
MARKER = "INVENTED-MARKER-7f3a"


def opened_path(fd: int) -> str:
    return os.readlink(f"/proc/self/fd/{fd}")


def failing(case: str, log: FileLog, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make one step of the next append raise."""
    write, fsync = agent.audit._write_all, os.fsync

    def write_failing(fd: int, data: bytes) -> None:
        # The log's line ends in a newline; the kept values' file does not.
        if data.endswith(b"\n") == (case == "the line's write"):
            write(fd, data[: len(data) // 2])
            raise OSError("the disk went away")
        write(fd, data)

    def fsync_failing(fd: int) -> None:
        path = opened_path(fd)
        if (
            (case == "the line's fsync" and path == str(log.path))
            or (case == "the temporary file's fsync" and path.endswith(".tmp"))
            or (case == "the folder's fsync" and path == str(log.kept))
        ):
            raise OSError("fsync failed")
        fsync(fd)

    def replace_failing(*_: Any) -> None:
        raise OSError("rename failed")

    if case.endswith("write"):
        monkeypatch.setattr(agent.audit, "_write_all", write_failing)
    elif case.endswith("fsync"):
        monkeypatch.setattr(os, "fsync", fsync_failing)
    else:
        monkeypatch.setattr(os, "replace", replace_failing)


@pytest.mark.parametrize(
    "case",
    [
        "the line's write",
        "the line's fsync",
        "the temporary file's write",
        "the temporary file's fsync",
        "the rename",
        "the folder's fsync",
    ],
)
def test_a_failed_append_leaves_no_value_of_its_turn_on_disk(
    case: str, as_persona: AsPersona, audit: Audit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One step of the append raises: no answer, the log as it was, and no file under the folder
    of kept values holds the marker the question carried; the next append then succeeds."""
    assert isinstance(audit.log, FileLog)
    ask(CHICAGO, UNANSWERED, as_persona=as_persona, audit=audit)
    before = audit.log.path.read_bytes()
    with monkeypatch.context() as patch:
        failing(case, audit.log, patch)
        with pytest.raises(AuditWriteFailed):
            ask(CHICAGO, f"{QUESTION} {MARKER}", as_persona=as_persona, audit=audit)
    assert audit.log.path.read_bytes() == before
    held = [f.name for f in audit.log.kept.rglob("*") if MARKER.encode() in f.read_bytes()]
    assert held == [], case
    assert sorted(f.name for f in audit.log.kept.iterdir()) == ["0.json"]
    ask(CHICAGO, UNANSWERED, as_persona=as_persona, audit=audit)
    verify_chain(read_log(audit.log.path.read_bytes()))


# The writer's own refusals, past the schema


@pytest.fixture
def written(as_persona: AsPersona, audit: Audit) -> tuple[dict[str, Any], dict[str, Any]]:
    """A record `ask` wrote, without its header, and its kept values: a body the writer takes."""
    assert isinstance(audit.log, FileLog)
    ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit)
    (record,) = read_log(audit.log.path.read_bytes())
    kept = {p: tuple(v) for p, v in json.loads((audit.log.kept / "0.json").read_text()).items()}
    return {k: v for k, v in record.items() if k not in ("seq", "prev", "digest")}, kept


def wrong_body(case: str, body: dict[str, Any], kept: dict[str, Any]) -> None:
    if case == "a time that is no real instant":
        body["at"] = "2026-02-30T10:00:05.000000Z"
    elif case == "SQL with a value in clear":
        body["runs"][0]["query"]["text"] = "select count(*) from payments where entity_id = 'x'"
    elif case == "a missing kept value":
        del kept["answer"]
    elif case == "an extra kept value":
        kept["note"] = (new_salt(), "an invented note")
    elif case == "a kept value that does not open its seal":
        kept["question"] = (new_salt(), kept["question"][1])
    else:
        raise AssertionError(case)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("a time that is no real instant", "no real instant"),
        ("SQL with a value in clear", "value in clear"),
        ("a missing kept value", "kept values for"),
        ("an extra kept value", "kept values for"),
        ("a kept value that does not open its seal", "does not open its seal"),
    ],
)
def test_the_writer_refuses_a_record_the_schema_lets_through(
    case: str, message: str, written: tuple[dict[str, Any], dict[str, Any]], tmp_path: Path
) -> None:
    body, kept = written
    log = FileLog(tmp_path / "other.jsonl", tmp_path / "other-kept")
    log.append(copy_of(body), dict(kept))
    changed, changed_kept = copy_of(body), dict(kept)
    wrong_body(case, changed, changed_kept)
    before = log.path.read_bytes()
    with pytest.raises(RecordRefused, match=message):
        log.append(changed, changed_kept)
    assert log.path.read_bytes() == before
    assert sorted(f.name for f in log.kept.iterdir()) == ["0.json"]


def copy_of(value: Any) -> Any:
    return json.loads(json.dumps(value))


# The folder of kept values


@pytest.mark.parametrize("mode", [0o750, 0o705, 0o755])
def test_a_folder_of_kept_values_open_to_others_is_refused(
    mode: int, as_persona: AsPersona, audit: Audit
) -> None:
    assert isinstance(audit.log, FileLog)
    audit.log.kept.mkdir()
    os.chmod(audit.log.kept, mode)
    with pytest.raises(AuditWriteFailed, match="KeptFolderRefused"):
        ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit)
    assert stat.S_IMODE(audit.log.kept.stat().st_mode) == mode, "refused, not changed"
    assert list(audit.log.kept.iterdir()) == []
    assert audit.log.path.read_bytes() == b""


def test_a_folder_of_kept_values_that_is_a_link_is_refused(
    as_persona: AsPersona, audit: Audit, tmp_path: Path
) -> None:
    assert isinstance(audit.log, FileLog)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    audit.log.kept.symlink_to(elsewhere)
    with pytest.raises(AuditWriteFailed, match="KeptFolderRefused"):
        ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit)
    assert list(elsewhere.iterdir()) == []


def test_a_leftover_temporary_file_is_replaced_not_followed(
    as_persona: AsPersona, audit: Audit, tmp_path: Path
) -> None:
    """A temporary file left at the position's name, a link to a file elsewhere, is removed and
    the kept values written to a new file of mode 0600; the linked file is untouched."""
    assert isinstance(audit.log, FileLog)
    audit.log.kept.mkdir(mode=0o700)
    target = tmp_path / "planted"
    target.write_text("planted")
    os.chmod(target, 0o644)
    (audit.log.kept / ".0.json.tmp").symlink_to(target)
    ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit)
    assert target.read_text() == "planted"
    kept = audit.log.kept / "0.json"
    assert not kept.is_symlink() and stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert sorted(f.name for f in audit.log.kept.iterdir()) == ["0.json"]


def test_a_temporary_link_that_survives_to_the_open_is_refused_not_followed(
    as_persona: AsPersona, audit: Audit, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the removal of a leftover temporary file made to do nothing, the planted link reaches
    the open, which refuses it (`O_EXCL`, `O_NOFOLLOW`): no answer, nothing written through it."""
    assert isinstance(audit.log, FileLog)
    audit.log.kept.mkdir(mode=0o700)
    target = tmp_path / "planted"
    target.write_text("planted")
    link = audit.log.kept / ".0.json.tmp"
    link.symlink_to(target)
    unlink = Path.unlink

    def kept_links(self: Path, missing_ok: bool = False) -> None:
        if self != link:
            unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", kept_links)
    with pytest.raises(AuditWriteFailed, match="FileExistsError"):
        ask(CHICAGO, f"{QUESTION} {MARKER}", as_persona=as_persona, audit=audit)
    assert target.read_text() == "planted"
    assert audit.log.path.read_bytes() == b""
    assert not (audit.log.kept / "0.json").exists()


# Strings in other quoting forms


@pytest.mark.parametrize(
    "sql",
    [
        "select count(*) from payments where entity_id = N'x'",
        "select count(*) from payments where entity_id = e'x'",
        "select count(*) from payments where entity_id = $$x$$",
    ],
)
def test_bind_refuses_a_string_it_would_leave_in_clear(sql: str) -> None:
    with pytest.raises(UnboundValue, match="string form"):
        bind(sql, "duckdb")


# A turn that raises


def failed_record(audit: Audit) -> tuple[dict[str, Any], dict[str, Any]]:
    """The log's one record, checked with its seals opened, and its kept values."""
    assert isinstance(audit.log, FileLog)
    (record,) = read_log(audit.log.path.read_bytes())
    verify_chain([record])
    assert list(SCHEMA.iter_errors(record)) == []
    opened = json.loads((audit.log.kept / "0.json").read_text())
    assert set(opened) == sealed_paths(record) and "answer" not in record
    for path, (salt, value) in opened.items():
        assert field(record, path) == seal(salt, value)
    return record, opened


def test_a_model_that_raises_after_a_run_leaves_a_failed_record_of_that_run(
    as_persona: AsPersona, audit: Audit
) -> None:
    with pytest.raises(TimeoutError, match="endpoint timed out") as raised:
        ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=ShownThenFails())
    assert type(raised.value) is TimeoutError
    record, opened = failed_record(audit)
    assert (record["outcome"], record["reason"]) == ("failed", "model-error")
    (run,) = record["runs"]
    assert run["as"] == CHICAGO
    ((share,),) = opened["runs/0/result"][1]["rows"]
    assert 0 <= float(share) <= 1
    assert record["metrics"] == [{"name": "duplicate_payment_rate", "version": 1}]


def test_a_connection_maker_that_raises_leaves_a_failed_record(audit: Audit) -> None:
    def refusing(persona: str) -> duckdb.DuckDBPyConnection:
        raise NoPersonaError("the connection-maker knows no such persona")

    with pytest.raises(NoPersonaError, match="connection-maker"):
        ask(CHICAGO, QUESTION, as_persona=refusing, audit=audit)
    record, _ = failed_record(audit)
    assert (record["outcome"], record["reason"], record["runs"]) == (
        "failed",
        "connection-error",
        [],
    )


def test_a_policy_that_raises_leaves_a_failed_record(
    as_persona: AsPersona, audit: Audit, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raising(*_: Any) -> Any:
        raise RuntimeError("the policy engine failed to load")

    monkeypatch.setattr(agent.ask, "authorize", raising)
    with pytest.raises(RuntimeError, match="policy engine"):
        ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit)
    record, _ = failed_record(audit)
    assert (record["outcome"], record["reason"]) == ("failed", "policy-error")


class NoCursor:
    """A connection whose cursor raises an error that is not the engine's."""

    def cursor(self) -> Any:
        raise RuntimeError("the connection was handed over to another thread")


def test_a_tool_that_raises_leaves_a_failed_record(audit: Audit) -> None:
    def handed_over(persona: str) -> Any:
        return NoCursor()

    with pytest.raises(RuntimeError, match="another thread"):
        ask(CHICAGO, QUESTION, as_persona=handed_over, audit=audit)
    record, _ = failed_record(audit)
    assert (record["outcome"], record["reason"]) == ("failed", "tool-error")


def test_a_failed_turn_whose_record_cannot_be_written_raises_only_the_write_failure(
    as_persona: AsPersona, audit: Audit
) -> None:
    sink = Broken()
    with pytest.raises(AuditWriteFailed) as raised:
        ask(
            CHICAGO,
            QUESTION,
            as_persona=as_persona,
            audit=Audit(sink, audit.release, audit.data),
            model=ShownThenFails(),
        )
    (kept,) = sink.seen
    assert "answer" not in kept and "runs/0/result" in kept
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert "timed out" not in str(raised.value) and QUESTION not in str(raised.value)
