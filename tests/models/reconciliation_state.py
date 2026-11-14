"""The reconciliation's accepted state over the landed files, and how today's differs from it.

Two records of one accepted run: a digest per break class (SHA-256 of the class's contract numbers
each with its excess to the cent, sorted), committed in the landed test, and the per-contract
list itself, written beside the landing (`<landing>.reconciliation.csv`, outside the landing
folder and outside git) by `make reconcile-accept`. The digests prove in any clone that a class
holds exactly the accepted contracts and amounts; the list, where present, names the contract
numbers that entered, left or changed excess. Running this module accepts today's state: it builds
the project over the landing, writes the list and prints the table and digests to pin.
"""

import csv
import hashlib
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

from ingest.fetch import landing_path
from tests.models.dbt_build import Build, built, query

BREAKS = (
    "contract_not_found",
    "negative_award",
    "paid_under_two_names",
    "reduced_after_payment",
    "paid_before_fixed_award",
    "unexplained",
)
Rows = dict[str, dict[str, Decimal]]


def break_rows(build: Build) -> Rows:
    """Each break class's contract numbers with their excess over the award held to, or what was
    paid where the contracts table lacks the number."""
    rows: Rows = {kind: {} for kind in BREAKS}
    for kind, number, excess in query(
        build,
        "select break_class, contract_number, case when break_class = 'contract_not_found'"
        " then paid else difference end from chicago_contract_reconciliation where is_break",
    ):
        rows[kind][number] = Decimal(excess).quantize(Decimal("0.01"))
    return rows


def digests(rows: Rows) -> dict[str, str]:
    return {
        kind: hashlib.sha256(
            "\n".join(f"{n}|{v}" for n, v in sorted(members.items())).encode()
        ).hexdigest()
        for kind, members in rows.items()
    }


def accepted_path(landing: Path) -> Path:
    return landing.parent / f"{landing.name}.reconciliation.csv"


def write_accepted(path: Path, rows: Rows) -> None:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["break_class", "contract_number", "excess"])
        for kind in BREAKS:
            for number, excess in sorted(rows[kind].items()):
                writer.writerow([kind, number, f"{excess}"])


def read_accepted(path: Path) -> Rows:
    rows: Rows = {kind: {} for kind in BREAKS}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            rows.setdefault(row["break_class"], {})[row["contract_number"]] = Decimal(row["excess"])
    return rows


def changes(accepted: Rows, now: Rows) -> list[str]:
    """One line per contract number that entered, left or changed excess in a break class."""
    lines = []
    for kind in sorted(accepted.keys() | now.keys()):
        before, after = accepted.get(kind, {}), now.get(kind, {})
        for number in sorted(before.keys() | after.keys()):
            if number not in before:
                lines.append(f"{kind}: {number} entered, {after[number]}")
            elif number not in after:
                lines.append(f"{kind}: {number} left, was {before[number]}")
            elif before[number] != after[number]:
                lines.append(f"{kind}: {number} changed, {before[number]} to {after[number]}")
    return lines


def main() -> None:
    landing = landing_path().resolve()
    with tempfile.TemporaryDirectory() as tmp:
        build = built(Path(tmp), "landed", landing)
        rows = break_rows(build)
        table = query(
            build,
            "select break_class, count(*), sum(case when difference > 0 then difference"
            " when break_class = 'contract_not_found' then paid else 0 end)"
            " from chicago_contract_reconciliation group by 1 order by 2 desc",
        )
    path = accepted_path(landing)
    write_accepted(path, rows)
    print(f"accepted {sum(map(len, rows.values()))} breaks into {path}")
    print("table to pin:", table)
    print("digests to pin:", digests(rows))


if __name__ == "__main__":
    sys.exit(main())
