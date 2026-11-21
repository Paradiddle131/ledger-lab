import copy
from pathlib import Path
import pytest
from models.contracts import gate
from tests.models.test_payments_contract import HASHED, changed, git_repo, with_files, committed
from tests.models.test_contract_gate import PATH, schema_of

def renumbered(rid, query=None):
    def change(c):
        q = schema_of(c)["quality"]
        orig = next(r for r in q if r["id"] == HASHED)
        orig["id"] = rid
        cp = copy.deepcopy(orig)
        if query: cp["query"] = query
        q.insert(0, cp)  # copy first, so the original (last) is what rules() keeps on main
    return change

@pytest.mark.parametrize("rid", [7, ""])
def test_two_step(tmp_path: Path, rid):
    # step 1: rule given id rid with a copy under it; gate vs committed main
    pub = git_repo(tmp_path.joinpath("a").mkdir() or tmp_path / "a", {PATH: changed(lambda c: None)})
    ok1, r1 = gate.check(with_files(pub, {PATH: changed(renumbered(rid))}))
    print("STEP1", rid, ok1, r1[-400:])
    repo = git_repo(tmp_path.joinpath("b").mkdir() or tmp_path / "b", {PATH: changed(renumbered(rid))})
    ok2, r2 = gate.check(with_files(repo, {PATH: changed(renumbered(rid, "select 0"))}))
    print("STEP2", rid, ok2, r2[-400:])
