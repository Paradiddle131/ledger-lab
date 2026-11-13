"""The UK Department for Transport's spend as the fixture landing holds it: the committed real rows
(`uk_transport_sample.yaml`) and a few invented people, served again as the Department serves its
monthly files and landed through the landing step with the test key, so every hashed cell is the
step's own work and no hash made with a real key enters the tree.

Contains public sector information licensed under the Open Government Licence v3.0; every person's
name and every figure on a person's row is invented.
"""

import csv
import io
from pathlib import Path
from typing import Any

import yaml

from ingest import fetch
from ingest.entities import Record, Register
from ingest.individuals import KEY_VARIABLE
from ingest.uk_transport.spend import FAMILY, LAYOUTS, SET, step

SAMPLE = Path(__file__).with_name("uk_transport_sample.yaml")
KEY = {KEY_VARIABLE: "k" * 32}
# Invented people, each row as the sample's: file, paying body, date, supplier, transaction number
# and amount as served. One person is paid in two files under one transaction number; the last row
# is one the Department itself redacted.
PEOPLE = (
    (
        "dft-spending-over-25000-april-2025.csv",
        "Maritime and Coastguard Agency",
        "15/04/2025",
        "JANE EXAMPLE",
        "4400123",
        "£30,000.00",
    ),
    (
        "dft-spending-over-25000-march-2026.csv",
        "Maritime and Coastguard Agency",
        "16/03/2026",
        "JANE EXAMPLE",
        "4400123",
        "£31,250.50",
    ),
    (
        "dft-spending-over-25000-september-2025.csv",
        "National Highways",
        "09/09/2025",
        "JOHN Q EXAMPLE",
        "1049999",
        "£45,000.00",
    ),
    (
        "dft-spending-over-25000-october-2025.csv",
        "DVSA",
        "14/10/2025",
        "REDACTED",
        "5100099999",
        "£27,500.00",
    ),
)


def sample_rows() -> list[tuple[str, ...]]:
    """The committed real rows, then the invented people's."""
    return [tuple(r) for r in yaml.safe_load(SAMPLE.read_text())["rows"]] + list(PEOPLE)


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


def land(path: Path, register: list[dict[str, Any]]) -> Path:
    """The sample landed at `path` through the step, with the sample register vouching as the
    landed one does."""
    vouching = Register(Record(r["legal_name"], r["category"], r["form"]) for r in register)
    path.write_bytes(step(served_files(sample_rows()), KEY, vouching))
    return path
