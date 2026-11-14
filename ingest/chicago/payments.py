"""The City of Chicago's payments: how they land, and the profile of their mess.

`TABLE` is how the shared Chicago landing (`table.py`) turns the verified CSV into Parquet, together
with the contracts as one set (`together.py`). A payee who is a person, or may be one, leaves only
as a keyed hash. The table has no payee identifier, address or description, and its voucher and
contract numbers must be codes (a digit and no space, or one of the table's placeholders); a code
column that reads as words fails the step, since it could carry a name, and the set's landing
refuses it. The voucher and contract numbers stay in clear on a hashed row, as the keys that tie
payments to vouchers and contracts; the City's public search can look a payee up by them. The
department is one of the City's departments; five of them are also payees, hashed for lack of a
legal form, and stay in clear as departments. Every column lands as the text the publisher served,
with blanks as nulls; typing is the models' job.

`profile` counts the mess the models must survive. Payments older than two years are rolled up by
the publisher into one row per vendor, contract and year, dated by the bare year and carrying no
voucher; those from 1996 to 2002 share the year 2002, so no earlier year appears. A payment on no
contract carries `DV` (a direct voucher: settlements, refunds, utilities, reimbursements).
"""

import re
from pathlib import Path
from typing import TypedDict

from ingest.chicago.table import Table, TableError, counter

COLUMNS: tuple[str, ...] = (
    "voucher_number",
    "amount",
    "check_date",
    "department_name",
    "contract_number",
    "vendor_name",
)
CODES = ("voucher_number", "contract_number")
# A code holds a digit and no space, so it cannot spell a name; the table's two placeholders are the
# only codes without one: `DV`, no contract, and `Multiple`, a payment covering several vouchers.
CODE = re.compile(r"\S*\d\S*")
PLACEHOLDERS = frozenset({"DV", "Multiple"})

ITEMISED = r"regexp_full_match(check_date, '\d{2}/\d{2}/\d{4}')"
ROLLED_UP = r"regexp_full_match(check_date, '\d{4}')"
HASHED = "vendor_name LIKE 'person:%'"


class Profile(TypedDict):
    rows: int
    itemised_rows: int
    rolled_up_rows: int
    rolled_up_by_year: dict[str, int]
    without_contract: int
    negative_amounts: int
    rows_hashed: int
    payees_hashed: int


def _codes_only(row: dict[str, str]) -> None:
    for column in CODES:
        value = row[column]
        if value and value not in PLACEHOLDERS and not CODE.fullmatch(value):
            raise TableError(f"a {column} is not a code")


TABLE = Table(COLUMNS, name="vendor_name", check=_codes_only)


def profile(path: Path) -> Profile:
    """The landed file's mess, counted."""
    con, one = counter(path)
    years = con.execute(
        f"SELECT check_date, count(*) FROM t WHERE {ROLLED_UP} GROUP BY 1 ORDER BY 1"  # noqa: S608
    ).fetchall()
    return Profile(
        rows=one("SELECT count(*) FROM t"),
        itemised_rows=one(f"SELECT count(*) FROM t WHERE {ITEMISED}"),  # noqa: S608
        rolled_up_rows=one(f"SELECT count(*) FROM t WHERE {ROLLED_UP}"),  # noqa: S608
        rolled_up_by_year={str(y): int(n) for y, n in years},
        without_contract=one(
            "SELECT count(*) FROM t WHERE contract_number IS NULL OR contract_number = 'DV'"
        ),
        negative_amounts=one("SELECT count(*) FROM t WHERE TRY_CAST(amount AS DECIMAL(18, 2)) < 0"),
        rows_hashed=one(f"SELECT count(*) FROM t WHERE {HASHED}"),  # noqa: S608
        payees_hashed=one(f"SELECT count(DISTINCT vendor_name) FROM t WHERE {HASHED}"),  # noqa: S608
    )
