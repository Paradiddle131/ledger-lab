"""The UK Department for Transport's spend sample: the committed real rows
(`uk_transport_sample.yaml`) served again as the Department serves its monthly files and landed
through the landing step with a test key and the vendor seeds' register, so every hashed cell is
the step's own work with that key and no hash made with a real key enters the tree. The UK sample
seed holds exactly what this lands (`python -m tests.models.uk_transport_sample` rewrites it).

Contains public sector information licensed under the Open Government Licence v3.0.
"""

import csv
import io
import tempfile
from pathlib import Path

import duckdb
import yaml

from ingest import fetch
from ingest.entities import Record, Register
from ingest.individuals import KEY_VARIABLE
from ingest.uk_transport.spend import COLUMNS, FAMILY, LAYOUTS, SET, step

SAMPLE = Path(__file__).with_name("uk_transport_sample.yaml")
MODELS = Path(__file__).resolve().parents[2] / "models"
SEED = MODELS / "seeds" / "staging" / "uk_transport" / "uk_transport_spend_sample.csv"
REGISTER = MODELS / "seeds" / "intermediate" / "vendors" / "vendor_legal_entities_sample.csv"
# The key the sample is landed with: a test key, so no committed row depends on the real one.
TEST_KEY = {KEY_VARIABLE: "test-key-" + "k" * 32}


def sample_rows() -> list[tuple[str, ...]]:
    """The committed rows: file, paying body, date, supplier, transaction number, amount."""
    return [tuple(r) for r in yaml.safe_load(SAMPLE.read_text())["rows"]]


def served_files(rows: list[tuple[str, ...]]) -> dict[str, bytes]:
    """Each pinned month's file, by its pinned URL, holding these rows as the Department serves a
    month: its header, Windows-1252, CRLF line ends."""
    urls = {
        entry["url"].rsplit("/", 1)[1]: entry["url"]
        for name, entry in fetch.load_sources(fetch.SOURCES).items()
        if name.startswith(f"{SET}-")
    }
    files: dict[str, io.StringIO] = {}
    for file, body, day, supplier, number, amount in rows:
        if file not in files:
            files[file] = io.StringIO()
            csv.writer(files[file], lineterminator="\r\n").writerow(LAYOUTS[0])
        cells = [FAMILY, body, day, "", "", supplier, number, "", amount, ""]
        csv.writer(files[file], lineterminator="\r\n").writerow(cells)
    return {urls[f]: text.getvalue().encode("cp1252") for f, text in files.items()}


def landed() -> list[tuple[str | None, ...]]:
    """The sample's rows as the step lands them, in `COLUMNS`, every cell as text or null."""
    with REGISTER.open() as f:
        register = Register(
            Record(
                r["Entity.LegalName"],
                r["Entity.EntityCategory"],
                r["Entity.LegalForm.EntityLegalFormCode"],
            )
            for r in csv.DictReader(f)
        )
    parquet = step(served_files(sample_rows()), TEST_KEY, register)
    columns = ", ".join(f'cast("{c}" as varchar)' for c in COLUMNS)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / SET
        path.write_bytes(parquet)
        sql = f"select {columns} from read_parquet(?) order by source_file, source_row"  # noqa: S608
        return duckdb.connect().execute(sql, [str(path)]).fetchall()


def seed_rows() -> list[tuple[str | None, ...]]:
    """The committed seed's rows, an empty cell read as null as dbt reads it."""
    with SEED.open(newline="") as f:
        rows = list(csv.reader(f))
    assert tuple(rows[0]) == COLUMNS
    return [tuple(cell or None for cell in row) for row in rows[1:]]


def write_seed() -> None:
    with SEED.open("w", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(COLUMNS)
        writer.writerows([cell or "" for cell in row] for row in landed())


if __name__ == "__main__":
    write_seed()
