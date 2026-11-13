"""The City of Chicago's contracts: the step that lands them, and the profile of their mess.

`step` turns the verified CSV into Parquet through the shared Chicago landing (`table.py`). A vendor
who is a person, or may be one, leaves only as a keyed hash, with the vendor id on that row hashed
beside it (the same id stays in clear on a row whose vendor name is clear). On their rows the
purchase-order description (which repeats the name in truncated and reordered forms no replacement
catches), ZIP code and the link to the signed contract are blanked. The street address columns
are left out on every row, since they carry contact people (`ATTN`, `C/O`) and nothing needs them.
On every row a description holding a hashed vendor's name, in any order and punctuation, or a
marker introducing a person (the estate of, a deceased) is blanked; an organisation row's
description is otherwise landed as served, and is the known remaining place a person's name could
sit. The contract and specification numbers stay in clear, as the keys that tie payments to
contracts; the City's public search can look a hashed vendor up by them. A company named after a
person passes in clear, as the pseudonymiser allows, even where that person is hashed on other
rows. Every column lands as the text the publisher served, with blanks as nulls; typing is the
models' job.

`profile` counts the mess the models must survive: contracts carrying several revisions,
negative awards (revisions that lower an award), revision numbers that are not numbers, the
order-number formats in use, and how many hashed rows still carry a contract or specification
number.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

from ingest.chicago.table import Table, counter, land

COLUMNS: tuple[str, ...] = (
    "purchase_order_description",
    "purchase_order_contract_number",
    "revision_number",
    "specification_number",
    "contract_type",
    "start_date",
    "end_date",
    "approval_date",
    "department",
    "vendor_name",
    "vendor_id",
    "address_1",
    "address_2",
    "city",
    "state",
    "zip",
    "award_amount",
    "procurement_type",
    "contract_pdf",
)
TABLE = Table(
    COLUMNS,
    name="vendor_name",
    identifier="vendor_id",
    personal=("purchase_order_description", "zip", "contract_pdf"),
    dropped=("address_1", "address_2"),
    descriptions=("purchase_order_description",),
)


HASHED = "vendor_name LIKE 'person:%'"


class Profile(TypedDict):
    rows: int
    contracts: int
    contracts_with_several_rows: int
    most_rows_for_one_contract: int
    negative_awards: int
    non_numeric_revisions: int
    vendors_hashed: int
    hashed_rows: int
    hashed_rows_with_contract_number: int
    hashed_rows_with_specification_number: int
    order_number_formats: dict[str, int]


def step(raw: bytes, env: Mapping[str, str] | None = None) -> bytes:
    """The verified CSV as Parquet, with every vendor who may be a person hashed."""
    return land(raw, TABLE, env)


def profile(path: Path) -> Profile:
    """The landed file's mess, counted."""
    con, one = counter(path)
    shapes = con.execute(
        "SELECT regexp_replace(regexp_replace(purchase_order_contract_number, '[0-9]', '9', 'g'),"
        " '[A-Za-z]', 'A', 'g') AS shape, count(*) AS n FROM t GROUP BY shape ORDER BY n DESC"
    ).fetchall()
    return Profile(
        rows=one("SELECT count(*) FROM t"),
        contracts=one("SELECT count(DISTINCT purchase_order_contract_number) FROM t"),
        contracts_with_several_rows=one(
            "SELECT count(*) FROM (SELECT 1 FROM t GROUP BY purchase_order_contract_number"
            " HAVING count(*) > 1)"
        ),
        most_rows_for_one_contract=one(
            "SELECT max(n) FROM"
            " (SELECT count(*) AS n FROM t GROUP BY purchase_order_contract_number)"
        ),
        negative_awards=one(
            "SELECT count(*) FROM t WHERE TRY_CAST(award_amount AS DECIMAL(18, 2)) < 0"
        ),
        non_numeric_revisions=one(
            "SELECT count(*) FROM t WHERE TRY_CAST(revision_number AS INTEGER) IS NULL"
        ),
        vendors_hashed=one(
            "SELECT count(DISTINCT vendor_name) FROM t WHERE vendor_name LIKE 'person:%'"
        ),
        hashed_rows=one(f"SELECT count(*) FROM t WHERE {HASHED}"),  # noqa: S608
        hashed_rows_with_contract_number=one(
            f"SELECT count(purchase_order_contract_number) FROM t WHERE {HASHED}"  # noqa: S608
        ),
        hashed_rows_with_specification_number=one(
            f"SELECT count(specification_number) FROM t WHERE {HASHED}"  # noqa: S608
        ),
        order_number_formats={str(k): int(v) for k, v in shapes},
    )
