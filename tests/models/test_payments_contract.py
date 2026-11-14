"""The payments data product's contract (`models/contracts/payments.odcs.yaml`): valid ODCS, kept
by the sample build and the real landing's build, in step with the dbt docs and the built mart, and
guarded by the gate that fails `make check` on a change that breaks a consumer.

The Data Contract CLI tests a DuckDB database with DuckDB 1.5, which cannot open the 2.0 database
dbt builds, so each test hands it a copy written in 1.5's storage format. Every changed contract
and every broken database here is made inside the test from the committed contract and a build.
"""

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from ingest.fetch import landing_path
from models.contracts import gate
from tests.models.dbt_build import PROJECT, Build, built, query

CONTRACT = gate.REPO / gate.CONTRACT
MART_YAML = PROJECT / "models" / "marts" / "payments" / "_payments__models.yml"
Contract = dict[str, Any]
DbtSide = tuple[dict[str, Any], dict[str, Any], list[tuple[str, str]]]
# Presence and type of all 14 columns, 8 required, 1 unique, 2 accepted-values and 2 bound checks,
# and the 4 quality rules.
CHECKS = 2 * 14 + 8 + 1 + 2 + 2 + 4


def committed() -> Contract:
    return yaml.safe_load(CONTRACT.read_text())


def properties(contract: Contract) -> list[dict[str, Any]]:
    (schema,) = contract["schema"]
    return schema["properties"]


def column(contract: Contract, name: str) -> dict[str, Any]:
    return next(p for p in properties(contract) if p["name"] == name)


def for_the_cli(database: Path, directory: Path) -> Path:
    """A copy of a dbt build in DuckDB 1.5's storage format, which the CLI's DuckDB reads."""
    target = directory / "for_the_cli.duckdb"
    with duckdb.connect() as db:
        db.execute("attach ? as build (read_only)", [str(database)])
        db.execute("attach ? as cli (storage_version 'v1.5.0')", [str(target)])
        db.execute("copy from database build to cli")
    return target


def contract_test(database: Path, directory: Path) -> dict[str, str]:
    """`datacontract test` over a database: each check's result, by its quality rule's id in the
    contract for a rule, else by the CLI's key for the check."""
    results = directory / "contract-results.json"
    run = gate.datacontract(
        "test",
        str(CONTRACT),
        "--output-format",
        "json",
        "--output",
        str(results),
        env=dict(os.environ) | {"DATACONTRACT_DUCKDB_DATABASE": str(database)},
    )
    assert results.is_file(), run.stdout + run.stderr
    checks = {
        c["qualityId"] or c["key"]: c["result"] for c in json.loads(results.read_text())["checks"]
    }
    assert (run.returncode == 0) == all(r == "passed" for r in checks.values()), run.stdout
    return checks


# The library calls the CLI's `breaking` and `test` commands make, for many contracts or databases
# in one process of the CLI's environment, since every start of the command costs seconds.
BATCH = """
import json, os, sys
from datacontract.data_contract import DataContract
job = json.load(sys.stdin)
out = {}
for name, item in job["items"].items():
    if job["kind"] == "breaking":
        before, after = (DataContract(data_contract_file=f, inline_references=False) for f in item)
        out[name] = before.breaking(after).is_breaking
    else:
        os.environ["DATACONTRACT_DUCKDB_DATABASE"] = item
        run = DataContract(data_contract_file=job["contract"]).test()
        out[name] = {c.qualityId or c.key: c.result.value for c in run.checks}
print(json.dumps(out))
"""


def in_one_process(kind: str, items: dict[str, Any]) -> dict[str, Any]:
    job = {"kind": kind, "items": items, "contract": str(CONTRACT)}
    run = gate.in_contract_env("python", "-c", BATCH, stdin=json.dumps(job))
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.splitlines()[-1])


def test_the_contract_is_valid_odcs() -> None:
    run = gate.datacontract("lint", str(CONTRACT))
    assert run.returncode == 0, run.stdout + run.stderr
    assert "valid against ODCS v3.2.0" in run.stdout


