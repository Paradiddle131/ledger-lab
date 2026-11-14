"""A landing in the lab's Cloud Storage bucket: `LEDGER_LAB_LANDING=gs://<lab project>-landing`.

The fetch kit writes there exactly the bytes it would write to a local landing, after the same
verification and the same step in memory, as objects at the bucket's root named as the local files
are (`chicago-contracts`), so no name holds a slash. The bucket must be the lab's own, named from
`LEDGER_LAB_GCP_PROJECT` through `platform/gcp_project.py` as `platform/gcp/landing.tf` names it;
any other bucket, or a folder inside it, is refused before any call.

Object storage has no rename, so a set lands in the order a local landing renames, with copies:
every new file is uploaded under a hidden temporary name (`.<name>.new.<random>`) first; then each
object already landed under a file's name is copied aside to a hidden name (`.<name>.old.<random>`)
and deleted; then each temporary object is copied to its file's name. A failure on the way removes
what it placed and copies the old objects back, so the bucket holds every new file or every old
one; if copying back fails too, the old objects stay under their hidden names, which the error
names. A process killed mid-way leaves hidden objects behind, and a name it had emptied stays empty,
never an old object beside a new one; landing again replaces them all. A copy within the bucket
replaces an object whole, so a reader never sees half of one.

The credential is the one the session's network proxy adds to every request to Cloud Storage, as
for `make cost-check`; the client sends none of its own.
"""

import contextlib
import importlib.util
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fsspec import AbstractFileSystem

SCHEME = "gs://"
PROJECT_SEAM = Path(__file__).resolve().parent.parent / "platform" / "gcp_project.py"


class BucketError(Exception):
    """The landing bucket refused a write or a read, or is not the lab's; the message names
    objects by their names in the bucket and never the bucket, the project or an API's words."""


def is_bucket(landing: str) -> bool:
    return landing.startswith(SCHEME)


def lab_project(env: Mapping[str, str]) -> str:
    """The lab's project from the one module that names it (`platform/` is run by path, so it is
    loaded from its file)."""
    spec = importlib.util.spec_from_file_location("gcp_project", PROJECT_SEAM)
    if spec is None or spec.loader is None:
        raise BucketError(f"cannot load {PROJECT_SEAM.name}")
    seam = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seam)
    try:
        return seam.lab_project(env)
    except seam.NoLabProjectError as error:
        raise BucketError(f"a bucket landing needs the lab's own project: {error}") from None


def bucket_name(landing: str, env: Mapping[str, str]) -> str:
    """The bucket a `gs://` landing names, refused unless it is the lab's landing bucket."""
    name = landing.removeprefix(SCHEME)
    if "/" in name.rstrip("/"):
        raise BucketError("a bucket landing is the bucket's root, never a folder inside it")
    if name.rstrip("/") != f"{lab_project(env)}-landing":
        raise BucketError(
            "a bucket landing must be the lab's landing bucket, gs://<project>-landing"
        )
    return name.rstrip("/")


def cloud_storage() -> AbstractFileSystem:
    """gcsfs's plain client: JSON API calls over aiohttp through the session's proxy, with every
    upload's MD5 checked against the one Cloud Storage computes. The plain class, not the package's
    default, which first asks a gRPC API whether a bucket is zonal."""
    from gcsfs.core import GCSFileSystem

    return GCSFileSystem(
        token="anon",  # noqa: S106 - no credential of its own; the proxy adds the session's
        consistency="md5",
        session_kwargs={"trust_env": True},
        skip_instance_cache=True,
        use_listings_cache=False,
    )


class Bucket:
    """Files landed as objects at `root`, the bucket's root, of the file system `fs`."""

    def __init__(self, fs: AbstractFileSystem, root: str) -> None:
        self.fs = fs
        self.root = root

    @classmethod
    def of_landing(cls, landing: str, env: Mapping[str, str]) -> "Bucket":
        root = bucket_name(landing, env)
        return cls(cloud_storage(), root)

    def __str__(self) -> str:
        return "the landing bucket"

    def _at(self, name: str) -> str:
        return f"{self.root}/{name}"

    def _call(self, doing: str, name: str, call: Any, *args: Any) -> Any:
        try:
            return call(*args)
        except Exception as error:
            raise BucketError(f"cannot {doing} {name} ({type(error).__name__})") from None

    def names(self) -> set[str]:
        """The names of the objects at the bucket's root, from one listing (a file system's
        `exists` on Cloud Storage costs a listing and a read for each name)."""
        listed = self._call("list", "the objects", self.fs.ls, self.root, False)
        return {str(path).rsplit("/", 1)[-1] for path in listed}

    def exists(self, name: str) -> bool:
        return name in self.names()

    def read(self, name: str) -> bytes:
        return self._call("read", name, self.fs.cat_file, self._at(name))

    def _copy(self, source: str, target: str) -> None:
        self._call(f"copy {source} to", target, self.fs.cp_file, self._at(source), self._at(target))

    def _remove(self, name: str) -> None:
        self._call("delete", name, self.fs.rm_file, self._at(name))

    def land_all(self, files: Mapping[str, bytes]) -> list[str]:
        """Land every file or none, by name, leaving what was landed before as it was."""
        hidden: list[str] = []
        aside: dict[str, str] = {}
        placed: list[str] = []
        try:
            partials = {name: f".{name}.new.{uuid.uuid4().hex}" for name in files}
            for name, data in files.items():
                hidden.append(partials[name])
                self._call(
                    "upload", partials[name], self.fs.pipe_file, self._at(partials[name]), data
                )
            landed = self.names()
            for name in files:
                if name in landed:
                    old = f".{name}.old.{uuid.uuid4().hex}"
                    hidden.append(old)
                    self._copy(name, old)
                    aside[name] = old
                    self._remove(name)
            for name, partial in partials.items():
                self._copy(partial, name)
                placed.append(name)
        except BucketError as error:
            try:
                for name in placed:
                    self._remove(name)
                for name, old in list(aside.items()):
                    self._copy(old, name)
                    del aside[name]
            except BucketError:
                kept = ", ".join(aside.values())
                hidden = [h for h in hidden if h not in aside.values()]
                raise BucketError(
                    f"{error}, nor put back what it replaced, kept as {kept}"
                ) from None
            raise
        finally:
            for leftover in hidden:
                with contextlib.suppress(BucketError):  # never uploaded, or left for the operator
                    self._remove(leftover)
        return list(files)
