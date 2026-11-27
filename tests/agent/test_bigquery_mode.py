"""The own agent's BigQuery mode (`agent/bigquery.py`), with no GCP call.

Three kinds of proof:
- against a fake BigQuery at httpx's transport and a fake mint, each job the agent sends is read
  as it leaves: its byte cap is the bytes left of the turn within the persona queries' cap, its
  labels tie it to the turn, a job refused at the cap stops the turn and is sent once, and no
  query runs on a connection BigQuery would run as anyone but the asking persona;
- `make bigquery-agent`'s pytest run, in a child process with no network, no gcloud and no
  inherited variable, over the stand-in for the lab's BigQuery (`tests/access/bigquery_stub.py`)
  serving the samples' build under its row access policies and grants: the BigQuery parameters of
  the controls' proving tests pass, and one planted fault per way a query could run on another
  identity or past its cap turns exactly the tests it touches red;
- the declared configuration: no dataset the agent's queries name is one `platform/gcp` grants
  the agent account anything on.
The live test here (`test_each_job_of_a_turn_carries_its_labels`) runs on the owner's machine.
"""

import json
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from itertools import takewhile
from pathlib import Path
from typing import Any

import httpx
import pytest
from gcp_project import lab_project_or_none

from access.bigquery import AGENT_VARIABLE, MAX_BYTES_BILLED, PersonaQueryError, PersonaSession
from agent.ask import AGENT, Audit, NoPersonaError, ask, load_context
from agent.audit import FileLog, read_log
from agent.bigquery import (
    LABEL_KEY,
    LABEL_VALUE,
    BigQueryConnection,
    Unplaced,
    WrongIdentity,
    connector,
    job_labels,
    label_value,
    load_catalogues,
    prepare,
    relations,
)
from agent.limits import BYTES_CAP, load_limits
from agent.model import Reply, ToolCall, Turn, VerifiedQuery, model_from_config
from tests.conftest import REPO

PROJECT = "the-lab"
AGENT_ID = "the-agent"
CHICAGO = "accountant-chicago"
UK = "accountant-uk-transport"
QUESTION = "What share of the City of Chicago's served payment rows repeat another served row?"
MIB = 2**20


def account(persona: str) -> str:
    return f"{persona}@{PROJECT}.iam.gserviceaccount.com"


