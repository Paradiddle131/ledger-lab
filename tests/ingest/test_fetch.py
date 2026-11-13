"""The fetch kit lands only bytes that match the recorded SHA-256, from a local HTTP server."""

import hashlib
import json
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from conftest import REPO, load_checked
from jsonschema import Draft202012Validator

from ingest import fetch

FILE = b"id,amount\n1,10.00\n2,-3.50\n"
LICENCE = "https://licence.example.org/open"


class Site:
    """A local HTTP server: fixed files, redirects, and one live table behind a query."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {"/file.csv": FILE}
        self.redirects: dict[str, str] = {"/moved.csv": "/file.csv"}
        self.table: list[tuple[int, str]] = [(2, "-3.50"), (1, "10.00"), (3, "7.25")]
        self.requests: list[dict[str, Any]] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                url = urlsplit(self.path)
                site.requests.append(
                    {"path": url.path, "query": parse_qs(url.query), "headers": dict(self.headers)}
                )
                if url.path in site.redirects:
                    self.send_response(302)
                    self.send_header("Location", site.redirects[url.path])
                    self.end_headers()
                    return
                body = site.files.get(url.path)
                if url.path == "/table":
                    body = site.query(parse_qs(url.query))
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def query(self, params: dict[str, list[str]]) -> bytes:
        """The table as a portal serves it: in the asked order, up to the asked cut-off."""
        rows = list(self.table)
        if params.get("$order") == ["id"]:
            rows.sort()
        if "$where" in params:
            cut_off = int(params["$where"][0].removeprefix("id <= "))
            rows = [row for row in rows if row[0] <= cut_off]
        return ("id,amount\n" + "".join(f"{i},{a}\n" for i, a in rows)).encode()


@pytest.fixture
def site(monkeypatch: pytest.MonkeyPatch) -> Iterator[Site]:
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    served = Site()
    thread = threading.Thread(target=served.server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    yield served
    served.server.shutdown()
    served.server.server_close()


@pytest.fixture
def landing(tmp_path: Path) -> Path:
    return tmp_path / "landing"


@pytest.fixture
def write_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Path]:
    """Point the kit at a temporary sources file holding the given entries."""

    def write(**entries: dict[str, Any]) -> Path:
        path = tmp_path / "sources.yaml"
        path.write_text(yaml.safe_dump({"sources": entries}))
        monkeypatch.setattr(fetch, "SOURCES", path)
        return path

    return write


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def entry(url: str, digest: str, **extra: Any) -> dict[str, Any]:
    return {"url": url, "sha256": digest, "licence": LICENCE, "publisher": "Example", **extra}


def everything_under(path: Path) -> list[Path]:
    return sorted(p for p in path.rglob("*")) if path.exists() else []


def test_a_match_lands_the_bytes_as_published(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    write_sources(payments=entry(f"{site.base}/file.csv", sha256(FILE)))
    landed = fetch.fetch("payments", landing)
    assert landed == landing / "payments"
    assert landed.read_bytes() == FILE
    assert everything_under(landing) == [landed]


def test_a_mismatch_fails_and_keeps_nothing(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    write_sources(payments=entry(f"{site.base}/file.csv", sha256(b"something else")))
    with pytest.raises(fetch.ChecksumMismatchError, match="payments.*does not match"):
        fetch.fetch("payments", landing)
    assert everything_under(landing) == []


def test_the_command_exits_non_zero_on_a_mismatch(
    site: Site,
    landing: Path,
    write_sources: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LEDGER_LAB_LANDING", str(landing))
    write_sources(payments=entry(f"{site.base}/file.csv", sha256(b"something else")))
    assert fetch.main(["payments"]) == 1
    assert "does not match" in capsys.readouterr().err
    assert everything_under(landing) == []


def test_the_command_lands_under_the_landing_variable(
    site: Site,
    landing: Path,
    write_sources: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LEDGER_LAB_LANDING", str(landing))
    write_sources(payments=entry(f"{site.base}/file.csv", sha256(FILE)))
    assert fetch.main(["payments"]) == 0
    assert (landing / "payments").read_bytes() == FILE
    assert str(landing / "payments") in capsys.readouterr().out


def test_the_landing_path_defaults_to_landing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGER_LAB_LANDING", raising=False)
    assert fetch.landing_path() == Path("landing")


def test_a_redirect_is_followed_and_the_final_bytes_verified(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    write_sources(payments=entry(f"{site.base}/moved.csv", sha256(FILE)))
    assert fetch.fetch("payments", landing).read_bytes() == FILE
    assert [r["path"] for r in site.requests] == ["/moved.csv", "/file.csv"]


def test_an_entry_without_a_licence_link_is_refused_before_any_request(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    unlicensed = entry(f"{site.base}/file.csv", sha256(FILE))
    del unlicensed["licence"]
    write_sources(payments=unlicensed)
    with pytest.raises(fetch.SourcesError, match="licence"):
        fetch.fetch("payments", landing)
    assert site.requests == []
    assert everything_under(landing) == []


def test_a_licence_that_is_not_a_link_is_refused(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    write_sources(payments=entry(f"{site.base}/file.csv", sha256(FILE), licence="open"))
    with pytest.raises(fetch.SourcesError, match="licence"):
        fetch.fetch("payments", landing)
    assert site.requests == []


def test_an_unknown_source_is_refused(
    landing: Path, write_sources: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    write_sources()
    with pytest.raises(fetch.SourcesError, match="no source named 'payments'"):
        fetch.fetch("payments", landing)
    assert fetch.main(["payments"]) == 1
    assert "no source named" in capsys.readouterr().err


def test_an_error_status_fails_and_keeps_nothing(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    write_sources(payments=entry(f"{site.base}/gone.csv", sha256(FILE)))
    with pytest.raises(fetch.FetchError, match="404"):
        fetch.fetch("payments", landing)
    assert everything_under(landing) == []


def test_every_host_gets_the_generic_client_name(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    write_sources(payments=entry(f"{site.base}/moved.csv", sha256(FILE)))
    fetch.fetch("payments", landing)
    agents = {r["headers"]["User-Agent"] for r in site.requests}
    assert agents == {"ledger-lab research-bot"}
    for request in site.requests:
        assert "From" not in request["headers"]
        assert not any("@" in value for value in request["headers"].values())


TABLE_AT_CUT_OFF = b"id,amount\n1,10.00\n2,-3.50\n3,7.25\n"


def pinned_query(site: Site, digest: str) -> dict[str, Any]:
    return {
        "query": {
            "url": f"{site.base}/table",
            "order": {"$order": "id"},
            "cut_off": {"$where": "id <= 3"},
        },
        "sha256": digest,
        "licence": LICENCE,
        "publisher": "Example",
    }


def test_a_pinned_query_asks_for_its_order_and_cut_off(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    write_sources(table=pinned_query(site, sha256(TABLE_AT_CUT_OFF)))
    assert fetch.fetch("table", landing).read_bytes() == TABLE_AT_CUT_OFF
    assert site.requests[0]["query"] == {"$order": ["id"], "$where": ["id <= 3"]}


def test_a_pinned_query_repeats_while_the_live_table_grows(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    write_sources(table=pinned_query(site, sha256(TABLE_AT_CUT_OFF)))
    fetch.fetch("table", landing)
    site.table.append((4, "1.00"))
    assert fetch.fetch("table", landing).read_bytes() == TABLE_AT_CUT_OFF


def test_a_revised_row_before_the_cut_off_fails_loudly_as_drift(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    write_sources(table=pinned_query(site, sha256(TABLE_AT_CUT_OFF)))
    site.table[0] = (2, "-3.60")
    with pytest.raises(fetch.ChecksumMismatchError, match="table.*does not match"):
        fetch.fetch("table", landing)
    assert everything_under(landing) == []


def test_a_query_without_a_cut_off_is_refused(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    unpinned = pinned_query(site, sha256(TABLE_AT_CUT_OFF))
    del unpinned["query"]["cut_off"]
    write_sources(table=unpinned)
    with pytest.raises(fetch.SourcesError, match="cut_off"):
        fetch.fetch("table", landing)
    assert site.requests == []


def test_a_source_step_sees_only_verified_bytes_and_only_its_output_is_written(
    site: Site,
    landing: Path,
    write_sources: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bytes] = []

    def redact(raw: bytes) -> bytes:
        seen.append(raw)
        return raw.replace(b"10.00", b"xx")

    monkeypatch.setitem(fetch.STEPS, "payments", redact)
    write_sources(payments=entry(f"{site.base}/file.csv", sha256(FILE)))
    landed = fetch.fetch("payments", landing)
    assert seen == [FILE]
    assert landed.read_bytes() == FILE.replace(b"10.00", b"xx")
    assert everything_under(landing) == [landed]


def test_a_source_step_never_runs_on_unverified_bytes(
    site: Site,
    landing: Path,
    write_sources: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bytes] = []
    monkeypatch.setitem(fetch.STEPS, "payments", lambda raw: seen.append(raw) or raw)
    write_sources(payments=entry(f"{site.base}/file.csv", sha256(b"something else")))
    with pytest.raises(fetch.ChecksumMismatchError):
        fetch.fetch("payments", landing)
    assert seen == []
    assert everything_under(landing) == []


def test_every_step_names_a_recorded_source() -> None:
    recorded = fetch.load_sources(fetch.SOURCES)
    assert set(fetch.STEPS) <= set(recorded)


def test_the_checked_in_sources_file_meets_its_schema() -> None:
    load_checked(REPO / "ingest/sources.yaml", REPO / "ingest/sources.schema.json")
    Draft202012Validator.check_schema(json.loads((REPO / "ingest/sources.schema.json").read_text()))


def test_the_module_runs_from_the_repo_root() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ingest.fetch", "no-such-source"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "no source named 'no-such-source'" in result.stderr
