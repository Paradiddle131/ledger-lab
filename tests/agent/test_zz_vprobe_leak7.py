import dataclasses, json
from tests.agent.test_bigquery_mode import Fake, TOTALS, CHICAGO, QUESTION
from agent.ask import ask
from agent.model import Reply

class Seeing:
    def __init__(self): self.turns = []
    def plan(self, turn): self.turns.append(turn); from agent.model import ToolCall; return (ToolCall("total-1"),)
    def step(self, turn):
        self.turns.append(turn)
        from agent.model import ToolCall
        return ToolCall("total-1") if not turn.results else Reply("done")

def test_zz_leak7(audit):
    fake = Fake(); model = Seeing()
    answer = ask(CHICAGO, QUESTION, as_persona=fake.connect(), audit=audit, model=model, queries=TOTALS)
    a = json.dumps(dataclasses.asdict(answer), default=str)
    t = json.dumps([dataclasses.asdict(x) for x in model.turns], default=str)
    print("\nANSWER has project/job:", "the-lab" in a, "job_" in a, "| TURNS has project/job:", "the-lab" in t, "job_" in t, "| turns seen", len(model.turns), "citations", len(answer.citations))
