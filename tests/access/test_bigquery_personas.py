"""Each human persona acts as its own service identity on BigQuery, by a token minted by
impersonation (`access/bigquery.py`).

The `bigquery_persona` test mints a token for each human persona's account from this machine's
own login, through the agent account, and runs `SELECT SESSION_USER()` with it: BigQuery must
answer with the persona's account, and process no bytes. Behind the cloud session's proxy the
token is replaced on the way, so it runs on the owner's machine (`make bigquery-personas`), with
the lab project set and no `GCP_PROJECT`. The proposer, an agent persona with no warehouse grant,
holds no query role, so it is not among them. The other tests hold, offline, what the mint asks
`gcloud` for and what a query sends and reads back, against stubs, that a failure of the live
test names no account, project, token or path under the login's home under the report options
that add most to it, that an interrupt ends the run naming the persona alone,
and that neither a plain run nor another lab's session reaches GCP.
"""

import json
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from itertools import takewhile
from pathlib import Path
from typing import Any

import httpx
import pytest
from gcp_project import lab_project

from access.bigquery import (
    AGENT_VARIABLE,
    PersonaQueryError,
    PersonaSession,
    _run,
    chain,
    mint,
)
from access.identities import identity
from tests.conftest import REPO

PROJECT = "the-lab"
# Built, not written out: a fresh clone holds no address on a real domain.
PERSONA = f"auditor@{PROJECT}.iam.gserviceaccount.com"
SECRET_WORDS = (PROJECT, "someone@example.com")


@pytest.mark.bigquery_persona
def test_each_human_persona_queries_bigquery_as_its_own_identity(
    as_persona_on_bigquery: Callable[[str], PersonaSession], access: dict[str, Any], human: str
) -> None:
    # A failure reports the persona and one scrubbed line, never a traceback
    # (`pytest_runtest_makereport` in tests/conftest.py): its frames, locals and chained errors
    # would name the project, the accounts, the token and paths under the login's home.
    answer = as_persona_on_bigquery(human).query("SELECT SESSION_USER()")
    if answer.rows != [(identity(access, human).service_account(lab_project()),)]:
        pytest.fail("BigQuery answered as another identity")
    if answer.bytes_processed != 0:
        pytest.fail("the query processed bytes")