def test_the_sample_build_keeps_the_contract(build: Build, tmp_path: Path) -> None:
    checks = contract_test(for_the_cli(build.database, tmp_path), tmp_path)
    assert set(checks.values()) == {"passed"}
    (schema,) = committed()["schema"]
    assert {q["id"] for q in schema["quality"]} <= set(checks)
    assert len(checks) == CHECKS


# One break of the sample build per rule, and the checks it fails: each rule fails on its own break.
BREAKS = {
    "a key served twice": (
        "insert into payments select * from payments where entity_id = 'chicago' limit 1",
        {"payments__payment_key__field_unique", "every-chicago-payment-once"},
    ),
    "a payment in another entity's currency": (
        "update payments set currency = 'EUR' where payment_key ="
        " (select min(payment_key) from payments where entity_id = 'uk-transport')",
        {"currency-is-the-entitys-own"},
    ),
    "a hashed name flagged clear": (
        "update payments set vendor_is_hashed = false where payment_key ="
        " (select min(payment_key) from payments where vendor_is_hashed)",
        {"hashed-exactly-when-the-name-is-a-hash"},
    ),
    "a clear name flagged hashed": (
        "update payments set vendor_is_hashed = true where payment_key ="
        " (select min(payment_key) from payments where not vendor_is_hashed)",
        {"hashed-exactly-when-the-name-is-a-hash"},
    ),
    "a UK payment lost": (
        "delete from payments where payment_key ="
        " (select min(payment_key) from payments where entity_id = 'uk-transport')",
        {"every-uk-transport-payment-once"},
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
            "every-chicago-payment-once",
        },
    ),
    "a missing amount": (
        "update payments set amount = null"
        " where payment_key = (select min(payment_key) from payments)",
        {"payments__amount__field_required"},
    ),
}


@pytest.fixture(scope="module")
def broken(build: Build, tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, str]]:
    """The contract test's results on one copy of the sample build per break."""
    databases = {}
    for name, (sql, _) in BREAKS.items():
        database = for_the_cli(build.database, tmp_path_factory.mktemp("broken"))
        with duckdb.connect(str(database)) as db:
            db.execute(sql)
        databases[name] = str(database)
    return in_one_process("test", databases)


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


def differences(
    contract: Contract, manifest: dict[str, Any], model: dict[str, Any], mart: list[tuple[str, str]]
) -> list[str]:
    """Where the contract and the dbt project disagree about the mart: its columns and their order
    in the dbt YAML and the built table, each column's type as built, the model's and each column's
    description as dbt renders it, and the not-null, unique and accepted-values tests dbt runs."""
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


def drop(name: str) -> Callable[[Contract], None]:
    def change(c: Contract) -> None:
        properties(c).remove(column(c, name))

    return change


def setting(name: str, key: str, value: object) -> Callable[[Contract], None]:
    def change(c: Contract) -> None:
        column(c, name)[key] = value

    return change


IN_STEP = {
    "a column": drop("document_number"),
    "a type": setting("amount", "physicalType", "DECIMAL(18,2)"),
    "nullability": setting("amount", "required", False),
    "uniqueness": setting("payment_key", "unique", False),
    "accepted values": setting("currency", "enum", [{"value": "USD"}]),
    "a description": setting("vendor_name", "description", "Another meaning."),
}


@pytest.mark.parametrize("name", list(IN_STEP))
def test_the_in_step_check_sees_the_contract_differ_in(dbt_side: DbtSide, name: str) -> None:
    contract = committed()
    IN_STEP[name](contract)
    assert contract != committed()
    assert differences(contract, *dbt_side) != []


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


def drop_rule(rule: str) -> Callable[[Contract], None]:
    def change(c: Contract) -> None:
        (schema,) = c["schema"]
        schema["quality"] = [q for q in schema["quality"] if q["id"] != rule]

    return change


def add_column(c: Contract) -> None:
    properties(c).append({"name": "department", "logicalType": "string", "physicalType": "VARCHAR"})


def unchanged(c: Contract) -> None:
    pass


