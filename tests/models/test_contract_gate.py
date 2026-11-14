"""The contract gate's one way through for a deliberate break: a new major version of the contract
whose `breakingChanges` custom property lists exactly the breaks the gate finds against
`origin/main`, each with a reason, and nothing else; and the breaks no version can declare.

Every contract here is the committed one changed inside the test, in a throwaway clone whose
`origin/main` the test sets.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from models.contracts import gate
from tests.models.test_payments_contract import (
    CONTRACT,
    HASHED,
    Contract,
    changed,
    committed,
    drop,
    drop_rule,
    git_repo,
    on_rule,
    properties,
    setting,
    unsetting,
    with_files,
)

PATH = str(gate.CONTRACT)
SPLIT = "payee-columns-split-the-published-name"
REMOVED_NAME = "schema.payments.properties.vendor_name: Removed property vendor_name"
Change = Callable[[Contract], None]


def remove_the_payee_name(c: Contract) -> None:
    """The published payee name gone, with the two rules that read it."""
    drop("vendor_name")(c)
    drop_rule(HASHED)(c)
    drop_rule(SPLIT)(c)


def bigquery_types(c: Contract) -> None:
    """Every column's storage type as BigQuery names it."""
    names = {"VARCHAR": "STRING", "DECIMAL(28,6)": "NUMERIC", "BIGINT": "INT64", "BOOLEAN": "BOOL"}
    for p in properties(c):
        p["physicalType"] = names.get(p["physicalType"], p["physicalType"])


def add_trivial_rule(c: Contract) -> None:
    (schema,) = c["schema"]
    schema["quality"].append({"id": "trivial", "type": "sql", "query": "select 0", "mustBe": 0})


def both(*changes: Change) -> Change:
    def change(c: Contract) -> None:
        for each in changes:
            each(c)

    return change


def found(change: Change) -> list[str]:
    """The declarable breaks the gate finds in `change` made to the committed contract."""
    after = committed()
    change(after)
    declarable, refused = gate.breaks(committed(), after)
    assert not refused, refused
    return declarable


def declaring(version: str, entries: object) -> Change:
    """The contract at `version`, its `breakingChanges` holding `entries`."""

    def change(c: Contract) -> None:
        c["version"] = version
        c["customProperties"] = [{"property": gate.DECLARED, "value": entries}]

    return change


def listed(lines: list[str], reason: str = "The split columns hold the same fact.") -> list[object]:
    return [{"change": line, "reason": reason} for line in lines]


