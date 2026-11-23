import copy
from tests.models.test_contract_gate import *  # noqa
from tests.models.test_contract_gate import published_repo  # noqa

def two_idless_in_one_list(c):
    s = schema_of(c)
    r = copy.deepcopy(next(q for q in s["quality"] if q["id"] == HASHED))
    del r["id"]
    r2 = copy.deepcopy(r); r2["description"] = "second"
    s["quality"] += [r, r2]

def int_named_property(c):
    rule = copy.deepcopy(next(q for q in schema_of(c)["quality"] if q["id"] == HASHED))
    del rule["id"]
    schema_of(c)["properties"].append({"name": 5, "logicalType": "string", "physicalType": "VARCHAR", "physicalName": "five", "quality": [copy.deepcopy(rule)]})
    c["schema"].append({"name": "payments.5", "physicalName": "payee_rules",
        "properties": [{"name": "k", "logicalType": "string", "physicalType": "VARCHAR"}], "quality": [rule]})

def test_idless_pair(published_repo):
    ok, report = gate.check(with_files(published_repo, {PATH: declared(two_idless_in_one_list, "2.0.0")}))
    print("IDLESS", ok, report[:600])
    assert "both read as" not in report

def test_int_prop(published_repo):
    try:
        ok, report = gate.check(with_files(published_repo, {PATH: declared(int_named_property, "2.0.0")}))
    except Exception as e:
        print("INTPROP raised", type(e).__name__, e); return
    print("INTPROP", ok, report[:900])
