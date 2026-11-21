from pathlib import Path
from models.contracts import gate
from tests.models.test_payments_contract import HASHED, changed, git_repo, with_files
from tests.models.test_contract_gate import PATH, duplicate_rule

def test_no_contract_on_main(tmp_path: Path):
    repo = git_repo(tmp_path, {"README.md": "x\n"})
    ok, r = gate.check(with_files(repo, {PATH: changed(duplicate_rule(HASHED, query="select 0"))}))
    print("NOMAIN", ok, r)
