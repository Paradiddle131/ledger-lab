"""The audit log's tamper evidence: canonical form, a record's digest, a seal, the chain check.

A record (`agent/audit.schema.json`) is hashed over its canonical form, the JSON Canonicalization
Scheme of RFC 8785 (keys sorted by their UTF-16 code units, no whitespace, strings in UTF-8 with
the minimal escapes, numbers in their shortest ECMAScript form, integers only within 2**53), as the
`rfc8785` package writes it. Its `digest` is the SHA-256 of that form of every field but `digest`
itself, in lowercase hex, and its `prev` is the `digest` of the record before it, so changing or
removing a record inside the log breaks the chain at the next one.

A seal stands in the record for a value its readers may not see: the SHA-256 of the canonical form
of the pair `[salt, value]`, where the salt is random, at least 128 bits, and kept with the value
outside the log. Without the salt the seal confirms no guess at the value; with it, the value, or
the same value produced again by re-running a query, is checked against the seal.
"""

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

import rfc8785

# The `prev` of the first record of the log.
GENESIS = "0" * 64
# 128 bits as base64url, the shortest salt a seal takes.
SHORTEST_SALT = 22


class ChainBroken(ValueError):
    """A record that does not follow from the one before it, or whose body is not its digest's."""


def canonical(value: Any) -> bytes:
    return rfc8785.dumps(value)


def record_digest(record: Mapping[str, Any]) -> str:
    """The SHA-256 of a record's canonical form without its own `digest` field."""
    body = {key: value for key, value in record.items() if key != "digest"}
    return hashlib.sha256(canonical(body)).hexdigest()


def seal(salt: str, value: Any) -> str:
    """The seal of a value under a salt: what the record carries in place of the value."""
    if len(salt) < SHORTEST_SALT:
        raise ValueError(f"a salt has at least {SHORTEST_SALT} characters, 128 bits as base64url")
    return hashlib.sha256(canonical([salt, value])).hexdigest()


def verify_chain(records: Iterable[Mapping[str, Any]], prev: str = GENESIS, seq: int = 0) -> str:
    """Check that each record follows the one before it and matches its own digest; return the
    last digest, the head a later segment starts from or an anchor records.

    A segment that does not start the log passes the digest and position of the record before it.
    """
    for record in records:
        if record["seq"] != seq:
            raise ChainBroken(f"record {record['seq']}: expected position {seq}")
        if record["prev"] != prev:
            raise ChainBroken(f"record {seq}: prev does not match the digest of the record before")
        if record["digest"] != record_digest(record):
            raise ChainBroken(f"record {seq}: the body does not match its digest")
        prev, seq = record["digest"], seq + 1
    return prev
