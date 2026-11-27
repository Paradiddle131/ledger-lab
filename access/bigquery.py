"""Acting as a persona on BigQuery: a token minted by impersonation, and capped queries with it.

The token comes from `gcloud auth print-access-token --impersonate-service-account`, run as the
login of the machine the test runs on, never from a key file, and lives only in memory. The one
identity `platform/gcp/identities.tf` lets mint persona tokens is the agent account, so the login
reaches the persona's account through it: the chain is the agent account, named in
`LEDGER_LAB_AGENT_ACCOUNT` by its account id (the part before the @) or by its whole address in the
lab's project, then the persona's account, and the login needs only
`iam.serviceAccounts.implicitDelegation` on the agent account. With the variable unset
the login mints the persona's token itself, which only the agent account may do.

Behind the cloud session's proxy the Authorization header on `*.googleapis.com` is replaced with
the session's own credential, so a query made there runs as the agent account whatever token it
carries; the persona queries run off that proxy, on the owner's machine, and each test reads
`SESSION_USER()` to show whose identity BigQuery saw.

A query goes to the `jobs.query` REST method as standard SQL, uncached, under a byte cap fixed in
code that no setting raises (a persona's, or the publish's in `access/row_policies.py`), and runs
where the tables it reads are. A refusal reports BigQuery's HTTP status and reason, and a failed
mint `gcloud`'s exit status, or that it timed out or could not run, never their messages or the
command, which name the project and the accounts.
"""

import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

AGENT_VARIABLE = "LEDGER_LAB_AGENT_ACCOUNT"
# A persona query reads a sample-sized product or none; BigQuery bills at least 10 MB for each
# table a query reads, so this cap admits a few small tables and stays far inside the free tier.
MAX_BYTES_BILLED = 100_000_000
QUERY_TIMEOUT_MS = 30_000
ENDPOINT = "https://bigquery.googleapis.com/bigquery/v2/projects/{project}/queries"
ACCOUNT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class PersonaQueryError(RuntimeError):
    """A persona's token could not be minted, or BigQuery refused or did not finish its query."""

    def __init__(self, message: str, status: int | None = None, reason: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


def service_account(account_id: str, project: str) -> str:
    return f"{account_id}@{project}.iam.gserviceaccount.com"


def agent_id(value: str, project: str) -> str:
    """The agent account's id from `LEDGER_LAB_AGENT_ACCOUNT`, which names it by its id or by its
    whole address in the lab's project; an address in another project is refused."""
    agent = value.strip()
    if "@" in agent:
        agent, _, domain = agent.partition("@")
        if domain != service_account("", project)[1:]:
            raise PersonaQueryError(f"{AGENT_VARIABLE} names an account outside the lab's project")
    if not ACCOUNT_ID.fullmatch(agent):
        raise PersonaQueryError(f"{AGENT_VARIABLE} is not a service account id")
    return agent


def chain(persona_account: str, project: str, env: Mapping[str, str]) -> list[str]:
    """The accounts the mint goes through, the persona's last: the agent account first when
    `LEDGER_LAB_AGENT_ACCOUNT` names it."""
    agent = env.get(AGENT_VARIABLE, "").strip()
    if not agent:
        return [persona_account]
    return [service_account(agent_id(agent, project), project), persona_account]


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), capture_output=True, text=True, check=False, timeout=120)  # noqa: S603


def mint(accounts: Sequence[str], run: Runner = _run) -> str:
    """A short-lived access token for the last account, through the ones before it."""
    gcloud = shutil.which("gcloud") or "gcloud"
    command = [
        gcloud,
        "auth",
        "print-access-token",
        f"--impersonate-service-account={','.join(accounts)}",
        "--verbosity=error",
    ]
    # A timeout's text holds the command, which names the accounts and the project; an OS error's
    # names gcloud's path, which can name the login's home.
    try:
        done = run(command)
    except subprocess.TimeoutExpired:
        raise PersonaQueryError("minting the persona's token timed out") from None
    except OSError:
        raise PersonaQueryError("minting the persona's token could not run gcloud") from None
    if done.returncode != 0:
        raise PersonaQueryError(f"minting the persona's token failed with status {done.returncode}")
    token = done.stdout.strip()
    if not token or any(c.isspace() for c in token):
        raise PersonaQueryError("minting the persona's token printed no single token")
    return token


@dataclass(frozen=True)
class Answer:
    # Each row's cells as BigQuery's REST answer gives them: text, or None for a null.
    rows: list[tuple[Any, ...]]
    bytes_processed: int
    # Each cell's type as the answer's schema names it (`STRING`, `INTEGER`, `NUMERIC` and the
    # like), empty when the answer carries no schema.
    types: tuple[str, ...] = ()


# BigQuery's reason for a query refused by its bytes-billed cap, which it refuses before running.
OVER_CAP = "bytesBilledLimitExceeded"
# The most tables one dataset's listing may return; a listing that runs past it is refused.
LISTED = 1000


