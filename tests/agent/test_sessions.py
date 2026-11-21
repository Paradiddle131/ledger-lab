"""The own agent's conversations (`agent/sessions.py`): one person to one session, the purge.

The tests of the store run on this checkout's stack, on a schema of the `agent_sessions` database
made for each test and dropped after it, so they are marked `stack` and run by `make stack-test`.
The purge of an audit log's kept values and the planted personal value need no database and run in
`make check`.
"""

import logging
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import psycopg
import pytest
import yaml
from psycopg import sql

from agent import ask as ask_module
from agent import sessions as sessions_module
from agent.ask import Audit, ask, load_context
from agent.audit import (
    ChainBroken,
    FileLog,
    KeptFolderRefused,
    read_log,
    verify_chain,
    verify_links,
)
from agent.model import Exchange, Reply, ToolCall, Turn, model_from_config
from agent.sessions import RETENTION, Conversation, ForeignSession, Sessions

AsPersona = Callable[[str], duckdb.DuckDBPyConnection]
REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "tests/agent/audit_examples.yaml"
QUESTION = "What share of the City of Chicago's served payment rows repeat another served row?"
CHICAGO = "accountant-chicago"
UK = "accountant-uk-transport"


class Recorder:
    """The scripted model, recording the turns it planned and stepped on."""

    def __init__(self) -> None:
        self.planned: list[Turn] = []
        self.turns: list[Turn] = []
        self._model = model_from_config({}, load_context())

    def plan(self, turn: Turn) -> tuple[ToolCall, ...]:
        self.planned.append(turn)
        return self._model.plan(turn)

    def step(self, turn: Turn) -> ToolCall | Reply:
        self.turns.append(turn)
        return self._model.step(turn)


def compose(*args: str) -> str:
    docker = shutil.which("docker")
    assert docker is not None, "the live session tests need the Docker CLI"
    done = subprocess.run(
        [docker, "compose", *args], capture_output=True, text=True, check=False, env=os.environ
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture
def libpq(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """libpq's variables for this checkout's stack, set for the test, naming a schema of the
    `agent_sessions` database made for it (in `PGOPTIONS`) and dropped after it; its name."""
    if "COMPOSE_PROJECT_NAME" not in os.environ:
        pytest.fail("run the live session tests through `make stack-test`")
    environment = yaml.safe_load(compose("config", "--format", "json"))["services"]["db"]
    host, _, port = compose("port", "db", "5432").rpartition(":")
    schema = f"test_{uuid.uuid4().hex}"
    for name, value in {
        "PGHOST": host,
        "PGPORT": port,
        "PGUSER": environment["environment"]["POSTGRES_USER"],
        "PGPASSWORD": environment["environment"]["POSTGRES_PASSWORD"],
        "PGDATABASE": "agent_sessions",
    }.items():
        monkeypatch.setenv(name, value)
    with psycopg.connect(autocommit=True) as admin:
        admin.execute(sql.SQL("create schema {}").format(sql.Identifier(schema)))
        monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")
        try:
            yield schema
        finally:
            admin.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(schema)))


@pytest.fixture
def store(libpq: str) -> Iterator[Sessions]:
    sessions = Sessions.connect()
    sessions.create()
    try:
        yield sessions
    finally:
        sessions.connection.close()


def asked(
    recorder: Recorder, as_persona: AsPersona, audit: Audit, persona: str, conversation: Any
) -> Any:
    return ask(
        persona,
        QUESTION,
        as_persona=as_persona,
        audit=audit,
        model=recorder,
        conversation=conversation,
    )