class Fake:
    """A fake BigQuery at httpx's transport and a fake gcloud mint: a token names the account it
    was minted for, `SELECT SESSION_USER()` answers the account a token names (or `identity`
    when set), each other job bills `bill` bytes, is refused over its cap like BigQuery, and
    answers one row; every request and mint is kept."""

    def __init__(self, bill: int | None = 10 * MIB, identity: str | None = None) -> None:
        self.bill = bill
        self.identity = identity
        self.bodies: list[dict[str, Any]] = []
        self.tokens: list[str] = []
        self.mints: list[list[str]] = []
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def mint(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        accounts = command[3].split("=", 1)[1].split(",")
        self.mints.append(accounts)
        return subprocess.CompletedProcess(
            list(command), 0, f"token-{len(self.mints)}:{accounts[-1]}", ""
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        token = request.headers["authorization"].removeprefix("Bearer ")
        self.tokens.append(token)
        reference = {"projectId": PROJECT, "location": "EU", "jobId": f"job_{len(self.bodies)}"}
        if body["query"] == "SELECT SESSION_USER()":
            who = self.identity if self.identity is not None else token.split(":", 1)[1]
            rows = [{"f": [{"v": who}]}] if who else []
            return httpx.Response(
                200,
                json={
                    "jobComplete": True,
                    "jobReference": reference,
                    "rows": rows,
                    "totalBytesBilled": "0",
                    "schema": {"fields": [{"name": "f0_", "type": "STRING"}]},
                },
            )
        if self.bill is not None and self.bill > int(body["maximumBytesBilled"]):
            error = {"reason": "bytesBilledLimitExceeded", "message": "over"}
            return httpx.Response(400, json={"error": {"errors": [error]}})
        answer: dict[str, Any] = {
            "jobComplete": True,
            "jobReference": reference,
            "schema": {"fields": [{"name": "total", "type": "FLOAT"}]},
            "rows": [{"f": [{"v": "0.25"}]}],
        }
        if self.bill is not None:
            answer["totalBytesBilled"] = str(self.bill)
        return httpx.Response(200, json=answer)

    def connect(self, env: dict[str, str] | None = None) -> Callable[[str], BigQueryConnection]:
        given = {AGENT_VARIABLE: AGENT_ID} if env is None else env
        return connector(PROJECT, self.client, env=given, run=self.mint)

    @property
    def queries(self) -> list[dict[str, Any]]:
        return [b for b in self.bodies if b["query"] != "SELECT SESSION_USER()"]


@pytest.fixture
def fake() -> Iterator[Fake]:
    made = Fake()
    yield made
    made.client.close()


# Invented queries over the payments table every accountant reads, each billing one table.
TOTALS = {
    f"total-{n}": VerifiedQuery(
        f"total-{n}",
        f"What do the payments add up to, take {n}?",
        "invented_metric",
        1,
        "select sum(amount) as total from payments where entity_id = 'chicago'",
    )
    for n in range(1, 4)
}


class Calls:
    """A model that plans the given calls, makes them in order, then replies."""

    def __init__(self, *calls: str) -> None:
        self.calls = [ToolCall(c) for c in calls]

    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        return tuple(self.calls)

    def step(self, turn: Turn) -> ToolCall | Reply:
        made = len(turn.results)
        return self.calls[made] if made < len(self.calls) else Reply("done")


def records(audit: Audit) -> list[dict[str, Any]]:
    assert isinstance(audit.log, FileLog)
    return read_log(audit.log.path.read_bytes())


def asked(fake: Fake, audit: Audit, per_turn: int, *calls: str, persona: str = CHICAGO) -> Any:
    limits = replace(load_limits(), bytes_billed_per_turn=per_turn)
    return ask(
        persona,
        QUESTION,
        as_persona=fake.connect(),
        audit=audit,
        model=Calls(*calls),
        queries=TOTALS,
        limits=limits,
    )


def test_every_job_carries_the_bytes_left_of_the_turn_and_the_turns_labels(
    fake: Fake, audit: Audit
) -> None:
    """The identity check, then three queries billing 10 MiB each: each job's cap is what is left
    of the turn, never more than the persona queries' cap, and each carries the turn's labels."""
    per_turn = MAX_BYTES_BILLED + 15 * MIB
    answer = asked(fake, audit, per_turn, *TOTALS)
    assert len(answer.citations) == 3 and answer.citations[0].rows == ((0.25,),)
    caps = [int(b["maximumBytesBilled"]) for b in fake.bodies]
    left = [per_turn, per_turn, per_turn - 10 * MIB, per_turn - 20 * MIB]
    assert caps == [min(n, MAX_BYTES_BILLED) for n in left]
    (record,) = records(audit)
    labels = job_labels(AGENT, record["asker"]["session"], record["asker"]["turn"])
    assert labels == {"agent": "own-agent", "conversation": record["asker"]["session"], "turn": "1"}
    for body in fake.bodies:
        assert body["labels"] == labels
        assert body["jobCreationMode"] == "JOB_CREATION_REQUIRED"
        assert (body["useQueryCache"], body["useLegacySql"]) == (False, False)
    for body in fake.queries:
        assert body["parameterMode"] == "NAMED"
        assert body["queryParameters"] == [
            {
                "name": "v1",
                "parameterType": {"type": "STRING"},
                "parameterValue": {"value": "chicago"},
            }
        ]
        assert "chicago" not in body["query"]
        assert f"`{PROJECT}.payment_review.payments_masked`" in body["query"]
    assert [(r["engine"], r["as"], r["job"]) for r in record["runs"]] == [
        ("bigquery", CHICAGO, f"{PROJECT}:EU.job_{n}") for n in (2, 3, 4)
    ]
    assert record["outcome"] == "answered"


def test_a_job_refused_at_the_cap_stops_the_turn_and_is_sent_once(fake: Fake, audit: Audit) -> None:
    """With 15 MiB a turn, the second query's job carries 5 MiB, which its bill passes: BigQuery
    refuses it before it runs, the turn stops at the byte cap, and the third is never sent; the
    refused job is not sent again, nor in parts."""
    answer = asked(fake, audit, 15 * MIB, *TOTALS)
    assert [int(b["maximumBytesBilled"]) for b in fake.queries] == [15 * MIB, 5 * MIB]
    assert [c.query for c in answer.citations] == ["total-1"]
    assert [r.query for r in answer.refusals] == ["total-2"]
    assert "BigQuery refused it before it ran" in answer.refusals[0].reason
    (record,) = records(audit)
    assert (record["outcome"], record["reason"], len(record["runs"])) == ("stopped", BYTES_CAP, 1)


def test_with_no_bytes_left_no_job_is_sent(fake: Fake, audit: Audit) -> None:
    answer = asked(fake, audit, 10 * MIB, *TOTALS)
    assert len(fake.queries) == 1
    assert "no bytes are left" in answer.refusals[0].reason
    (record,) = records(audit)
    assert (record["outcome"], record["reason"]) == ("stopped", BYTES_CAP)


def test_a_job_whose_answer_names_no_bill_counts_its_whole_cap(audit: Audit) -> None:
    fake = Fake(bill=None)
    per_turn = MAX_BYTES_BILLED + 50 * MIB
    asked(fake, audit, per_turn, "total-1", "total-2")
    assert [int(b["maximumBytesBilled"]) for b in fake.queries] == [MAX_BYTES_BILLED, 50 * MIB]


@pytest.mark.parametrize("cap", [0, -1, MAX_BYTES_BILLED + 1])
def test_a_job_cap_above_the_persona_cap_or_not_above_zero_is_refused(fake: Fake, cap: int) -> None:
    session = PersonaSession(PROJECT, "token", fake.client)
    with pytest.raises(PersonaQueryError, match="byte cap"):
        session.query("SELECT 1", max_bytes_billed=cap)
    assert fake.bodies == []


@pytest.mark.parametrize(
    "seen",
    [account(AGENT_ID), account(UK), ""],
    ids=["the-agents-own-account", "another-personas-account", "no-answer"],
)
def test_a_connection_bigquery_runs_as_another_identity_runs_no_query(
    audit: Audit, seen: str
) -> None:
    """The agent's own token (as the cloud proxy's replacement carries it), a token kept from
    another persona's turn, or an identity check answering nothing: the turn raises before any
    query, its record failed for the connection."""
    fake = Fake(identity=seen)
    with pytest.raises(WrongIdentity) as raised:
        asked(fake, audit, 15 * MIB, "total-1")
    assert AGENT_ID not in str(raised.value) and PROJECT not in str(raised.value)
    assert [b["query"] for b in fake.bodies] == ["SELECT SESSION_USER()"]
    (record,) = records(audit)
    assert (record["outcome"], record["reason"], record["runs"]) == (
        "failed",
        "connection-error",
        [],
    )
    fake.client.close()


def test_a_connection_made_for_another_persona_is_refused_before_any_job(
    fake: Fake, audit: Audit
) -> None:
    other = fake.connect()(UK)
    with pytest.raises(WrongIdentity):
        ask(
            CHICAGO,
            QUESTION,
            as_persona=lambda _: other,
            audit=audit,
            model=Calls("total-1"),
            queries=TOTALS,
        )
    assert fake.bodies == []


def test_a_failed_mint_sends_nothing_and_answers_nothing(fake: Fake, audit: Audit) -> None:
    def failing(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(command), 1, "", "denied")

    connect = connector(PROJECT, fake.client, env={AGENT_VARIABLE: AGENT_ID}, run=failing)
    with pytest.raises(PersonaQueryError, match="minting the persona's token failed"):
        ask(
            CHICAGO,
            QUESTION,
            as_persona=connect,
            audit=audit,
            model=Calls("total-1"),
            queries=TOTALS,
        )
    assert fake.bodies == []
    (record,) = records(audit)
    assert (record["outcome"], record["reason"]) == ("failed", "connection-error")


def test_each_connection_mints_its_own_token_for_the_persona_through_the_agent(fake: Fake) -> None:
    connect = fake.connect()
    for persona in (CHICAGO, UK, CHICAGO):
        connect(persona)
    assert fake.mints == [[account(AGENT_ID), account(p)] for p in (CHICAGO, UK, CHICAGO)]
    connector(PROJECT, fake.client, env={}, run=fake.mint)(UK)
    assert fake.mints[-1] == [account(UK)]


MISSING: list[Any] = [None, "", "   ", 7, "nobody", f"{CHICAGO} ", "​"]


def test_a_question_without_a_persona_mints_nothing_and_sends_nothing(
    fake: Fake, audit: Audit
) -> None:
    for missing in MISSING:
        with pytest.raises(NoPersonaError):
            ask(
                missing,
                QUESTION,
                as_persona=fake.connect(),
                audit=audit,
                model=model_from_config({}, load_context()),
            )
    assert (fake.mints, fake.bodies) == ([], [])


@pytest.mark.parametrize(
    "session",
    ["0" * 32, "a" * 63, "a" * 64, "b" * 64, "x-1" * 21 + "z"],
)
def test_job_labels_follow_bigquerys_label_rules(session: str) -> None:
    labels = job_labels(AGENT, session, 12)
    assert len(labels) <= 64
    assert all(LABEL_KEY.fullmatch(k) and LABEL_VALUE.fullmatch(v) for k, v in labels.items())
    if len(session) <= 63:
        assert labels["conversation"] == session
    else:
        assert labels["conversation"].startswith("_") and len(labels["conversation"]) == 63
    assert label_value("a" * 64) != label_value("b" * 64)


def unsent(request: httpx.Request) -> httpx.Response:
    raise AssertionError("placing a query sends nothing")


# A session no test here sends anything on: placing a query reads only the relations.
OFFLINE = PersonaSession(PROJECT, "unused", httpx.Client(transport=httpx.MockTransport(unsent)))


def placed(persona: str, sql: str) -> tuple[str, dict[str, Any]]:
    connection = BigQueryConnection(
        persona, PROJECT, OFFLINE, relations(persona, load_catalogues())
    )
    return prepare(sql, connection)


def test_a_query_is_placed_where_the_persona_reads_its_tables() -> None:
    sql = load_context()["duplicate-payment-rate"].sql
    masked, _ = placed(CHICAGO, sql)
    clear, parameters = placed("payables-clerk", sql)
    assert f"`{PROJECT}.payment_review.payments_masked` AS payments" in masked
    assert f"`{PROJECT}.payment_review.payments` AS payments" in clear
    assert parameters == {"v1": "chicago"} and "@v1" in clear


@pytest.mark.parametrize(
    ("sql", "words"),
    [
        ("select count(*) from chicago_contract_reconciliation", "no table named"),
        ("select count(*) from payment_review.payments", "qualified"),
        ("select count(*) from intermediate.payments", "qualified"),
        ("select count(*) from payments_masked", "no table named"),
    ],
)
def test_a_table_the_persona_reads_through_no_relation_is_refused(sql: str, words: str) -> None:
    with pytest.raises(Unplaced, match=words):
        placed(CHICAGO, sql)


def test_a_name_with_two_places_is_refused() -> None:
    connection = BigQueryConnection(CHICAGO, PROJECT, OFFLINE, {"t": (("a", "t"), ("b", "t"))})
    with pytest.raises(Unplaced, match="two datasets"):
        prepare("select 1 as x from t", connection)


def test_no_dataset_the_agents_queries_name_is_granted_to_the_agent_account() -> None:
    """The agent account's declared rights in `platform/gcp`: its one dataset role, the builder
    role on the datasets the models build (`tests/platform/test_access_rules.py` holds that list
    of grants exact); the agent's BigQuery queries name only purpose and aggregate datasets, none
    of those, so its own token, were it to carry one of them, would be refused there. The builder
    role itself reads every table of the built datasets: the agent account is not without a data
    role, only without one where the agent's queries read."""
    from tests.platform.test_access_rules import (
        DATASET_ACCESS,
        expected_aggregate_datasets,
        expected_built,
        expected_purpose_datasets,
    )

    agents = {n: v for n, v in DATASET_ACCESS.items() if "google_service_account.agent" in v[3]}
    assert agents == {
        "agent_builds_warehouse": (
            "${local.built}",
            "${google_bigquery_dataset.warehouse[each.key].dataset_id}",
            "${google_project_iam_custom_role.warehouse_builder.name}",
            "${data.google_service_account.agent.email}",
        )
    }
    views = set(expected_purpose_datasets().values()) | set(expected_aggregate_datasets().values())
    catalogues = load_catalogues()
    named = {
        dataset
        for persona in (p["id"] for p in catalogues.access["personas"])
        for places in relations(persona, catalogues).values()
        for dataset, _ in places
    }
    assert named and named <= views
    assert named & expected_built() == set()


# The live test of the labels, which `make bigquery-agent` runs on the owner's machine.
@pytest.mark.bigquery_persona
def test_each_job_of_a_turn_carries_its_labels(
    audit: Audit, request: pytest.FixtureRequest
) -> None:
    """Each job a turn ran, read back by its creator (`jobs.get`), carries the agent, the
    conversation and the turn the turn's audit record names."""
    if lab_project_or_none() is None:
        pytest.skip("LEDGER_LAB_GCP_PROJECT is unset: the lab has no BigQuery to act in")
    maker: Callable[[str], BigQueryConnection] = request.getfixturevalue("agent_on_bigquery")
    opened: list[BigQueryConnection] = []

    def connect(persona: str) -> BigQueryConnection:
        opened.append(maker(persona))
        return opened[-1]

    answer = ask(
        CHICAGO,
        QUESTION,
        as_persona=connect,
        audit=audit,
        model=model_from_config({}, load_context()),
    )
    if not answer.citations:
        pytest.fail("the turn ran no query")
    (record,) = records(audit)
    want = job_labels(AGENT, record["asker"]["session"], record["asker"]["turn"])
    for run in record["runs"]:
        found = re.fullmatch(r"[^:]+:([^.]+)\.(.+)", run["job"])
        if found is None:
            pytest.fail("a run's job reference is not a project, location and id")
        if opened[0].session.job_labels(found[2], found[1]) != want:
            pytest.fail("a job's labels are not the turn's")


def recipe_pytest_arguments() -> list[str]:
    """The pytest arguments `make bigquery-agent` runs, read from its one recipe line."""
    lines = (REPO / "Makefile").read_text().splitlines()
    target = lines.index(next(line for line in lines if line.startswith("bigquery-agent:")))
    (command,) = takewhile(lambda line: line.startswith("\t"), lines[target + 1 :])
    uv, run, *arguments = shlex.split(command)
    assert [uv, run, arguments[0]] == ["uv", "run", "pytest"], command
    return arguments[1:]


def run_over_stub(
    tmp_path: Path, build: Any, fault: str = ""
) -> tuple[int, str, list[dict[str, Any]]]:
    """`make bigquery-agent`'s pytest run over the stand-in, with the fault planted; its exit
    status, its report and the jobs the stand-in ran."""
    bin_dir, config, gcloud_log = tmp_path / "bin", tmp_path / "gcloud-config", tmp_path / "ran"
    bin_dir.mkdir()
    config.mkdir()
    (tmp_path / "home").mkdir()
    (bin_dir / "gcloud").write_text(
        f'#!/bin/sh\necho "$@" >> {shlex.quote(str(gcloud_log))}\nexit 1\n'
    )
    (bin_dir / "gcloud").chmod(0o755)
    # The audit record names the checkout's HEAD, which the child reads with git.
    git = shutil.which("git")
    assert git is not None
    (bin_dir / "git").symlink_to(git)
    jobs = tmp_path / "jobs.jsonl"
    env = {
        "PATH": str(bin_dir),
        "HOME": str(tmp_path / "home"),
        "CLOUDSDK_CONFIG": str(config),
        "LEDGER_LAB_GCP_PROJECT": PROJECT,
        "LEDGER_LAB_AGENT_ACCOUNT": AGENT_ID,
        "STUB_WAREHOUSE": str(build.database),
        "STUB_BUILD": str(build.cwd),
        "STUB_FAULT": fault,
        "STUB_LOG": str(tmp_path / "served.json"),
        "STUB_JOBS": str(jobs),
    }
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *recipe_pytest_arguments(),
            "-p",
            "tests.access.bigquery_stub",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
        env=env,
        timeout=600,
        check=False,
    )
    report = run.stdout + run.stderr
    assert not gcloud_log.exists(), "the child ran gcloud"
    for word in (PROJECT, AGENT_ID, "stub-token", "iam.gserviceaccount.com"):
        assert word not in report, word
    ran = [json.loads(line) for line in jobs.read_text().splitlines()] if jobs.exists() else []
    return run.returncode, report, ran


TWO = "tests/agent/test_ask.py::test_two_personas_asking_the_same_question_get_different_rows"
NONE = "tests/agent/test_ask.py::test_a_question_without_a_persona_gets_no_answer"
RUNAWAY = "tests/agent/test_cost.py::test_runaway_join_stops_at_the_cap"
LABELS = "tests/agent/test_bigquery_mode.py::test_each_job_of_a_turn_carries_its_labels"


def failed(report: str) -> set[str]:
    return {m[1] for m in re.finditer(r"^FAILED (\S+?)(?:\[bigquery\])?(?: |$)", report, re.M)}


def test_the_target_runs_the_controls_proving_tests_and_the_labels_on_bigquery() -> None:
    arguments = recipe_pytest_arguments()
    assert arguments[:2] == ["-m", "bigquery_persona"]
    assert set(arguments[3:]) == {TWO, NONE, RUNAWAY, LABELS}


def test_the_agents_bigquery_mode_passes_over_the_stand_in(tmp_path: Path, build: Any) -> None:
    code, report, jobs = run_over_stub(tmp_path, build)
    assert code == pytest.ExitCode.OK, report[-3000:]
    assert "4 passed" in report, report[-3000:]
    # Every job: capped within the persona queries' cap, labelled as the agent's, created, and
    # run as a persona that asked; the identity check before each turn's first query.
    assert jobs, report[-3000:]
    for job in jobs:
        assert 0 < job["cap"] <= MAX_BYTES_BILLED, job
        assert job["labels"]["agent"] == "own-agent" and job["creation"] == "JOB_CREATION_REQUIRED"
    assert {j["who"] for j in jobs} == {CHICAGO, UK, "group-controller"}
    probes = [j for j in jobs if j["query"] == "SELECT SESSION_USER()"]
    assert len(probes) == 4  # one per turn that opened a connection
    # The runaway join: sent once, refused at the turn's bytes, never split or sent again.
    runaway = [j for j in jobs if j["who"] == "group-controller" and j not in probes]
    assert [j["ended"] for j in runaway] == ["bytesBilledLimitExceeded"]
    assert runaway[0]["cap"] < runaway[0]["bill"]


# Each fault, and the tests it turns red: a token of the agent's own account, a token kept from
# another persona's mint, a failed mint, the UK accountant's row policies opened, and a job run
# past its cap.
FAULTS = {
    "agent-token": {TWO, RUNAWAY, LABELS},
    "cached-token": {TWO, RUNAWAY},
    "mint-fails": {TWO, RUNAWAY, LABELS},
    "rows-open": {TWO},
    "cap-ignored": {RUNAWAY},
}


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_each_planted_fault_turns_exactly_its_tests_red(
    tmp_path: Path, build: Any, fault: str
) -> None:
    code, report, jobs = run_over_stub(tmp_path, build, fault)
    assert code == pytest.ExitCode.TESTS_FAILED, report[-3000:]
    assert failed(report) == FAULTS[fault], report[-3000:]
    # No query ran on a connection whose identity was not the asker's: with the agent's token only
    # identity checks ran, with no token nothing, and with a kept token only the Chicago
    # accountant's own two turns' queries, whose kept token is its own.
    ran = [j["who"] for j in jobs if j["query"] != "SELECT SESSION_USER()"]
    expected = {"agent-token": [], "mint-fails": [], "cached-token": [CHICAGO, CHICAGO]}
    if fault in expected:
        assert ran == expected[fault], jobs
    if fault == "mint-fails":
        assert jobs == []


def test_the_agents_bigquery_mode_refuses_another_labs_session_before_any_mint(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gcp_project import NoLabProjectError

    calls: list[Any] = []
    monkeypatch.setenv("GCP_PROJECT", "another-lab")
    monkeypatch.setenv("LEDGER_LAB_GCP_PROJECT", PROJECT)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: calls.append(a))
    with pytest.raises(NoLabProjectError):
        request.getfixturevalue("agent_on_bigquery")
    assert calls == []
