"""The City of Chicago's contracts: the step that lands them, and the profile of their mess.

`step` turns the verified CSV into Parquet. A vendor who is a person, or may be one, leaves only
as a keyed hash, and nothing else in the row names or locates them: the purchase-order
description, which repeats the name in forms no replacement catches (truncated, reordered), and
the street address and ZIP code are dropped. Every column
lands as the text the publisher served, with blanks as nulls; typing is the models' job.

`profile` counts the mess the models must survive: contracts carrying several revisions,
negative awards (revisions that lower an award), revision numbers that are not numbers, and the
order-number formats in use.
"""

import csv
import io
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

import duckdb

from ingest.individuals import pseudonymiser

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
# What would still name or locate a vendor who is a person once their name is hashed.
PERSON_LOCATORS = ("purchase_order_description", "address_1", "address_2", "zip")


class Profile(TypedDict):
    rows: int
    contracts: int
    contracts_with_several_rows: int
    most_rows_for_one_contract: int
    negative_awards: int
    non_numeric_revisions: int
    vendors_hashed: int
    order_number_formats: dict[str, int]


class ContractsError(ValueError):
    """The served file is not the contracts table this step knows."""


def _hide_person(row: dict[str, str], hidden: str) -> None:
    row["vendor_name"] = hidden
    for column in PERSON_LOCATORS:
        row[column] = ""


def step(raw: bytes, env: Mapping[str, str] | None = None) -> bytes:
    """The verified CSV as Parquet, with every vendor who may be a person hashed."""
    hide = pseudonymiser(env)
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""))
    if tuple(reader.fieldnames or ()) != COLUMNS:
        raise ContractsError("the served columns differ from the contracts table's")
    with tempfile.TemporaryDirectory() as work:
        scrubbed, parquet = Path(work, "contracts.csv"), Path(work, "contracts.parquet")
        with scrubbed.open("w", newline="") as out:
            writer = csv.DictWriter(out, COLUMNS)
            writer.writeheader()
            for row in reader:
                hidden = hide(row["vendor_name"] or None)
                if hidden is not None and hidden != row["vendor_name"]:
                    _hide_person(row, hidden)
                writer.writerow(row)
        duckdb.read_csv(str(scrubbed), header=True, all_varchar=True).write_parquet(str(parquet))
        return parquet.read_bytes()


def profile(path: Path) -> Profile:
    """The landed file's mess, counted."""
    con = duckdb.connect()
    con.read_parquet(str(path)).create_view("c")

    def one(sql: str) -> int:
        row = con.execute(sql).fetchone()
        if row is None:
            raise ContractsError(f"no answer from {sql!r}")
        return int(row[0])

    shapes = con.execute(
        "SELECT regexp_replace(regexp_replace(purchase_order_contract_number, '[0-9]', '9', 'g'),"
        " '[A-Za-z]', 'A', 'g') AS shape, count(*) AS n FROM c GROUP BY shape ORDER BY n DESC"
    ).fetchall()
    return Profile(
        rows=one("SELECT count(*) FROM c"),
        contracts=one("SELECT count(DISTINCT purchase_order_contract_number) FROM c"),
        contracts_with_several_rows=one(
            "SELECT count(*) FROM (SELECT 1 FROM c GROUP BY purchase_order_contract_number"
            " HAVING count(*) > 1)"
        ),
        most_rows_for_one_contract=one(
            "SELECT max(n) FROM"
            " (SELECT count(*) AS n FROM c GROUP BY purchase_order_contract_number)"
        ),
        negative_awards=one(
            "SELECT count(*) FROM c WHERE TRY_CAST(award_amount AS DECIMAL(18, 2)) < 0"
        ),
        non_numeric_revisions=one(
            "SELECT count(*) FROM c WHERE TRY_CAST(revision_number AS INTEGER) IS NULL"
        ),
        vendors_hashed=one(
            "SELECT count(DISTINCT vendor_name) FROM c WHERE vendor_name LIKE 'person:%'"
        ),
        order_number_formats={str(k): int(v) for k, v in shapes},
    )
