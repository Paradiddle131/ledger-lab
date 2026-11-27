import httpx, json
from tests.agent.test_bigquery_mode import Fake, asked, records, TOTALS, MIB

class Slow(Fake):
    def handle(self, request):
        body = json.loads(request.content)
        if body["query"] == "SELECT SESSION_USER()":
            return super().handle(request)
        self.bodies.append(body)
        self.tokens.append(request.headers["authorization"])
        return httpx.Response(200, json={"jobComplete": False, "jobReference": {"projectId": "the-lab", "location": "EU", "jobId": "j"}})

def test_zz_timecap(audit):
    fake = Slow()
    answer = asked(fake, audit, 500 * MIB, *TOTALS)
    r = records(audit)[-1]
    print("\nOUTCOME", r["outcome"], r.get("reason"), "queries sent:", len(fake.queries), "refusals:", [x.reason for x in answer.refusals])
