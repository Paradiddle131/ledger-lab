from pathlib import Path
import pytest
from semantics import versions
from tests.semantics.test_metric_versions import REAL, working, replaced

SQL = "models/models/marts/group_totals/group_totals.sql"
CHI = "models/models/intermediate/chicago/_chicago__models.yml"

def node_config(path, name, config):
    return replaced(path, f"  - name: {name}\n", f"  - name: {name}\n    config: {config}\n")

def twin_payments(files):
    files[SQL] = files[SQL].replace("\nselect", "\n{{ config(enabled=(target.type != 'bigquery')) }}\nselect", 1)
    files["models/models/scratch/group_totals.sql"] = "{{ config(enabled=(target.type == 'bigquery'), schema='payments') }}\n" + REAL[SQL]

def bq_only_in_folder(files):
    files["models/models/marts/group_totals/payment_rows.sql"] = "{{ config(enabled=(target.type == 'bigquery')) }}\nselect * from {{ ref('payments') }}\n"

def twin(files):
    files[SQL] = files[SQL].replace("\nselect", "\n{{ config(enabled=(target.type != 'bigquery')) }}\nselect", 1)
    files["models/models/scratch/group_totals.sql"] = "{{ config(enabled=(target.type == 'bigquery'), database='elsewhere', schema='scratch') }}\n" + REAL[SQL]

def stored(files):
    files["models/tests/marts/probe_every_payment.sql"] = "{{ config(schema='group_totals', store_failures=true, alias='payees_probe') }}\nselect vendor_name, amount, payment_date from {{ ref('int_chicago__payments') }}\n"

EDITS = {
    "twin_bq_only": twin,
    "twin_payments_schema": twin_payments,
    "bq_only_in_folder": bq_only_in_folder,
    "alias_by_target": replaced(SQL, "\nselect", "\n{{ config(alias=('group_totals' if target.type != 'bigquery' else 'gt_elsewhere')) }}\nselect"),
    "schema_else_none": node_config(CHI, "int_chicago__payments", "{schema: \"{{ 'payments' if target.type == 'bigquery' else none }}\"}"),
    "test_store_failures": stored,
}

@pytest.mark.parametrize("name", list(EDITS))
def test_probe(tmp_path: Path, name: str) -> None:
    ok, report = versions.check(working(tmp_path, EDITS[name]))
    print("\nPROBE", name, ok, report.replace("\n", " | ")[:300])
