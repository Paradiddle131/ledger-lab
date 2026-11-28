"""The native mode (`agent/native.py`): fenced to the free window, on the persona's own token, shown
only the persona's tables, each job capped; and one golden question answered live as a persona,
off the cloud proxy, on the owner's machine (`make bigquery-native`)."""

import json
import subprocess
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
import yaml
from gcp_project import lab_project_or_none

from access.bigquery import AGENT_VARIABLE, MAX_BYTES_BILLED, PersonaQueryError
from access.identities import UnknownPersonaError
from agent.ask import Audit, ask, load_context
from agent.bigquery import BigQueryConnection, load_catalogues, relations
from agent.limits import load_limits
from agent.model import model_from_config
from agent.native import (
    ENDPOINT,
    LAST_FREE_DAY,
    NativeAgentError,
    OutsideFreeWindow,
    ask_native,
    free_window_open,
    read_messages,
    system_instruction,
)
from tests.conftest import REPO

PROJECT = "the-lab"
AGENT_ID = "the-agent"
CHICAGO = "accountant-chicago"
QUESTION = "What share of the City of Chicago's served payment rows repeat another served row?"
IN_WINDOW = date(2026, 12, 1)
GOLDEN_CASE = "duplicate-rows-as-the-payables-clerk"

ANSWER = [
    {"systemMessage": {"text": {"parts": ["Working on it"], "textType": "PROGRESS"}}},
    {"systemMessage": {"data": {"generatedSql": "SELECT 0.25 AS rate"}}},
    {
        "systemMessage": {
            "data": {
                "result": {
                    "schema": {"fields": [{"name": "rate", "type": "FLOAT"}]},
                    "data": [{"rate": 0.25}],
                }
            }
        }
    },
    {"systemMessage": {"text": {"parts": ["A quarter repeat."], "textType": "FINAL_RESPONSE"}}},
]


class Fake:
    """A fake chat endpoint at httpx's transport and a fake gcloud mint; every request is kept."""

    def __init__(self, status: int = 200, answer: Any = None) -> None:
        self.status = status
        self.answer = ANSWER if answer is None else answer
        self.requests: list[httpx.Request] = []
        self.mints: list[list[str]] = []
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))

    def mint(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        accounts = command[3].split("=", 1)[1].split(",")
        self.mints.append(accounts)
        return subprocess.CompletedProcess(list(command), 0, f"token-for:{accounts[-1]}", "")

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.answer)

    def ask(self, persona: str = CHICAGO, day: date = IN_WINDOW, **rest: Any) -> Any:
        return ask_native(
            persona,
            QUESTION,
            project=PROJECT,
            client=self.client,
            today=day,
            env={AGENT_VARIABLE: AGENT_ID},
            run=self.mint,
            **rest,
        )

    @property
    def body(self) -> dict[str, Any]:
        (request,) = self.requests
        return json.loads(request.content)


@pytest.fixture
def fake() -> Iterator[Fake]:
    made = Fake()
    yield made
    made.client.close()


def test_the_last_free_day_is_a_day_short_of_the_pricing_pages_date() -> None:
    assert date(2026, 12, 30) == LAST_FREE_DAY
    assert free_window_open(LAST_FREE_DAY)
    assert not free_window_open(LAST_FREE_DAY + timedelta(days=1))


@pytest.mark.parametrize("day", [date(2026, 12, 31), date(2027, 1, 1), date(2030, 6, 1)])
def test_past_the_free_window_nothing_is_minted_or_sent(fake: Fake, day: date) -> None:
    with pytest.raises(OutsideFreeWindow):
        fake.ask(day=day)
    assert fake.mints == [] and fake.requests == []


def test_the_call_carries_the_personas_own_token_minted_through_the_agent(fake: Fake) -> None:
    fake.ask()
    assert fake.mints == [
        [
            f"{AGENT_ID}@{PROJECT}.iam.gserviceaccount.com",
            f"{CHICAGO}@{PROJECT}.iam.gserviceaccount.com",
        ]
    ]
    (request,) = fake.requests
    assert (
        request.headers["authorization"]
        == f"Bearer token-for:{CHICAGO}@{PROJECT}.iam.gserviceaccount.com"
    )
    assert str(request.url) == ENDPOINT.format(project=PROJECT)


def test_each_call_mints_its_own_token(fake: Fake) -> None:
    fake.ask()
    fake.ask()
    assert len(fake.mints) == 2


def test_a_persona_the_catalogue_lacks_is_refused_before_the_mint(fake: Fake) -> None:
    with pytest.raises(UnknownPersonaError):
        fake.ask(persona="nobody-here")
    assert fake.mints == [] and fake.requests == []


def test_a_failed_mint_sends_nothing() -> None:
    fake = Fake()

    def failing(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(command), 1, "", "denied")

    with pytest.raises(PersonaQueryError):
        ask_native(
            CHICAGO, QUESTION, project=PROJECT, client=fake.client, today=IN_WINDOW, run=failing
        )
    assert fake.requests == []


def test_the_agent_is_shown_only_the_personas_tables(fake: Fake) -> None:
    fake.ask()
    shown = fake.body["inlineContext"]["datasourceReferences"]["bq"]["tableReferences"]
    places = relations(CHICAGO, load_catalogues())
    want = {(d, t) for found in places.values() for d, t in found}
    assert {(r["datasetId"], r["tableId"]) for r in shown} == want
    assert want and {r["projectId"] for r in shown} == {PROJECT}
    assert len(shown) == len(want)