@pytest.mark.stack
def test_no_cross_session_recall(as_persona: AsPersona, audit: Audit, store: Sessions) -> None:
    opened: list[str] = []

    def connect(persona: str) -> duckdb.DuckDBPyConnection:
        opened.append(persona)
        return as_persona(persona)

    first, second = store.start(CHICAGO), store.start(CHICAGO)
    model = Recorder()
    answered = asked(model, connect, audit, CHICAGO, Conversation(store, first))

    # A second session of the same person starts with nothing of the first.
    asked(model, connect, audit, CHICAGO, Conversation(store, second))
    assert model.turns[-1].history == ()

    # The first session, asked again, shows its own exchange, on steps only, and counts the turn.
    asked(model, connect, audit, CHICAGO, Conversation(store, first))
    assert model.turns[-1].history == (Exchange(1, QUESTION, answered.text),)
    assert all(turn.history == () for turn in model.planned)
    assert store.history(first, CHICAGO) == (
        Exchange(1, QUESTION, answered.text),
        Exchange(2, QUESTION, answered.text),
    )

    # Another person naming the first session reads nothing of it: no model step, no connection.
    steps, plans = len(model.turns), len(model.planned)
    with pytest.raises(ForeignSession):
        asked(model, connect, audit, UK, Conversation(store, first))
    # An id no session the store starts can have is refused the same way, under an id `ask` makes.
    with pytest.raises(ForeignSession):
        asked(model, connect, audit, UK, Conversation(store, "Someone-Elses-Session"))
    assert (len(model.turns), len(model.planned)) == (steps, plans)
    assert opened == [CHICAGO, CHICAGO, CHICAGO]
    with pytest.raises(ForeignSession):
        store.history(first, UK)
    with pytest.raises(ForeignSession):
        store.add(first, UK, Exchange(3, "planted", "planted"), datetime.now(UTC))
    assert len(store.history(first, CHICAGO)) == 2

    records = read_log(audit.log.path.read_bytes())  # type: ignore[attr-defined]
    turns = [(r["asker"]["session"], r["asker"]["turn"], r["outcome"]) for r in records]
    assert turns == [
        (first, 1, "answered"),
        (second, 1, "answered"),
        (first, 2, "answered"),
        (records[-2]["asker"]["session"], 1, "refused"),
        (records[-1]["asker"]["session"], 1, "refused"),
    ]
    # Each refusal is recorded under a session id `ask` made, never the one the caller named.
    made = {r["asker"]["session"] for r in records[-2:]}
    assert len(made) == 2
    assert made.isdisjoint({first, second, "Someone-Elses-Session"})
    for refused in records[-2:]:
        assert refused["reason"] == "foreign-session"
        assert refused["asker"]["persona"] == UK
        assert refused["runs"] == []


