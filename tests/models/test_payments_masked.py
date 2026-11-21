"""The masked copy of the payments product (`models/models/marts/payments/payments_masked.sql`).

Its masking list is the column list and rules of `access/columns.yaml` for the payments table, it
holds the mart's columns in the mart's order, and on the sample build each masked column is the
persona databases' rendering of its rule (`access/compile.py`'s `masked`) and every other column
the mart's own value, row for row.
"""

import re
from typing import Any

from access.compile import Column, masked
from tests.conftest import REPO
from tests.models.dbt_build import Build, query

MODEL = REPO / "models/models/marts/payments/payments_masked.sql"


def masking_list() -> dict[str, str]:
    block = re.search(r"set masked = \{(.*?)\}", MODEL.read_text(), re.DOTALL)
    assert block, "payments_masked.sql sets no `masked`"
    return dict(re.findall(r"'([a-z_]+)': '([a-z-]+)'", block.group(1)))


def test_the_masking_list_is_the_column_lists_rules(columns: dict[str, Any]) -> None:
    rules = {t["id"]: t["masking"] for t in columns["tags"]}
    want = {
        column.split(".", 1)[1]: rules[tag]
        for column, tag in columns["products"]["payments"]["columns"].items()
        if column.startswith("payments.")
    }
    assert masking_list() == want


def names(build: Build, table: str) -> list[str]:
    return [
        row[0]
        for row in query(
            build,
            "select column_name from information_schema.columns where table_name = ?"
            " order by ordinal_position",
            table,
        )
    ]


def test_the_copy_holds_the_marts_columns_masked_as_the_persona_databases_mask_them(
    build: Build,
) -> None:
    columns = names(build, "payments")
    assert names(build, "payments_masked") == columns
    rules = masking_list()
    expected = ", ".join(
        masked(Column(c, "VARCHAR", rules[c])) if c in rules else f'"{c}"' for c in columns
    )
    differ = query(
        build,
        f"select count(*) from (select {expected} from payments"  # noqa: S608
        " except all select * from payments_masked)",
    )
    assert differ == [(0,)]
    assert query(build, "select count(*) from payments") == query(
        build, "select count(*) from payments_masked"
    )
