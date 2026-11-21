import re
from datetime import UTC, datetime
import pytest
from agent.ask import ask, Answer
from agent.integrity import trace, reproduce, WITHHELD
from agent.model import Citation, ToolCall, Reply
from tests.agent.test_answer_integrity import Saying, share_of_chicago, records, CHICAGO, QUESTION, OUTSIDE, AUDITOR, BY_YEAR, cited

def c(rows): return Citation("q","m",3,"select 1",tuple("abcdef"[:len(rows[0])]),rows,datetime(2020,1,2,tzinfo=UTC))

def test_pure():
    for t in ["-$12.25","($12.25)","−€12.25","- 12.25","( 12.25 )","USD-12.25","-12.25"]:
        print("SIGN", repr(t), [f.number for f in trace(t,[c(((12.25,),))]).failures])
    print("NL", [f.number for f in trace("paid Ⅻ",[c(((5.0,),))]).failures], [t.number for t in trace("paid Ⅻ",[c(((5.0,),))]).traced])
    for t in ["the total was 12.25","spend was 2024.5"]:
        r = trace(t,[c((("12" if "12." in t else "2024", 25 if "12." in t else 5),))])
        print("ASIDE", repr(t), [f.number for f in r.failures], [x.number for x in r.traced])
    r = trace("1,2345",[c(((1234,5),))]); print("THOU", [f.number for f in r.failures])
    print("EMPTY", reproduce(Answer(CHICAGO,"?","",(),()),(),lambda p: 1/0))

@pytest.mark.parametrize("form", ["-${s}", "(${s})", "−€{s}", "- {s}"])
def test_e2e_sign(as_persona, audit, form):
    share = share_of_chicago(as_persona, audit)
    a = ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=Saying("duplicate-payment-rate", "The share fell to " + form.format(s=repr(share)) + "."))
    print("E2E", repr(a.text), records(audit)[-1]["outcome"])

class CapThenRun:
    def plan(self, turn): return (ToolCall("duplicate-payment-rate"),)
    def step(self, turn): return ToolCall("not-a-planned-query")

def test_cap(as_persona, audit):
    a = ask(CHICAGO, OUTSIDE, as_persona=as_persona, audit=audit, model=CapThenRun())
    print("CAP", a.text, records(audit)[-1]["outcome"], [x.query for x in a.citations])
