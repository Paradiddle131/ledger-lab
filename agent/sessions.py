"""The own agent's conversations: one person to one session, transcripts kept for a retention.

A session belongs to the persona that started it. `Sessions` keeps each session and its transcript
on the stack's shared PostgreSQL server, in the `agent_sessions` database
(`platform/stack/compose.yaml`), as two tables:
- `agent_session`: the session's id, the persona it belongs to and when it started;
- `agent_exchange`: per turn of a session, its number, when it ended, the question as asked and
  the text the turn showed, and nothing else of the turn (no rows, no SQL, no refusals).

What `Sessions` does:
- `start(persona)` opens a session for that persona and returns its id;
- `history(session, persona)` returns the session's exchanges in turn order, read with a query
  that names both the session and the persona, and raises `ForeignSession` when no session of
  that id belongs to that persona (another persona's, a purged one or one never started), so a
  persona never reads another's transcript through it;
- `add(session, persona, exchange, at)` keeps one exchange, refusing (`ForeignSession`) a session
  that is not that persona's;
- `purge(before)` deletes every exchange that ended before `before`, then every session that
  started before it and has no exchange left, in one transaction, and returns the counts.

`ask(..., conversation=Conversation(store, session))` (`agent/ask.py`) reads the history before
the turn, shows it to the model on every step but the planning one, writes the audit record with
that session id and the number after its last kept exchange, then keeps the exchange; without a
conversation, each `ask` is a session of one turn that keeps no transcript.

`purge(store, log, now)` is the one purge: it takes `RETENTION`, the one setting both purges read,
from `now`, deletes the audit log's kept values and salts of the records written before that instant
(`agent.audit.FileLog.purge`, which refuses a missing log, a log whose chain does not verify and a
missing folder of kept values, removing nothing), then the transcripts that ended before it
(`Sessions.purge`), so the
record still shows the turn happened, who asked and which query ran on which data, and no figure
in it can be checked any more. `python -m agent.sessions purge --log <log> --kept <folder>` runs it
now, connecting by libpq's own variables (`PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`,
`PGDATABASE`, ...), and prints the counts it removed; a `--log` that is not a file is refused
before it connects (exit 2), and a refusal of the log's purge exits with its error.

Stated limits:
- the scoping is the queries': any client holding the database's credentials reads every
  transcript, and the stack has one database user;
- the transcript holds the question and the answer in clear, personal values a person typed
  included, until the purge removes them; nothing here encrypts them;
- nothing runs the purge on a schedule: whoever operates the agent runs the command;
- an exchange is kept after its turn's audit record is written: when keeping it raises, the turn
  is recorded and its answer is not returned, and the session misses that exchange;
- the turn number is the one after the session's last kept exchange: a failed turn keeps no
  exchange, so the session's next turn is recorded under the failed turn's number again, and a
  `foreign-session` refusal is recorded as turn 1;
- two turns in one session at once may take the same turn number, and the second exchange is then
  refused by the table's key after its record was written;
- a `--kept` folder that exists but belongs to another log is not told apart from the log's own.
"""

import argparse
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import psycopg

from agent.audit import FileLog
from agent.model import Exchange

# How long a transcript and an audit record's kept values and salts are kept, from the end of the
# turn: the one setting both purges read.
RETENTION = timedelta(days=30)

TABLES = """
create table if not exists agent_session (
    id text primary key,
    persona text not null,
    started timestamptz not null
);
create table if not exists agent_exchange (
    session text not null references agent_session (id) on delete cascade,
    turn integer not null check (turn >= 1),
    ended timestamptz not null,
    question text not null,
    answer text not null,
    primary key (session, turn)
);
"""


class ForeignSession(PermissionError):
    """A session that is not the asking persona's: another's, purged, or never started. Its message
    names neither the session's owner nor any of its exchanges."""


class SessionStore(Protocol):
    def history(self, session: str, persona: str) -> tuple[Exchange, ...]: ...

    def add(self, session: str, persona: str, exchange: Exchange, at: datetime) -> None: ...


