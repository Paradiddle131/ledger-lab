"""`datacontract test` of the payments contract against the BigQuery build.

The committed contract names one server, the local DuckDB copy, and DuckDB's physical types. This
runs the same contract against the payments mart on BigQuery from a copy written to a temporary
folder outside the repository and deleted after the run, which differs from the committed file in
two things only: its one server is BigQuery, the lab's project (from `platform/gcp_project.py`,
through `ingest/bucket.py`) and the `intermediate` dataset where `models/dbt_project.yml` builds
the mart, in `LEDGER_LAB_BQ_LOCATION`; and each column's `physicalType` is BigQuery's type for the
DuckDB type the contract declares (`TYPES`), a type not in that map stopping the run before any
call. The CLI then checks the column types against BigQuery's INFORMATION_SCHEMA, so a DuckDB
`DECIMAL(28,6)` is held to BigQuery's `NUMERIC` (38 digits, 9 after the point), not to its own
precision and scale.

The CLI runs in a throwaway environment with its BigQuery engine (`uv run --isolated --no-project
--with datacontract-cli[bigquery]==1.2.9`), which the lock does not pin, so its dependencies
resolve when it runs. In that environment, before the test, two of the CLI's own functions are
wrapped (`engine`): the connection's credentials are the access token in
`CLOUDSDK_AUTH_ACCESS_TOKEN` when the environment holds one (the one gcloud and bq send), else
Application Default Credentials as the CLI resolves them; and the connection's client carries
`maximum_bytes_billed` of 1,000,000,000 bytes in its default job configuration, which every query
job it makes takes, and reads results over the REST API, its Storage Read API client (gRPC, which
this session's proxy does not carry) neither passed nor created. Only each check's name and result
are printed, never a reason, since a reason can name the project or hold a value.

`uv run python models/contracts/warehouse.py`; exits 1 when the contract fails or cannot run.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / "models/contracts/payments.odcs.yaml"
CLI = "datacontract-cli[bigquery]==1.2.9"
DATASET = "intermediate"
CAP = 1_000_000_000
# BigQuery's type for each DuckDB type the contract declares, as INFORMATION_SCHEMA names it.
TYPES = {
    "VARCHAR": "STRING",
    "BIGINT": "INT64",
    "BOOLEAN": "BOOL",
    "DATE": "DATE",
    "DECIMAL(28,6)": "NUMERIC",
}


def for_bigquery(contract: dict[str, Any], project: str, location: str) -> dict[str, Any]:
    """The contract with BigQuery as its one server and BigQuery's physical types."""
    copy = dict(contract)
    copy["servers"] = [
        {
            "server": "bigquery",
            "type": "bigquery",
            "project": project,
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


def engine(contract_file: str) -> int:
    """Run in the CLI's environment: wrap the BigQuery connection, test, print the checks."""
    from datacontract.data_contract import DataContract
    from datacontract.engines.ibis.connections import connect

    token = os.environ.get("CLOUDSDK_AUTH_ACCESS_TOKEN", "")
    resolve = connect._bigquery_credentials
    open_bigquery = connect._connect_bigquery

    def credentials(config: Any) -> Any:
        if token:
            from google.oauth2.credentials import Credentials

            return Credentials(token=token)
        return resolve(config)

    def capped(ibis: Any, server: Any, config: Any) -> Any:
        con = open_bigquery(ibis, server, config)
        con.client.default_query_job_config.maximum_bytes_billed = CAP
        # Results come back through the REST API, not the Storage Read API's gRPC stream.
        con.storage_client = None
        type(con.client)._ensure_bqstorage_client = lambda self, *args, **kwargs: None
        return con

    connect._bigquery_credentials = credentials
    connect._connect_bigquery = capped
    run = DataContract(data_contract_file=contract_file, server="bigquery").test()
    for check in run.checks or []:
        print(f"{check.result}: {check.name}")
    print(f"result: {run.result}")
    return 0 if run.has_passed() else 1


def main() -> int:
    sys.path.insert(0, str(REPO))
    from ingest.bucket import lab_project

    project = lab_project(os.environ)
    location = os.environ.get("LEDGER_LAB_BQ_LOCATION", "US")
    contract = yaml.safe_load(CONTRACT.read_text())
    folder = Path(tempfile.mkdtemp(prefix="contract-bigquery-"))
    try:
        copy = folder / "payments.odcs.yaml"
        copy.write_text(yaml.safe_dump(for_bigquery(contract, project, location), sort_keys=False))
        uv = os.environ.get("UV") or shutil.which("uv") or "uv"
        command = [uv, "run", "--isolated", "--no-project", "--with", CLI, "python"]
        run = subprocess.run(  # noqa: S603
            [*command, str(Path(__file__)), "--engine", str(copy)],
            capture_output=True,
            text=True,
            check=False,
            cwd=folder,
        )
        print(run.stdout.replace(project, "<project>"))
        if run.returncode != 0 and not run.stdout.strip():
            print(f"warehouse contract test: the engine exited {run.returncode}", file=sys.stderr)
        return run.returncode
    finally:
        shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--engine"]:
        sys.exit(engine(sys.argv[2]))
    sys.exit(main())