def in_parts[T](
    read: Callable[[tuple[str, ...]], T], columns: Sequence[str], keep: Sequence[str]
) -> list[tuple[tuple[str, ...], T]]:
    """`read` over the columns, or, where BigQuery refuses it as over the bytes-billed cap (a
    query bills every row of each column it names, whatever the row access policies leave), over
    parts of them split in two, each part keeping the `keep` columns, so the cap stays as it is and
    no part bills more than it allows. Any other refusal, or a part that cannot be split further,
    is raised."""
    whole = tuple(columns)
    try:
        return [(whole, read(whole))]
    except PersonaQueryError as error:
        kept = tuple(c for c in whole if c in keep)
        rest = [c for c in whole if c not in keep]
        if error.reason != OVER_CAP or len(rest) < 2:
            raise
        half = len(rest) // 2
        return [
            *in_parts(read, (*kept, *rest[:half]), keep),
            *in_parts(read, (*kept, *rest[half:]), keep),
        ]


class PersonaSession:
    """Queries on the lab's BigQuery that carry one persona's token."""

    def __init__(
        self,
        project: str,
        token: str,
        client: httpx.Client,
        max_bytes_billed: int = MAX_BYTES_BILLED,
    ) -> None:
        self._url = ENDPOINT.format(project=quote(project, safe=""))
        self._max_bytes_billed = max_bytes_billed
        # httpx.Headers masks the Authorization value in its repr, which a traceback's locals show.
        self._headers = httpx.Headers({"Authorization": f"Bearer {token}"})
        self._client = client

    def query(self, sql: str) -> Answer:
        body = {
            "query": sql,
            "useLegacySql": False,
            "useQueryCache": False,
            "maximumBytesBilled": str(self._max_bytes_billed),
            "timeoutMs": QUERY_TIMEOUT_MS,
        }
        response = self._client.post(self._url, json=body, headers=self._headers)
        if response.status_code != httpx.codes.OK:
            reason = _reason(response)
            raise PersonaQueryError(
                f"BigQuery refused the query: HTTP {response.status_code}, reason {reason}",
                status=response.status_code,
                reason=reason,
            )
        answer = response.json()
        if not answer.get("jobComplete"):
            raise PersonaQueryError("the query did not finish within its timeout")
        if answer.get("pageToken"):
            raise PersonaQueryError("the answer runs past one page, more than a persona test reads")
        rows = [tuple(cell.get("v") for cell in row["f"]) for row in answer.get("rows", [])]
        fields = answer.get("schema", {}).get("fields", [])
        types = tuple(str(f.get("type", "")) for f in fields)
        return Answer(rows, int(answer.get("totalBytesProcessed", "0")), types)

    def _get(self, path: str) -> dict[str, Any]:
        """A metadata read (`tables.list`, `tables.get`): free, and refused as a query is."""
        url = self._url.removesuffix("/queries") + path
        response = self._client.get(url, headers=self._headers)
        if response.status_code != httpx.codes.OK:
            reason = _reason(response)
            raise PersonaQueryError(
                f"BigQuery refused the metadata read: HTTP {response.status_code}, reason {reason}",
                status=response.status_code,
                reason=reason,
            )
        answer = response.json()
        if not isinstance(answer, dict):
            raise PersonaQueryError("BigQuery answered the metadata read in an unexpected shape")
        return answer

    def tables(self, dataset: str) -> list[tuple[str, str]]:
        """Each table, view or other kind of table a dataset holds, by name with its kind, as
        BigQuery lists them to this identity (`tables.list`, a free metadata read)."""
        answer = self._get(f"/datasets/{quote(dataset, safe='')}/tables?maxResults={LISTED}")
        if answer.get("nextPageToken"):
            raise PersonaQueryError("the dataset's listing runs past one page")
        try:
            return [
                (str(t["tableReference"]["tableId"]), str(t.get("type", "")))
                for t in answer.get("tables", [])
            ]
        except (KeyError, TypeError):
            raise PersonaQueryError("the dataset's listing has an unexpected shape") from None

    def table(self, dataset: str, table: str) -> tuple[str, list[tuple[str, str]], str | None]:
        """A table's kind, its columns with their types in order, and a view's stored query, as
        BigQuery shows them to this identity (`tables.get`, a free metadata read)."""
        answer = self._get(
            f"/datasets/{quote(dataset, safe='')}/tables/{quote(table, safe='')}"
        )
        try:
            fields = answer.get("schema", {}).get("fields", [])
            columns = [(str(f["name"]), str(f["type"])) for f in fields]
            view = answer.get("view")
            stored = str(view["query"]) if isinstance(view, dict) else None
            return str(answer.get("type", "")), columns, stored
        except (KeyError, TypeError):
            raise PersonaQueryError("the table's metadata has an unexpected shape") from None


def _reason(response: httpx.Response) -> str | None:
    """BigQuery's first error reason, such as `accessDenied`; never its message."""
    try:
        errors = response.json()["error"]["errors"]
        reason = errors[0]["reason"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    return reason if isinstance(reason, str) else None
