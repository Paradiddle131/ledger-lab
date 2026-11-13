"""The fetch kit lands only bytes that match the recorded SHA-256, from a local HTTP server."""

import gzip
import hashlib
import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from conftest import REPO, load_checked
from jsonschema import Draft202012Validator

from ingest import StepError, fetch

FILE = b"id,amount\n1,10.00\n2,-3.50\n"
GZIPPED = gzip.compress(FILE, mtime=0)
LICENCE = "https://licence.example.org/open"


class Site:
    """A local HTTP server: fixed files, redirects, one live table behind a query, a gzip-encoded
    file, a server that drips one byte at a time, and one that cuts its first `cuts` responses
    short."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {"/file.csv": FILE, "/encoded.csv": GZIPPED}
        self.redirects: dict[str, str] = {"/moved.csv": "/file.csv"}
        self.table: list[tuple[int, str]] = [(2, "-3.50"), (1, "10.00"), (3, "7.25")]
        self.requests: list[dict[str, Any]] = []
        self.cuts = 0
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
                if url.path == "/drip":
                    self.drip()
                    return
                if url.path == "/cut.csv" and site.cuts > 0:
                    site.cuts -= 1
                    self.cut()
                    return
                if url.path == "/cut.csv":
                    body = FILE
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                if url.path == "/encoded.csv":
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                self.wfile.write(body)

            def drip(self) -> None:
                self.send_response(200)
                self.send_header("Content-Length", "1000")
                self.end_headers()
                try:
                    for _ in range(1000):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.02)
                except OSError:
                    pass

            def cut(self) -> None:
                """Promise the whole file, send half of it, and close the connection."""
                self.send_response(200)
                self.send_header("Content-Length", str(len(FILE)))
                self.end_headers()
                self.wfile.write(FILE[: len(FILE) // 2])
                self.wfile.flush()
                self.close_connection = True

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def query(self, params: dict[str, list[str]]) -> bytes:
        """The table as a portal serves it: in the asked order, up to the asked cut-off, and only
        a default first page of two rows unless a limit is asked."""
        rows = list(self.table)
        if params.get("$order") == ["id"]:
            rows.sort()
        if "$where" in params:
            cut_off = int(params["$where"][0].removeprefix("id <= "))
            rows = [row for row in rows if row[0] <= cut_off]
        rows = rows[: int(params.get("$limit", ["2"])[0])]
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


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch, "BACKOFF", 0.0)


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
    return {
        "url": url,
        "sha256": digest,
        "licence": LICENCE,
        "publisher": "Example",
        "names_people": False,
        **extra,
    }


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


def test_a_download_the_server_cuts_short_lands_on_the_retry(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    site.cuts = 1
    write_sources(payments=entry(f"{site.base}/cut.csv", sha256(FILE)))
    assert fetch.fetch("payments", landing).read_bytes() == FILE
    assert [r["path"] for r in site.requests] == ["/cut.csv", "/cut.csv"]


def test_a_download_cut_short_every_time_fails_after_the_last_attempt(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    site.cuts = fetch.ATTEMPTS
    write_sources(payments=entry(f"{site.base}/cut.csv", sha256(FILE)))
    with pytest.raises(fetch.FetchError, match=f"after {fetch.ATTEMPTS} attempts"):
        fetch.fetch("payments", landing)
    assert len(site.requests) == fetch.ATTEMPTS
    assert everything_under(landing) == []


def test_an_error_status_is_not_retried(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    write_sources(payments=entry(f"{site.base}/gone.csv", sha256(FILE)))
    with pytest.raises(fetch.FetchError, match="404"):
        fetch.fetch("payments", landing)
    assert len(site.requests) == 1


def test_a_checksum_mismatch_is_not_retried(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    write_sources(payments=entry(f"{site.base}/file.csv", sha256(b"something else")))
    with pytest.raises(fetch.ChecksumMismatchError):
        fetch.fetch("payments", landing)
    assert len(site.requests) == 1


def test_the_deadline_holds_across_retries(
    site: Site,
    landing: Path,
    write_sources: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fetch, "DEADLINE", 0.3)
    monkeypatch.setattr(fetch, "BACKOFF", 0.5)
    site.cuts = fetch.ATTEMPTS
    write_sources(payments=entry(f"{site.base}/cut.csv", sha256(FILE)))
    started = time.monotonic()
    with pytest.raises(fetch.FetchError, match="deadline"):
        fetch.fetch("payments", landing)
    assert time.monotonic() - started < 0.5
    assert len(site.requests) == 1


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
            "url": f"{site.base}/table?$limit=50000",
            "order": {"$order": "id"},
            "cut_off": {"$where": "id <= 3"},
        },
        "sha256": digest,
        "licence": LICENCE,
        "publisher": "Example",
        "names_people": False,
    }


def test_a_pinned_query_asks_for_its_order_and_cut_off(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    write_sources(table=pinned_query(site, sha256(TABLE_AT_CUT_OFF)))
    assert fetch.fetch("table", landing).read_bytes() == TABLE_AT_CUT_OFF
    assert site.requests[0]["query"] == {
        "$limit": ["50000"],
        "$order": ["id"],
        "$where": ["id <= 3"],
    }


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


def test_a_url_keeps_its_own_query_string(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    site.files["/plain"] = FILE
    write_sources(payments=entry(f"{site.base}/plain?format=csv&limit=5", sha256(FILE)))
    fetch.fetch("payments", landing)
    assert site.requests[0]["query"] == {"format": ["csv"], "limit": ["5"]}


def test_the_checksum_is_over_the_bytes_the_publisher_serves(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    write_sources(payments=entry(f"{site.base}/encoded.csv", sha256(GZIPPED)))
    assert fetch.fetch("payments", landing).read_bytes() == GZIPPED
    assert site.requests[0]["headers"]["Accept-Encoding"] == "identity"


def test_a_source_that_names_people_without_a_step_is_refused_before_any_request(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    write_sources(payees=entry(f"{site.base}/file.csv", sha256(FILE), names_people=True))
    with pytest.raises(fetch.SourcesError, match="payees names people but has no step"):
        fetch.fetch("payees", landing)
    assert site.requests == []
    assert everything_under(landing) == []


def test_a_source_that_names_people_lands_only_its_step_output(
    site: Site,
    landing: Path,
    write_sources: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(fetch.STEPS, "payees", lambda raw: raw.upper())
    write_sources(payees=entry(f"{site.base}/file.csv", sha256(FILE), names_people=True))
    assert fetch.fetch("payees", landing).read_bytes() == FILE.upper()


def test_an_entry_must_say_whether_it_names_people(
    site: Site, landing: Path, write_sources: Callable[..., Path]
) -> None:
    unsaid = entry(f"{site.base}/file.csv", sha256(FILE))
    del unsaid["names_people"]
    write_sources(payments=unsaid)
    with pytest.raises(fetch.SourcesError, match="names_people"):
        fetch.fetch("payments", landing)
    assert site.requests == []


def test_a_failing_step_is_reported_by_source_without_its_data(
    site: Site,
    landing: Path,
    write_sources: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken(raw: bytes) -> bytes:
        raise ValueError(f"cannot parse row {raw!r}")

    monkeypatch.setenv("LEDGER_LAB_LANDING", str(landing))
    monkeypatch.setitem(fetch.STEPS, "payments", broken)
    write_sources(payments=entry(f"{site.base}/file.csv", sha256(FILE)))
    assert fetch.main(["payments"]) == 1
    err = capsys.readouterr().err
    assert "payments: its step failed (ValueError)" in err
    assert "10.00" not in err
    with pytest.raises(fetch.FetchError) as raised:
        fetch.fetch("payments", landing)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert everything_under(landing) == []


def test_a_failed_write_leaves_no_partial_file_and_reports_it(
    site: Site,
    landing: Path,
    write_sources: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    in_the_way = landing / "payments"
    (in_the_way / "occupied").mkdir(parents=True)
    monkeypatch.setenv("LEDGER_LAB_LANDING", str(landing))
    write_sources(payments=entry(f"{site.base}/file.csv", sha256(FILE)))
    assert fetch.main(["payments"]) == 1
    assert "payments: cannot write" in capsys.readouterr().err
    assert everything_under(landing) == [in_the_way, in_the_way / "occupied"]


def test_a_response_over_the_byte_cap_fails_and_keeps_nothing(
    site: Site,
    landing: Path,
    write_sources: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fetch, "MAX_BYTES", len(FILE) - 1)
    write_sources(payments=entry(f"{site.base}/file.csv", sha256(FILE)))
    with pytest.raises(fetch.FetchError, match="more than"):
        fetch.fetch("payments", landing)
    assert everything_under(landing) == []


def test_a_slow_drip_server_fails_at_the_deadline(
    site: Site,
    landing: Path,
    write_sources: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fetch, "DEADLINE", 0.3)
    write_sources(payments=entry(f"{site.base}/drip", sha256(FILE)))
    started = time.monotonic()
    with pytest.raises(fetch.FetchError, match="deadline"):
        fetch.fetch("payments", landing)
    assert time.monotonic() - started < 2
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


def test_a_set_lands_every_member_through_its_step_as_one_file(
    site: Site,
    landing: Path,
    write_sources: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site.files["/second.csv"] = FILE.upper()
    seen: list[dict[str, bytes]] = []

    def join(files: dict[str, bytes]) -> bytes:
        seen.append(files)
        return b"".join(files.values())

    monkeypatch.setitem(fetch.SET_STEPS, "spend", join)
    write_sources(
        **{
            "spend-2": entry(f"{site.base}/second.csv", sha256(FILE.upper()), names_people=True),
            "spend-1": entry(f"{site.base}/file.csv", sha256(FILE), names_people=True),
            "spending": entry(f"{site.base}/file.csv", sha256(FILE)),
        }
    )
    landed = fetch.fetch_set("spend", landing)
    assert landed == landing / "spend"
    assert landed.read_bytes() == FILE + FILE.upper()
    assert [list(files) for files in seen] == [
        [f"{site.base}/file.csv", f"{site.base}/second.csv"]
    ], "the step gets each member by its URL, in the sources file's order, and no other source"
    assert everything_under(landing) == [landed]


def test_a_set_member_whose_download_is_cut_short_lands_on_the_retry(
    site: Site,
    landing: Path,
    write_sources: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site.cuts = 1
    monkeypatch.setitem(fetch.SET_STEPS, "spend", lambda files: b"".join(files.values()))
    write_sources(
        **{
            "spend-1": entry(f"{site.base}/file.csv", sha256(FILE)),
            "spend-2": entry(f"{site.base}/cut.csv", sha256(FILE)),
        }
    )
    assert fetch.fetch_set("spend", landing).read_bytes() == FILE + FILE
    assert [r["path"] for r in site.requests] == ["/file.csv", "/cut.csv", "/cut.csv"]


def test_a_set_whose_members_share_a_url_is_refused_before_any_request(
    site: Site,
    landing: Path,
    write_sources: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(fetch.SET_STEPS, "spend", lambda files: b"".join(files.values()))
    write_sources(
        **{
            "spend-1": entry(f"{site.base}/file.csv", sha256(FILE)),
            "spend-2": entry(f"{site.base}/file.csv", sha256(FILE)),
        }
    )
    with pytest.raises(fetch.SourcesError, match="spend-1 and spend-2 share one URL"):
        fetch.fetch_set("spend", landing)
    assert site.requests == []


def test_a_steps_own_refusal_reaches_the_operator_by_its_message(
    site: Site,
    landing: Path,
    write_sources: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(files: dict[str, bytes]) -> bytes:
        raise StepError("file.csv row 2: the amount is not a sum")

    monkeypatch.setenv("LEDGER_LAB_LANDING", str(landing))
    monkeypatch.setitem(fetch.SET_STEPS, "spend", refuse)
    write_sources(**{"spend-1": entry(f"{site.base}/file.csv", sha256(FILE))})
    assert fetch.main(["spend"]) == 1
    assert "spend: file.csv row 2: the amount is not a sum" in capsys.readouterr().err
    assert everything_under(landing) == []


def test_a_set_with_one_drifted_member_lands_nothing_and_never_runs_its_step(
    site: Site,
    landing: Path,
    write_sources: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, bytes]] = []
    monkeypatch.setitem(fetch.SET_STEPS, "spend", lambda files: seen.append(files) or b"")
    write_sources(
        **{
            "spend-1": entry(f"{site.base}/file.csv", sha256(FILE)),
            "spend-2": entry(f"{site.base}/file.csv?copy=2", sha256(b"drifted")),
        }
    )
    with pytest.raises(fetch.ChecksumMismatchError, match="spend-2"):
        fetch.fetch_set("spend", landing)
    assert seen == []
    assert everything_under(landing) == []


def test_a_set_without_members_is_refused_before_any_request(
    site: Site,
    landing: Path,
    write_sources: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(fetch.SET_STEPS, "spend", lambda files: b"")
    write_sources(spending=entry(f"{site.base}/file.csv", sha256(FILE)))
    with pytest.raises(fetch.SourcesError, match="no source named 'spend-…'"):
        fetch.fetch_set("spend", landing)
    assert site.requests == []


def test_the_command_lands_a_set_by_its_name(
    site: Site,
    landing: Path,
    write_sources: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEDGER_LAB_LANDING", str(landing))
    monkeypatch.setitem(fetch.SET_STEPS, "spend", lambda files: b"".join(files.values()))
    write_sources(**{"spend-1": entry(f"{site.base}/file.csv", sha256(FILE), names_people=True)})
    assert fetch.main(["spend"]) == 0
    assert (landing / "spend").read_bytes() == FILE
    with pytest.raises(fetch.SourcesError, match="spend-1 names people but has no step"):
        fetch.fetch("spend-1", landing)


def test_every_set_step_names_recorded_members_and_no_source() -> None:
    recorded = fetch.load_sources(fetch.SOURCES)
    for name in fetch.SET_STEPS:
        assert name not in recorded
        assert any(source.startswith(f"{name}-") for source in recorded), name
