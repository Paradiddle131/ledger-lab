"""Run the MetricFlow command line (`mf`) with its update check off and every network call refused.

The command asks PyPI for a newer release of itself on every run (the `update_checker` package
sends one request to pypi.org for the package's JSON) and offers no way to turn that off, so this
runner replaces the check with none. Run it isolated (`python -I`), so nothing in the folder you
run from can stand in for a module of MetricFlow's; it installs the refusal of `refusal.py`
beside it before anything else is imported.

    DBT_PROJECT_DIR=models DBT_PROFILES_DIR=models uv run python -I semantics/offline/offline_mf.py <mf arguments>
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def no_newer_release(*_: object, **__: object) -> None:
    return None


def main() -> None:
    sys.path.insert(0, str(HERE))
    from refusal import refuse_the_network  # pyright: ignore[reportMissingImports]

    sys.addaudithook(refuse_the_network)
    import update_checker

    setattr(update_checker.UpdateChecker, "check", no_newer_release)  # noqa: B010
    from dbt_metricflow.cli.main import cli

    cli(args=sys.argv[1:], prog_name="mf")


if __name__ == "__main__":
    main()
