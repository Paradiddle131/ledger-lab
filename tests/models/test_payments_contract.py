"""The payments data product's contract (`models/contracts/payments.odcs.yaml`): valid ODCS, kept
by the sample build and the real landing's build, in step with the dbt docs and the built mart, and
guarded by the gate that fails `make check` on a change that breaks a consumer.

`lint` and the gate run the Data Contract CLI in this environment. `datacontract test` needs the
CLI's DuckDB engine, which needs DuckDB 1.5: it runs from uv's cache in a throwaway environment over
a copy of the build in 1.5's storage format, and its tests skip, saying so, when that environment
is not cached. Every changed contract and every broken database here is made inside the test.
"""

import copy
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from datacontract.data_contract import DataContract

from ingest.entities import Register
from ingest.fetch import landing_path
from ingest.individuals import identifier_hasher, is_organisation, person_hasher
from ingest.uk_transport.spend import BODIES
from models.contracts import engine, gate
from tests.models.dbt_build import PROJECT, Build, built, query

CONTRACT = gate.REPO / gate.CONTRACT
MART_YAML = PROJECT / "models" / "marts" / "payments" / "_payments__models.yml"
Contract = dict[str, Any]
# The dbt side of the mart: the manifest, the model's YAML and the built columns with their types.
DbtSide = tuple[dict[str, Any], dict[str, Any], list[tuple[str, str]]]
# Presence and type of all 17 columns, 8 required, 1 unique, 2 accepted-values and 2 bound checks,
# and the 7 quality rules.
CHECKS = 2 * 17 + 8 + 1 + 2 + 2 + 7


def committed() -> Contract:
    return yaml.safe_load(CONTRACT.read_text())


def properties(contract: Contract) -> list[dict[str, Any]]:
    (schema,) = contract["schema"]
    return schema["properties"]


def column(contract: Contract, name: str) -> dict[str, Any]:
    return next(p for p in properties(contract) if p["name"] == name)


def rule(contract: Contract, rule_id: str) -> dict[str, Any]:
    (schema,) = contract["schema"]
    return next(q for q in schema["quality"] if q["id"] == rule_id)


@pytest.fixture(scope="module")
def engine_env() -> None:
    reason = engine.unavailable()
    if reason:
        pytest.skip(reason)


def contract_test(database: Path, directory: Path) -> dict[str, str]:
    """`datacontract test` over a dbt build: each check's result, by its quality rule's id in the
    contract for a rule, else by the CLI's key for the check."""
    results = directory / "contract-results.json"
    copied = engine.for_the_cli(database, directory / "for_the_cli.duckdb")
    run = engine.in_engine_env(
        "datacontract",
        "test",
        str(CONTRACT),
        "--output-format",
        "json",
        "--output",
        str(results),
        env=dict(os.environ) | {"DATACONTRACT_DUCKDB_DATABASE": str(copied)},
    )
    assert results.is_file(), run.stdout + run.stderr
    checks = {
        c["qualityId"] or c["key"]: c["result"] for c in json.loads(results.read_text())["checks"]
    }
    assert (run.returncode == 0) == all(r == "passed" for r in checks.values()), run.stdout
    return checks


# The library call the CLI's `test` command makes, for many databases in one process of the
# engine's environment, since every start of the command costs seconds.
BATCH = """
import json, os, sys
from datacontract.data_contract import DataContract
job = json.load(sys.stdin)
out = {}
for name, database in job["databases"].items():
    os.environ["DATACONTRACT_DUCKDB_DATABASE"] = database
    run = DataContract(data_contract_file=job["contract"]).test()
    out[name] = {c.qualityId or c.key: c.result.value for c in run.checks}
print(json.dumps(out))
"""


def test_the_contract_is_valid_odcs() -> None:
    run = DataContract(data_contract_file=str(CONTRACT), inline_references=False).lint()
    assert run.has_passed(), run.pretty()
    assert any("ODCS v3.2.0" in (c.name or "") for c in run.checks or [])


