"""A dbt run whose parse leaves each semantic model's `node_relation` unset, as a backward step of
the wall clock during the parse does, on the first runs only.

    python clock_step.py <count file> <stepped runs> dbt <dbt arguments>
    python clock_step.py <count file> <stepped runs> gate <project> <report>
    python clock_step.py <count file> <stepped runs> bigquery <project> <work folder>

`dbt` runs dbt's command line; `gate` runs the metric-versions gate's parse of the project
(`semantics/versions.py --parse`), and `bigquery` its BigQuery parse (`--bigquery-parse`, run in the
lock's `bigquery` group). Each run adds a line to the count file. While the file has at
most `stepped runs` lines, the manifest loader's start time is put ahead of the time every node is
made at, which is what a clock stepping back between the two leaves: dbt then skips the step that
sets a semantic model's `node_relation`, and its parse fails validating the semantic manifest.
"""

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
# Far beyond any parse's length, so every node is made "before" the loader started.
AHEAD = 1000.0
# The gate's parses this stand-in runs, by kind.
GATE = {"gate": "--parse", "bigquery": "--bigquery-parse"}


def step_back() -> None:
    from dbt.parser import manifest

    made = manifest.ManifestLoader.__init__

    def init(self: manifest.ManifestLoader, *args: object, **kwargs: object) -> None:
        made(self, *args, **kwargs)  # pyright: ignore[reportArgumentType]
        self.started_at += AHEAD

    manifest.ManifestLoader.__init__ = init  # pyright: ignore[reportAttributeAccessIssue]


def main(argv: list[str]) -> int:
    # dbt imports every top-level `dbt_*` module it can find as a plugin, and this folder holds
    # `dbt_build.py`, which the `bigquery` group cannot import.
    sys.path[:] = [entry for entry in sys.path if Path(entry or ".").resolve() != HERE]
    count, stepped, kind, *rest = argv
    runs = Path(count)
    with runs.open("a") as file:
        file.write("run\n")
    if len(runs.read_text().splitlines()) <= int(stepped):
        step_back()
    if kind in GATE:
        spec = importlib.util.spec_from_file_location(
            "versions", REPO / "semantics" / "versions.py"
        )
        assert spec is not None and spec.loader is not None
        versions = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(versions)
        return versions.main([GATE[kind], *rest])
    from dbt.cli.main import cli

    sys.argv = ["dbt", *rest]
    cli()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
