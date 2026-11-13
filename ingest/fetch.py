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
bytes by URL, in the sources file's order, and returns either the one file landed as `<set>`, or
files by member name, each landed as that member (`chicago` lands `chicago-contracts` and
`chicago-payments`, scrubbed together); the files land together or not at all (`land_all`): on a
failure the landing keeps every file it held before, never a new file beside an old one. Members
may not share a URL. Nothing runs or lands until every member has verified.

Holding the response in memory bounds a source: one over `MAX_BYTES` fails, as does one still
arriving at `DEADLINE`. A download whose connection drops (cut short, closed, timed out) starts
again from the first byte, up to `ATTEMPTS` in all, within the same deadline; an error status, a
cap or deadline breach and a checksum mismatch fail at once.
"""

import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import yaml
from jsonschema import Draft202012Validator

from ingest import StepError
from ingest.chicago import together
from ingest.ecb import rates
from ingest.entities import REGISTER
from ingest.filings import facts
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
# Downloads a dropped connection may take in all, and the wait before the next, times the attempt
# number: portals now and then close a long response before its end, and a rerun completes.
ATTEMPTS = 3
BACKOFF = 5.0
# The transport errors a rerun can cure. httpx's own transport retries cover only connecting, not
# a body cut short; an error status, a proxy refusal or an unsupported scheme stays fatal.
DROPPED = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)

# A source's own step between verification and landing, by source name.
STEPS: dict[str, Callable[[bytes], bytes]] = {
    "ecb-rates": rates.step,
    "gleif-legal-entities": legal_entities.step,
    "nl-government-budget": budget.step,
}
# A set's step, by set name: it lands every member, one URL each, as one file named after the set
# or as files named after its members.
SET_STEPS: dict[str, Callable[[dict[str, bytes]], bytes | dict[str, bytes]]] = {
    spend.SET: spend.step,
    together.SET: together.step,
    facts.SET: facts.step,
}
# The sets whose steps read the landed legal-entity register; the kit refuses them before any
# download while it is not landed.
READS_THE_REGISTER = frozenset({spend.SET, together.SET})


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
    the byte cap, downloading again from the start when the connection drops."""
    url = request_url(entry)
    ends = time.monotonic() + DEADLINE
    attempt = 1
    while True:
        try:
            return _download_once(url, ends)
        except DROPPED as error:
            wait = BACKOFF * attempt
            if attempt == ATTEMPTS:
                raise FetchError(f"{url}: {error} (after {ATTEMPTS} attempts)") from error
            if time.monotonic() + wait > ends:
                raise FetchError(f"{url}: {error}; no time to retry by the deadline") from error
            time.sleep(wait)
            attempt += 1
        except httpx.HTTPError as error:
            raise FetchError(f"{url}: {error}") from error


def _download_once(url: httpx.URL, ends: float) -> bytes:
    headers = {"User-Agent": CLIENT_NAME, "Accept-Encoding": "identity"}
    body = bytearray()
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
    return bytes(body)


def verify(name: str, entry: dict[str, Any], raw: bytes) -> None:
    digest = hashlib.sha256(raw).hexdigest()
    if digest != entry["sha256"]:
        raise ChecksumMismatchError(
            f"{name}: SHA-256 {digest} does not match the recorded {entry['sha256']}; "
            "the publisher's data has drifted or the pin is wrong, and nothing was written"
        )


def run_step[Raw, Out](name: str, step: Callable[[Raw], Out], raw: Raw) -> Out:
    """The source's step; a failure is reported by source and kind only, since its message
    could quote the rows, names included, except a step's own refusal (`StepError`), whose
    message names a file, row or column and never a value."""
    try:
        return step(raw)
    except StepError as error:
        raise FetchError(f"{name}: {error}") from None
    except Exception as error:
        raise FetchError(f"{name}: its step failed ({type(error).__name__})") from None


def land(name: str, path: Path, data: bytes) -> Path:
    """Write the file whole or not at all (`land_all` of one file)."""
    return land_all(name, {path: data})[0]


def land_all(name: str, files: dict[Path, bytes]) -> list[Path]:
    """Land every file or none, leaving what was landed before as it was.

    Each file is written whole to a hidden temporary file beside its place; then each file already
    landed there is moved aside to a hidden name, and the new ones are renamed into place. If any
    of that fails, the new files are removed and the old ones moved back, so the landing holds
    either every new file or every old one; if moving them back fails too, the old files are kept
    under their hidden names, which the error names, never deleted. A process killed outright
    mid-way leaves the hidden files behind, and a place it had emptied stays empty, never holding
    an old file beside a new one; landing again replaces them all."""
    partials: dict[Path, Path] = {}
    aside: dict[Path, Path] = {}
    placed: list[Path] = []
    path = next(iter(files))
    try:
        for path, data in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            partials[path] = _hidden(path)
            partials[path].write_bytes(data)
        for path in files:
            if path.is_file():
                old = path.with_name(f".{path.name}.old.{uuid.uuid4().hex}")
                path.replace(old)
                aside[path] = old
        for path, partial in partials.items():
            partial.replace(path)
            placed.append(path)
    except OSError as error:
        try:
            for landed in placed:
                landed.unlink(missing_ok=True)
            for landed, old in list(aside.items()):
                old.replace(landed)
                del aside[landed]
        except OSError:
            kept = ", ".join(old.name for old in aside.values())
            aside.clear()  # the old files stay where they were moved aside
            raise FetchError(
                f"{name}: cannot write {path}: {error.strerror}, nor move back what it replaced,"
                f" which is kept as {kept}"
            ) from None
        raise FetchError(f"{name}: cannot write {path}: {error.strerror}") from None
    finally:
        for leftover in [*partials.values(), *aside.values()]:
            leftover.unlink(missing_ok=True)
    return list(files)


def _hidden(path: Path) -> Path:
    """A fresh hidden file beside `path` (`.<name>.new.<random>`)."""
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.new.", delete=False
    ) as f:
        return Path(f.name)


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


def fetch_set(name: str, landing: Path) -> list[Path]:
    """Download and verify every member of a set, then land its step's output: one file as
    `<name>`, or files by member name."""
    sources = load_sources(SOURCES)
    members = [member for member in sources if member.startswith(f"{name}-")]
    if not members:
        raise SourcesError(f"no source named '{name}-…' in {SOURCES} for the set {name!r}")
    urls: dict[str, str] = {}
    for member in members:
        url = str(request_url(sources[member]))
        if url in urls:
            raise SourcesError(f"{urls[url]} and {member} share one URL in the set {name!r}")
        urls[url] = member
    if name in READS_THE_REGISTER and not (landing / REGISTER).is_file():
        raise SourcesError(f"{name} keeps in clear what {REGISTER} vouches for: land it first")
    files = {}
    for member in members:
        raw = download(sources[member])
        verify(member, sources[member], raw)
        files[str(request_url(sources[member]))] = raw
    landed = run_step(name, SET_STEPS[name], files)
    if isinstance(landed, bytes):
        return land_all(name, {landing / name: landed})
    if not set(landed) <= set(members):
        raise FetchError(f"{name}: its step names a file that is no member of the set")
    return land_all(name, {landing / member: data for member, data in landed.items()})


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m ingest.fetch <source or set>", file=sys.stderr)
        return 2
    try:
        if argv[0] in SET_STEPS:
            landed = fetch_set(argv[0], landing_path())
        else:
            landed = [fetch(argv[0], landing_path())]
    except FetchError as error:
        print(f"fetch: {error}", file=sys.stderr)
        return 1
    for path in landed:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