@dataclass(frozen=True)
class Conversation:
    """The session a question is asked in, and the store that keeps its transcript."""

    store: SessionStore
    session: str


@dataclass(frozen=True)
class Purged:
    """What one purge removed: exchanges, sessions and the audit log's positions of kept values."""

    exchanges: int
    sessions: int
    kept: tuple[int, ...]


class Sessions:
    """The sessions and their transcripts on one PostgreSQL connection, which stays its caller's
    to close."""

    def __init__(self, connection: psycopg.Connection) -> None:
        self.connection = connection

    @classmethod
    def connect(cls, conninfo: str = "") -> "Sessions":
        """A store on a new connection; an empty `conninfo` takes libpq's environment variables."""
        return cls(psycopg.connect(conninfo))

    def create(self) -> None:
        """Create the two tables where they do not exist."""
        with self.connection.transaction():
            self.connection.execute(TABLES)

    def start(self, persona: str, at: datetime | None = None) -> str:
        session = uuid.uuid4().hex
        with self.connection.transaction():
            self.connection.execute(
                "insert into agent_session (id, persona, started) values (%s, %s, %s)",
                (session, persona, at or datetime.now(UTC)),
            )
        return session

    def _owned(self, session: str, persona: str) -> None:
        found = self.connection.execute(
            "select 1 from agent_session where id = %s and persona = %s", (session, persona)
        ).fetchone()
        if found is None:
            raise ForeignSession("no session of that id belongs to the asking persona")

    def history(self, session: str, persona: str) -> tuple[Exchange, ...]:
        with self.connection.transaction():
            self._owned(session, persona)
            rows = self.connection.execute(
                "select e.turn, e.question, e.answer from agent_exchange e"
                " join agent_session s on s.id = e.session"
                " where s.id = %s and s.persona = %s order by e.turn",
                (session, persona),
            ).fetchall()
        return tuple(Exchange(turn, question, answer) for turn, question, answer in rows)

    def add(self, session: str, persona: str, exchange: Exchange, at: datetime) -> None:
        with self.connection.transaction():
            self._owned(session, persona)
            self.connection.execute(
                "insert into agent_exchange (session, turn, ended, question, answer)"
                " values (%s, %s, %s, %s, %s)",
                (session, exchange.turn, at, exchange.question, exchange.answer),
            )

    def purge(self, before: datetime) -> tuple[int, int]:
        """Delete the exchanges that ended before `before`, then the sessions started before it
        with no exchange left; the counts of each."""
        with self.connection.transaction():
            exchanges = self.connection.execute(
                "delete from agent_exchange where ended < %s", (before,)
            ).rowcount
            sessions = self.connection.execute(
                "delete from agent_session s where s.started < %s and not exists"
                " (select 1 from agent_exchange e where e.session = s.id)",
                (before,),
            ).rowcount
        return exchanges, sessions


def purge(store: Sessions, log: FileLog, now: datetime) -> Purged:
    """Remove the audit log's kept values, then the transcripts, older than `RETENTION` at `now`.

    The log goes first, so a log the purge refuses (`FileLog.purge`) leaves the transcripts as they
    were and the command fails; when the transcripts' purge then raises, the kept values are
    already gone and a second run removes the transcripts."""
    before = now - RETENTION
    kept = tuple(log.purge(before))
    exchanges, sessions = store.purge(before)
    return Purged(exchanges, sessions, kept)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent.sessions")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("purge", help=f"remove what is older than {RETENTION.days} days")
    command.add_argument("--log", type=Path, required=True, help="the audit log file")
    command.add_argument("--kept", type=Path, required=True, help="its folder of kept values")
    arguments = parser.parse_args(argv)
    if not arguments.log.is_file():
        parser.error(f"--log {arguments.log} is not a file: the purge needs the log that exists")
    store = Sessions.connect()
    try:
        store.create()
        done = purge(store, FileLog(arguments.log, arguments.kept), datetime.now(UTC))
    finally:
        store.connection.close()
    print(
        f"purged {done.exchanges} exchanges, {done.sessions} sessions and the kept values of"
        f" {len(done.kept)} audit records older than {RETENTION.days} days"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