# Stubs for gcloud and BigQuery, loaded as a plugin into a pytest run of the live test: one failure
# per persona below, the others pass. A failing persona's stub sits under httpx's client, at its
# transport; a passing persona's query runs through httpx and httpcore as it would live, to a
# server on the loopback address whose answer carries the project and the agent account in a
# header, so a log of the exchange would show them. No call leaves the machine. The plugin writes
# the names of the variables the child was given beside itself, so the test holds that the child
# inherits none.
STUBS = """
import http.server, json, os, signal, socket, subprocess, threading, httpx, pytest
import access.bigquery

_connect = socket.create_connection

def _loopback_only(address, *args, **kwargs):
    if address[0] != "127.0.0.1":
        raise RuntimeError("no network in this run")
    return _connect(address, *args, **kwargs)

socket.create_connection = _loopback_only
AGENT = f"the-agent@{os.environ['LEDGER_LAB_GCP_PROJECT']}.iam.gserviceaccount.com"
with open(os.path.join(os.path.dirname(__file__), "variables"), "w") as names:
    # pytest's own variables, and LC_CTYPE, which Python sets when it coerces a C locale.
    own = {"PYTEST_CURRENT_TEST", "PYTEST_VERSION", "LC_CTYPE"}
    names.write("\\n".join(sorted(k for k in os.environ if k not in own)))

class _BigQuery(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        who = self.headers["Authorization"].rsplit(":", 1)[-1]
        answer = {"jobComplete": True, "totalBytesProcessed": "0", "rows": [{"f": [{"v": who}]}]}
        body = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("X-Probe", f"{os.environ['LEDGER_LAB_GCP_PROJECT']} {AGENT}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

_server = http.server.HTTPServer(("127.0.0.1", 0), _BigQuery)
threading.Thread(target=_server.serve_forever, daemon=True).start()
LOOPBACK = f"http://127.0.0.1:{_server.server_port}/bigquery/v2/projects/{{project}}/queries"

def pytest_keyboard_interrupt(excinfo):
    # A real second Ctrl-C while pytest renders the first, in `interrupt-twice`.
    if "interrupt-twice" in os.environ["STUB_MODES"]:
        os.kill(os.getpid(), signal.SIGINT)

@pytest.fixture(autouse=True)
def _stubs(request, monkeypatch):
    human = request.node.callspec.params["human"]
    mode = os.environ["STUB_MODES"].split(",")
    mode = dict(m.split("=") for m in mode if m).get(human, "ok")
    if mode == "bad-agent":
        monkeypatch.setenv("LEDGER_LAB_AGENT_ACCOUNT", "Bad_Agent")
    if mode == "setup-fails":
        raise RuntimeError(f"no setup for {AGENT} in {os.getcwd()}")
    if mode == "ok":
        monkeypatch.setattr(access.bigquery, "ENDPOINT", LOOPBACK)

    def run(command, *args, **kwargs):
        if mode == "interrupt-mint":
            raise KeyboardInterrupt
        if mode == "timeout":
            raise subprocess.TimeoutExpired(command, 120)
        if mode == "mint-fails":
            return subprocess.CompletedProcess(command, 1, "", "denied for " + command[3])
        token = "stub-token:" + command[3].rsplit(",", 1)[-1]
        return subprocess.CompletedProcess(command, 0, token, "")

    def handle(self, request):
        token = request.headers["authorization"]
        who = token.rsplit(":", 1)[-1]
        if mode in ("interrupt-query", "interrupt-twice"):
            raise KeyboardInterrupt
        if mode == "unreachable":
            raise httpx.ConnectError(f"cannot reach {request.url} with {token}", request=request)
        if mode == "not-json":
            return httpx.Response(200, text=f"<html>{request.url} {token}</html>")
        if mode == "refused":
            error = {"reason": "accessDenied", "message": "Access Denied: " + who}
            return httpx.Response(403, json={"error": {"errors": [error]}})
        answer = {"jobComplete": True, "totalBytesProcessed": "10" if mode == "bytes" else "0",
                  "rows": [{"f": [{"v": AGENT if mode == "another" else who}]}]}
        return httpx.Response(200, json=answer)

    monkeypatch.setattr(subprocess, "run", run)
    if mode != "ok":
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    yield
    if mode == "teardown-fails":
        raise RuntimeError(f"no teardown for {AGENT} in {os.getcwd()}")
    if mode == "interrupt-twice":
        raise KeyboardInterrupt
"""
# The scrubbed line each failing persona's report must hold; any error that is not the test's
# own verdict or a `PersonaQueryError` is named by its type alone.
FAILURES = {
    "accountant-chicago": ("mint-fails", "minting the persona's token failed with status 1"),
    "accountant-uk-transport": ("timeout", "minting the persona's token timed out"),
    "accountant-nl-government": ("bad-agent", f"{AGENT_VARIABLE} is not a service account id"),
    "group-controller": ("another", "BigQuery answered as another identity"),
    "payables-clerk": ("bytes", "the query processed bytes"),
    "approver": ("refused", "BigQuery refused the query: HTTP 403, reason accessDenied"),
    "metric-owner": ("unreachable", "ConnectError, its message left out"),
    "auditor": ("not-json", "JSONDecodeError, its message left out"),
}
# The options that add most to a failure's report: tracebacks at their longest, with each frame's
# locals, pytest's own frames, captured and live logs at every level, and every summary (`-rA`).
WORST_CASE = [
    "--tb=long",
    "-l",
    "--full-trace",
    "--log-level=DEBUG",
    "--log-cli-level=DEBUG",
    "-rA",
]


def recipe_pytest_arguments() -> list[str]:
    """The pytest arguments `make bigquery-personas` runs, read from its one recipe line."""
    lines = (REPO / "Makefile").read_text().splitlines()
    target = lines.index(next(line for line in lines if line.startswith("bigquery-personas:")))
    (command,) = takewhile(lambda line: line.startswith("\t"), lines[target + 1 :])
    uv, run, *arguments = shlex.split(command)
    assert [uv, run, arguments[0]] == ["uv", "run", "pytest"], command
    return arguments[1:]