@pytest.fixture(scope="module")
def published_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A clone whose `origin/main` holds the committed contract, at version 1.0.0."""
    assert committed()["version"] == "1.0.0"
    return git_repo(tmp_path_factory.mktemp("published"), {PATH: CONTRACT.read_text()})


def gate_on(repo: Path, change: Change, path: str = PATH) -> tuple[bool, str]:
    return gate.check(with_files(repo, {path: changed(change)}))


def test_removing_the_payee_name_breaks_these_and_fails_without_a_new_major_version(
    published_repo: Path,
) -> None:
    breaks = found(remove_the_payee_name)
    assert breaks == [
        REMOVED_NAME,
        "schema.payments.quality.[1]: Removed quality rule at schema.payments.quality.[1]",
        "schema.payments.quality.[5]: Removed quality rule at schema.payments.quality.[5]",
        f"quality rule payments/{HASHED} is gone (removed, moved or renamed)",
        f"quality rule payments/{SPLIT} is gone (removed, moved or renamed)",
    ]
    ok, report = gate_on(
        published_repo, both(remove_the_payee_name, lambda c: c.update(version="2.0.0"))
    )
    assert not ok
    assert all(f"payments: {line} (not listed in {gate.DECLARED})" in report for line in breaks)


PASSING = {
    "the payee name removed": remove_the_payee_name,
    "every storage type changed for BigQuery": bigquery_types,
    "a rule's query changed": on_rule(HASHED, "query", "select 0"),
}


@pytest.mark.parametrize("name", list(PASSING))
@pytest.mark.parametrize("version", ["2.0.0", "3.1.4"])
def test_a_new_major_version_listing_exactly_its_breaks_passes(
    published_repo: Path, name: str, version: str
) -> None:
    change = PASSING[name]
    ok, report = gate_on(published_repo, both(change, declaring(version, listed(found(change)))))
    assert ok, report


def test_a_new_major_version_with_no_break_and_no_list_passes(published_repo: Path) -> None:
    ok, report = gate_on(published_repo, lambda c: c.update(version="2.0.0"))
    assert ok, report


def entries_without(line: str, breaks: list[str]) -> list[object]:
    return listed([b for b in breaks if b != line])


BREAKS = found(remove_the_payee_name)
FAILING: dict[str, tuple[Change, str]] = {
    "a break not listed": (
        declaring("2.0.0", entries_without(REMOVED_NAME, BREAKS)),
        f"payments: {REMOVED_NAME} (not listed in {gate.DECLARED})",
    ),
    "a listed break that did not happen": (
        declaring(
            "2.0.0", listed([*BREAKS, "schema.payments.properties.amount: Removed property amount"])
        ),
        "lists a break that did not happen: schema.payments.properties.amount",
    ),
    "a reason missing": (
        declaring("2.0.0", [*listed(BREAKS[1:]), {"change": REMOVED_NAME}]),
        f"lists a break without a reason: {REMOVED_NAME}",
    ),
    "a blank reason": (
        declaring("2.0.0", [*listed(BREAKS[1:]), {"change": REMOVED_NAME, "reason": "  "}]),
        f"lists a break without a reason: {REMOVED_NAME}",
    ),
    "a break listed at a path the gate does not report": (
        declaring(
            "2.0.0",
            [*listed(BREAKS[1:]), *listed(["payments.vendor_name: Removed property vendor_name"])],
        ),
        f"payments: {REMOVED_NAME} (not listed in {gate.DECLARED})",
    ),
    "a catch-all entry": (
        declaring("2.0.0", listed(["*"])),
        "lists a break that did not happen: *",
    ),
    "a break listed twice": (
        declaring("2.0.0", listed([*BREAKS, REMOVED_NAME])),
        f"lists a break twice: {REMOVED_NAME}",
    ),
    "a list that is not a list": (
        declaring("2.0.0", "everything the split needs"),
        f"{gate.DECLARED} must be a list",
    ),
    "an entry with more than a change and a reason": (
        declaring(
            "2.0.0", [*listed(BREAKS[1:]), {"change": REMOVED_NAME, "reason": "r", "scope": "*"}]
        ),
        f"{gate.DECLARED} entry 5 must hold a change and a reason",
    ),
    "the list given twice": (
        lambda c: c.update(
            version="2.0.0",
            customProperties=[{"property": gate.DECLARED, "value": listed(BREAKS)}] * 2,
        ),
        f"customProperties holds {gate.DECLARED} more than once",
    ),
    "a minor version raised": (
        declaring("1.1.0", listed(BREAKS)),
        "version '1.1.0' is not a major version above '1.0.0'",
    ),
    "the version unchanged": (
        declaring("1.0.0", listed(BREAKS)),
        "version '1.0.0' is not a major version above '1.0.0'",
    ),
    "the major version lowered": (
        declaring("0.9.0", listed(BREAKS)),
        "version '0.9.0' is not a major version above '1.0.0'",
    ),
    "a version that is not MAJOR.MINOR.PATCH": (
        declaring("2.0", listed(BREAKS)),
        "version '2.0' is not a major version above '1.0.0'",
    ),
    "a version with a prefix": (
        declaring("v2.0.0", listed(BREAKS)),
        "version 'v2.0.0' is not a major version above '1.0.0'",
    ),
}


@pytest.mark.parametrize("name", list(FAILING))
def test_a_break_fails_unless_a_new_major_version_lists_exactly_it_with_a_reason(
    published_repo: Path, name: str
) -> None:
    declare, says = FAILING[name]
    ok, report = gate_on(published_repo, both(remove_the_payee_name, declare))
    assert not ok, report
    assert says in report


def test_a_break_under_the_same_major_version_fails_as_before(published_repo: Path) -> None:
    ok, report = gate_on(
        published_repo, both(remove_the_payee_name, lambda c: c.update(version="1.1.0"))
    )
    assert not ok
    assert f"payments: {REMOVED_NAME}\n" in report
    assert "no break can be declared" in report


def test_a_moved_contract_keeps_its_list_and_its_limits(published_repo: Path) -> None:
    moved = "contracts/payments.odcs.yaml"
    declared = both(remove_the_payee_name, declaring("2.0.0", listed(BREAKS)))
    ok, report = gate_on(published_repo, declared, moved)
    assert ok, report
    ok, report = gate_on(published_repo, both(declared, drop("amount")), moved)
    assert not ok
    assert "Removed property amount (not listed" in report


def at_two(change: Change, lines: list[str]) -> str:
    """The committed contract changed, at 2.0.0, listing `lines`."""
    return changed(both(change, declaring("2.0.0", listed(lines))))


# What no version can declare: each listed under a new major version as the gate would report it
# were it declarable, so the refusal alone fails it.
DECLASSIFIED = "payments.vendor_name is no longer classified personal"
RECLASSIFIED = "payments.document_number is no longer classified personal"
GONE = f"contract payments ({PATH} on origin/main) is gone or has a new id"
REFUSED: dict[str, tuple[dict[str, str], str]] = {
    "a personal column declassified": (
        {PATH: at_two(unsetting("vendor_name", "classification"), [DECLASSIFIED])},
        f"payments: {DECLASSIFIED}, which no version can declare",
    ),
    "a personal column reclassified": (
        {PATH: at_two(setting("document_number", "classification", "restricted"), [RECLASSIFIED])},
        f"payments: {RECLASSIFIED}, which no version can declare",
    ),
    "the contract's id changed": (
        {PATH: at_two(lambda c: c.update(id="payments-v2"), [GONE])},
        GONE,
    ),
    "the contract retired": ({}, GONE),
    "the unchanged contract beside a broken copy with its id at models/contracts/a.odcs.yaml": (
        {PATH: CONTRACT.read_text(), "models/contracts/a.odcs.yaml": changed(drop("amount"))},
        "contract payments is in both ",
    ),
    "the unchanged contract beside a broken copy with its id at models/contracts/zz.odcs.yaml": (
        {PATH: CONTRACT.read_text(), "models/contracts/zz.odcs.yaml": changed(drop("amount"))},
        "contract payments is in both ",
    ),
}


@pytest.mark.parametrize("name", list(REFUSED))
def test_no_version_declares_a_lowered_classification_or_a_contract_gone_or_doubled(
    published_repo: Path, name: str
) -> None:
    files, says = REFUSED[name]
    ok, report = gate.check(with_files(published_repo, files))
    assert not ok, report
    assert says in report


def test_no_version_declares_an_unreadable_contract(published_repo: Path) -> None:
    text = at_two(remove_the_payee_name, BREAKS) + "schema: [\n"
    ok, report = gate.check(with_files(published_repo, {PATH: text}))
    assert not ok
    assert f"{gate.CONTRACT} is unreadable" in report


@pytest.fixture(scope="module")
def merged_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A clone whose `origin/main` holds the payee name's removal, merged as 2.0.0 with its list."""
    merged = changed(both(remove_the_payee_name, declaring("2.0.0", listed(BREAKS))))
    return git_repo(tmp_path_factory.mktemp("merged"), {PATH: merged})


