"""The audit log's checks: canonical form, digests, seals, the chain, the links and bound SQL.

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
"""

import hashlib
import json
import re
import secrets
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import rfc8785
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

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


def check_bound(sql: str, engine: str) -> None:
    """Refuse SQL text that carries a string literal, or a number but 0 and 1, in clear: the
    writer calls it before a query's text enters a record, so every other value is a parameter."""
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
