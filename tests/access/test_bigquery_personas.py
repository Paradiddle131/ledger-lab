"""Each human persona acts as its own service identity on BigQuery, by a token minted by
impersonation (`access/bigquery.py`).

The `bigquery_persona` test mints a token for each human persona's account from this machine's
own login, through the agent account, and runs `SELECT SESSION_USER()` with it: BigQuery must
answer with the persona's account, and bill nothing. Behind the cloud session's proxy the token is
replaced on the way, so it runs on the owner's machine (`make bigquery-personas`), with the lab
project set and no `GCP_PROJECT`. The proposer, an agent persona with no warehouse grant, holds no
query role, so it is not among them. The other tests hold, offline, what the mint asks `gcloud`
for and what a query sends and reads back, against stubs.
"""

import json
import subprocess
from collections.abc import Callable, Sequence
from typing import Any

import httpx
import pytest
from gcp_project import lab_project

from access.bigquery import (
    AGENT_VARIABLE,
    MAX_BYTES_BILLED,
    PersonaQueryError,
    PersonaSession,
    chain,
    mint,
)
from access.identities import identity

PERSONA = "auditor@the-lab-001.iam.gserviceaccount.com"
SECRET_WORDS = ("the-lab-001", "someone@example.com")


@pytest.mark.bigquery_persona
def test_each_human_persona_queries_bigquery_as_its_own_identity(
    as_persona_on_bigquery: Callable[[str], PersonaSession], access: dict[str, Any], human: str
) -> None:
    answer = as_persona_on_bigquery(human).query("SELECT SESSION_USER()")
    assert answer.rows == [(identity(access, human).service_account(lab_project()),)]
    assert answer.bytes_processed == 0


def test_the_chain_goes_through_the_agent_account_when_it_is_named() -> None:
    env = {AGENT_VARIABLE: "the-agent"}
    assert chain(PERSONA, "the-lab-001", env) == [
        "the-agent@the-lab-001.iam.gserviceaccount.com",
        PERSONA,
    ]
    assert chain(PERSONA, "the-lab-001", {}) == [PERSONA]
    assert chain(PERSONA, "the-lab-001", {AGENT_VARIABLE: "  "}) == [PERSONA]


@pytest.mark.parametrize("account_id", ["Agent", "a@b", "the-agent,other-agent", "ab", "agent-"])
def test_an_agent_account_that_is_not_an_account_id_is_refused(account_id: str) -> None:
    with pytest.raises(PersonaQueryError, match=AGENT_VARIABLE):
        chain(PERSONA, "the-lab-001", {AGENT_VARIABLE: account_id})


def runner(
    returncode: int, stdout: str, stderr: str = ""
) -> tuple[list[list[str]], Callable[[Sequence[str]], subprocess.CompletedProcess[str]]]:
    calls: list[list[str]] = []

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(list(command), returncode, stdout, stderr)

    return calls, run


def test_the_mint_asks_gcloud_for_the_last_accounts_token_through_the_others() -> None:
    calls, run = runner(0, "ya29.the-token\n", "WARNING: using impersonation")
    assert mint(["the-agent@x", PERSONA], run) == "ya29.the-token"
    (command,) = calls
    assert command[0].endswith("gcloud")
    assert command[1:] == [
        "auth",
        "print-access-token",
        f"--impersonate-service-account=the-agent@x,{PERSONA}",
        "--verbosity=error",
    ]


def test_a_failed_mint_reports_its_status_only() -> None:
    _, run = runner(1, "", "PERMISSION_DENIED on the-lab-001 for someone@example.com")
    with pytest.raises(PersonaQueryError) as refused:
        mint([PERSONA], run)
    assert str(refused.value) == "minting the persona's token failed with status 1"


@pytest.mark.parametrize("stdout", ["", "\n", "two tokens\n"])
def test_a_mint_that_prints_no_single_token_is_refused(stdout: str) -> None:
    _, run = runner(0, stdout)
    with pytest.raises(PersonaQueryError, match="no single token"):
        mint([PERSONA], run)


def session(
    reply: Callable[[httpx.Request], httpx.Response],
) -> tuple[list[httpx.Request], PersonaSession]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply(request)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    return seen, PersonaSession("the-lab-001", "ya29.the-token", client)


def test_a_query_is_capped_standard_sql_carrying_the_personas_token() -> None:
    seen, persona = session(
        lambda _: httpx.Response(
            200,
            json={
                "jobComplete": True,
                "totalBytesProcessed": "0",
                "rows": [{"f": [{"v": PERSONA}, {"v": None}]}],
            },
        )
    )
    answer = persona.query("SELECT SESSION_USER(), NULL")
    assert answer.rows == [(PERSONA, None)]
    assert answer.bytes_processed == 0
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == (
        "https://bigquery.googleapis.com/bigquery/v2/projects/the-lab-001/queries"
    )
    assert request.headers["authorization"] == "Bearer ya29.the-token"
    assert json.loads(request.content) == {
        "query": "SELECT SESSION_USER(), NULL",
        "useLegacySql": False,
        "useQueryCache": False,
        "maximumBytesBilled": str(MAX_BYTES_BILLED),
        "timeoutMs": 30_000,
    }


def test_an_answer_with_no_rows_reads_as_none() -> None:
    _, persona = session(
        lambda _: httpx.Response(200, json={"jobComplete": True, "totalBytesProcessed": "10"})
    )
    answer = persona.query("SELECT 1 FROM t WHERE FALSE")
    assert (answer.rows, answer.bytes_processed) == ([], 10)


def test_a_refused_query_reports_status_and_reason_never_the_message() -> None:
    message = "Access Denied: Table the-lab-001:payments.t: User someone@example.com"
    _, persona = session(
        lambda _: httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": message,
                    "errors": [{"reason": "accessDenied", "message": message}],
                }
            },
        )
    )
    with pytest.raises(PersonaQueryError) as refused:
        persona.query("SELECT * FROM payments.t")
    assert (refused.value.status, refused.value.reason) == (403, "accessDenied")
    assert str(refused.value) == "BigQuery refused the query: HTTP 403, reason accessDenied"
    assert not any(word in str(refused.value) for word in SECRET_WORDS)


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"error": {"errors": []}}'])
def test_a_refusal_without_a_reason_says_so(body: bytes) -> None:
    _, persona = session(lambda _: httpx.Response(500, content=body))
    with pytest.raises(PersonaQueryError) as refused:
        persona.query("SELECT 1")
    assert (refused.value.status, refused.value.reason) == (500, None)


@pytest.mark.parametrize(
    ("answer", "words"),
    [
        ({"jobComplete": False}, "did not finish"),
        ({"jobComplete": True, "pageToken": "next", "rows": []}, "past one page"),
    ],
)
def test_an_unfinished_or_paged_answer_is_refused(answer: dict[str, Any], words: str) -> None:
    _, persona = session(lambda _: httpx.Response(200, json=answer))
    with pytest.raises(PersonaQueryError, match=words):
        persona.query("SELECT 1")