BREAKING = {
    "a removed column": drop("document_number"),
    "a renamed key": rename("payment_key", "payment_id"),
    "a narrowed type": setting("amount", "physicalType", "DECIMAL(18,2)"),
    "a changed logical type": setting("fiscal_year", "logicalType", "string"),
    "a column made nullable": setting("amount", "required", False),
    "a dropped uniqueness": setting("payment_key", "unique", False),
    "a dropped key": setting("payment_key", "primaryKey", False),
    "an added accepted value": add_accepted("currency", "CHF"),
    "a loosened bound": set_bound("fiscal_period", "maximum", 13),
    "a removed quality rule": drop_rule("hashed-exactly-when-the-name-is-a-hash"),
}
NOT_BREAKING = {
    "no change": unchanged,
    "an added optional column": add_column,
    "a changed description": setting("vendor_name", "description", "The payee."),
    "a changed classification": setting("vendor_name", "classification", "restricted"),
    "a nullable column made required": setting("fiscal_year", "required", True),
    "a removed accepted value": remove_accepted("entity_id", "nl-government"),
    "a tightened bound": set_bound("fiscal_period", "maximum", 11),
}


@pytest.fixture(scope="module")
def breaks(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bool]:
    """Whether the CLI's comparison finds each changed contract breaking against the committed."""
    directory = tmp_path_factory.mktemp("contracts")
    before = directory / "before.odcs.yaml"
    before.write_text(yaml.safe_dump(committed(), sort_keys=False))
    pairs = {}
    for n, (name, change) in enumerate({**BREAKING, **NOT_BREAKING}.items()):
        changed = committed()
        change(changed)
        after = directory / f"after-{n}.odcs.yaml"
        after.write_text(yaml.safe_dump(changed, sort_keys=False))
        pairs[name] = [str(before), str(after)]
    return in_one_process("breaking", pairs)


@pytest.mark.parametrize("name", list(BREAKING))
def test_a_breaking_change_fails_the_gate(breaks: dict[str, bool], name: str) -> None:
    assert breaks[name] is True


@pytest.mark.parametrize("name", list(NOT_BREAKING))
def test_a_change_that_breaks_no_consumer_passes_the_gate(
    breaks: dict[str, bool], name: str
) -> None:
    assert breaks[name] is False


def clone_with(tmp_path: Path, main_holds_contract: bool | None) -> Path:
    """A repository holding the committed contract, its `origin/main` holding the contract, an
    older tree without one, or (None) no `origin/main` at all."""
    repo = tmp_path / "repo"
    (repo / gate.CONTRACT.parent).mkdir(parents=True)
    git = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com"]
    git += ["-c", "commit.gpgsign=false"]

    def run(*args: str) -> None:
        subprocess.run([*git, *args], cwd=repo, check=True, capture_output=True)

    run("init", "--quiet")
    (repo / "README.md").write_text("a clone\n")
    run("add", "README.md")
    if main_holds_contract:
        (repo / gate.CONTRACT).write_text(CONTRACT.read_text())
        run("add", str(gate.CONTRACT))
    run("commit", "--quiet", "-m", "main")
    if main_holds_contract is not None:
        run("update-ref", "refs/remotes/origin/main", "HEAD")
    (repo / gate.CONTRACT).write_text(CONTRACT.read_text())
    return repo


def test_the_first_contract_passes_while_main_holds_none(tmp_path: Path) -> None:
    ok, report = gate.check(clone_with(tmp_path, main_holds_contract=False))
    assert ok, report
    assert "holds no payments contract" in report


def test_a_clone_without_main_fails_and_says_how_to_fetch_it(tmp_path: Path) -> None:
    with pytest.raises(gate.GateError, match="git fetch origin main"):
        gate.check(clone_with(tmp_path, main_holds_contract=None))


def test_the_gate_reads_main_from_the_clone_and_stops_a_breaking_change(tmp_path: Path) -> None:
    repo = clone_with(tmp_path, main_holds_contract=True)
    ok, report = gate.check(repo)
    assert ok, report
    narrowed = committed()
    setting("amount", "physicalType", "DECIMAL(18,2)")(narrowed)
    (repo / gate.CONTRACT).write_text(yaml.safe_dump(narrowed, sort_keys=False))
    ok, report = gate.check(repo)
    assert not ok
    assert "schema.payments.properties.amount.physicalType" in report
    (repo / gate.CONTRACT).unlink()
    ok, report = gate.check(repo)
    assert not ok
    assert "is gone" in report


@pytest.mark.landed
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
    checks = contract_test(for_the_cli(build.database, tmp_path), tmp_path)
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
