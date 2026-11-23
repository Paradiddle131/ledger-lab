import copy
from tests.models.test_contract_gate import *  # noqa
from tests.models.test_contract_gate import rule_key_twin, idless_twin_second, git_repo, weaken_first


def no_quality(c):
    for s in c["schema"]:
        s.pop("quality", None)
        for p in s.get("properties") or []:
            p.pop("quality", None)


def no_schema(c):
    c["schema"] = []


def test_empty_quality_fails(published_repo):
    ok, report = gate.check(with_files(published_repo, {PATH: changed(no_quality)}))
    print("EMPTY-QUALITY", ok, report[:300])
    assert not ok


def test_empty_schema_fails(published_repo):
    ok, report = gate.check(with_files(published_repo, {PATH: changed(no_schema)}))
    print("EMPTY-SCHEMA", ok, report[:300])
    assert not ok


def test_guard_on_nothing():
    print("GUARD-EMPTY", gate.repeated_rule_keys({}), gate.repeated_rule_keys({"schema": []}))


def test_non_dict_rule(published_repo):
    def bad(c):
        schema_of(c)["quality"].append("select 1")
    try:
        ok, report = gate.check(with_files(published_repo, {PATH: changed(bad)}))
        print("NONDICT", ok, report[:400])
    except Exception as e:
        print("NONDICT-RAISED", type(e).__name__, e)


def test_idless_twin_on_main_then_weaken(tmp_path):
    def twin(c):
        rule_key_twin(c, with_id=False)
    repo = git_repo(tmp_path, {PATH: changed(twin)})
    def weakened(c):
        rule_key_twin(c, with_id=False)
        next(q for q in c["schema"][0]["properties"] if q["name"]=="vendor_name")["quality"][0]["query"] = "select 0"
    ok, report = gate.check(with_files(repo, {PATH: changed(weakened)}))
    print("WEAKEN", ok, report[:600])
    assert not ok


def test_int_and_text_id(published_repo):
    def two(c):
        r = copy.deepcopy(next(q for q in schema_of(c)["quality"] if q["id"] == HASHED))
        schema_of(c)["quality"] += [{**r, "id": 7}, {**copy.deepcopy(r), "id": "7"}]
    print("INTTEXT", gate.repeated_rule_keys(committed() if False else (lambda c: (two(c), c)[1])(committed())))
