"""What the steps that hash people share once the names are hashed: finding a hashed name inside a
free-text cell, and writing the scrubbed rows to Parquet without them touching disk.

A cell's words are its runs of letters and digits after the pseudonymiser's normalisation, so no
punctuation (`(NAME)`, `NAME'S`, `LAST, FIRST`) can hide a name. `HashedNames` holds the names a
step hashed, each kept only if a word of it holds a letter (a run of figures alone names no one),
and finds one inside a cell two ways:

- `in_any_order`: a run of the cell's words is a name's words in some order;
- `cut_short`: the cell ends in a name's first words, whole, and the start of its next one
  (`JANE EXAMP`), as a publisher's cut of a long description leaves it.

`parquet_from_csv` reads CSV text held in memory into Parquet through an in-memory filesystem, with
a DuckDB connection that has no directory to spill to (`in_memory`), and returns the bytes.
"""

import re
import uuid
from collections.abc import Iterable
from typing import Any

import duckdb
import fsspec

from ingest.individuals import normalise

MEMORY = fsspec.filesystem("memory")
WORD = re.compile(r"[^\W_]+")
LETTER = re.compile(r"[^\W\d_]")


def words(text: str) -> list[str]:
    """A cell's words: its runs of letters and digits, normalised as a name is."""
    return WORD.findall(normalise(text))


class HashedNames:
    """The names a step hashed, ready to be found inside a cell."""

    def __init__(self, names: Iterable[str]) -> None:
        spelt = [w for w in map(words, names) if any(LETTER.search(word) for word in w)]
        self.unordered = {tuple(sorted(w)) for w in spelt}
        self.lengths = {len(w) for w in spelt}
        self.longest = max(self.lengths, default=0)
        self.cut: dict[tuple[str, ...], set[str]] = {}
        for w in spelt:
            for k in range(1, len(w)):
                self.cut.setdefault(tuple(w[:k]), set()).add(w[k])

    def in_any_order(self, cell: str) -> bool:
        found = words(cell)
        return any(
            tuple(sorted(found[i : i + n])) in self.unordered
            for n in self.lengths
            for i in range(len(found) - n + 1)
        )

    def cut_short(self, cell: str) -> bool:
        found = words(cell)
        for n in range(1, min(self.longest, len(found))):
            head, last = tuple(found[-n - 1 : -1]), found[-1]
            if any(rest.startswith(last) for rest in self.cut.get(head, ())):
                return True
        return False


def in_memory() -> duckdb.DuckDBPyConnection:
    """A DuckDB connection that cannot spill to disk and reads and writes the in-memory files."""
    con = duckdb.connect(config={"temp_directory": ""})
    con.register_filesystem(MEMORY)
    return con


def parquet_from_csv(text: str, **read_csv: Any) -> bytes:
    """CSV text with a header row as Parquet bytes, read with DuckDB's `read_csv` options, never
    on disk; the in-memory files are removed whatever happens."""
    work = f"/{uuid.uuid4().hex}"
    try:
        MEMORY.pipe(f"{work}/rows.csv", text.encode())
        con = in_memory()
        con.read_csv(f"memory://{work}/rows.csv", header=True, **read_csv).write_parquet(
            f"memory://{work}/rows.parquet"
        )
        return MEMORY.cat(f"{work}/rows.parquet")
    finally:
        if MEMORY.exists(work):
            MEMORY.rm(work, recursive=True)
