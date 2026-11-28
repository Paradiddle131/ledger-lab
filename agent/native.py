"""The native mode: a question put to BigQuery's managed data agent (Google's Conversational
Analytics API, `geminidataanalytics.googleapis.com`) as the asking persona, on that persona's own
token, so the warehouse's grants, row access policies and policy tags decide what its queries
return, as they do for the own agent's BigQuery mode (`agent/bigquery.py`).

`ask_native(persona, question, project=..., client=...)` makes one stateless `chat` call
(`POST /v1beta/projects/<project>/locations/global:chat`) with the context inline, and reads the
messages the call answers with into a `NativeAnswer`: the final text, the SQL the agent generated,
and the rows of its results. No conversation or data agent is created, so nothing is kept on the
service between calls.

Fenced to the free window. Google's Data Agents pricing page
(https://cloud.google.com/products/data-agents/pricing) says the agents' tokens are free through
2026-12-31 and billed per token after. The mode refuses before it mints a token or opens a
connection on any UTC date past `LAST_FREE_DAY`, 2026-12-30: one day short of the page's date, so
no call is sent on 2026-12-31 in UTC while some timezone already reads 2027-01-01. The check reads
the date the caller passes (by default today's, in UTC), so the stop is in code, not a reminder.

Each call carries:
- the persona's token, minted for its own service account through the agent account when
  `LEDGER_LAB_AGENT_ACCOUNT` names it (`access/bigquery.py`'s `chain` and `mint`), every call; a
  persona the catalogue does not hold is refused before the mint;
- the tables the persona reads on BigQuery, as `datasourceReferences.bq.tableReferences`: the
  relations `agent/bigquery.py`'s `relations` gives it (its purposes' views and the aggregate views
  it reads), so the agent is shown no table the persona is not granted, and the warehouse refuses
  the rest;
- `options.datasource.bigQueryMaxBilledBytes`, the per-job cap: the turn's `bytes_billed_per_turn`
  (`agent/limits.py`), never more than the persona queries' own cap (`MAX_BYTES_BILLED`); a query
  estimated past it fails without a charge, as the cost-management page
  (https://docs.cloud.google.com/gemini/data-agents/conversational-analytics-api/manage-costs) says;
- a system instruction holding the metric context's glossary (`semantics/context.json`), the
  context generated from the metric definitions.

Stated limits:
- the cap is per BigQuery job, and the agent may run more than one job in a turn: a turn's jobs
  together may bill up to that many times the cap, unlike the own agent's turn total;
- the field names come from the API's v1beta reference: the `chat` method, `Context` and `Message`
  pages; `ConversationOptions.datasource.bigQueryMaxBilledBytes` was read from a summary of the
  reference, not its verbatim text, and that the stateless `inlineContext` honours it is read from
  its being a `Context`, which no page says outright;
- the answer is not checked by `agent.integrity`, nor written to the audit log: the native agent
  writes its own text and figures, so a figure here is the agent's, not a cited cell; the rows are
  read so a caller can compare them with the verified query's;
- the call's response is read whole, as a JSON array of messages (one message alone is read as
  one), not as a stream;
- behind the cloud session's proxy, which replaces the token on `*.googleapis.com` with the
  session's own, the call would not run as the persona: the mode's live test runs only off that
  proxy, on the owner's machine.
"""

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from access.bigquery import MAX_BYTES_BILLED, Runner, chain, mint
from access.bigquery import _run as run_gcloud
from access.identities import identity
from agent.bigquery import Catalogues, load_catalogues, relations
from agent.limits import Limits, load_limits

ENDPOINT = (
    "https://geminidataanalytics.googleapis.com/v1beta/projects/{project}/locations/global:chat"
)
# The pricing page's free trial runs through 2026-12-31; the mode stops a day short (docstring).
LAST_FREE_DAY = date(2026, 12, 30)
CONTEXT = Path(__file__).resolve().parents[1] / "semantics" / "context.json"
CALL_TIMEOUT_S = 120.0
FINAL = "FINAL_RESPONSE"


class OutsideFreeWindow(RuntimeError):
    """The date is past the native agent's free window: no token is minted and no call is sent."""


class NativeAgentError(RuntimeError):
    """The chat call failed or answered with nothing this mode can read; the message names no
    token, account or project."""


@dataclass(frozen=True)
class NativeAnswer:
    """What the native agent answered: its final text, the SQL it generated, the columns and rows
    of its last result, and the text of any error message it sent."""

    persona: str
    question: str
    text: str
    sql: tuple[str, ...]
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    errors: tuple[str, ...]


