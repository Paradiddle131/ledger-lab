from access.bigquery import PersonaSession
from agent.bigquery import BigQueryConnection
from tests.agent.test_bigquery_mode import AGENT_ID, CHICAGO, QUESTION, TOTALS, Calls, Fake, account
from agent.ask import ask
class Swapping:
    def __init__(self, first, then): self.first, self.then, self.n = first, then, 0
    def query(self, *a, **k):
        self.n += 1
        return (self.first if self.n == 1 else self.then).query(*a, **k)
def test_zz_swap(audit):
    fake = Fake(); good = fake.connect()(CHICAGO)
    agent = PersonaSession("the-lab", f"token-x:{account(AGENT_ID)}", fake.client)
    swapped = BigQueryConnection(CHICAGO, "the-lab", Swapping(good.session, agent), good.relations)
    answer = ask(CHICAGO, QUESTION, as_persona=lambda _: swapped, audit=audit, model=Calls("total-1"), queries=TOTALS)
    print("\nSWAP tokens per job:", [t.split(":", 1)[1] for t in fake.tokens], "citations", len(answer.citations))
