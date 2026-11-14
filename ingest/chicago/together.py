"""Chicago's contracts and payments, landed together as one set (`python -m ingest.fetch chicago`).

The City names one payee in both tables, and a payee one table hashes may sit in the other's
free text (a contract description naming someone paid only by voucher). So neither table lands
alone: the fetch kit lands the set only once both members have verified, and its step scrubs the
two together (`table.land_all`), hashing a name in both tables or neither and blanking any
description that names a payee either table hashes; payments carry no free text. The two landed
files keep their own names, `chicago-contracts` and `chicago-payments`. The step tells the members
apart by their served columns and refuses a set that is not exactly the two tables.
"""

import csv
import io
from collections.abc import Mapping

from ingest.chicago import contracts, payments
from ingest.chicago.table import Table, TableError, land_all
from ingest.entities import Register

SET = "chicago"
TABLES: dict[str, Table] = {
    "chicago-contracts": contracts.TABLE,
    "chicago-payments": payments.TABLE,
}


def step(
    files: Mapping[str, bytes],
    env: Mapping[str, str] | None = None,
    register: Register | None = None,
) -> dict[str, bytes]:
    """The verified members, by URL, as the two landed files by name."""
    served: dict[str, tuple[bytes, Table]] = {}
    for raw in files.values():
        header = tuple(next(csv.reader(io.StringIO(raw.decode("utf-8"), newline="")), ()))
        names = [name for name, table in TABLES.items() if table.columns == header]
        if len(names) != 1 or names[0] in served:
            raise TableError(
                "a member of the set is not one of the City's two tables, or repeats one"
            )
        served[names[0]] = (raw, TABLES[names[0]])
    if served.keys() != TABLES.keys():
        raise TableError("the set lacks one of the City's two tables")
    return land_all(served, env, register)
