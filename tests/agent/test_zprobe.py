import json
from agent.ask import ask
from agent.model import ToolCall
class CapThenRun:
    def plan(self, turn): return (ToolCall("duplicate-payment-rate"),)
    def step(self, turn): return ToolCall("not-a-planned-query")
def test_cap(as_persona, audit):
    a = ask("accountant-chicago", "Which vendor was paid the most?", as_persona=as_persona, audit=audit, model=CapThenRun())
    rec = [json.loads(l) for l in audit.log.path.read_text().splitlines()][-1]
    print("CAPBASE", a.text, rec["outcome"], [x.query for x in a.citations])
