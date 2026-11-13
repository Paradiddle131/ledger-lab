"""`uv run python -m ingest.fetch <source>`: download a recorded source, verify it, land it.

The source's entry in `sources.yaml` gives its URL or pinned query and the SHA-256 of the bytes
the publisher serves: they are asked for with no content encoding and hashed undecoded, as they
arrive, which is what a published checksum or `sha256sum` over a plain download gives. The whole
response is held in memory, hashed, and compared; only on a match does anything touch disk.
Between the two sits the one seam where a source's own step runs (hashing the people it names,
converting its format): `STEPS[<source>]` receives the verified raw bytes and returns what is
written. A source with no step lands the verified bytes as they are. Unverified bytes never reach
the landing path, and for a source with a step the raw bytes never reach disk at all. A source
whose entry says it names people is refused, before any request, until it has a step.

A set is several sources landed together by one step, under a name of its own that prefixes each
member's name before a dash (`<set>-<member>`): `SET_STEPS[<set>]` receives every member's verified
bytes by URL, in the sources file's order, and returns the one file landed as `<set>`. Nothing runs
or lands until every member has verified.

Holding the response in memory bounds a source: one over `MAX_BYTES` fails, as does one still
arriving at `DEADLINE`.
"""

import hashlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import yaml
from jsonschema import Draft202012Validator

from ingest.chicago import contracts
from ingest.ecb import rates
from ingest.gleif import legal_entities
from ingest.nl_government import budget
from ingest.uk_transport import spend

HERE = Path(__file__).resolve().parent
SOURCES = HERE / "sources.yaml"
SCHEMA = HERE / "sources.schema.json"

# The one name every host sees; it carries no email or account name.
CLIENT_NAME = "ledger-lab research-bot"
# Each connect or read may wait this long: portals can be slow to start a large pinned query.
TIMEOUT = httpx.Timeout(60.0)
# The whole download must finish within this many seconds; it is checked as each chunk arrives,
# so a server that falls silent is noticed after at most one more read timeout.
DEADLINE = 1800.0
# The largest response held in memory.
MAX_BYTES = 512 * 1024 * 1024

# A source's own step between verification and landing, by source name.
STEPS: dict[str, Callable[[bytes], bytes]] = {
    "chicago-contracts": contracts.step,
    "ecb-rates": rates.step,
    "gleif-legal-entities": legal_entities.step,
    "nl-government-budget": budget.step,
}
# A set's step, by set name: it lands every member, one URL each, as one file.
SET_STEPS: dict[str, Callable[[dict[str, bytes]], bytes]] = {
    spend.SET: spend.step,
}


class FetchError(Exception):
    """A source could not be fetched; nothing was written."""


class SourcesError(FetchError):
    """The sources file breaks its schema or lacks the asked source."""


class ChecksumMismatchError(FetchError):
    """The publisher's response differs from the recorded SHA-256."""


def load_sources(path: Path) -> dict[str, dict[str, Any]]:
    """Every source in the file, refusing the whole file if any entry breaks the schema."""
    doc = yaml.safe_load(path.read_text())
    validator = Draft202012Validator(json.loads(SCHEMA.read_text()))
    errors = [
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in validator.iter_errors(doc)
    ]
    if errors:
        raise SourcesError(f"{path} breaks {SCHEMA.name}:\n" + "\n".join(errors))
    return doc["sources"]


def landing_path() -> Path:
    return Path(os.environ.get("LEDGER_LAB_LANDING") or "landing")


def request_url(entry: dict[str, Any]) -> httpx.URL:
    """A fixed file's URL as written, or a pinned query's URL with its order and cut-off joined
    to the parameters it already carries (a row limit, a format)."""
    if "url" in entry:
        return httpx.URL(entry["url"])
    query = entry["query"]
    return httpx.URL(query["url"]).copy_merge_params({**query["order"], **query["cut_off"]})


def download(entry: dict[str, Any]) -> bytes:
    """The bytes the publisher serves, undecoded, following redirects, within the deadline and
    the byte cap."""
    url = request_url(entry)
    headers = {"User-Agent": CLIENT_NAME, "Accept-Encoding": "identity"}
    ends = time.monotonic() + DEADLINE
    body = bytearray()
    try:
        with (
            httpx.Client(headers=headers, follow_redirects=True, timeout=TIMEOUT) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            for chunk in response.iter_raw():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise FetchError(f"{url}: response is more than {MAX_BYTES} bytes")
                if time.monotonic() > ends:
                    raise FetchError(f"{url}: still arriving at the {DEADLINE:g} s deadline")
    except httpx.HTTPError as error:
        raise FetchError(f"{url}: {error}") from error
    return bytes(body)


def verify(name: str, entry: dict[str, Any], raw: bytes) -> None:
    digest = hashlib.sha256(raw).hexdigest()
    if digest != entry["sha256"]:
        raise ChecksumMismatchError(
            f"{name}: SHA-256 {digest} does not match the recorded {entry['sha256']}; "
            "the publisher's data has drifted or the pin is wrong, and nothing was written"
        )


def run_step[Raw](name: str, step: Callable[[Raw], bytes], raw: Raw) -> bytes:
    """The source's step; a failure is reported by source and kind only, since its message
    could quote the rows, names included."""
    try:
        return step(raw)
    except Exception as error:
        raise FetchError(f"{name}: its step failed ({type(error).__name__})") from None


def land(name: str, path: Path, data: bytes) -> Path:
    """Write the file whole or not at all: a temporary file beside it, then a rename; on any
    failure the temporary file is removed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as f:
            partial = Path(f.name)
        try:
            partial.write_bytes(data)
            partial.replace(path)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    except OSError as error:
        raise FetchError(f"{name}: cannot write {path}: {error.strerror}") from None
    return path


def fetch(name: str, landing: Path) -> Path:
    """Download, verify and land one source; the landed file is `<landing>/<name>`."""
    sources = load_sources(SOURCES)
    if name not in sources:
        raise SourcesError(f"no source named {name!r} in {SOURCES}")
    entry = sources[name]
    step = STEPS.get(name)
    if entry["names_people"] and step is None:
        raise SourcesError(f"{name} names people but has no step to hash them before landing")
    raw = download(entry)
    verify(name, entry, raw)
    return land(name, landing / name, run_step(name, step, raw) if step else raw)


def fetch_set(name: str, landing: Path) -> Path:
    """Download and verify every member of a set, then land its step's output as `<name>`."""
    sources = load_sources(SOURCES)
    members = [member for member in sources if member.startswith(f"{name}-")]
    if not members:
        raise SourcesError(f"no source named '{name}-…' in {SOURCES} for the set {name!r}")
    files = {}
    for member in members:
        raw = download(sources[member])
        verify(member, sources[member], raw)
        files[str(request_url(sources[member]))] = raw
    return land(name, landing / name, run_step(name, SET_STEPS[name], files))


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m ingest.fetch <source or set>", file=sys.stderr)
        return 2
    try:
        landed = (fetch_set if argv[0] in SET_STEPS else fetch)(argv[0], landing_path())
    except FetchError as error:
        print(f"fetch: {error}", file=sys.stderr)
        return 1
    print(landed)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
