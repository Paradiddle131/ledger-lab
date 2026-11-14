"""The bucket landing lands a set all or none, against an in-memory file system in place of Cloud
Storage; only the lab's own landing bucket is accepted, before any call. The `bucket` test reads
the real bucket's listing and compares it with a local landing."""

import base64
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import google_crc32c
import pytest
from conftest import REPO
from fsspec.implementations.memory import MemoryFileSystem

from ingest.bucket import (
    LOCK,
    SCHEME,
    Bucket,
    BucketError,
    bucket_name,
    lab_project,
)
from ingest.fetch import landing_path

OLD = {"set-1": b"old one", "set-2": b"old two"}
NEW = {"set-1": b"new one", "set-2": b"new two"}
ACCOUNT = "agent@example.com"
BUCKET_NAME = "the-lab-project-landing"


class Killed(BaseException):
    """The process killed outright: nothing after it runs, not even a cleanup."""


class Faulty(MemoryFileSystem):
    """An in-memory bucket whose uploads, copies and deletes are counted, and fail from the
    `fail_from`th of them on with `error`, or only on the calls `fail_on` picks; a new one each
    time, never fsspec's cached instance."""

    cachable = False

    def __init__(self) -> None:
        super().__init__(skip_instance_cache=True)
        self.calls: list[tuple[str, ...]] = []
        self.fail_from: int | None = None
        self.fail_on: Any = None
        self.error: BaseException = OSError("refused")

    def _hook(self, *call: str) -> None:
        self.calls.append(call)
        late = self.fail_from is not None and len(self.calls) > self.fail_from
        if late or (self.fail_on is not None and self.fail_on(*call)):
            raise self.error

    def pipe_file(self, path: str, value: bytes, *args: Any, **kwargs: Any) -> Any:
        self._hook("upload", path.rsplit("/", 1)[-1])
        return super().pipe_file(path, value, *args, **kwargs)

    def cp_file(self, path1: str, path2: str, **kwargs: Any) -> Any:
        self._hook("copy", path1.rsplit("/", 1)[-1], path2.rsplit("/", 1)[-1])
        return super().cp_file(path1, path2, **kwargs)

    def rm_file(self, path: str) -> Any:
        self._hook("delete", path.rsplit("/", 1)[-1])
        return super().rm_file(path)


@pytest.fixture
def fs() -> Faulty:
    return Faulty()


@pytest.fixture
def bucket(fs: Faulty) -> Iterator[Bucket]:
    root = f"/bucket-{uuid.uuid4().hex}"
    fs.mkdir(root)
    yield Bucket(fs, root)
    fs.fail_from = fs.fail_on = None
    fs.rm(root, recursive=True)


def held(bucket: Bucket) -> dict[str, bytes]:
    """Every object in the bucket by name, hidden ones included."""
    return {name: bucket.read(name) for name in bucket.names()}


