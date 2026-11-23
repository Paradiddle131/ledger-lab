from agent.ask import ask, load_context
from agent.model import model_from_config
from tests.agent.test_answer_integrity import records
def test_all(as_persona, audit):
    qs = load_context()
    model = model_from_config({}, qs)
    for who in ["auditor", "accountant-chicago", "accountant-uk-transport"]:
        for name, q in qs.items():
            a = ask(who, q.question, as_persona=as_persona, audit=audit, model=model)
            r = records(audit)[-1]
            print("\nROW", who, name, r["outcome"], len(r.get("runs") or []))
