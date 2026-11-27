import json, httpx
from tests.agent.test_bigquery_mode import Fake, asked, records, TOTALS, MIB

class Mixed(Fake):
    """Query 1 answers with no bill, query 2 fails with a 500, query 3 bills its whole cap."""
    def handle(self, request):
        body = json.loads(request.content)
        if body["query"] == "SELECT SESSION_USER()":
            return super().handle(request)
        n = len(self.queries) + 1
        if n == 1:
            self.bill = None
            return super().handle(request)
        if n == 2:
            self.bodies.append(body); self.tokens.append(request.headers["authorization"])
            return httpx.Response(500, json={"error": {"errors": [{"reason": "backendError", "message": "x"}]}})
        self.bill = int(body["maximumBytesBilled"])
        return super().handle(request)

def test_zz_mixed(audit):
    per_turn = 250 * MIB
    fake = Mixed()
    answer = asked(fake, audit, per_turn, *TOTALS)
    caps = [int(b["maximumBytesBilled"]) for b in fake.bodies]
    r = records(audit)[-1]
    print("\nCAPS", caps, "sum of query caps", sum(caps[1:]), "turn", per_turn, "<=", sum(caps[1:]) <= per_turn, "outcome", r["outcome"], r.get("reason"))