def test_every_call_caps_each_job_within_the_turn_and_the_persona_cap(fake: Fake) -> None:
    fake.ask()
    cap = fake.body["inlineContext"]["options"]["datasource"]["bigQueryMaxBilledBytes"]
    assert cap == str(min(load_limits().bytes_billed_per_turn, MAX_BYTES_BILLED))
    assert 0 < int(cap) <= MAX_BYTES_BILLED


def test_the_question_and_the_glossary_go_in_and_nothing_is_kept_on_the_service(
    fake: Fake,
) -> None:
    fake.ask()
    body = fake.body
    assert body["messages"] == [{"userMessage": {"text": QUESTION}}]
    assert body["inlineContext"]["systemInstruction"] == system_instruction()
    assert "Days to pay" in body["inlineContext"]["systemInstruction"]
    # Stateless: no conversation, no data agent.
    assert set(body) == {"messages", "inlineContext"}


def test_the_answer_reads_the_final_text_the_sql_and_the_rows(fake: Fake) -> None:
    answer = fake.ask()
    assert answer.text == "A quarter repeat."
    assert answer.sql == ("SELECT 0.25 AS rate",)
    assert (answer.columns, answer.rows) == (("rate",), ((0.25,),))
    assert answer.errors == ()


def test_an_error_message_is_kept_and_no_final_text_is_invented() -> None:
    answer = read_messages(CHICAGO, QUESTION, [{"systemMessage": {"error": {"text": "denied"}}}])
    assert (answer.text, answer.errors, answer.rows) == ("", ("denied",), ())


@pytest.mark.parametrize("status", [400, 403, 429, 500])
def test_a_refused_call_raises_with_its_status_and_no_token(status: int) -> None:
    fake = Fake(status=status, answer={"error": {"message": "no"}})
    with pytest.raises(NativeAgentError, match=str(status)) as raised:
        fake.ask()
    assert "token-for" not in str(raised.value)
    assert CHICAGO not in str(raised.value) and PROJECT not in str(raised.value)


def test_an_unreachable_service_raises_without_its_cause() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(down))
    fake = Fake()
    with pytest.raises(NativeAgentError) as raised:
        ask_native(
            CHICAGO,
            QUESTION,
            project=PROJECT,
            client=client,
            today=IN_WINDOW,
            env={},
            run=fake.mint,
        )
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


# The live golden question, which `make bigquery-native` runs on the owner's machine.
@pytest.mark.bigquery_persona
def test_one_golden_question_is_answered_as_the_persona_with_the_verified_querys_rows(
    audit: Audit, request: pytest.FixtureRequest
) -> None:
    """The native agent, asked a golden question as its persona on the persona's own token,
    answers with the rows the question's verified query returns on BigQuery as that persona
    (the own agent's BigQuery mode): the golden set's frozen rows are the committed sample's,
    not the warehouse's, so the verified query on the warehouse is the yardstick."""
    project = lab_project_or_none()
    if project is None:
        pytest.skip("LEDGER_LAB_GCP_PROJECT is unset: the lab has no BigQuery to act in")
    if not free_window_open(datetime.now(UTC).date()):
        pytest.skip("past the native agent's free window")
    golden = yaml.safe_load((REPO / "agent" / "evals" / "golden.yaml").read_text())
    case = next(c for c in golden["cases"] if c["id"] == GOLDEN_CASE)
    maker: Callable[[str], BigQueryConnection] = request.getfixturevalue("agent_on_bigquery")
    own = ask(
        case["persona"],
        case["question"],
        as_persona=maker,
        audit=audit,
        model=model_from_config({}, load_context()),
    )
    if not own.citations:
        pytest.fail("the verified query ran no query as the persona")
    with httpx.Client() as client:
        answer = ask_native(case["persona"], case["question"], project=project, client=client)
    if not answer.sql:
        pytest.fail("the native agent ran no query")
    if not same_figures(answer.rows, own.citations[0].rows):
        pytest.fail("the native agent's rows are not the verified query's")


def same_figures(got: Sequence[Sequence[Any]], want: Sequence[Sequence[Any]]) -> bool:
    """Equal shapes and, cell by cell, equal numbers to 1e-9 or equal text; the native agent's
    rows come back as JSON, so a number may arrive as text."""
    if [len(r) for r in got] != [len(r) for r in want]:
        return False
    for g, w in zip((c for r in got for c in r), (c for r in want for c in r), strict=True):
        try:
            if abs(float(g) - float(w)) > 1e-9:
                return False
        except (TypeError, ValueError):
            if g != w:
                return False
    return True


def test_figures_compare_by_number_and_shape() -> None:
    assert same_figures([["0.25"]], [[0.25]])
    assert same_figures([[None]], [[None]])
    assert not same_figures([[0.25]], [[0.26]])
    assert not same_figures([[0.25, 1]], [[0.25]])
    assert not same_figures([], [[0.25]])


def test_the_golden_case_is_in_the_golden_set_and_asks_the_verified_question() -> None:
    golden = yaml.safe_load((REPO / "agent" / "evals" / "golden.yaml").read_text())
    (case,) = [c for c in golden["cases"] if c["id"] == GOLDEN_CASE]
    assert load_context()[case["query"]].question == case["question"]


def test_make_bigquery_native_runs_the_live_golden_question_under_its_marker() -> None:
    lines = (REPO / "Makefile").read_text().splitlines()
    start = lines.index("bigquery-native: sync")
    recipe = lines[start + 1].split()
    assert recipe[:5] == ["uv", "run", "pytest", "-m", "bigquery_persona"]
    (target,) = [word for word in recipe if word.startswith("tests/")]
    path, name = target.split("::")
    assert (REPO / path).is_file()
    assert (
        name
        == test_one_golden_question_is_answered_as_the_persona_with_the_verified_querys_rows.__name__
    )
