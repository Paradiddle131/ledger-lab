"""The audit log: canonical form, digests, seals, the chain, the links, bound SQL and the writer.

A record (`agent/audit.schema.json`) is hashed over its canonical form, the JSON Canonicalization
Scheme of RFC 8785 (keys sorted by their UTF-16 code units, no whitespace, strings in UTF-8 with
the minimal escapes, numbers in their shortest ECMAScript form, integers only within 2**53), as the
`rfc8785` package writes it. Its `digest` is the SHA-256 of that form of every field but `digest`
itself, in lowercase hex, and its `prev` is the `digest` of the record before it.

What the chain shows: a record edited, removed or moved by someone who does not also rewrite every
record after it. What it does not: whoever can write the log can rewrite it from any record on, add
records at its end or cut its tail, and the result verifies; only a head digest kept outside the
writer's reach shows that. Keeping one writer at a time on the log is the writer's job.

A seal stands in the record for a value its readers may not see: the SHA-256 of the canonical form
of the pair `[salt, value]`. Each sealed field takes its own salt from `new_salt()`, kept with the
value outside the log; the one deliberate reuse is an approval's `seen`, sealed under its
proposal's draft salt so the two seals are equal exactly when the approver saw that draft, and a
purge deletes that salt under both positions. Without the salt a seal confirms no guess at the
value; with it, the value, or a result produced again by re-running a query on the same engine, is
checked against the seal.

The writer, `FileLog`, keeps the log as a file of one canonical record per line and the salt and
value of each seal in a separate folder, one file per record position. What `FileLog.append` does,
in this order, holding an exclusive `flock` on the log file:
- reads the log, refusing one whose last line is torn or whose records do not verify
  (`verify_chain`), and takes the next position and the last digest from it;
- adds `seq`, `prev` and `digest` to the record and checks it (`check_record`): it validates against
  `agent/audit.schema.json`, every time is a real instant, every run's SQL passes `check_bound`, the
  kept values are exactly one per sealed field of its kind and each opens its field's seal;
- refuses a folder of kept values whose mode (read without following a link) grants anything to
  group or others, or that is a link, and creates a missing one with owner-only permissions;
- writes the kept values to `<folder>/<seq>.json` through a temporary file: it removes a temporary
  file left at that name, creates it anew (`O_EXCL`, `O_NOFOLLOW`, mode 0600), writes, `fsync`s and
  renames it over the position's file, replacing one left by an append that failed after it, and
  `fsync`s the folder; when the write, the `fsync`, the rename or the folder's `fsync` raises, it
  removes the temporary file and the position's file, tries to `fsync` the folder and raises;
- appends the record's line with one `O_APPEND` file descriptor and `fsync`s it; when the write or
  the `fsync` raises, it truncates the log back to its length before the append, removes the
  position's file of kept values, tries to `fsync` the folder and raises.

Stated limits of the writer: the lock is advisory, so it keeps one writer at a time only among
writers that take it; the folder's `fsync` after a removal is tried and its failure not raised, so
the removal may not survive a crash; when a clean-up step itself raises (the truncation, its
`fsync` or a removal), its error is raised in place of the first and what it was cleaning can stay:
a whole line is then the record of a turn that showed no answer, a torn one makes every later
append refuse the log until it is repaired by hand, and a temporary or position's file of kept
values holds that turn's values in clear with no record naming it; a crash of the process between a
write and its clean-up leaves the same; the log is read whole on each append; the folder's mode is
checked, its parents' are not, and nothing here encrypts the kept values.
"""

import contextlib
import fcntl
import functools
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import rfc8785
import sqlglot
from jsonschema import Draft202012Validator
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.expressions.core import Expression

# The `prev` of the first record of the log.
GENESIS = "0" * 64
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# A salt is base64url of at least 128 bits; new_salt() gives 256.
SALT = re.compile(r"^[A-Za-z0-9_-]{22,}$")
TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$")
TIME_FIELDS = {"at", "landed_at"}
# The only numbers SQL text may carry in clear; any other value is bound as a parameter.
ALLOWED_NUMBERS = {"0", "1"}
DIALECTS = {"duckdb": "duckdb", "bigquery": "bigquery"}


class ChainBroken(ValueError):
    """A record that does not follow from the one before it, or whose body is not its digest's."""


class LinkBroken(ValueError):
    """A record whose reference to another record in the log does not hold."""


class UnboundValue(ValueError):
    """SQL text that carries a value in clear instead of binding it as a parameter."""


def canonical(value: Any) -> bytes:
    return rfc8785.dumps(value)