def run_over_stubs(tmp_path: Path, modes: dict[str, str]) -> tuple[int, str]:
    """`make bigquery-personas`'s pytest run under WORST_CASE, over the stubs, each persona failing
    the way `modes` names; its exit status and rendered report."""
    (tmp_path / "persona_stubs.py").write_text(STUBS)
    # The child is hermetic: its PATH holds only a gcloud that logs its call and fails, its gcloud
    # configuration is empty, and it inherits no credential, proxy or GCP variable, so a stub that
    # drifted could reach neither the real gcloud nor the network.
    bin_dir, config, gcloud_log = tmp_path / "bin", tmp_path / "gcloud-config", tmp_path / "ran"
    bin_dir.mkdir()
    config.mkdir()
    (bin_dir / "gcloud").write_text(
        f'#!/bin/sh\necho "$@" >> {shlex.quote(str(gcloud_log))}\nexit 1\n'
    )
    (bin_dir / "gcloud").chmod(0o755)
    env = {
        "PATH": str(bin_dir),
        "CLOUDSDK_CONFIG": str(config),
        "LEDGER_LAB_GCP_PROJECT": PROJECT,
        "LEDGER_LAB_AGENT_ACCOUNT": "the-agent",
        "PYTHONPATH": str(tmp_path),
        "STUB_MODES": ",".join(f"{p}={mode}" for p, mode in modes.items()),
    }
    run = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            *recipe_pytest_arguments(),
            "-p",
            "persona_stubs",
            "-p",
            "no:cacheprovider",
            *WORST_CASE,
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
        env=env,
        timeout=300,
        check=False,
    )
    report = run.stdout + run.stderr
    assert not gcloud_log.exists(), "the child ran gcloud"
    given = (tmp_path / "variables").read_text().split("\n")
    assert given == sorted(env), "the child inherited a variable it was not given"
    # Neither the project, the agent account, a token, a path under the login's home nor one
    # under the checkout appears anywhere in the rendered report.
    for word in (PROJECT, "the-agent", "stub-token", str(Path.home()), str(REPO)):
        assert word not in report, word
    return run.returncode, report


def test_a_failing_persona_query_names_the_persona_alone(tmp_path: Path) -> None:
    _, report = run_over_stubs(tmp_path, {p: mode for p, (mode, _) in FAILURES.items()})
    assert "8 failed, 1 passed" in report, report[-2000:]
    for persona, (_, words) in FAILURES.items():
        assert f"{persona}: {words}" in report, persona


@pytest.mark.parametrize("mode", ["setup-fails", "teardown-fails"])
def test_a_persona_test_failing_in_its_setup_or_teardown_names_the_persona_alone(
    tmp_path: Path, mode: str
) -> None:
    code, report = run_over_stubs(tmp_path, {"auditor": mode})
    assert code == pytest.ExitCode.TESTS_FAILED, report[-2000:]
    assert "auditor: RuntimeError, its message left out" in report, report[-2000:]


def test_a_passing_persona_query_through_httpcore_logs_nothing_of_the_exchange(
    tmp_path: Path,
) -> None:
    """The passing personas' queries reach the loopback server through httpx and httpcore, under
    DEBUG logs and `-rA`; the server's header names the project and the agent account, which
    `run_over_stubs` holds out of the report."""
    code, report = run_over_stubs(tmp_path, {})
    assert code == pytest.ExitCode.OK, report[-2000:]
    assert "9 passed" in report, report[-2000:]
    assert "httpcore" not in report, report[-2000:]


@pytest.mark.parametrize("mode", ["interrupt-mint", "interrupt-query", "interrupt-twice"])
def test_an_interrupted_persona_test_ends_the_run_naming_the_persona_alone(
    tmp_path: Path, mode: str
) -> None:
    """Ctrl-C reaches the run as the KeyboardInterrupt the stub raises in the mint or the query,
    and, for `interrupt-twice`, by a real SIGINT while pytest reports the first and again in the
    fixture's teardown at the session's end."""
    code, report = run_over_stubs(tmp_path, {"auditor": mode})
    assert code == pytest.ExitCode.INTERRUPTED, report[-2000:]
    assert "auditor: interrupted" in report, report[-2000:]
    assert "Traceback" not in report, report[-2000:]


def test_the_live_test_carries_its_marker() -> None:
    marks = test_each_human_persona_queries_bigquery_as_its_own_identity.pytestmark  # type: ignore[attr-defined]
    assert "bigquery_persona" in {mark.name for mark in marks}


def test_gcloud_runs_captured_and_under_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: seen.append(kwargs))
    _run(["gcloud"])
    assert seen == [{"capture_output": True, "text": True, "check": False, "timeout": 120}]


