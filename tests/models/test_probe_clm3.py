import copy
from pathlib import Path
from models.contracts import gate
from tests.models.test_payments_contract import HASHED, CONTRACT, changed, git_repo, with_files

def dup(query=None):
    def change(c):
        (s,) = c["schema"]
        cp = copy.deepcopy(next(q for q in s["quality"] if q["id"] == HASHED))
        if query: cp["query"] = query
        s["quality"].append(cp)
    return change

def test_before(tmp_path: Path):
    (tmp_path/"a").mkdir(); (tmp_path/"b").mkdir()
    pub = git_repo(tmp_path/"a", {str(gate.CONTRACT): CONTRACT.read_text()})
    print("S1", gate.check(with_files(pub, {str(gate.CONTRACT): changed(dup())}))[0], gate.__file__)
    repo = git_repo(tmp_path/"b", {str(gate.CONTRACT): changed(dup())})
    print("S2", gate.check(with_files(repo, {str(gate.CONTRACT): changed(dup("select 0"))}))[0])

def weaken_first(c):
    dup()(c)
    (s,) = c["schema"]
    next(q for q in s["quality"] if q["id"] == HASHED)["query"] = "select 0"

def test_report(tmp_path: Path):
    repo = git_repo(tmp_path, {str(gate.CONTRACT): changed(dup())})
    ok, r = gate.check(with_files(repo, {str(gate.CONTRACT): changed(dup("select 0"))}))
    print("COPY", ok, r[-300:])
    ok, r = gate.check(with_files(repo, {str(gate.CONTRACT): changed(weaken_first)}))
    print("ORIG", ok, r[-300:])
