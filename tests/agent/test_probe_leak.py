# Probe with invented values only.
import logging
from pathlib import Path

import pytest

from agent.ask import ask
from agent.audit import FileLog, read_log
from agent.sessions import Conversation, ForeignSession
from tests.agent.test_sessions import CHICAGO, UK, Recorder, QUESTION, libpq, store  # noqa: F401

INVENTED = "zz-invented-person-07700900999-zz"


@pytest.mark.stack
def test_probe(as_persona, audit, store, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    log = audit.log
    other = store.start(CHICAGO)
    for named in (INVENTED, other, INVENTED + "\n", "a" * 64, "AB"):
        with pytest.raises(ForeignSession):
            ask(UK, QUESTION, as_persona=as_persona, audit=audit, model=Recorder(),
                conversation=Conversation(store, named))
    records = read_log(log.path.read_bytes())
    print("SESSIONS", [r["asker"] for r in records])
    root = Path(log.path).parent
    hits = []
    for p in root.rglob("*"):
        if p.is_file():
            t = p.read_text(encoding="utf-8", errors="replace")
            for v in (INVENTED, other):
                if v in t:
                    hits.append((str(p.relative_to(root)), v[:6]))
    print("HITS", hits, "KEPT_ROOT", log.kept)
    out = capfd.readouterr()
    assert INVENTED not in caplog.text + out.out + out.err
    assert other not in caplog.text
    assert not hits
