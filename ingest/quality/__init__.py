"""`uv run python -m ingest.quality <source>`: check a landed file against its source's suite.

Each landed source has a GX Core expectation suite here, `<source>.json`, in GX's own suite format,
and the query that shapes its batch, `<source>.sql`: DuckDB reads the landed Parquet file as the
view `landed`, and the query returns every landed column as landed, then the typed values and the
per-row measures the suite's rules read, each named for what it holds. GX validates that one batch
in memory through a checkpoint and returns its own validation result; nothing here evaluates a rule.

A suite covers the file's schema (its columns in order, and their types), the keys that may not be
null, the shapes and ranges of its values, and its known mess: each mess threshold is an exact
count measured on the file the pins in `ingest/sources.yaml` serve, a suite parameter
(`{"$PARAMETER": <name>}`) whose value there is the suite's `meta.pinned`, so a count that moves
fails the check. Each pinned rule is written so its report line prints the value it observed (a
count, a sum, a maximum, or a list written as one text and read as the column's most common
value), never only how many rows broke it. To re-measure after a re-pin: land the source, run
`uv run python -m ingest.quality <source>`, read each failing pinned rule's observed value,
confirm the move is the publisher's change and not a broken landing, copy the value into
`meta.pinned` and run the command again until it passes; the diff of `meta.pinned` is the
reviewed change. A test passes the values its own fixture holds instead.

Columns that can hold a name, or a keyed hash of one, are the suite's `meta.names`: they carry only
rules whose result is a count (a test holds every suite to it), and a failed rule's result keeps
the count and never a value (`RESULT_FORMAT`), so neither the result nor the report, which prints
its observed values and counts, can carry a name. GX runs here with its usage statistics off, no
progress bars, no data docs and no action, so a run reads the local landing and calls out to no
network.

`<source>` is a landed file's name, or a set's name for every landed file it prefixes
(`chicago` checks `chicago-contracts` and `chicago-payments`). The command exits non-zero on a
broken rule, on a file that is not landed, and on a landing that is not a local folder.
"""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import great_expectations as gx
import pandas as pd
from great_expectations.core import ExpectationSuiteValidationResult
from great_expectations.data_context.types.base import (
    DataContextConfig,
    InMemoryStoreBackendDefaults,
    ProgressBarsConfig,
)

HERE = Path(__file__).resolve().parent
# Counts only: a failed rule's result lists none of the values that broke it (GX's default lists
# up to twenty).
RESULT_FORMAT = {"result_format": "BASIC", "partial_unexpected_count": 0}


class QualityError(Exception):
    """A suite cannot run: no such suite, nothing landed, or a landing that is not local."""


@dataclass(frozen=True)
class Suite:
    source: str
    expectations: list[dict[str, Any]]
    meta: dict[str, Any]
    query: str

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self.meta["names"])

    @property
    def pinned(self) -> dict[str, Any]:
        return self.meta["pinned"]


def sources() -> list[str]:
    """Every landed source with a suite."""
    return sorted(p.stem for p in HERE.glob("*.json"))


def suite(source: str) -> Suite:
    if source not in sources():
        raise QualityError(f"{source}: no suite")
    doc = json.loads((HERE / f"{source}.json").read_text())
    if doc["name"] != source:
        raise QualityError(f"{source}: the suite is named {doc['name']}")
    return Suite(source, doc["expectations"], doc["meta"], (HERE / f"{source}.sql").read_text())


def local_landing() -> Path:
    """The local landing; a bucket or any other URL is refused without naming it."""
    value = os.environ.get("LEDGER_LAB_LANDING") or "landing"
    if "://" in value:
        raise QualityError("LEDGER_LAB_LANDING is not a local folder; the checks read one")
    return Path(value)


def batch(the: Suite, path: Path) -> pd.DataFrame:
    """The suite's batch: its query over the landed file, held in memory. The query reads the
    file's rows as `landed` and the key-value metadata its step wrote as `landed_metadata`."""
    con = duckdb.connect(
        config={"autoinstall_known_extensions": False, "autoload_known_extensions": False}
    )
    con.read_parquet(str(path)).create_view("landed")
    con.execute(
        "create table landed_metadata as select decode(key) as key, decode(value) as value"
        " from parquet_kv_metadata(?)",
        [str(path)],
    )
    return con.execute(the.query).df()