def load_record(line: str) -> dict[str, Any]:
    """A record from one line of the log, refusing a duplicate key or a non-finite number, which
    the parsed object would hide from its digest."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in items]
        if len(set(keys)) != len(keys):
            raise ChainBroken(f"a duplicate key in {sorted(keys)}")
        return dict(items)

    def constant(name: str) -> Any:
        raise ChainBroken(f"a non-finite number: {name}")

    record = json.loads(line, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(record, dict):
        raise ChainBroken("a log line that is not an object")
    return record


def record_digest(record: Mapping[str, Any]) -> str:
    """The SHA-256 of a record's canonical form without its own `digest` field."""
    body = {key: value for key, value in record.items() if key != "digest"}
    return hashlib.sha256(canonical(body)).hexdigest()


def new_salt() -> str:
    """A fresh salt for one sealed field: 256 bits from the operating system's secure source."""
    return secrets.token_urlsafe(32)


def seal(salt: str, value: Any) -> str:
    """The seal of a value under a salt: what the record carries in place of the value."""
    if not SALT.match(salt):
        raise ValueError("a salt is at least 22 base64url characters, 128 bits")
    return hashlib.sha256(canonical([salt, value])).hexdigest()


def sealed_result(columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> dict[str, Any]:
    """A result as it is sealed: its columns, and its rows sorted by their canonical form, so a
    re-run on the same engine matches whatever order the rows come back in."""
    return {"columns": list(columns), "rows": sorted((list(r) for r in rows), key=canonical)}


def instant(text: Any) -> datetime:
    """A time field as the instant it names, refusing one that is not a real UTC instant."""
    if not isinstance(text, str) or not TIME.match(text):
        raise ChainBroken(f"not an RFC 3339 UTC time: {text!r}")
    try:
        return datetime.fromisoformat(text).astimezone(UTC)
    except ValueError as error:
        raise ChainBroken(f"not a real instant: {text!r}") from error


def check_times(value: Any) -> None:
    """Parse every time field of a record, at any depth."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in TIME_FIELDS:
                instant(item)
            else:
                check_times(item)
    elif isinstance(value, list):
        for item in value:
            check_times(item)


def _header(record: Mapping[str, Any], seq: int) -> None:
    for field in ("seq", "prev", "digest", "kind", "at"):
        if field not in record:
            raise ChainBroken(f"record {seq}: no {field}")
    if type(record["seq"]) is not int:
        raise ChainBroken(f"record {seq}: its position is not an integer")
    for field in ("prev", "digest"):
        if not isinstance(record[field], str) or not SHA256.match(record[field]):
            raise ChainBroken(f"record {seq}: {field} is not a SHA-256 in lowercase hex")
    check_times(record)


def verify_chain(records: Iterable[Mapping[str, Any]], prev: str = GENESIS, seq: int = 0) -> str:
    """Check that each record follows the one before it and matches its own digest; return the
    last digest, the head a later segment starts from and an anchor outside the log would keep.

    A segment that does not start the log passes the digest and position of the record before it.
    """
    for record in records:
        _header(record, seq)
        if record["seq"] != seq:
            raise ChainBroken(f"record {record['seq']}: expected position {seq}")
        if record["prev"] != prev:
            raise ChainBroken(f"record {seq}: prev does not match the digest of the record before")
        if record["digest"] != record_digest(record):
            raise ChainBroken(f"record {seq}: the body does not match its digest")
        prev, seq = record["digest"], seq + 1
    return prev


def verify_links(records: Sequence[Mapping[str, Any]]) -> None:
    """Check each approval against the proposal it names, as a reader of the whole log alone can:
    the proposal is an earlier record of the log, of kind proposal, with that digest; the approver
    saw its draft, is not its requester and decided no earlier than it was drafted; and no
    proposal has two decisions. Pass the log from its first record, after verify_chain.

    That the approver is who they say is a signature's to show, which the approvals item adds.
    """
    decided: set[int] = set()
    for record in records:
        if record["kind"] != "approval":
            continue
        seq, named = record["seq"], record["proposal"]["seq"]
        if not 0 <= named < seq or named >= len(records):
            raise LinkBroken(f"record {seq}: names no earlier record of the log")
        proposal = records[named]
        if proposal["kind"] != "proposal":
            raise LinkBroken(f"record {seq}: names a record that is not a proposal")
        if proposal["digest"] != record["proposal"]["digest"]:
            raise LinkBroken(f"record {seq}: the proposal's digest does not match")
        if record["seen"] != proposal["draft"]:
            raise LinkBroken(f"record {seq}: the approver did not see the proposal's draft")
        if record["approver"] == proposal["requester"]["persona"]:
            raise LinkBroken(f"record {seq}: the requester decided their own proposal")
        if instant(record["at"]) < instant(proposal["at"]):
            raise LinkBroken(f"record {seq}: decided before the proposal was drafted")
        if named in decided:
            raise LinkBroken(f"record {seq}: a second decision on proposal {named}")
        decided.add(named)


# String forms other than a plain quoted literal, as sqlglot parses them, each with a test: on
# duckdb N'...', e'...' and $$...$$; on bigquery 0x..., b'...' and r'...'.
OTHER_STRINGS = (
    exp.National,
    exp.ByteString,
    exp.RawString,
    exp.HexString,
)


def check_bound(sql: str, engine: str) -> None:
    """Refuse SQL text that does not parse, or that carries in clear a plain string literal, a
    number but 0 and 1, or a string in one of the `OTHER_STRINGS` forms: the writer calls it before
    a query's text enters a record. Stated limit: comments are not read, so a value written in a
    comment passes."""
    try:
        trees = sqlglot.parse(sql, read=DIALECTS[engine])
    except ParseError as error:
        raise UnboundValue(f"the SQL does not parse: {error}") from error
    for tree in trees:
        if tree is None:
            continue
        for literal in tree.find_all(exp.Literal):
            if literal.is_string or literal.this not in ALLOWED_NUMBERS:
                raise UnboundValue(f"a value in clear: {literal.sql()}")
        for other in tree.find_all(*OTHER_STRINGS):
            raise UnboundValue(f"a string in clear: {other.sql()}")


def bind(sql: str, engine: str) -> tuple[str, dict[str, Any]]:
    """One SQL statement rewritten with its values as named parameters: each plain quoted string
    literal (`'...'`) and each integer but 0 and 1 becomes `$v<n>`, a value met twice the same
    parameter (so a grouped expression stays equal to its selected one), and the text is sqlglot's
    in the engine's dialect. Refuses a text that does not parse, is not one statement, carries a
    number that is not an integer or a string in one of the `OTHER_STRINGS` forms, and passes its
    result through `check_bound`, which reads no comment."""
    try:
        trees = [tree for tree in sqlglot.parse(sql, read=DIALECTS[engine]) if tree is not None]
    except ParseError as error:
        raise UnboundValue(f"the SQL does not parse: {error}") from error
    if len(trees) != 1:
        raise UnboundValue(f"not one statement but {len(trees)}")
    names: dict[tuple[type, Any], str] = {}

    def lift(node: Expression) -> Expression:
        if isinstance(node, OTHER_STRINGS):
            raise UnboundValue(f"a string form the tool does not bind: {node.sql()}")
        if not isinstance(node, exp.Literal):
            return node
        if node.is_string:
            value: Any = node.this
        elif node.this in ALLOWED_NUMBERS:
            return node
        elif re.fullmatch(r"[0-9]+", node.this):
            value = int(node.this)
        else:
            raise UnboundValue(f"a number the tool does not bind: {node.sql()}")
        name = names.setdefault((type(value), value), f"v{len(names) + 1}")
        return exp.Placeholder(this=name)

    text = trees[0].transform(lift).sql(dialect=DIALECTS[engine])
    check_bound(text, engine)
    return text, {name: value for (_, value), name in names.items()}


def cell_text(value: Any) -> str | None:
    """A result cell as it is sealed: null for null, else Python's `str` of the value the engine's
    driver returned, so a re-run through the same driver gives the same text."""
    return None if value is None else str(value)


def sealed_paths(record: Mapping[str, Any]) -> set[str]:
    """The paths of a record's sealed fields, as its kept values are keyed."""
    kind = record.get("kind")
    if kind == "answer":
        paths = {"question"} | ({"answer"} if record.get("outcome") != "failed" else set())
        for index, run in enumerate(record.get("runs", [])):
            paths.add(f"runs/{index}/result")
            if "parameters" in run.get("query", {}):
                paths.add(f"runs/{index}/query/parameters")
        return paths
    if kind == "proposal":
        return {"request", "inputs", "draft"}
    if kind == "approval":
        return {"seen"}
    raise ValueError("a record of no known kind")


def _at(record: Mapping[str, Any], path: str) -> Any:
    value: Any = record
    for step in path.split("/"):
        value = value[int(step)] if isinstance(value, list) else value[step]
    return value


@functools.cache
def _validator() -> Draft202012Validator:
    schema = json.loads(Path(__file__).with_name("audit.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


class RecordRefused(ValueError):
    """A record the writer will not write; its message names fields, never their values."""


def check_record(record: Mapping[str, Any], kept: Mapping[str, tuple[str, Any]]) -> None:
    """Refuse a record that breaks the schema, holds a time that is no real instant or SQL that
    fails `check_bound`, or whose kept values are not exactly one per sealed field, each opening
    its field's seal. The messages name the failing fields only, never a value."""
    failed = sorted("/".join(map(str, e.absolute_path)) for e in _validator().iter_errors(record))
    if failed:
        raise RecordRefused(f"the record breaks the schema at {failed}")
    try:
        check_times(record)
    except ChainBroken:
        raise RecordRefused("the record holds a time that is no real instant") from None
    for index, run in enumerate(record.get("runs", [])):
        try:
            check_bound(run["query"]["text"], run["engine"])
        except UnboundValue:
            raise RecordRefused(f"run {index}: its SQL carries a value in clear") from None
    paths = sealed_paths(record)
    if set(kept) != paths:
        raise RecordRefused(f"kept values for {sorted(kept)}, sealed fields {sorted(paths)}")
    for path in sorted(paths):
        salt, value = kept[path]
        if _at(record, path) != seal(salt, value):
            raise RecordRefused(f"the kept value of {path} does not open its seal")


class AuditLog(Protocol):
    def append(
        self, body: Mapping[str, Any], kept: Mapping[str, tuple[str, Any]]
    ) -> dict[str, Any]:
        """Write one record of `body`'s fields, adding `seq`, `prev` and `digest`, with `kept`, the
        salt and value of each of its seals by field path; return the record, or raise."""
        ...

    def records(self) -> list[dict[str, Any]]:
        """The log's records in order, its chain verified; raise when it cannot be read."""
        ...


def _read_all(fd: int, size: int) -> bytes:
    chunks, offset = [], 0
    while offset < size:
        chunk = os.pread(fd, size - offset, offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def read_log(data: bytes) -> list[dict[str, Any]]:
    """The records of a log file's bytes, refusing a torn last line; the chain is not checked."""
    if not data:
        return []
    if not data.endswith(b"\n"):
        raise ChainBroken("the log ends in a torn line")
    return [load_record(line) for line in data.decode("utf-8").split("\n")[:-1]]


class FileLog:
    """The log as a file of canonical lines at `path`, and the salt and value of each seal in
    `kept`, one file per record position; see the module's docstring for the order of an append."""

    def __init__(self, path: Path, kept: Path) -> None:
        self.path = path
        self.kept = kept

    def append(
        self, body: Mapping[str, Any], kept: Mapping[str, tuple[str, Any]]
    ) -> dict[str, Any]:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            size = os.fstat(fd).st_size
            records = read_log(_read_all(fd, size))
            prev = verify_chain(records)
            record = {key: value for key, value in body.items() if key not in ("seq", "prev")}
            record |= {"seq": len(records), "prev": prev}
            record.pop("digest", None)
            record["digest"] = record_digest(record)
            check_record(record, kept)
            self._keep(record["seq"], kept)
            # From here the position's kept values are on disk: any step that raises before the
            # line is durable truncates the log back and removes them.
            try:
                _write_all(fd, canonical(record) + b"\n")
                os.fsync(fd)
            except BaseException:
                try:
                    os.ftruncate(fd, size)
                    os.fsync(fd)
                finally:
                    self._forget(record["seq"])
                raise
            return record
        finally:
            os.close(fd)

    def records(self) -> list[dict[str, Any]]:
        """The log's records under a shared `flock`, refusing a torn last line or a chain that does
        not verify; a log not yet written holds none."""
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return []
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            records = read_log(_read_all(fd, os.fstat(fd).st_size))
            verify_chain(records)
            return records
        finally:
            os.close(fd)

    def _folder(self) -> None:
        """Create the folder of kept values owner-only, or refuse one that is a link or whose mode
        grants anything to group or others."""
        with contextlib.suppress(FileExistsError):
            self.kept.mkdir(mode=0o700, parents=True)
        mode = os.lstat(self.kept).st_mode
        if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) & 0o077:
            raise KeptFolderRefused("the folder of kept values is a link or open to others")

    def _sync_folder(self) -> None:
        folder = os.open(self.kept, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)

    def _keep(self, seq: int, kept: Mapping[str, tuple[str, Any]]) -> None:
        self._folder()
        final = self.kept / f"{seq}.json"
        temporary = self.kept / f".{seq}.json.tmp"
        data = canonical({path: [salt, value] for path, (salt, value) in kept.items()})
        temporary.unlink(missing_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(temporary, flags, 0o600)
        try:
            try:
                _write_all(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, final)
            self._sync_folder()
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            finally:
                self._forget(seq)
            raise

    def _forget(self, seq: int) -> None:
        """Remove the kept values of a position whose record was not written, then try to `fsync`
        the folder; a failure of that `fsync` is not raised, so the first error is."""
        (self.kept / f"{seq}.json").unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            self._sync_folder()


class KeptFolderRefused(PermissionError):
    """A folder of kept values that is a link or grants anything to group or others."""


class AuditWriteFailed(RuntimeError):
    """A turn's audit record could not be written, so the turn shows no answer. Its message names
    the kind of failure only, and it carries no cause, so no value of the turn travels with it."""