def test_every_quality_rule_has_an_id_the_gate_can_match() -> None:
    assert all(key.split("/")[1] and "(no id)" not in key for key in gate.rules(committed()))


@pytest.mark.usefixtures("engine_env")
def test_the_sample_build_keeps_the_contract(build: Build, tmp_path: Path) -> None:
    checks = contract_test(build.database, tmp_path)
    assert set(checks.values()) == {"passed"}
    (schema,) = committed()["schema"]
    assert {q["id"] for q in schema["quality"]} <= set(checks)
    assert len(checks) == CHECKS


# One break of the sample build per rule, and the checks it fails: each rule fails on its own break.
BREAKS = {
    "a key served twice": (
        "insert into payments select * from payments where entity_id = 'chicago' limit 1",
        {"payments__payment_key__field_unique", "chicago-row-count-equals-its-model"},
    ),
    "a payment in another entity's currency": (
        "update payments set currency = 'EUR' where payment_key ="
        " (select min(payment_key) from payments where entity_id = 'uk-transport')",
        {"currency-is-the-entitys-own"},
    ),
    "a hashed name flagged clear": (
        "update payments set vendor_is_hashed = false where payment_key ="
        " (select min(payment_key) from payments where vendor_is_hashed)",
        # The split columns follow the flag, so a wrong flag breaks them too.
        {
            "hashed-exactly-when-the-name-is-a-hash",
            "payee-columns-split-the-published-name",
            "document-reference-is-a-chicago-voucher-no-hashed-payee-shares",
        },
    ),
    "a clear name flagged hashed": (
        "update payments set vendor_is_hashed = true where payment_key ="
        " (select min(payment_key) from payments where not vendor_is_hashed)",
        # The split columns follow the flag, so a wrong flag breaks them too.
        {
            "hashed-exactly-when-the-name-is-a-hash",
            "payee-columns-split-the-published-name",
            "document-reference-is-a-chicago-voucher-no-hashed-payee-shares",
        },
    ),
    "a UK payment lost": (
        "delete from payments where payment_key ="
        " (select min(payment_key) from payments where entity_id = 'uk-transport')",
        {"uk-transport-row-count-equals-its-model"},
    ),
    "a period outside the year": (
        "update payments set fiscal_period = 13 where payment_key ="
        " (select min(payment_key) from payments where fiscal_period is not null)",
        {"payments__fiscal_period__field_maximum"},
    ),
    "an unknown entity and currency": (
        "update payments set entity_id = 'other', currency = 'XXX' where payment_key ="
        " (select min(payment_key) from payments where entity_id = 'chicago')",
        {
            "payments__entity_id__field_enum",
            "payments__currency__field_enum",
            "currency-is-the-entitys-own",
            "chicago-row-count-equals-its-model",
            # A reference is shown only on Chicago rows: a row leaving Chicago keeps one wrongly.
            "document-reference-is-a-chicago-voucher-no-hashed-payee-shares",
        },
    ),
    "a clear name copied into the person key": (
        "update payments set vendor_person_key = vendor_name where payment_key ="
        " (select min(payment_key) from payments where not vendor_is_hashed)",
        {"one-of-organisation-name-and-person-key", "payee-columns-split-the-published-name"},
    ),
    "a person key that is not the published hash": (
        "update payments set vendor_person_key = 'person:' || repeat('0', 64) where payment_key ="
        " (select min(payment_key) from payments where vendor_is_hashed)",
        {"payee-columns-split-the-published-name"},
    ),
    "a hashed payee's voucher number in the clear reference": (
        "update payments set document_reference = document_number where payment_key ="
        " (select min(payment_key) from payments"
        " where vendor_is_hashed and entity_id = 'chicago' and document_number is not null)",
        {"document-reference-is-a-chicago-voucher-no-hashed-payee-shares"},
    ),
    "a UK transaction number in the clear reference": (
        "update payments set document_reference = document_number where payment_key ="
        " (select min(payment_key) from payments where entity_id = 'uk-transport'"
        " and not vendor_is_hashed and document_number not like 'id:%')",
        {"document-reference-is-a-chicago-voucher-no-hashed-payee-shares"},
    ),
    "a placeholder in the clear reference": (
        "update payments set document_reference = document_number where payment_key ="
        " (select min(payment_key) from payments where document_number = 'Multiple'"
        " and not vendor_is_hashed)",
        {"document-reference-is-a-chicago-voucher-no-hashed-payee-shares"},
    ),
    "a missing amount": (
        "update payments set amount = null"
        " where payment_key = (select min(payment_key) from payments)",
        {"payments__amount__field_required"},
    ),
}