def test_the_bigquery_mode_refuses_another_labs_session_before_any_mint(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gcp_project import NoLabProjectError

    calls: list[Any] = []
    monkeypatch.setenv("GCP_PROJECT", "another-lab")
    monkeypatch.setenv("LEDGER_LAB_GCP_PROJECT", PROJECT)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **k: calls.append(a))
    with pytest.raises(NoLabProjectError):
        request.getfixturevalue("as_persona_on_bigquery")
    assert calls == []


def test_a_plain_run_leaves_the_persona_queries_out() -> None:
    options = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    marks = options["addopts"][options["addopts"].index("-m") + 1]
    assert "not bigquery_persona" in marks.split(" and ")


def test_the_chain_goes_through_the_agent_account_when_it_is_named() -> None:
    env = {AGENT_VARIABLE: "the-agent"}
    assert chain(PERSONA, PROJECT, env) == [
        f"the-agent@{PROJECT}.iam.gserviceaccount.com",
        PERSONA,
    ]
    assert chain(PERSONA, PROJECT, {}) == [PERSONA]
    assert chain(PERSONA, PROJECT, {AGENT_VARIABLE: "  "}) == [PERSONA]


@pytest.mark.parametrize(
    "account_id", ["Agent", "a@b", "the-agent,other-agent", "ab", "agent-", "a" * 31]
)
def test_an_agent_account_that_is_not_an_account_id_is_refused(account_id: str) -> None:
    with pytest.raises(PersonaQueryError, match=AGENT_VARIABLE):
        chain(PERSONA, PROJECT, {AGENT_VARIABLE: account_id})


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
    _, run = runner(1, "", "PERMISSION_DENIED on the-lab for someone@example.com")
    with pytest.raises(PersonaQueryError) as refused:
        mint([PERSONA], run)
    assert str(refused.value) == "minting the persona's token failed with status 1"


@pytest.mark.parametrize(
    ("failure", "words"),
    [
        (subprocess.TimeoutExpired([f"--impersonate-service-account={PERSONA}"], 120), "timed out"),
        (FileNotFoundError(2, "No such file", f"gcloud {PERSONA}"), "could not run gcloud"),
    ],
)
def test_a_mint_that_times_out_or_cannot_run_names_no_account(
    failure: Exception, words: str
) -> None:
    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise failure

    with pytest.raises(PersonaQueryError) as refused:
        mint([PERSONA], run)
    assert str(refused.value) == f"minting the persona's token {words}"
    assert refused.value.__cause__ is None and refused.value.__suppress_context__


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
    return seen, PersonaSession(PROJECT, "ya29.the-token", client)


def test_nothing_the_session_holds_shows_its_token_in_a_repr() -> None:
    # A traceback's locals show reprs: the headers handed to httpx are among them.
    _, persona = session(lambda _: httpx.Response(200, json={"jobComplete": True}))
    assert "ya29.the-token" not in repr(vars(persona))


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
        f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/queries"
    )
    assert request.headers["authorization"] == "Bearer ya29.the-token"
    assert json.loads(request.content) == {
        "query": "SELECT SESSION_USER(), NULL",
        "useLegacySql": False,
        "useQueryCache": False,
        "maximumBytesBilled": "100000000",
        "timeoutMs": 30_000,
    }


def test_an_answer_with_no_rows_reads_as_none() -> None:
    _, persona = session(
        lambda _: httpx.Response(200, json={"jobComplete": True, "totalBytesProcessed": "10"})
    )
    answer = persona.query("SELECT 1 FROM t WHERE FALSE")
    assert (answer.rows, answer.bytes_processed) == ([], 10)


def test_a_refused_query_reports_status_and_reason_never_the_message() -> None:
    message = "Access Denied: Table the-lab:payments.t: User someone@example.com"
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
    assert str(refused.value) == "BigQuery refused the query: HTTP 500, reason None"


def test_a_reason_that_is_not_text_is_left_out() -> None:
    _, persona = session(
        lambda _: httpx.Response(
            403, json={"error": {"errors": [{"reason": {"message": f"on {PROJECT}"}}]}}
        )
    )
    with pytest.raises(PersonaQueryError) as refused:
        persona.query("SELECT 1")
    assert refused.value.reason is None
    assert PROJECT not in str(refused.value)


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
