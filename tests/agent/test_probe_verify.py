import pytest
from agent.ask import ask
from agent.integrity import WITHHELD
from tests.agent.test_answer_integrity import CHICAGO, QUESTION, Saying, records, share_of_chicago, cited, failed_numbers
FORMS = ["-​{}", "−⁠{}", "-­{}", "(​{})", "➖{}", "˗{}", "⸺{}", "（{}）", "﹙{}﹚",
         "-US${}", "-HK${}", "-R$ {}", "-A${}", "-Rs {}", "-usd {}", "-**{}**", "(**{}**)", "-'{}'", "negative {}", "minus {}",
         "-${}", "({})", "- {}"]
@pytest.mark.parametrize("form", FORMS)
def test_probe(as_persona, audit, form):
    share = share_of_chicago(as_persona, audit)
    model = Saying("duplicate-payment-rate", f"The share fell to {form.format(repr(share))}.")
    answer = ask(CHICAGO, QUESTION, as_persona=as_persona, audit=audit, model=model)
    print("E2E", ascii(form), "shown" if answer.text != WITHHELD else "withheld", records(audit)[-1]["outcome"])
def test_text_cell_sign():
    print("TEXT", failed_numbers("fell to -12.25", cited((("12.25",),))))
def test_code_guards():
    print("CODE", failed_numbers("-ABCDEF 12.25", cited(((12.25,),))), failed_numbers("(USDEUR 12.25)", cited(((12.25,),))))
