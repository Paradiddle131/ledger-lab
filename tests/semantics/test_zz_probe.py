import duckdb
from tests.semantics.test_verified_queries import BY_ID, QUERIES, verify, ask_mf, run_sql

def test_probe(build):
    with duckdb.connect(str(build.database), read_only=True) as db:
        for q in [
            "select count(*) from chicago_contract_reconciliation",
            "select count(*) from chicago_contract_reconciliation where award_held_to > 0 and paid > award_held_to",
            "select count(*) from chicago_contract_reconciliation where award_held_to <> award_after_revisions",
            "select count(*) from chicago_contract_reconciliation where award_held_to < 0",
            "select count(*) from chicago_contract_reconciliation where last_ends_on = date '2026-10-31'",
            "select basis, count(*), count(fiscal_period), count(amount_at_constant_rates) from group_totals group by basis",
        ]:
            print(q, db.execute(q).fetchall())
    print("N queries", len(QUERIES))