def free_window_open(day: date) -> bool:
    return day <= LAST_FREE_DAY


def system_instruction(path: Path = CONTEXT) -> str:
    """The glossary of the metric context, one term a line, for the agent's system instruction."""
    glossary = json.loads(path.read_text(encoding="utf-8"))["glossary"]
    lines = [f"- {term['term']}: {term['definition']}" for _, term in sorted(glossary.items())]
    return "Answer finance questions on these tables. Terms used here:\n" + "\n".join(lines)


def table_references(
    project: str, places: Mapping[str, tuple[tuple[str, str], ...]]
) -> list[dict[str, str]]:
    """Each relation the persona reads, once, in a stable order."""
    unique = sorted({place for found in places.values() for place in found})
    return [
        {"projectId": project, "datasetId": dataset, "tableId": table} for dataset, table in unique
    ]


def chat_body(
    question: str,
    project: str,
    places: Mapping[str, tuple[tuple[str, str], ...]],
    cap: int,
    instruction: str,
) -> dict[str, Any]:
    return {
        "messages": [{"userMessage": {"text": question}}],
        "inlineContext": {
            "systemInstruction": instruction,
            "datasourceReferences": {"bq": {"tableReferences": table_references(project, places)}},
            "options": {"datasource": {"bigQueryMaxBilledBytes": str(cap)}},
        },
    }


def read_messages(persona: str, question: str, messages: Sequence[Any]) -> NativeAnswer:
    """The final text, the generated SQL, the last result and the errors of the messages."""
    texts: list[str] = []
    final: list[str] = []
    sql: list[str] = []
    errors: list[str] = []
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    for message in messages:
        system = message.get("systemMessage") if isinstance(message, Mapping) else None
        if not isinstance(system, Mapping):
            continue
        text = system.get("text")
        if isinstance(text, Mapping):
            parts = [p for p in text.get("parts", ()) if isinstance(p, str)]
            (final if text.get("textType") == FINAL else texts).extend(parts)
        data = system.get("data")
        if isinstance(data, Mapping):
            if isinstance(data.get("generatedSql"), str):
                sql.append(data["generatedSql"])
            result = data.get("result")
            if isinstance(result, Mapping):
                fields = result.get("schema", {}).get("fields", ())
                columns = tuple(f.get("name", "") for f in fields if isinstance(f, Mapping))
                rows = tuple(
                    tuple(row.get(c) for c in columns)
                    for row in result.get("data", ())
                    if isinstance(row, Mapping)
                )
        error = system.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("text"), str):
            errors.append(error["text"])
    answer = "\n".join(final) if final else ""
    return NativeAnswer(persona, question, answer, tuple(sql), columns, rows, tuple(errors))


def ask_native(
    persona: str,
    question: str,
    *,
    project: str,
    client: httpx.Client,
    today: date | None = None,
    env: Mapping[str, str] | None = None,
    catalogues: Catalogues | None = None,
    limits: Limits | None = None,
    instruction: str | None = None,
    run: Runner = run_gcloud,
) -> NativeAnswer:
    """Ask the native agent `question` as `persona`; see the module's docstring."""
    day = datetime.now(UTC).date() if today is None else today
    if not free_window_open(day):
        raise OutsideFreeWindow(
            f"the native data agent is free only through {LAST_FREE_DAY:%Y-%m-%d} here; "
            "no call is sent after it"
        )
    known = load_catalogues() if catalogues is None else catalogues
    account = identity(known.access, persona).service_account(project)
    places = relations(persona, known)
    ceilings = load_limits() if limits is None else limits
    cap = min(ceilings.bytes_billed_per_turn, MAX_BYTES_BILLED)
    body = chat_body(
        question, project, places, cap, system_instruction() if instruction is None else instruction
    )
    token = mint(chain(account, project, os.environ if env is None else env), run)
    # httpx.Headers masks the Authorization value in its repr, which a traceback's locals show.
    headers = httpx.Headers({"Authorization": f"Bearer {token}"})
    del token
    url = ENDPOINT.format(project=quote(project, safe=""))
    try:
        response = client.post(url, json=body, headers=headers, timeout=CALL_TIMEOUT_S)
    except httpx.HTTPError:
        raise NativeAgentError("the native agent's chat call did not complete") from None
    if response.status_code != 200:
        raise NativeAgentError(
            f"the native agent's chat call answered status {response.status_code}"
        )
    try:
        answered = response.json()
    except ValueError:
        raise NativeAgentError("the native agent's chat call answered no JSON") from None
    messages = answered if isinstance(answered, list) else [answered]
    return read_messages(persona, question, messages)