@pytest.fixture(scope="module")
def broken(
    engine_env: None, build: Build, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, dict[str, str]]:
    """The contract test's results on one copy of the sample build per break."""
    databases = {}
    for name, (sql, _) in BREAKS.items():
        directory = tmp_path_factory.mktemp("broken")
        database = engine.for_the_cli(build.database, directory / "for_the_cli.duckdb")
        with duckdb.connect(str(database)) as db:
            db.execute(sql)
        databases[name] = str(database)
    job = {"databases": databases, "contract": str(CONTRACT)}
    run = engine.in_engine_env("python", "-c", BATCH, stdin=json.dumps(job))
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.splitlines()[-1])


@pytest.mark.parametrize("name", list(BREAKS))
def test_the_contract_test_fails_on_a_build_that_breaks_a_rule(
    broken: dict[str, dict[str, str]], name: str
) -> None:
    checks = broken[name]
    assert len(checks) == CHECKS
    assert {key for key, result in checks.items() if result != "passed"} == BREAKS[name][1]


def dbt_tests(model: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each column's generic dbt tests by name, with their arguments."""
    found: dict[str, dict[str, Any]] = {}
    for col in model["columns"]:
        tests: dict[str, Any] = {}
        for test in col.get("data_tests", []):
            name, args = (test, {}) if isinstance(test, str) else next(iter(test.items()))
            tests[name] = (args or {}).get("arguments", {})
        found[col["name"]] = tests
    return found


def logical(physical: str | None) -> str | None:
    """The ODCS logical type of a DuckDB type the mart uses."""
    if physical is not None and physical.startswith("DECIMAL"):
        return "number"
    families = {"VARCHAR": "string", "DATE": "date", "BIGINT": "integer", "BOOLEAN": "boolean"}
    return families.get(physical or "")


def differences(
    contract: Contract, manifest: dict[str, Any], model: dict[str, Any], mart: list[tuple[str, str]]
) -> list[str]:
    """Where the contract and the dbt project disagree about the mart: its columns and their order
    in the dbt YAML and in the built table, each column's physical type as built and its logical
    type's family, the model's and each column's description as dbt renders it, and whether dbt
    runs a not-null, unique or accepted-values test (with the same values) on each column.

    It cannot see a bound, the primary key or a classification, which dbt does not hold, nor a dbt
    test's own configuration: a test with severity `warn` or a `where` filter counts as present.
    `datacontract test` holds the bounds and the key to the build itself."""
    (schema,) = contract["schema"]
    props = schema["properties"]
    node = manifest["nodes"]["model.ledger_lab.payments"]
    found: list[str] = []
    names = [p["name"] for p in props]
    if names != [c["name"] for c in model["columns"]]:
        found.append("the dbt YAML's columns differ from the contract's")
    if names != [name for name, _ in mart]:
        found.append("the built mart's columns differ from the contract's")
    if schema["description"].strip() != node["description"].strip():
        found.append("the model's description differs from dbt's")
    types = dict(mart)
    tests = dbt_tests(model)
    for p in props:
        name = p["name"]
        if types.get(name) != p["physicalType"]:
            found.append(
                f"{name}: built as {types.get(name)}, the contract says {p['physicalType']}"
            )
        if logical(types.get(name)) != p["logicalType"]:
            found.append(f"{name}: built as {types.get(name)}, not a {p['logicalType']}")
        rendered = node["columns"].get(name, {}).get("description", "")
        if p.get("description", "").strip() != rendered.strip():
            found.append(f"{name}: the description differs from dbt's")
        if p.get("required", False) != ("not_null" in tests.get(name, {})):
            found.append(f"{name}: required in one and not the other")
        if p.get("unique", False) != ("unique" in tests.get(name, {})):
            found.append(f"{name}: unique in one and not the other")
        enum = [e["value"] for e in p.get("enum", [])]
        if enum != tests.get(name, {}).get("accepted_values", {}).get("values", []):
            found.append(f"{name}: accepted values differ")
    return found


@pytest.fixture(scope="module")
def dbt_side(build: Build) -> DbtSide:
    manifest = json.loads((build.cwd / "target" / "manifest.json").read_text())
    (model,) = yaml.safe_load(MART_YAML.read_text())["models"]
    mart = query(
        build,
        "select column_name, data_type from information_schema.columns"
        " where table_name = 'payments' order by ordinal_position",
    )
    return manifest, model, [(name, kind) for name, kind in mart]


def test_the_contract_and_the_dbt_project_say_the_same_of_the_mart(dbt_side: DbtSide) -> None:
    assert differences(committed(), *dbt_side) == []


Sides = tuple[Contract, dict[str, Any], dict[str, Any], list[tuple[str, str]]]


def swap_dbt_yaml_columns(s: Sides) -> None:
    columns = s[2]["columns"]
    columns[0], columns[1] = columns[1], columns[0]


def swap_built_columns(s: Sides) -> None:
    s[3][0], s[3][1] = s[3][1], s[3][0]


def on_column(name: str, key: str, value: object) -> Callable[[Sides], None]:
    def change(s: Sides) -> None:
        column(s[0], name)[key] = value

    return change


def describe_model(s: Sides) -> None:
    (schema,) = s[0]["schema"]
    schema["description"] = "Another product."


def drop_column(s: Sides) -> None:
    properties(s[0]).remove(column(s[0], "document_number"))


# One difference at a time, on either side, and exactly what the check reports for it.
IN_STEP: dict[str, tuple[Callable[[Sides], None], list[str]]] = {
    "the dbt YAML's order": (
        swap_dbt_yaml_columns,
        ["the dbt YAML's columns differ from the contract's"],
    ),
    "the built mart's order": (
        swap_built_columns,
        ["the built mart's columns differ from the contract's"],
    ),
    "a dropped column": (
        drop_column,
        [
            "the dbt YAML's columns differ from the contract's",
            "the built mart's columns differ from the contract's",
        ],
    ),
    "the model's description": (describe_model, ["the model's description differs from dbt's"]),
    "a physical type": (
        on_column("amount", "physicalType", "DECIMAL(18,2)"),
        ["amount: built as DECIMAL(28,6), the contract says DECIMAL(18,2)"],
    ),
    "a logical type in the same family to the CLI": (
        on_column("amount", "logicalType", "integer"),
        ["amount: built as DECIMAL(28,6), not a integer"],
    ),
    "nullability": (
        on_column("amount", "required", False),
        ["amount: required in one and not the other"],
    ),
    "uniqueness": (
        on_column("payment_key", "unique", False),
        ["payment_key: unique in one and not the other"],
    ),
    "accepted values": (
        on_column("currency", "enum", [{"value": "USD"}]),
        ["currency: accepted values differ"],
    ),
    "a column's description": (
        on_column("vendor_name", "description", "Another meaning."),
        ["vendor_name: the description differs from dbt's"],
    ),
}


@pytest.mark.parametrize("name", list(IN_STEP))
def test_the_in_step_check_names_each_difference(dbt_side: DbtSide, name: str) -> None:
    change, expected = IN_STEP[name]
    sides: Sides = (committed(), *copy.deepcopy(dbt_side))
    change(sides)
    assert differences(*sides) == expected


def drop(name: str) -> Callable[[Contract], None]:
    def change(c: Contract) -> None:
        properties(c).remove(column(c, name))

    return change


def setting(name: str, key: str, value: object) -> Callable[[Contract], None]:
    def change(c: Contract) -> None:
        column(c, name)[key] = value

    return change


def unsetting(name: str, key: str) -> Callable[[Contract], None]:
    def change(c: Contract) -> None:
        del column(c, name)[key]

    return change


def rename(old: str, new: str) -> Callable[[Contract], None]:
    def change(c: Contract) -> None:
        column(c, old)["name"] = new

    return change


def add_accepted(name: str, value: str) -> Callable[[Contract], None]:
    def change(c: Contract) -> None:
        column(c, name)["enum"].append({"value": value})

    return change


def remove_accepted(name: str, value: str) -> Callable[[Contract], None]:
    def change(c: Contract) -> None:
        column(c, name)["enum"].remove({"value": value})

    return change


def set_bound(name: str, bound: str, value: int) -> Callable[[Contract], None]:
    def change(c: Contract) -> None:
        column(c, name)["logicalTypeOptions"][bound] = value

    return change


HASHED = "hashed-exactly-when-the-name-is-a-hash"


def drop_rule(rule_id: str) -> Callable[[Contract], None]:
    def change(c: Contract) -> None:
        (schema,) = c["schema"]
        schema["quality"] = [q for q in schema["quality"] if q["id"] != rule_id]

    return change


def on_rule(rule_id: str, key: str, value: object) -> Callable[[Contract], None]:
    def change(c: Contract) -> None:
        rule(c, rule_id)[key] = value

    return change


def swap_rule(c: Contract) -> None:
    drop_rule(HASHED)(c)
    (schema,) = c["schema"]
    schema["quality"].append({"id": "trivial", "type": "sql", "query": "select 0", "mustBe": 0})


def loosen_rule(c: Contract) -> None:
    del rule(c, HASHED)["mustBe"]
    rule(c, HASHED)["mustBeLessThan"] = 10


def add_rule(c: Contract) -> None:
    (schema,) = c["schema"]
    schema["quality"].append(
        {"id": "a-new-rule", "type": "sql", "query": "select count(*) from payments", "mustBe": 0}
    )


def add_column(c: Contract) -> None:
    properties(c).append({"name": "department", "logicalType": "string", "physicalType": "VARCHAR"})


def unchanged(c: Contract) -> None:
    pass


BREAKING = {
    "a removed column": drop("contract_number"),
    "a renamed key": rename("payment_key", "payment_id"),
    "a narrowed type": setting("amount", "physicalType", "DECIMAL(18,2)"),
    "a changed logical type": setting("fiscal_year", "logicalType", "string"),
    "a column made nullable": setting("amount", "required", False),
    "a dropped uniqueness": setting("payment_key", "unique", False),
    "a dropped key": setting("payment_key", "primaryKey", False),
    "an added accepted value": add_accepted("currency", "CHF"),
    "a loosened bound": set_bound("fiscal_period", "maximum", 13),
    "a removed quality rule": drop_rule(HASHED),
    "a rule's query gutted": on_rule(HASHED, "query", "select 0"),
    "a rule swapped for a trivial one": swap_rule,
    "a rule's id renamed": on_rule(HASHED, "id", "hashed"),
    "a rule's expected result loosened": loosen_rule,
    "a rule's dimension changed": on_rule(HASHED, "dimension", "accuracy"),
    "a personal column declassified": unsetting("vendor_name", "classification"),
    "a personal column reclassified": setting("document_number", "classification", "restricted"),
    "the contract's id changed": lambda c: c.update(id="payments-v2"),
}
NOT_BREAKING = {
    "no change": unchanged,
    "an added optional column": add_column,
    "a changed description": setting("vendor_name", "description", "The payee."),
    "a rule's wording changed": on_rule(HASHED, "description", "Hashed names are flagged."),
    "an added rule": add_rule,
    "a column newly classified personal": setting("payment_key", "classification", "personal"),
    "a nullable column made required": setting("fiscal_year", "required", True),
    "a removed accepted value": remove_accepted("entity_id", "nl-government"),
    "a tightened bound": set_bound("fiscal_period", "maximum", 11),
}


def git_repo(tmp_path: Path, main: dict[str, str] | None) -> Path:
    """A repository whose `origin/main` holds the files `main`, or (None) no `origin/main`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com"]
    git += ["-c", "commit.gpgsign=false"]

    def run(*args: str) -> None:
        subprocess.run([*git, *args], cwd=repo, check=True, capture_output=True)

    run("init", "--quiet")
    (repo / "README.md").write_text("a clone\n")
    for path, text in (main or {}).items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    run("add", ".")
    run("commit", "--quiet", "-m", "main")
    if main is not None:
        run("update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def with_files(repo: Path, files: dict[str, str]) -> Path:
    """The working tree's contract files replaced by `files`."""
    for old in repo.rglob(f"*{gate.SUFFIX}"):
        old.unlink()
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    return repo


@pytest.fixture(scope="module")
def published_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A clone whose `origin/main` holds the committed contract where it is committed."""
    return git_repo(
        tmp_path_factory.mktemp("published"), {str(gate.CONTRACT): CONTRACT.read_text()}
    )


def changed(change: Callable[[Contract], None]) -> str:
    contract = committed()
    change(contract)
    return yaml.safe_dump(contract, sort_keys=False)


@pytest.mark.parametrize("name", list(BREAKING))
def test_a_breaking_change_fails_the_gate(published_repo: Path, name: str) -> None:
    repo = with_files(published_repo, {str(gate.CONTRACT): changed(BREAKING[name])})
    ok, report = gate.check(repo)
    assert not ok, report
    assert "break consumers" in report


@pytest.mark.parametrize("name", list(NOT_BREAKING))
def test_a_change_that_breaks_no_consumer_passes_the_gate(published_repo: Path, name: str) -> None:
    repo = with_files(published_repo, {str(gate.CONTRACT): changed(NOT_BREAKING[name])})
    ok, report = gate.check(repo)
    assert ok, report


def test_the_gate_names_what_breaks(published_repo: Path) -> None:
    def several(c: Contract) -> None:
        setting("amount", "physicalType", "DECIMAL(18,2)")(c)
        on_rule(HASHED, "query", "select 0")(c)
        unsetting("vendor_name", "classification")(c)

    ok, report = gate.check(with_files(published_repo, {str(gate.CONTRACT): changed(several)}))
    assert not ok
    assert "payments: schema.payments.properties.amount" in report
    assert f"payments: quality rule payments/{HASHED} is changed beyond its wording" in report
    assert "payments: payments.vendor_name is no longer classified personal" in report


FILES = {
    "moved, with its gate's path": ({"contracts/payments.odcs.yaml": CONTRACT.read_text()}, True),
    "beside a new contract": (
        {
            str(gate.CONTRACT): CONTRACT.read_text(),
            "models/contracts/other.odcs.yaml": changed(lambda c: c.update(id="other")),
        },
        True,
    ),
    "renamed out of the contracts' suffix": (
        {"models/contracts/payments.yaml": CONTRACT.read_text()},
        False,
    ),
    "retired": ({}, False),
    "unreadable": ({str(gate.CONTRACT): "id: payments\nschema: [\n"}, False),
}


@pytest.mark.parametrize("name", list(FILES))
def test_the_gate_matches_contracts_by_id_wherever_they_are(
    published_repo: Path, name: str
) -> None:
    files, passes = FILES[name]
    ok, report = gate.check(with_files(published_repo, files))
    assert ok == passes, report


def test_an_unreadable_contract_is_called_unreadable(published_repo: Path) -> None:
    files = {str(gate.CONTRACT): "id: payments\nschema: [\n"}
    ok, report = gate.check(with_files(published_repo, files))
    assert not ok
    assert f"{gate.CONTRACT} is unreadable" in report


def test_the_first_contract_passes_while_main_holds_none(tmp_path: Path) -> None:
    repo = with_files(git_repo(tmp_path, {}), {str(gate.CONTRACT): CONTRACT.read_text()})
    ok, report = gate.check(repo)
    assert ok, report
    assert "holds no contract" in report


def test_a_clone_without_main_fails_and_says_how_to_fetch_it(tmp_path: Path) -> None:
    repo = with_files(git_repo(tmp_path, None), {str(gate.CONTRACT): CONTRACT.read_text()})
    with pytest.raises(gate.GateError, match="git fetch origin main"):
        gate.check(repo)


@pytest.mark.landed
@pytest.mark.usefixtures("engine_env")
def test_the_real_landing_keeps_the_contract(tmp_path: Path) -> None:
    landing = landing_path().resolve()
    for name in (
        "chicago-contracts",
        "chicago-payments",
        "gleif-legal-entities",
        "uk-transport-spend-2025",
    ):
        if not (landing / name).exists():
            pytest.skip(f"{name} is not landed")
    build = built(tmp_path, "landed", landing)
    checks = contract_test(build.database, tmp_path)
    print("contract checks on the real landing:", len(checks))
    assert set(checks.values()) == {"passed"}
    assert len(checks) == CHECKS
    rows = dict(query(build, "select entity_id, count(*) from payments group by 1"))
    print("mart rows by entity:", rows)
    assert rows == {"chicago": 475_609, "uk-transport": 31_103}
    hashed = dict(
        query(build, "select entity_id, count(*) from payments where vendor_is_hashed group by 1")
    )
    print("hashed payees by entity:", hashed)
    assert hashed == {"chicago": 350_698, "uk-transport": 12_008}
    keyed = query(
        build,
        "select count(*) from payments where entity_id = 'uk-transport'"
        " and document_number like 'id:%'",
    )
    print("UK rows whose document number is a keyed hash:", keyed[0][0])
    assert keyed == [(12_009,)]
    split_payees_and_documents(build, landing)


# The landed files' own counts: hashed and clear payees per entity, Chicago's voucher numbers a
# clear column may show (a clear payee's, no placeholder, carried by no hashed payee's row), and
# the hashed rows whose amount and served date no other payee shares, which the publisher's file
# then names the payee by.
LANDED_SPLIT = """
with rows as (
    select 'chicago' as entity_id, vendor_name as payee, voucher_number as number,
        amount, check_date as day
    from read_parquet($chicago)
    union all
    select 'uk-transport', supplier, transaction_number, amount, date
    from read_parquet($uk)
),
pairs as (
    select entity_id, amount, day, count(distinct payee) as payees
    from rows group by all
)
select rows.entity_id,
    count(*) filter (where payee like 'person:%'),
    count(*) filter (where payee not like 'person:%'),
    count(*) filter (where rows.entity_id = 'chicago' and payee not like 'person:%'
        and number <> 'Multiple' and number not in (
            select number from rows as h
            where h.entity_id = 'chicago' and h.payee like 'person:%' and number is not null)),
    count(*) filter (where payee like 'person:%' and payees = 1)
from rows
join pairs on rows.entity_id = pairs.entity_id
    and rows.amount is not distinct from pairs.amount and rows.day is not distinct from pairs.day
group by rows.entity_id
"""
# A UK placeholder, `#` where the department gives no number, which the landing hashes too.
UK_PLACEHOLDER = "#"


def uk_shared_transactions(landing: Path) -> dict[str, int]:
    """The UK transactions a clear row shows in clear and a hashed row carries as a keyed hash,
    found by the landing's own identifier hasher, and the hashed placeholder's rows; counts only."""
    as_id = identifier_hasher()
    rows = (
        duckdb.connect()
        .execute(
            "select supplier, transaction_number from read_parquet($uk)",
            {"uk": str(landing / "uk-transport-spend-2025")},
        )
        .fetchall()
    )
    keyed = [(s, t) for s, t in rows if t and t.startswith("id:")]
    shown = [t for _, t in rows if t and not t.startswith("id:") and t != UK_PLACEHOLDER]
    hits = {as_id(t) for t in set(shown)} & {t for _, t in keyed}
    placeholder = as_id(UK_PLACEHOLDER)
    return {
        "shared numbers": len(hits),
        "clear rows showing one": sum(1 for t in shown if as_id(t) in hits),
        "hashed rows carrying one": sum(1 for _, t in keyed if t in hits),
        "hashed placeholder rows": sum(1 for _, t in keyed if t == placeholder),
        "payees on the hashed placeholder": len({s for s, t in keyed if t == placeholder}),
    }


def split_payees_and_documents(build: Build, landing: Path) -> None:
    """The split columns on the real landing: as many person keys, organisation names and clear
    references per entity as the landed files' own counts give; every clear name an organisation
    by the landing's own classifier and register; no clear name whose person hash is a key; no
    clear reference a hashed payee's row carries; and no UK reference, since the UK landing's
    keyed numbers hide which clear numbers a hashed payee's line shares."""
    files = {
        "chicago": str(landing / "chicago-payments"),
        "uk": str(landing / "uk-transport-spend-2025"),
    }
    served = {e: rest for e, *rest in duckdb.connect().execute(LANDED_SPLIT, files).fetchall()}
    print("landed hashed, clear, clear-reference and amount-and-date-pinned rows:", served)
    assert served == {
        "chicago": [350_698, 124_911, 84_783, 247_360],
        "uk-transport": [12_008, 19_095, 0, 11_845],
    }
    shared = uk_shared_transactions(landing)
    print("UK transactions shown in clear beside a hashed line:", shared)
    assert shared == {
        "shared numbers": 89,
        "clear rows showing one": 135,
        "hashed rows carrying one": 114,
        "hashed placeholder rows": 729,
        "payees on the hashed placeholder": 91,
    }
    split = {
        e: rest
        for e, *rest in query(
            build,
            "select entity_id, count(vendor_person_key), count(vendor_organisation_name),"
            " count(document_reference),"
            " count(*) filter (where (vendor_person_key is null)"
            " = (vendor_organisation_name is null))"
            " from payments group by 1",
        )
    }
    print("person keys, organisation names, references, rows with not one of name and key:", split)
    assert split == {e: [*served[e][:3], 0] for e in served}
    names: dict[str, set[str]] = {e: set() for e in split}
    keys: dict[str, set[str]] = {e: set() for e in split}
    for e, name, key in query(
        build,
        "select distinct entity_id, vendor_organisation_name, vendor_person_key from payments",
    ):
        (names if key is None else keys)[e].add(name if key is None else key)
    register = Register.landed()
    known = {
        "chicago": register.vouched(names["chicago"]),
        "uk-transport": {*BODIES, *register.vouched(names["uk-transport"])},
    }
    # Counts only, so a failure prints no name: the UK department's own placeholder names no one,
    # and every other clear name is an organisation.
    not_organisations = {
        e: {n for n in names[e] if not is_organisation(n, known[e])} for e in names
    }
    assert "REDACTED" in not_organisations["uk-transport"]
    not_organisations["uk-transport"].discard("REDACTED")
    print("clear names, person keys by entity:", {e: (len(names[e]), len(keys[e])) for e in names})
    assert {e: len(n) for e, n in not_organisations.items()} == {"chicago": 0, "uk-transport": 0}
    as_person = person_hasher()
    unhashed = {e: len({as_person(n) for n in names[e]} & keys[e]) for e in names}
    assert unhashed == {"chicago": 0, "uk-transport": 0}
    assert query(
        build,
        "select count(*) from payments as r where r.document_reference is not null and exists"
        " (select 1 from payments as h where h.vendor_is_hashed and h.entity_id = r.entity_id"
        " and h.document_number = r.document_reference)",
    ) == [(0,)]
