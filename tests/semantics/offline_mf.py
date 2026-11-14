"""Run the MetricFlow command line (`mf`) with its update check off and every network call refused.

The command asks PyPI for a newer release of itself on every run (the `update_checker` package
sends one request to pypi.org for the package's JSON) and offers no way to turn that off, so this
runner replaces the check with none. It runs isolated (`python -I`), which skips `PYTHONPATH`, so
it installs the refusal of `no_network/refusal.py` itself before anything else is imported.

    python -I tests/semantics/offline_mf.py <mf arguments>
"""

import sys
from pathlib import Path

NO_NETWORK = Path(__file__).with_name("no_network")


def no_newer_release(*_: object, **__: object) -> None:
    return None


def main() -> None:
    sys.path.insert(0, str(NO_NETWORK))
    from refusal import refuse_the_network  # pyright: ignore[reportMissingImports]  (on the path above)

    sys.addaudithook(refuse_the_network)
    import update_checker

    setattr(update_checker.UpdateChecker, "check", no_newer_release)  # noqa: B010
    from dbt_metricflow.cli.main import cli

    cli(args=sys.argv[1:], prog_name="mf")


if __name__ == "__main__":
    main()