def on_merged(version: str, *changes: Change, entries: list[object] | None = None) -> Change:
    """The merged contract, changed, at `version`, its list the merged one unless `entries`."""
    return both(
        remove_the_payee_name,
        *changes,
        declaring(version, listed(BREAKS) if entries is None else entries),
    )


AFTER_MERGE: dict[str, tuple[Change, bool]] = {
    "unchanged": (on_merged("2.0.0"), True),
    "a minor version without a break, the list kept": (on_merged("2.1.0", add_trivial_rule), True),
    "a new break under the merged major version, the list kept": (
        on_merged("2.1.0", drop("amount")),
        False,
    ),
    "a new break under the merged major version, the list naming it": (
        on_merged("2.1.0", drop("amount"), entries=listed([*BREAKS, *found(drop("amount"))])),
        False,
    ),
    "a new major version keeping only the merged list": (on_merged("3.0.0", drop("amount")), False),
    "a new major version listing the merged and the new breaks": (
        on_merged("3.0.0", drop("amount"), entries=listed([*BREAKS, *found(drop("amount"))])),
        False,
    ),
    "a new major version listing only its own break": (
        on_merged("3.0.0", drop("amount"), entries=listed(found(drop("amount")))),
        True,
    ),
}


@pytest.mark.parametrize("name", list(AFTER_MERGE))
def test_a_merged_major_versions_list_excuses_no_later_break(merged_repo: Path, name: str) -> None:
    change, passes = AFTER_MERGE[name]
    ok, report = gate.check(with_files(merged_repo, {PATH: changed(change)}))
    assert ok == passes, report