def put(bucket: Bucket, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        bucket.fs.pipe_file(f"{bucket.root}/{name}", data)
    bucket.fs.calls.clear()  # type: ignore[attr-defined]


def test_files_land_under_their_names_and_leave_nothing_else(bucket: Bucket) -> None:
    assert bucket.land_all(NEW) == ["set-1", "set-2"]
    assert held(bucket) == NEW


def test_files_that_land_together_replace_the_old_ones_and_leave_nothing_else(
    bucket: Bucket,
) -> None:
    put(bucket, {"set-1": b"old one", "other": b"kept"})
    bucket.land_all(NEW)
    assert held(bucket) == {**NEW, "other": b"kept"}


def test_every_new_file_is_uploaded_before_any_old_one_is_touched(
    bucket: Bucket, fs: Faulty
) -> None:
    put(bucket, OLD)
    bucket.land_all(NEW)
    assert fs.calls[0] == ("upload", LOCK), "the lock comes first"
    kinds = [call[0] for call in fs.calls[1:]]
    assert kinds[:2] == ["upload", "upload"]
    assert all(call[-1].startswith(".") for call in fs.calls[1:3])
    assert kinds.count("upload") == 2, "each file is uploaded once"
    assert fs.calls[-1] == ("delete", LOCK), "the lock goes last"


@pytest.mark.parametrize(
    ("failing", "why"),
    [
        (lambda *c: c[0] == "upload" and c[1].startswith(".set-2.new."), "an upload"),
        (lambda *c: c[0] == "copy" and c[2].startswith(".set-2.old."), "moving one aside"),
        (lambda *c: c[0] == "copy" and c[1].startswith(".set-1.new."), "placing the first"),
        (lambda *c: c[0] == "copy" and c[1].startswith(".set-2.new."), "placing the second"),
    ],
)
def test_a_failure_leaves_the_old_files_as_they_were_and_nothing_else(
    bucket: Bucket, fs: Faulty, failing: Any, why: str
) -> None:
    put(bucket, OLD)
    fs.fail_on = failing
    with pytest.raises(BucketError, match="cannot") as raised:
        bucket.land_all(NEW)
    assert held(bucket) == OLD, why
    assert "refused" not in str(raised.value), "the API's words stay out of the message"


def test_a_failure_landing_a_new_set_leaves_the_bucket_empty(bucket: Bucket, fs: Faulty) -> None:
    fs.fail_on = lambda *c: c[0] == "copy" and c[1].startswith(".set-2.new.")
    with pytest.raises(BucketError):
        bucket.land_all(NEW)
    assert held(bucket) == {}


def test_a_failed_rollback_keeps_the_old_files_under_the_hidden_names_it_reports(
    bucket: Bucket, fs: Faulty
) -> None:
    """The second new file cannot be placed and the first old one cannot be put back: neither new
    file stays, and both old ones stay whole under hidden names, which the error names."""
    put(bucket, OLD)
    fs.fail_on = lambda *c: (
        c[0] == "copy"
        and (c[1].startswith(".set-2.new.") or (c[1].startswith(".set-1.old.") and c[2] == "set-1"))
    )
    with pytest.raises(BucketError, match=f"kept as .*the lock {LOCK} stays") as raised:
        bucket.land_all(NEW)
    now = held(bucket)
    kept = {name: data for name, data in now.items() if ".old." in name}
    assert sorted(kept.values()) == sorted(OLD.values())
    assert all(name in str(raised.value) for name in kept)
    assert now == {**kept, LOCK: b""}, "no new file and no temporary object stays, the lock does"


@pytest.mark.parametrize("killed_at", range(15))
def test_a_process_killed_at_any_call_never_leaves_an_old_file_beside_a_new_one(
    bucket: Bucket, fs: Faulty, killed_at: int
) -> None:
    """Killed outright before its `killed_at`th upload, copy or delete (no cleanup runs after it),
    the bucket holds no new file beside an old one and every old file somewhere until each new
    one is in place; once the stale lock is deleted, landing again places every new file."""
    put(bucket, OLD)
    fs.fail_from, fs.error = killed_at, Killed()
    with contextlib.suppress(Killed):
        bucket.land_all(NEW)
    now = held(bucket)
    at_names = {name: now.get(name) for name in NEW}
    assert not (
        any(at_names[n] == OLD[n] for n in NEW) and any(at_names[n] == NEW[n] for n in NEW)
    ), at_names
    if any(at_names[n] != NEW[n] for n in NEW):
        assert set(OLD.values()) <= set(now.values()), "an old file was lost before the new set"
    assert all(name in {*NEW, LOCK} or name.startswith((".set-1.", ".set-2.")) for name in now)
    fs.fail_from = None
    if LOCK in now:
        bucket.fs.rm_file(f"{bucket.root}/{LOCK}")
    bucket.land_all(NEW)
    assert {name: data for name, data in held(bucket).items() if name in NEW} == NEW


def test_an_unreadable_object_is_reported_by_name(bucket: Bucket) -> None:
    with pytest.raises(BucketError, match="cannot read gleif-legal-entities"):
        bucket.read("gleif-legal-entities")
    assert not bucket.exists("gleif-legal-entities")


LAB = {"LEDGER_LAB_GCP_PROJECT": "the-lab-project"}


def test_the_lab_landing_bucket_is_accepted() -> None:
    assert bucket_name("gs://the-lab-project-landing", LAB) == "the-lab-project-landing"
    assert bucket_name("gs://the-lab-project-landing/", LAB) == "the-lab-project-landing"


@pytest.mark.parametrize(
    ("landing", "env", "refusal"),
    [
        ("gs://another-bucket", LAB, "the lab's landing bucket"),
        ("gs://the-lab-project-landing/raw", LAB, "never a folder"),
        ("gs://the-lab-project-landing", {}, "unset"),
        ("gs://another-lab-landing", {"GCP_PROJECT": "another-lab"}, "belongs to another lab"),
    ],
)
def test_any_other_bucket_is_refused(landing: str, env: dict[str, str], refusal: str) -> None:
    with pytest.raises(BucketError, match=refusal):
        bucket_name(landing, env)


@pytest.mark.parametrize("interrupted_at", range(16))
def test_ctrl_c_at_any_call_keeps_every_old_file(
    bucket: Bucket, fs: Faulty, interrupted_at: int
) -> None:
    """Ctrl-C once, at any upload, copy or delete: the landing puts the old files back, or has
    already placed the whole new set, and an old file is never deleted while it is the only copy."""
    put(bucket, OLD)
    fs.fail_on = lambda *call: len(fs.calls) == interrupted_at + 1
    fs.error = KeyboardInterrupt()
    with contextlib.suppress(KeyboardInterrupt):
        bucket.land_all(NEW)
    fs.fail_on = None
    now = held(bucket)
    at_names = {name: now.get(name) for name in NEW}
    assert at_names in (OLD, NEW), at_names
    if at_names == OLD:
        assert now == OLD, "a completed rollback leaves nothing else"


def test_a_copy_whose_response_is_lost_never_leaves_an_old_file_beside_a_new_one(
    bucket: Bucket, fs: Faulty
) -> None:
    """`set-1` was landed before and `set-2` is new to the bucket; placing `set-2` succeeds on the
    server but the client sees an error: the rollback removes `set-2` too."""
    put(bucket, {"set-1": b"old one"})
    copy = Faulty.cp_file

    def lost(self: Faulty, path1: str, path2: str, **kwargs: Any) -> Any:
        out = copy(self, path1, path2, **kwargs)
        if path2.endswith("/set-2"):
            raise OSError("response lost")
        return out

    fs.cp_file = lost.__get__(fs)  # type: ignore[method-assign]
    with pytest.raises(BucketError):
        bucket.land_all(NEW)
    assert held(bucket) == {"set-1": b"old one"}


def test_a_second_landing_is_refused_while_one_holds_the_lock(bucket: Bucket) -> None:
    put(bucket, {**OLD, LOCK: b""})
    with pytest.raises(BucketError, match="delete it by hand") as raised:
        bucket.land_all(NEW)
    assert held(bucket) == {**OLD, LOCK: b""}, "the refused run changes nothing, its lock neither"
    assert bucket.root not in str(raised.value)


@pytest.mark.parametrize("at", range(14))
def test_a_landing_started_during_another_never_mixes_the_sets(
    bucket: Bucket, fs: Faulty, at: int
) -> None:
    """Landing A of the new files meets, at its `at`th call, a whole landing B of other bytes:
    the bucket ends holding one whole set, and a refused landing says why."""
    put(bucket, OLD)
    other = {"set-1": b"b one", "set-2": b"b two"}
    refusals: list[str] = []

    def meets(*call: str) -> bool:
        if len(fs.calls) == at + 1:
            fs.fail_on = None
            try:
                Bucket(fs, bucket.root).land_all(other)
            except BucketError as error:
                refusals.append(str(error))
        return False

    fs.fail_on = meets
    try:
        bucket.land_all(NEW)
    except BucketError as error:
        refusals.append(str(error))
    now = held(bucket)
    assert tuple(now.get(n) for n in NEW) in {tuple(s.values()) for s in (OLD, NEW, other)}, now
    assert all("lock" in r for r in refusals), refusals


def test_a_killed_landing_leaves_its_lock_and_the_next_one_reports_what_it_left(
    bucket: Bucket, fs: Faulty
) -> None:
    """Killed after moving the old files aside: the lock stays, so the next landing refuses until
    an operator deletes it; then the next landing removes the stale uploads of its own names and
    keeps every `.old.` copy, which it reports, since one may be the only copy of a file."""
    put(bucket, {**OLD, ".other.new.0": b"not ours"})
    fs.fail_from, fs.error = 7, Killed()  # the lock, 2 uploads, 2 × (copy aside, delete)
    with contextlib.suppress(Killed):
        bucket.land_all(NEW)
    fs.fail_from = None
    with pytest.raises(BucketError, match="lock"):
        bucket.land_all(NEW)
    bucket.fs.rm_file(f"{bucket.root}/{LOCK}")
    assert bucket.land_all(NEW) == ["set-1", "set-2"]
    now = held(bucket)
    old_copies = sorted(name for name in now if ".old." in name)
    assert [now[name] for name in old_copies] == [b"old one", b"old two"]
    assert sorted(bucket.left_behind) == old_copies
    assert {name for name in now if name.startswith(".")} == {*old_copies, ".other.new.0"}
    assert {name: now[name] for name in NEW} == NEW


READER = """
import sys
from ingest.bucket import Bucket, BucketError, cloud_storage
try:
    Bucket(cloud_storage(), sys.argv[1]).read("x")
except BucketError as error:
    print(error)
"""


def test_a_failing_call_prints_neither_the_bucket_nor_the_account() -> None:
    """gcsfs logs a non-retriable error with Google's words, which name the bucket and the
    account, through Python's last-resort handler to stderr; the client made here keeps them off,
    and the error names only the object. A process of its own, since pytest captures logging."""

    class Refusing(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps(
                {"error": {"code": 409, "message": f"{ACCOUNT} may not read {BUCKET_NAME}/x"}}
            ).encode()
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Refusing)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    proxies = {"HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"}
    env = {k: v for k, v in os.environ.items() if k not in proxies}
    env["STORAGE_EMULATOR_HOST"] = f"http://127.0.0.1:{server.server_port}"
    try:
        result = subprocess.run(
            [sys.executable, "-c", READER, BUCKET_NAME],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO,
            timeout=120,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result.stdout == "cannot read x (HttpError)\n", result.stdout + result.stderr
    for shown in (result.stdout, result.stderr):
        assert ACCOUNT not in shown and BUCKET_NAME not in shown, shown


@pytest.mark.bucket
def test_the_landing_bucket_holds_the_local_landing_byte_for_byte() -> None:
    """Every file of the local landing (`LEDGER_LAB_LANDING`) is an object of the lab's landing
    bucket of the same name, size, MD5 and CRC32C, and the bucket holds nothing else, no hidden
    object of an interrupted landing included; one listing, no object read."""
    local = landing_path()
    files = {p.name: p.read_bytes() for p in local.iterdir() if p.is_file()}
    assert files, "the local landing is empty"
    env = dict(os.environ)
    lab = Bucket.of_landing(f"{SCHEME}{lab_project(env)}-landing", env)
    listed = {str(o["name"]).rsplit("/", 1)[-1]: o for o in lab.fs.ls(lab.root, detail=True)}
    assert sorted(listed) == sorted(files)
    for name, data in files.items():
        stored = listed[name]
        assert int(stored["size"]) == len(data), name
        assert (
            stored["md5Hash"]
            == base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()
        ), name
        crc = google_crc32c.value(data).to_bytes(4, "big")
        assert stored["crc32c"] == base64.b64encode(crc).decode(), name
    print(f"{len(files)} objects, {sum(map(len, files.values()))} bytes, as landed locally")
