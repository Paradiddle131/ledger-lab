"""A landing in the lab's Cloud Storage bucket: `LEDGER_LAB_LANDING=gs://<lab project>-landing`.

The fetch kit writes there exactly the bytes it would write to a local landing, after the same
verification and the same step in memory, as objects at the bucket's root named as the local files
are (`chicago-contracts`), so no name holds a slash. The bucket must be the lab's own, named from
`LEDGER_LAB_GCP_PROJECT` through `platform/gcp_project.py` as `platform/gcp/landing.tf` names it;
any other bucket, a folder inside it, or another scheme is refused before any call.

One landing at a time: a landing first creates the lock object `.landing.lock` at the bucket's
root, only if no object of that name exists (a generation-match-0 precondition), and deletes it at
the end; a landing that finds it refuses, naming the lock and how to remove it. A killed landing,
or one whose rollback failed, leaves the lock behind on purpose: nothing lands until an operator
has checked the landed files and deleted it by hand.

Object storage has no rename, so a set lands in the order a local landing renames, with copies:
every new file is uploaded under a hidden temporary name (`.<name>.new.<random>`) first; then each
object already landed under a file's name is copied aside to a hidden name (`.<name>.old.<random>`)
and deleted; then each temporary object is copied to its file's name. A failure or an interrupt
(Ctrl-C included) on the way deletes every name of the set that held no object before, and those
whose old object it moved aside, and copies the old objects back, so the bucket holds every new
file or every old one, even when a copy succeeded but its answer was lost. An old object's hidden
copy is deleted only after the new set is placed or the old one put back: if copying back fails,
the old objects stay under their hidden names, which the error names. A copy within the bucket
replaces an object whole, so a reader never sees half of one.

A process killed mid-way leaves the lock, hidden objects and at worst emptied names, never an old
object beside a new one. Once the lock is deleted, the next landing of those names deletes their
stale `.new.` uploads, which are never the only copy of a landed file, and keeps every `.old.` copy,
which may be: it reports them (`left_behind`) for an operator to delete by hand.

The credential is the one the session's network proxy adds to every request to Cloud Storage, as
for `make cost-check`; the client sends none of its own.
"""

import contextlib
import importlib.util
import logging
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fsspec import AbstractFileSystem

SCHEME = "gs://"
LOCK = ".landing.lock"
PROJECT_SEAM = Path(__file__).resolve().parent.parent / "platform" / "gcp_project.py"
HELD = (
    f"another landing holds the lock object {LOCK} at the landing bucket's root, or a killed one"
    " left it: once no landing runs and the landed files are checked, delete it by hand"
    f" (`gcloud storage rm gs://<lab project>-landing/{LOCK}`)"
)


class BucketError(Exception):
    """The landing bucket refused a write or a read, or is not the lab's; the message names
    objects by their names in the bucket and never the bucket, the project or an API's words."""


def is_bucket(landing: str) -> bool:
    """A `gs://` landing; any other URL is refused, never taken for a local folder."""
    if landing.startswith(SCHEME):
        return True
    if "://" in landing:
        raise BucketError(f"a landing is a local folder or {SCHEME}<lab project>-landing")
    return False


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
    name = landing.removeprefix(SCHEME).removesuffix("/")
    if "/" in name:
        raise BucketError("a bucket landing is the bucket's root, never a folder inside it")
    if name != f"{lab_project(env)}-landing":
        raise BucketError(
            "a bucket landing must be the lab's landing bucket, gs://<project>-landing"
        )
    return name


def cloud_storage() -> AbstractFileSystem:
    """gcsfs's plain client: JSON API calls over aiohttp through the session's proxy, with every
    upload's MD5 checked against the one Cloud Storage computes. The plain class, not the package's
    default, which first asks a gRPC API whether a bucket is zonal. Its logger is silenced: on a
    refused call it logs Google's own words, which name the bucket and the account, and Python's
    last-resort handler would print them."""
    from gcsfs.core import GCSFileSystem

    log = logging.getLogger("gcsfs")
    log.addHandler(logging.NullHandler())
    log.propagate = False
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
        # The `.old.` copies of an interrupted landing that the last landing found and kept.
        self.left_behind: list[str] = []

    @classmethod
    def of_landing(cls, landing: str, env: Mapping[str, str]) -> "Bucket":
        root = bucket_name(landing, env)
        return cls(cloud_storage(), root)

    def __str__(self) -> str:
        return "the landing bucket"

    def _at(self, name: str) -> str:
        return f"{self.root}/{name}"

    def _call(self, doing: str, name: str, call: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return call(*args, **kwargs)
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

    def _discard(self, name: str) -> None:
        """Delete the object if it is there."""
        try:
            self.fs.rm_file(self._at(name))
        except FileNotFoundError:
            pass
        except Exception as error:
            raise BucketError(f"cannot delete {name} ({type(error).__name__})") from None

    def _lock(self) -> None:
        try:
            self.fs.pipe_file(self._at(LOCK), b"", mode="create")
        except FileExistsError:
            raise BucketError(HELD) from None
        except Exception as error:
            raise BucketError(f"cannot create the lock {LOCK} ({type(error).__name__})") from None

    def land_all(self, files: Mapping[str, bytes]) -> list[str]:
        """Land every file or none, by name, leaving what was landed before as it was."""
        self._lock()
        self.left_behind = []
        partials: dict[str, str] = {}
        aside: dict[str, str] = {}
        existed: set[str] = set()  # the names that held an object
        touched = False  # whether any name may have changed
        done = False  # every new file placed, or every old one put back
        try:
            listed = self.names()
            for name in files:
                for hidden in sorted(listed):
                    if hidden.startswith(f".{name}.new."):
                        self._discard(hidden)  # an interrupted landing's upload, never placed
                    elif hidden.startswith(f".{name}.old."):
                        self.left_behind.append(hidden)
            for name, data in files.items():
                partials[name] = f".{name}.new.{uuid.uuid4().hex}"
                self._call(
                    "upload", partials[name], self.fs.pipe_file, self._at(partials[name]), data
                )
            existed = {name for name in files if name in listed}
            touched = True
            for name in files:
                if name in existed:
                    old = f".{name}.old.{uuid.uuid4().hex}"
                    self._copy(name, old)
                    aside[name] = old
                    self._remove(name)
            for name, partial in partials.items():
                self._copy(partial, name)
            done = True
        except BaseException as error:
            restored: list[str] = []
            try:
                for name in files if touched else ():
                    if name not in existed or name in aside:  # what is there now is new
                        self._discard(name)
                for name, old in aside.items():
                    self._copy(old, name)
                    restored.append(name)
                done = True
            except BucketError:
                why = str(error) if isinstance(error, BucketError) else type(error).__name__
                kept = ", ".join(old for name, old in aside.items() if name not in restored)
                raise BucketError(
                    f"{why}, nor put back what it replaced, kept as {kept}; the lock {LOCK} stays"
                    " until they are checked"
                ) from None
            raise
        finally:
            for partial in partials.values():
                with contextlib.suppress(BucketError):
                    self._discard(partial)
            if done:
                # An old object's copy goes only now: until here it may be the only copy.
                for old in aside.values():
                    self._discard(old)
                self._remove(LOCK)
        return list(files)
