"""The bucket landing lands a set all or none, against an in-memory file system in place of Cloud
Storage; only the lab's own landing bucket is accepted, before any call. The `bucket` test reads
the real bucket's listing and compares it with a local landing."""

import base64
import contextlib
import hashlib
import os
import uuid
from collections.abc import Iterator
from typing import Any

import google_crc32c
import pytest
from fsspec.implementations.memory import MemoryFileSystem

from ingest.bucket import SCHEME, Bucket, BucketError, bucket_name, lab_project
from ingest.fetch import landing_path

OLD = {"set-1": b"old one", "set-2": b"old two"}
NEW = {"set-1": b"new one", "set-2": b"new two"}


class Killed(BaseException):
    """The process killed outright: nothing after it runs, not even a cleanup."""


class Faulty(MemoryFileSystem):
    """An in-memory bucket whose uploads, copies and deletes are counted, and fail from the
    `fail_from`th of them on with `error`, or only on the calls `fail_on` picks."""

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
    kinds = [call[0] for call in fs.calls]
    assert kinds[:2] == ["upload", "upload"]
    assert all(call[-1].startswith(".") for call in fs.calls[:2])
    assert kinds.count("upload") == 2, "each file is uploaded once"


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
    with pytest.raises(BucketError, match="kept as") as raised:
        bucket.land_all(NEW)
    now = held(bucket)
    kept = {name: data for name, data in now.items() if ".old." in name}
    assert sorted(kept.values()) == sorted(OLD.values())
    assert all(name in str(raised.value) for name in kept)
    assert now == kept, "no new file and no temporary object stays"


@pytest.mark.parametrize("killed_at", range(13))
def test_a_process_killed_at_any_call_never_leaves_an_old_file_beside_a_new_one(
    bucket: Bucket, fs: Faulty, killed_at: int
) -> None:
    """Killed outright before its `killed_at`th upload, copy or delete (no cleanup runs after it),
    the bucket holds no new file beside an old one and every old file somewhere until each new
    one is in place; landing again places every new file."""
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
    assert all(name in NEW or name.startswith((".set-1.", ".set-2.")) for name in now)
    fs.fail_from = None
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
