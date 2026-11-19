"""A dbt run whose parse leaves each semantic model's `node_relation` unset, as a backward step of
the wall clock during the parse does, on the first runs only.

    python clock_step.py <count file> <stepped runs> dbt <dbt arguments>
    python clock_step.py <count file> <stepped runs> gate <project> <report>

`dbt` runs dbt's command line; `gate` runs the metric-versions gate's parse of the project
(`semantics/versions.py --parse`). Each run adds a line to the count file. While the file has at
most `stepped runs` lines, the manifest loader's start time is put ahead of the time every node is
made at, which is what a clock stepping back between the two leaves: dbt then skips the step that
sets a semantic model's `node_relation`, and its parse fails validating the semantic manifest.
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# Far beyond any parse's length, so every node is made "before" the loader started.
AHEAD = 1000.0


def step_back() -> None:
    from dbt.parser import manifest

    made = manifest.ManifestLoader.__init__

    def init(self: manifest.ManifestLoader, *args: object, **kwargs: object) -> None:
        made(self, *args, **kwargs)  # pyright: ignore[reportArgumentType]
        self.started_at += AHEAD

    manifest.ManifestLoader.__init__ = init  # pyright: ignore[reportAttributeAccessIssue]


def main(argv: list[str]) -> int:
    count, stepped, kind, *rest = argv
    runs = Path(count)
    with runs.open("a") as file:
        file.write("run\n")
    if len(runs.read_text().splitlines()) <= int(stepped):
        step_back()
    if kind == "gate":
        spec = importlib.util.spec_from_file_location(
            "versions", REPO / "semantics" / "versions.py"
        )
        assert spec is not None and spec.loader is not None
        versions = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(versions)
        return versions.main(["--parse", *rest])
    from dbt.cli.main import cli

    sys.argv = ["dbt", *rest]
    cli()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