@pytest.mark.stack
def test_the_purge_command_removes_expired_transcripts_and_keeps_young_ones(
    as_persona: AsPersona,
    audit: Audit,
    store: Sessions,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log = audit.log
    assert isinstance(log, FileLog)
    # Written out, not taken from `RETENTION`, so a changed retention fails here.
    old = datetime.now(UTC) - timedelta(days=31)
    stale = store.start(CHICAGO, at=old)
    session = store.start(CHICAGO, at=old)
    young = store.start(CHICAGO, at=datetime.now(UTC) - timedelta(days=29))
    model = Recorder()
    # The first turn of the kept session ends past the retention, the second within it.
    with monkeypatch.context() as patched:
        patched.setattr(ask_module, "now", lambda: old.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        asked(model, as_persona, audit, CHICAGO, Conversation(store, session))
    asked(model, as_persona, audit, CHICAGO, Conversation(store, session))
    assert sorted(p.name for p in log.kept.iterdir()) == ["0.json", "1.json"]

    assert sessions_module.main(["purge", "--log", str(log.path), "--kept", str(log.kept)]) == 0
    assert "purged 1 exchanges, 1 sessions and the kept values of 1 audit records" in (
        capsys.readouterr().out
    )

    assert [e.turn for e in store.history(session, CHICAGO)] == [2]
    assert store.history(young, CHICAGO) == ()
    with pytest.raises(ForeignSession):
        store.history(stale, CHICAGO)
    # The record of the purged turn stays, and the chain still verifies; its values are gone.
    records = read_log(log.path.read_bytes())
    verify_chain(records)
    assert [r["asker"]["turn"] for r in records] == [1, 2]
    assert sorted(p.name for p in log.kept.iterdir()) == ["1.json"]


def test_the_retention_is_thirty_days() -> None:
    assert timedelta(days=30) == RETENTION


@pytest.mark.stack
def test_a_purge_refused_by_the_log_leaves_the_transcripts(
    as_persona: AsPersona, audit: Audit, store: Sessions, tmp_path: Path
) -> None:
    log = audit.log
    assert isinstance(log, FileLog)
    old = datetime.now(UTC) - timedelta(days=31)
    session = store.start(CHICAGO, at=old)
    store.add(session, CHICAGO, Exchange(1, QUESTION, "an old answer"), old)

    # A `--log` that is not a file: refused before anything is purged, and no file is made.
    with pytest.raises(SystemExit) as refused:
        sessions_module.main(["purge", "--log", str(tmp_path / "typo"), "--kept", str(log.kept)])
    assert refused.value.code == 2
    assert not (tmp_path / "typo").exists()

    # A log whose chain does not verify, or a folder of kept values that does not exist: the
    # log's purge refuses first, so the transcripts stay.
    asked(Recorder(), as_persona, audit, CHICAGO, Conversation(store, session))
    for wrong in ("missing-kept", "torn"):
        kept = tmp_path / "typo-kept" if wrong == "missing-kept" else log.kept
        if wrong == "torn":
            with log.path.open("ab") as line:
                line.write(b'{"torn"')
        with pytest.raises(Exception):  # noqa: B017
            sessions_module.main(["purge", "--log", str(log.path), "--kept", str(kept)])
        assert [e.turn for e in store.history(session, CHICAGO)] == [1, 2]
        assert sorted(p.name for p in log.kept.iterdir()) == ["0.json"]


def test_the_log_purge_refuses_a_missing_log_a_missing_folder_and_a_broken_chain(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        FileLog(tmp_path / "none.jsonl", tmp_path / "kept").purge(datetime.now(UTC))
    assert not (tmp_path / "none.jsonl").exists()

    records, sealed = examples()
    log = FileLog(tmp_path / "log.jsonl", tmp_path / "kept")
    for record in records:
        body = {k: v for k, v in record.items() if k not in ("seq", "prev", "digest")}
        log.append(body, {path: tuple(pair) for path, pair in sealed[record["seq"]].items()})
    later = datetime.now(UTC)
    with pytest.raises(FileNotFoundError):
        FileLog(log.path, tmp_path / "typo").purge(later)
    lines = log.path.read_bytes().splitlines(keepends=True)
    log.path.write_bytes(lines[0] + lines[2])
    with pytest.raises(ChainBroken):
        log.purge(later)
    assert sorted(p.name for p in log.kept.iterdir()) == ["0.json", "1.json", "2.json"]


def test_the_log_purge_refuses_a_folder_of_kept_values_open_to_others_or_a_link(
    tmp_path: Path,
) -> None:
    records, sealed = examples()
    log = FileLog(tmp_path / "log.jsonl", tmp_path / "kept")
    for record in records:
        body = {k: v for k, v in record.items() if k not in ("seq", "prev", "digest")}
        log.append(body, {path: tuple(pair) for path, pair in sealed[record["seq"]].items()})
    later = datetime.now(UTC)
    log.kept.chmod(0o750)
    with pytest.raises(KeptFolderRefused):
        log.purge(later)
    log.kept.chmod(0o700)
    (tmp_path / "link").symlink_to(log.kept)
    with pytest.raises(KeptFolderRefused):
        FileLog(log.path, tmp_path / "link").purge(later)
    assert sorted(p.name for p in log.kept.iterdir()) == ["0.json", "1.json", "2.json"]


def examples() -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    data = yaml.safe_load(EXAMPLES.read_text(encoding="utf-8"))
    return data["records"], data["sealed"]


def test_a_purge_removes_an_approvals_kept_draft_with_its_proposal(tmp_path: Path) -> None:
    records, sealed = examples()
    log = FileLog(tmp_path / "log.jsonl", tmp_path / "kept")
    for record in records:
        body = {k: v for k, v in record.items() if k not in ("seq", "prev", "digest")}
        log.append(body, {path: tuple(pair) for path, pair in sealed[record["seq"]].items()})
    at = [datetime.fromisoformat(r["at"]) for r in records]
    assert at[0] < at[1] < at[2]

    # Before the proposal: only the answer's values go.
    assert log.purge(at[1]) == [0]
    assert sorted(p.name for p in log.kept.iterdir()) == ["1.json", "2.json"]
    # Past the proposal but before its approval: the approval's `seen`, sealed under the draft's
    # salt, goes with the proposal's values.
    assert log.purge(at[1] + timedelta(seconds=1)) == [1, 2]
    assert list(log.kept.iterdir()) == []
    kept = read_log(log.path.read_bytes())
    verify_chain(kept)
    verify_links(kept)
    assert [r["digest"] for r in kept] == [r["digest"] for r in records]


# An invented personal value, as a person might type it into a question, and one in the form the
# store's session ids take.
PLANTED = "Planted Person, born 1970-01-02, planted.person@example.org"
SHAPED = "planted-person-1970-01-02-07700900123"


def test_a_planted_personal_value_never_reaches_the_logs(
    as_persona: AsPersona, audit: Audit, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    log = audit.log
    assert isinstance(log, FileLog)
    for question in (f"{QUESTION} {PLANTED}", PLANTED, QUESTION):
        ask(CHICAGO, question, as_persona=as_persona, audit=audit, model=Recorder())
    with pytest.raises(ask_module.NoPersonaError):
        ask(PLANTED, PLANTED, as_persona=as_persona, audit=audit, model=Recorder())

    assert len(read_log(log.path.read_bytes())) == 4
    for part in (PLANTED, "Planted Person", "1970-01-02", "planted.person@example.org"):
        assert part not in log.path.read_text(encoding="utf-8")
        assert part not in caplog.text
        assert all(part not in record.getMessage() for record in caplog.records)
    # The value went through the turn: it is among the kept values the purge deletes.
    assert any(PLANTED in p.read_text(encoding="utf-8") for p in log.kept.iterdir())


@pytest.mark.stack
def test_a_planted_personal_value_asked_in_a_session_never_reaches_the_logs(
    as_persona: AsPersona,
    audit: Audit,
    store: Sessions,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    log = audit.log
    assert isinstance(log, FileLog)
    session = store.start(CHICAGO)
    for question in (f"{QUESTION} {PLANTED}", PLANTED, QUESTION):
        ask(
            CHICAGO,
            question,
            as_persona=as_persona,
            audit=audit,
            model=Recorder(),
            conversation=Conversation(store, session),
        )
    # Named as the session: another's real session, the planted text, and a personal value of the
    # session ids' own form, which only the store can tell from a real id.
    for named in (session, PLANTED, SHAPED):
        with pytest.raises(ForeignSession):
            ask(
                UK,
                PLANTED,
                as_persona=as_persona,
                audit=audit,
                model=Recorder(),
                conversation=Conversation(store, named),
            )

    records = read_log(log.path.read_bytes())
    assert [r["outcome"] for r in records][-3:] == ["refused"] * 3
    written = capfd.readouterr()
    for part in (PLANTED, SHAPED, "Planted Person", "1970-01-02", "planted.person@example.org"):
        assert part not in log.path.read_text(encoding="utf-8")
        assert part not in caplog.text
        assert all(part not in record.getMessage() for record in caplog.records)
        assert part not in written.out + written.err
    # The value went through the session: its transcript keeps it until the purge.
    assert any(PLANTED in e.question for e in store.history(session, CHICAGO))
