import pytest
from agent.ask import ask
from agent.integrity import WITHHELD
from tests.agent.test_answer_integrity import CHICAGO, QUESTION, Saying, records, share_of_chicago
FORMS = ["The share is ‮{}‬.", "The share is mіnus {}.", "The share is down about {}.",
         "The share is О.ОƼ.", "The share is ⲻ {}.", "", "The share is high.",
         "The share is minus {}.", "The share is - {}.", "The share is {} in thousands.", "The share, in millions: {}."]
@pytest.mark.parametrize("form", FORMS)
def test_probe(as_persona, audit, form):
    share = repr(share_of_chicago(as_persona, audit))
    answer = ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=Saying("duplicate-payment-rate", form.format(share)))
    print("\nE2E", ascii(form), "cell", share, "withheld" if answer.text == WITHHELD else "SHOWN", records(audit)[-1]["outcome"])
