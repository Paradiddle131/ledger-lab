"""The dbt project built once on the local engine over the fixture landing (`dbt_build.py`)."""

import pytest
from dbt_build import Build, built, fixture_landing


@pytest.fixture(scope="session")
def build(tmp_path_factory: pytest.TempPathFactory) -> Build:
    """The whole project built once over the fixture landing."""
    tmp_path = tmp_path_factory.mktemp("dbt")
    return built(tmp_path, fixture_landing(tmp_path / "landing"))
