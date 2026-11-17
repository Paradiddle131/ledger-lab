"""`datacontract test` of the payments contract against the BigQuery build.

The committed contract names one server, the local DuckDB copy, and DuckDB's physical types. This
runs the same contract against the payments mart on BigQuery from a copy written to a temporary
folder outside the repository, removed when the run ends (a killed process can leave it), which
differs from the committed file in two things only: its one server is BigQuery, in the
`intermediate` dataset where the profile builds the mart and the region `ingest.load.location`
gives (a region outside the free tier's stops the run before any call), with a placeholder
(`PROJECT_PLACEHOLDER`) where the project goes, so the copy on disk holds no project id; and each
column's `physicalType` is BigQuery's type for the DuckDB type the contract declares (`TYPES`), a
type not in that map stopping the run before any call. The CLI then checks the column types
against BigQuery's INFORMATION_SCHEMA, so a DuckDB `DECIMAL(28,6)` is held to BigQuery's `NUMERIC`
(38 digits, 9 after the point), not to its own precision and scale.

The CLI runs in a throwaway environment with its BigQuery engine (`uv run --isolated --no-project
--with datacontract-cli[bigquery]==1.2.9`), which the lock does not pin, so its dependencies
resolve when it runs. It is started with only the variables `engine_environment` lets through:
`PATH`, `HOME`, `TMPDIR`, uv's own (`UV`, `UV_*`) and its folders (`XDG_CACHE_HOME`,
`XDG_CONFIG_HOME`, `XDG_DATA_HOME`), the proxy variables (`HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`,
`NO_PROXY`, each in upper or lower case), the CA variables (`SSL_CERT_FILE`, `SSL_CERT_DIR`,
`REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`), `LEDGER_LAB_GCP_PROJECT`, `LEDGER_LAB_BQ_LOCATION` and
`CLOUDSDK_AUTH_ACCESS_TOKEN`. Any other name is dropped, among them the individuals' hash key, the
`DATACONTRACT_` variables the CLI reads its settings from (which would otherwise replace the copy's
region, dataset or billing project, or the credentials) and the other variables the CLI reads, such
as `ENTROPY_DATA_*` and `DATAMESH_MANAGER_*`. The engine is a child of this process and can still
read this process's environment by other means (on Linux, from `/proc`), so the list narrows what
the engine is handed, not what it can reach. In that environment, before the test, two of the CLI's
own functions are wrapped (`engine`): the connection's credentials are the access token in
`CLOUDSDK_AUTH_ACCESS_TOKEN` when the environment holds one (the one gcloud and bq send), else
Application Default Credentials as the CLI resolves them in that environment; and the connection is
opened (`opened`) only while the copy's server holds the placeholder, the CLI's settings object has
each of the seven `get_bigquery_*` getters this file names (`GETTERS`, those of the pinned CLI),
and none of them nor any other `get_bigquery_*` getter it has holds a value, on the lab's project,
read there from `platform/gcp_project.py`, in place of the placeholder, and then restricted
(`restrict`): its client carries `maximum_bytes_billed` of 1,000,000,000 bytes in its default job
configuration, which the client merges into each query job it starts, and reads results over the
REST API: the Storage Read API client that ibis creates when it connects is dropped before any read
and never re-created (gRPC, which this session's proxy does not carry). Only each check's name and
result are printed, never a reason, since a reason can name the project or hold a value, and the
project id is replaced by `<project>` in what is printed.

`uv run python models/contracts/warehouse.py`; exits 1 when the contract fails or cannot run.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / "models/contracts/payments.odcs.yaml"
CLI = "datacontract-cli[bigquery]==1.2.9"
DATASET = "intermediate"
CAP = 1_000_000_000
# What the copy on disk holds where the project goes; the engine opens the lab's project instead.
PROJECT_PLACEHOLDER = "lab-project-from-the-environment"
SEAM = REPO / "platform/gcp_project.py"
# The variables the engine is started with: what uv needs to fetch the CLI through a proxy, the
# CA files Python's HTTP clients read, and the lab's project, region and access token.
ENGINE_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "UV",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "LEDGER_LAB_GCP_PROJECT",
        "LEDGER_LAB_BQ_LOCATION",
        "CLOUDSDK_AUTH_ACCESS_TOKEN",
    }
)
# The prefixes the engine's variables may also start with: uv's own settings.
ENGINE_PREFIXES = ("UV_",)
# The CLI's BigQuery settings, as datacontract-cli 1.2.9 names their getters
# (`datacontract/config/settings.py`); each must be found on its settings object and hold nothing.
GETTERS = (
    "get_bigquery_account_info_json_path",
    "get_bigquery_billing_project",
    "get_bigquery_dataset",
    "get_bigquery_impersonation_account",
    "get_bigquery_kms_key_name",
    "get_bigquery_location",
    "get_bigquery_project",
)
# BigQuery's type for each DuckDB type the contract declares, as INFORMATION_SCHEMA names it.
TYPES = {
    "VARCHAR": "STRING",
    "BIGINT": "INT64",
    "BOOLEAN": "BOOL",
    "DATE": "DATE",
    "DECIMAL(28,6)": "NUMERIC",
}


def lab_project() -> str:
    """The lab's project from the one module that names it, loaded from its file, so the engine's
    environment, which holds no project package, reads it the same way."""
    spec = importlib.util.spec_from_file_location("gcp_project", SEAM)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SEAM.name}")
    seam = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seam)
    return seam.lab_project(os.environ)


def for_bigquery(contract: dict[str, Any], location: str) -> dict[str, Any]:
    """The contract with BigQuery as its one server, the project a placeholder, and BigQuery's
    physical types."""
    copy = dict(contract)
    copy["servers"] = [
        {
            "server": "bigquery",
            "type": "bigquery",
            "project": PROJECT_PLACEHOLDER,
            "dataset": DATASET,
            "customProperties": [{"property": "location", "value": location}],
        }
    ]
    schemas = []
    for schema in contract["schema"]:
        properties = []
        for prop in schema.get("properties", []):
            declared = prop.get("physicalType")
            if declared is not None and declared not in TYPES:
                raise ValueError(f"{prop['name']}: no BigQuery type for {declared}")
            properties.append(prop | ({"physicalType": TYPES[declared]} if declared else {}))
        schemas.append(schema | {"properties": properties})
    copy["schema"] = schemas
    return copy


def restrict(con: Any) -> Any:
    """The connection with every query job capped and results read over REST."""
    con.client.default_query_job_config.maximum_bytes_billed = CAP
    con.storage_client = None
    type(con.client)._ensure_bqstorage_client = lambda self, *args, **kwargs: None
    return con


def engine_environment(env: Mapping[str, str]) -> dict[str, str]:
    """The environment the engine runs in: the variables of `env` named in `ENGINE_NAMES` or
    starting with one of `ENGINE_PREFIXES`, and no other."""
    return {k: v for k, v in env.items() if k in ENGINE_NAMES or k.startswith(ENGINE_PREFIXES)}


def missing(config: Any) -> list[str]:
    """The names in `GETTERS` that `config` has no callable for."""
    return [name for name in GETTERS if not callable(getattr(config, name, None))]


def overrides(config: Any) -> list[str]:
    """The names, among `GETTERS` and any other `get_bigquery_*` callable `config` has, whose
    getter answers a value other than None."""
    found = {
        n for n in dir(config) if n.startswith("get_bigquery_") and callable(getattr(config, n))
    }
    return [name for name in sorted(found | set(GETTERS)) if getattr(config, name)() is not None]


def engine(contract_file: str) -> int:
    """Run in the CLI's environment: wrap the BigQuery connection, test, print the checks."""
    from datacontract.data_contract import DataContract
    from datacontract.engines.ibis.connections import connect

    project = lab_project()
    token = os.environ.get("CLOUDSDK_AUTH_ACCESS_TOKEN", "")
    resolve = connect._bigquery_credentials
    open_bigquery = connect._connect_bigquery

    def credentials(config: Any) -> Any:
        if token:
            from google.oauth2.credentials import Credentials

            return Credentials(token=token)
        return resolve(config)

    def opened(ibis: Any, server: Any, config: Any) -> Any:
        if server.project != PROJECT_PLACEHOLDER:
            raise RuntimeError("the contract copy names a project; it must hold the placeholder")
        absent = missing(config)
        if absent:
            raise RuntimeError(f"the CLI's settings lack the getters this run checks: {absent}")
        overridden = overrides(config)
        if overridden:
            raise RuntimeError(f"the CLI's settings override the copy's server: {overridden}")
        # The same server object is read again for the column types' INFORMATION_SCHEMA query.
        server.project = project
        return restrict(open_bigquery(ibis, server, config))

    connect._bigquery_credentials = credentials
    connect._connect_bigquery = opened
    run = DataContract(data_contract_file=contract_file, server="bigquery").test()
    for check in run.checks or []:
        print(f"{check.result}: {check.name}")
    print(f"result: {run.result}")
    return 0 if run.has_passed() else 1


def main() -> int:
    sys.path.insert(0, str(REPO))
    from ingest.load import location

    where = location(os.environ)
    project = lab_project()
    contract = yaml.safe_load(CONTRACT.read_text())
    with tempfile.TemporaryDirectory(prefix="contract-bigquery-") as folder:
        copy = Path(folder) / "payments.odcs.yaml"
        copy.write_text(yaml.safe_dump(for_bigquery(contract, where), sort_keys=False))
        uv = os.environ.get("UV") or shutil.which("uv") or "uv"
        command = [uv, "run", "--isolated", "--no-project", "--with", CLI, "python"]
        run = subprocess.run(  # noqa: S603
            [*command, str(Path(__file__)), "--engine", str(copy)],
            capture_output=True,
            text=True,
            check=False,
            cwd=folder,
            env=engine_environment(os.environ),
        )
    print(run.stdout.replace(project, "<project>"))
    if run.returncode != 0 and not run.stdout.strip():
        print(f"warehouse contract test: the engine exited {run.returncode}", file=sys.stderr)
    return run.returncode


if __name__ == "__main__":
    if sys.argv[1:2] == ["--engine"]:
        sys.exit(engine(sys.argv[2]))
    sys.exit(main())