def validate(
    the: Suite, frame: pd.DataFrame, parameters: dict[str, Any] | None = None
) -> ExpectationSuiteValidationResult:
    """GX's validation of the frame against the suite, with the pinned values unless `parameters`
    gives a fixture's own."""
    context = gx.get_context(
        mode="ephemeral",
        project_config=DataContextConfig(
            store_backend_defaults=InMemoryStoreBackendDefaults(init_temp_docs_sites=False),
            analytics_enabled=False,
            progress_bars=ProgressBarsConfig(globally=False),
        ),
    )
    expectations = gx.ExpectationSuite(name=the.source, expectations=the.expectations)
    if len(expectations.expectations) != len(the.expectations):
        # GX logs and skips an expectation it cannot build; a rule must never vanish silently.
        raise QualityError(f"{the.source}: the suite holds an expectation GX cannot build")
    asset = context.data_sources.add_pandas("landed").add_dataframe_asset(the.source)
    definition = context.validation_definitions.add(
        gx.ValidationDefinition(
            name=the.source,
            data=asset.add_batch_definition_whole_dataframe("whole file"),
            suite=context.suites.add(expectations),
        )
    )
    checkpoint = context.checkpoints.add(
        gx.Checkpoint(
            name=the.source, validation_definitions=[definition], result_format=RESULT_FORMAT
        )
    )
    result = checkpoint.run(
        batch_parameters={"dataframe": frame},
        expectation_parameters=the.pinned if parameters is None else parameters,
    )
    (validation,) = result.run_results.values()
    return validation


def check(source: str, path: Path, parameters: dict[str, Any] | None = None) -> list[str]:
    """Validate one landed file and return its report, one line per rule; the first word of a
    line is `ok` or `FAIL`."""
    the = suite(source)
    try:
        frame = batch(the, path)
    except duckdb.Error as error:
        # A column the query reads is missing or of another type: a schema break. DuckDB's
        # message can quote a value, so only its class is reported.
        return [f"FAIL schema: the suite's query cannot read the file ({type(error).__name__})"]
    return report(the, validate(the, frame, parameters))


def raised(info: dict[str, Any]) -> bool:
    """Whether GX caught an exception evaluating a rule: its flag is on the result, or on one of
    the metrics the rule needed."""
    return bool(info.get("raised_exception")) or any(
        isinstance(v, dict) and v.get("raised_exception") for v in info.values()
    )


def report(the: Suite, result: ExpectationSuiteValidationResult) -> list[str]:
    lines = []
    for outcome in result.results:
        config = outcome.expectation_config
        if config is None:
            continue
        kind, column = config.type, config.kwargs.get("column")
        what = f"{kind} on {column}" if column else kind
        found, observed = outcome.result or {}, (outcome.result or {}).get("observed_value")
        evaluated = not raised(outcome.exception_info or {}) and bool(found)
        if not evaluated:
            # GX's exception message can quote the data, so only the fact is reported, as a
            # failure whatever GX's own verdict.
            detail = "could not be evaluated"
        elif observed is not None:
            detail = f"observed {observed}"
        elif "element_count" in found:
            detail = f"{found['unexpected_count']} of {found['element_count']} rows break it"
        else:
            detail = (
                f"{found.get('unexpected_count', 0)} values not in the set,"
                f" {found.get('missing_count', 0)} of the set missing"
            )
        lines.append(f"{'ok' if outcome.success and evaluated else 'FAIL'} {what}: {detail}")
    return sorted(lines, key=lambda line: (not line.startswith("FAIL"), line))


def landed_names(name: str) -> list[str]:
    """The landed files a name checks: the file itself, or every file a set's name prefixes."""
    known = sources()
    if name in known:
        return [name]
    members = [s for s in known if s.startswith(f"{name}-")]
    if not members:
        raise QualityError(f"{name}: no suite")
    return members


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m ingest.quality <source or set>", file=sys.stderr)
        return 2
    try:
        names, landing = landed_names(argv[0]), local_landing()
        missing = [n for n in names if not (landing / n).is_file()]
        if missing:
            raise QualityError(f"{', '.join(missing)} not landed in the local landing")
    except QualityError as error:
        print(f"quality: {error}", file=sys.stderr)
        return 1
    failed = 0
    for name in names:
        lines = check(name, landing / name)
        broken = sum(line.startswith("FAIL") for line in lines)
        failed += broken
        for line in lines:
            print(f"{name}: {line}")
        print(
            f"{name}: {'passed' if not broken else 'FAILED'}, {len(lines)} rules, {broken} broken"
        )
    return 1 if failed else 0
