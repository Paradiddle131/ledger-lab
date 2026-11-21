import logging
from pathlib import Path

import pytest

from agent import ask as ask_module
from agent.ask import ask
from agent.audit import FileLog, read_log
from agent.sessions import Conversation, ForeignSession
from tests.agent.test_sessions import CHICAGO, PLANTED, QUESTION, UK, Recorder, libpq, store  # noqa: F401


class Boom(Recorder):
    def step(self, turn):
        raise RuntimeError("model down")


@pytest.mark.stack
def test_planted_via_conversation(as_persona, audit, store, caplog):
    caplog.set_level(logging.DEBUG)
    first = store.start(CHICAGO)
    ask(CHICAGO, f"{QUESTION} {PLANTED}", as_persona=as_persona, audit=audit, model=Recorder(),
        conversation=Conversation(store, first))
    ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=Recorder(),
        conversation=Conversation(store, first))
    with pytest.raises(ForeignSession):
        ask(UK, PLANTED, as_persona=as_persona, audit=audit, model=Recorder(),
            conversation=Conversation(store, first))
    text = audit.log.path.read_text()
    print("in_log", PLANTED in text, "in_caplog", PLANTED in caplog.text)


@pytest.mark.stack
def test_failed_turn_number(as_persona, audit, store):
    s = store.start(CHICAGO)
    ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=Recorder(),
        conversation=Conversation(store, s))
    with pytest.raises(RuntimeError):
        ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=Boom(),
            conversation=Conversation(store, s))
    ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=Recorder(),
        conversation=Conversation(store, s))
    records = read_log(audit.log.path.read_bytes())
    print("TURNS", [(r["asker"]["session"] == s, r["asker"]["turn"], r["outcome"]) for r in records])


def test_purge_missing_log(tmp_path: Path):
    log = FileLog(tmp_path / "typo.jsonl", tmp_path / "kept")
    from datetime import UTC, datetime
    print("REMOVED", log.purge(datetime.now(UTC)), "EXISTS", (tmp_path / "typo.jsonl").exists())
