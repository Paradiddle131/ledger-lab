"""`make contract`: fail when a data contract breaks a consumer of the one on `origin/main`.

Every `*.odcs.yaml` file `origin/main` holds, anywhere in its tree, is matched by its contract id
with one in the working tree, wherever that one now sits; paths are listed NUL-separated, so a
path git would quote is read too. Each contract matched is compared with the Data Contract CLI and
the gate's rules on quality rules; a break of these kinds may be declared by a new major version:

- the CLI's `breaking` comparison, from a consumer's point of view, finds one: data a consumer
  could have relied on may now arrive otherwise (a column removed or renamed, any type changed, a
  column made nullable, a key or uniqueness dropped, an accepted value added, a bound loosened, a
  quality rule the CLI can grade loosened or removed);
- a quality rule `origin/main` holds is gone, under its id at the same place, or is changed in
  anything but its wording (`description`, `name`), since the CLI cannot grade a changed SQL query;
  a rule is tightened by adding a new one beside it.

Not breaking: an optional column added, a nullable column made required, an accepted value
removed, a bound tightened, a description changed, a column newly classified `personal`, a new rule
or a new contract.

A deliberate break passes only under a new major version that lists it. When a contract's
`version` is MAJOR.MINOR.PATCH with a major number above the one on `origin/main`, its ODCS
`customProperties` entry `breakingChanges` lists each such break as `change` (the break exactly as
this gate reports it, without the contract id) and `reason`, and the gate passes when the breaks it
finds equal the breaks listed: it fails on a break not listed, a listed break that did not happen,
an entry without a reason, or a list under a major version that did not go up, where the list is
not read. The list is compared with `origin/main`'s contract, so once a new major version is on
`origin/main` its list excuses nothing: the next break needs the next major version and a list of
its own breaks.

Before those, every contract of the working tree, new ones included, and every contract on
`origin/main` is read as properties, each by its logical names (contract id, schema name, property
name) and by what it describes: its table, the schema entry's `physicalName` (else its name), and
its column, the property's `physicalName` (else its name). A name is normalised by taking off one
pair of double quotes wrapping a part whole and lower-casing it; a table may be `table` or
`<schema>.table` where `<schema>` is the `schema` of one of the contract's servers, and a column
holds no dot. These fail whatever the version, and no list can excuse them:

- in any contract of the tree: a name that cannot be normalised (any other quote, white space,
  an empty part, a table of more than two parts or under a schema no server names, a column with a
  dot); two schema entries of one name (ignoring case); two properties of one schema with one name
  (ignoring case) or over one column; nested `properties` or `items` on a property, since the gate
  reads one level; and two schema entries, in one contract or across the tree, over one table;
- a column `origin/main` classifies `personal` (by normalised table and column) that a property in
  the tree describes without `classification: personal`, whatever it says instead;
- a table `origin/main` describes that the tree describes under another contract or schema name,
  or a column it describes under another property name: the CLI compares by logical names, so a
  removal it reports there would hide what changed in the same table or column;
- a column `origin/main` classifies `personal` that nothing in the tree describes, while the tree
  adds anywhere (new contracts included) a column `origin/main` does not describe and that is not
  classified `personal`: within one change a rename cannot be told from a removal and an addition,
  so each such addition is the removed one's possible successor; the removal alone stays
  declarable;
- a schema `origin/main` holds, by its exact name, gone from its contract, since the CLI's one
  `Removed schema` line would excuse every break under it;
- a contract gone or with a new id, a contract unreadable (to YAML or to the CLI), and two
  contracts with one id: none leaves one contract to hold a version and a list.

A contract already on `origin/main` whose shape the first rule refuses fails every change until a
change fixes it; the gate cannot let such a shape onto `origin/main`, since it refuses it in a new
contract too. The gate reads contracts, not data: a classification can still be lost across two
changes (an unclassified column added in one, the personal column removed in a later major
version), and a column whose values the models copy under a new physical name is outside it. What
covers those is review of the contract with the masking tags, and the tests that hold the contract
equal to the dbt project.

The comparison reads `origin/main` from the local clone and runs the CLI in this environment, so
it needs no network. A clone that does not hold `origin/main` cannot be compared, so the gate fails
and says how to fetch it, rather than passing a change it never saw; so does a clone whose
`origin/main` tree or contract files git cannot read (a partial clone whose remote is gone).
While `origin/main` holds no contract there is nothing published to break, so the first contracts
pass, and the gate says so.
"""

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from datacontract.data_contract import DataContract
from datacontract.model.breaking import BreakingChangeLevel

REPO = Path(__file__).resolve().parents[2]
CONTRACT = Path("models/contracts/payments.odcs.yaml")
BASE = "origin/main"
SUFFIX = ".odcs.yaml"
# What a quality rule may change without breaking a consumer: its wording.
WORDING = {"description", "name"}
# The contract's own custom property that lists the breaks of a new major version.
DECLARED = "breakingChanges"
NEVER = "which no version can declare"
SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
Contract = dict[str, Any]


class GateError(Exception):
    """The comparison cannot be made; the message says why and what to do."""


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        cwd=repo,
    )


def published(repo: Path, base: str = BASE) -> dict[str, str]:
    """Every contract file `base` holds, by path, with its text; GateError without `base`."""
    if git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").returncode != 0:
        raise GateError(
            f"{base} is not in this clone, so the contracts cannot be compared with it:"
            f" run `git fetch origin main` and try again"
        )
    tree = git(repo, "ls-tree", "-r", "-z", "--name-only", base)
    if tree.returncode != 0:
        raise GateError(f"{base}'s tree cannot be read: {tree.stderr.strip()}")
    texts: dict[str, str] = {}
    for path in tree.stdout.split("\0"):
        if path.endswith(SUFFIX):
            shown = git(repo, "show", f"{base}:{path}")
            if shown.returncode != 0:
                raise GateError(f"{base}'s {path} cannot be read: {shown.stderr.strip()}")
            texts[path] = shown.stdout
    return texts


def working(repo: Path) -> dict[str, str]:
    """Every contract file of the working tree, tracked or new, by path, with its text."""
    listed = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout
    paths = [p for p in listed.split("\0") if p.endswith(SUFFIX) and (repo / p).is_file()]
    return {p: (repo / p).read_text() for p in paths}


def parsed(path: str, text: str) -> Contract | str:
    """The contract, or why it cannot be read."""
    try:
        contract = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return f"{path} is unreadable: {str(e).splitlines()[0]}"
    if not isinstance(contract, dict) or not contract.get("id"):
        return f"{path} is unreadable: no contract id"
    return contract


def rules(contract: Contract) -> dict[str, dict[str, Any]]:
    """Every quality rule by place and id: `<schema>/<id>` or `<schema>.<column>/<id>`."""
    found: dict[str, dict[str, Any]] = {}
    for schema in contract.get("schema") or []:
        places = [(schema["name"], schema)] + [
            (f"{schema['name']}.{p['name']}", p) for p in schema.get("properties") or []
        ]
        for place, owner in places:
            for n, rule in enumerate(owner.get("quality") or []):
                found[f"{place}/{rule.get('id') or f'#{n} (no id)'}"] = rule
    return found


def personal(contract: Contract) -> set[str]:
    """Every column the contract classifies `personal`, as `<schema>.<column>`."""
    return {
        f"{schema['name']}.{p['name']}"
        for schema in contract.get("schema") or []
        for p in schema.get("properties") or []
        if p.get("classification") == "personal"
    }


def columns(contract: Contract) -> set[str]:
    """Every column of the contract, as `<schema>.<column>`."""
    return {
        f"{schema['name']}.{p['name']}"
        for schema in contract.get("schema") or []
        for p in schema.get("properties") or []
    }


def breaks(before: Contract, after: Contract) -> tuple[list[str], list[str]]:
    """What in `after` breaks a consumer of `before`, each as one line: what a new major version
    may declare, and what no version may."""
    found: list[str] = []
    try:
        result = DataContract(data_contract_str=yaml.safe_dump(before), inline_references=False)
        graded = result.breaking(
            DataContract(data_contract_str=yaml.safe_dump(after), inline_references=False)
        )
    except Exception as e:  # the CLI raises its own and pydantic's errors on a bad contract
        return [], [
            f"contract {after['id']} is unreadable, which no version can declare:"
            f" {str(e).splitlines()[0]}"
        ]
    found += [
        f"{entry.path}: {entry.message}"
        for entry in graded.entries
        if entry.level == BreakingChangeLevel.ERROR
    ]
    old, new = rules(before), rules(after)
    for key, rule in old.items():
        if key not in new:
            found.append(f"quality rule {key} is gone (removed, moved or renamed)")
        elif {k: v for k, v in rule.items() if k not in WORDING} != {
            k: v for k, v in new[key].items() if k not in WORDING
        }:
            found.append(f"quality rule {key} is changed beyond its wording")
    gone = [
        f"schema {name} is gone or renamed, {NEVER}"
        for name in schema_names(before)
        if name not in schema_names(after)
    ]
    return found, gone


def schema_names(contract: Contract) -> list[str]:
    return [
        s["name"]
        for s in contract.get("schema") or []
        if isinstance(s, dict) and isinstance(s.get("name"), str)
    ]


def identifier(part: str) -> str | None:
    """One part of a name, lower-cased, without the double quotes that may wrap it whole; None
    when it is empty or holds any other quote or white space."""
    if len(part) >= 2 and part[0] == part[-1] == '"':
        part = part[1:-1]
    if not part or any(ch in "\"'`" or ch.isspace() for ch in part):
        return None
    return part.lower()


def table_name(contract: Contract, schema: dict[str, Any]) -> str | None:
    """The table a schema entry describes: its `physicalName`, else its name, as `table` or
    `<server schema>.table`, each part an identifier, the prefix one a server of the contract
    names; None when the name cannot be put in that form."""
    name = schema.get("physicalName") or schema.get("name")
    if not isinstance(name, str):
        return None
    parts = [identifier(part) for part in name.split(".")]
    if len(parts) > 2 or None in parts:
        return None
    prefixes = {
        identifier(server["schema"])
        for server in contract.get("servers") or []
        if isinstance(server, dict) and isinstance(server.get("schema"), str)
    }
    if len(parts) == 2 and parts[0] not in prefixes:
        return None
    return parts[-1]


def column_name(prop: dict[str, Any]) -> str | None:
    """The physical column a property describes: its `physicalName`, else its name, an
    identifier with no dot; None otherwise."""
    name = prop.get("physicalName") or prop.get("name")
    found = identifier(name) if isinstance(name, str) else None
    return None if found is None or "." in found else found


@dataclass(frozen=True)
class Described:
    """One property of a contract, by its logical names and the table and column it describes."""

    contract: str
    schema: str
    name: str
    table: str
    column: str
    classification: object

    @property
    def where(self) -> tuple[str, str]:
        return self.table, self.column

    def __str__(self) -> str:
        return f"{self.contract}'s {self.schema}.{self.name} ({self.table}.{self.column})"


def normalised(contract: Contract, strict: bool) -> tuple[list[Described], list[str]]:
    """Every property of the contract, and, when `strict`, what keeps its shape from being read
    as one property per physical column of one table per schema entry. Not strict (a contract
    already on origin/main), a name that cannot be normalised is only lower-cased."""
    found: list[Described] = []
    wrong: list[str] = []
    cid = str(contract["id"])
    names: set[str] = set()
    for schema in contract.get("schema") or []:
        if not isinstance(schema, dict) or not isinstance(schema.get("name"), str):
            wrong.append(f"a schema entry has no name, {NEVER}")
            continue
        sname: str = schema["name"]
        table = table_name(contract, schema)
        if table is None:
            wrong.append(f"schema {sname}'s table name cannot be normalised, {NEVER}")
            table = str(schema.get("physicalName") or sname).lower()
        if sname.lower() in names:
            wrong.append(f"two schema entries are named {sname}, {NEVER}")
        names.add(sname.lower())
        props: set[str] = set()
        columns: set[str] = set()
        for prop in schema.get("properties") or []:
            if not isinstance(prop, dict) or not isinstance(prop.get("name"), str):
                wrong.append(f"a property of schema {sname} has no name, {NEVER}")
                continue
            pname: str = prop["name"]
            if "properties" in prop or "items" in prop:
                wrong.append(
                    f"{sname}.{pname} nests properties, which this gate does not read, {NEVER}"
                )
            column = column_name(prop)
            if column is None:
                wrong.append(f"{sname}.{pname}'s column name cannot be normalised, {NEVER}")
                column = str(prop.get("physicalName") or pname).lower()
            if pname.lower() in props:
                wrong.append(f"schema {sname} has two properties named {pname}, {NEVER}")
            if column in columns:
                wrong.append(f"schema {sname} has two properties over column {column}, {NEVER}")
            props.add(pname.lower())
            columns.add(column)
            found.append(Described(cid, sname, pname, table, column, prop.get("classification")))
    return found, wrong if strict else []


def refusals(before: dict[str, Contract], after: dict[str, Contract]) -> dict[str, list[str]]:
    """What in the working tree's contracts `after` breaks a consumer of `origin/main`'s
    `before` whatever their versions, by contract id, compared by normalised table and column."""
    found: dict[str, list[str]] = {cid: [] for cid in after}
    old: list[Described] = []
    for contract in before.values():
        old += normalised(contract, strict=False)[0]
    new: list[Described] = []
    tables: dict[str, list[tuple[str, str]]] = {}
    for cid, contract in after.items():
        described, wrong = normalised(contract, strict=True)
        found[cid] += wrong
        new += described
        for schema in contract.get("schema") or []:
            if isinstance(schema, dict) and isinstance(schema.get("name"), str):
                table = table_name(contract, schema)
                if table is not None:
                    tables.setdefault(table, []).append((cid, schema["name"]))
    for table, owners in tables.items():
        if len(owners) > 1:
            names = " and ".join(f"{cid}'s schema {name}" for cid, name in sorted(owners))
            for cid in dict.fromkeys(cid for cid, _ in owners):
                found[cid].append(f"table {table} is described by both {names}, {NEVER}")
    was_table = {(d.table): set[tuple[str, str]]() for d in old}
    was_column = {d.where: set[tuple[str, str, str]]() for d in old}
    for d in old:
        was_table[d.table].add((d.contract, d.schema))
        was_column[d.where].add((d.contract, d.schema, d.name))
    personal = {d.where for d in old if d.classification == "personal"}
    moved: set[tuple[str, str]] = set()
    for d in new:
        if (d.contract, d.schema) in moved:
            pass  # said once for the schema
        elif d.table in was_table and (d.contract, d.schema) not in was_table[d.table]:
            moved.add((d.contract, d.schema))
            names = ", ".join(f"{c}'s schema {s}" for c, s in sorted(was_table[d.table]))
            found[d.contract].append(
                f"{d.contract}'s schema {d.schema} describes table {d.table}, which {BASE}"
                f" describes as {names}, {NEVER}:"
                " a table keeps its contract and schema name while it is described"
            )
        elif d.where in was_column and (d.contract, d.schema, d.name) not in was_column[d.where]:
            names = ", ".join(f"{c}'s {s}.{n}" for c, s, n in sorted(was_column[d.where]))
            found[d.contract].append(
                f"{d} describes a column {BASE} describes as {names}, {NEVER}:"
                " a column keeps its property name while it is described"
            )
        if d.where in personal and d.classification != "personal":
            found[d.contract].append(
                f"{d} describes a column {BASE} classifies personal and is classified"
                f" {d.classification or 'nothing'}, {NEVER}"
            )
    described = {d.where for d in new}
    olds = {d.where for d in old}
    unclassified = [d for d in new if d.where not in olds and d.classification != "personal"]
    for table, column in sorted(personal - described):
        for d in unclassified:
            found[d.contract].append(
                f"{table}.{column}, which {BASE} classifies personal, is described nowhere while"
                f" {d} is added without that classification, {NEVER}: within one change a"
                " rename cannot be told from a removal and an addition"
            )
    return found


def major(contract: Contract) -> int | None:
    """The major number of the contract's `version`, or None when it is not MAJOR.MINOR.PATCH."""
    match = SEMVER.fullmatch(str(contract.get("version")))
    return int(match.group(1)) if match else None


def declaration(contract: Contract) -> tuple[list[str], list[str]]:
    """The breaks the contract's `breakingChanges` lists, and what is wrong with the list."""
    props = contract.get("customProperties") or []
    if not isinstance(props, list):
        return [], ["customProperties must be a list"]
    entries = [p for p in props if isinstance(p, dict) and p.get("property") == DECLARED]
    if len(entries) > 1:
        return [], [f"customProperties holds {DECLARED} more than once"]
    if not entries:
        return [], []
    value = entries[0].get("value")
    if not isinstance(value, list):
        return [], [f"{DECLARED} must be a list of breaks, each a change and a reason"]
    listed: list[str] = []
    wrong: list[str] = []
    for n, item in enumerate(value):
        if not isinstance(item, dict) or set(item) - {"change", "reason"}:
            wrong.append(f"{DECLARED} entry {n + 1} must hold a change and a reason, nothing else")
            continue
        change, reason = item.get("change"), item.get("reason")
        if not isinstance(change, str) or not change.strip():
            wrong.append(f"{DECLARED} entry {n + 1} names no change")
            continue
        if not isinstance(reason, str) or not reason.strip():
            wrong.append(f"{DECLARED} lists a break without a reason: {change}")
        if change in listed:
            wrong.append(f"{DECLARED} lists a break twice: {change}")
        listed.append(change)
    return listed, wrong


def undeclared(
    before: Contract, after: Contract, found: list[str], refused: list[str]
) -> list[str]:
    """The breaks `found` that `after` does not declare under a new major version, and every
    listed break that did not happen or is wrongly listed."""
    old, new = major(before), major(after)
    if old is None or new is None or new <= old:
        if not found:
            return []
        version = f"version {after.get('version')!r} is not a major version above"
        return [*found, f"{version} {before.get('version')!r}, so no break can be declared"]
    listed, wrong = declaration(after)
    wrong += [f"{line} (not listed in {DECLARED})" for line in found if line not in listed]
    wrong += [
        f"{DECLARED} lists a break that did not happen: {x}"
        for x in listed
        if x not in found and x not in refused
    ]
    return wrong


def compare(before: dict[str, str], after: dict[str, str], base: str = BASE) -> tuple[bool, str]:
    """Whether the contract files `after` keep every promise the files `before` made, and why."""
    if not before:
        return True, f"contract: {base} holds no contract, so nothing published can break"
    found: list[str] = []
    current: dict[str, Contract] = {}
    where: dict[str, str] = {}
    for path, text in after.items():
        contract = parsed(path, text)
        if isinstance(contract, str):
            found.append(contract)
        elif contract["id"] in current:
            found.append(f"contract {contract['id']} is in both {where[contract['id']]} and {path}")
        else:
            current[contract["id"]], where[contract["id"]] = contract, path
    published: dict[str, tuple[str, Contract]] = {}
    for path, text in before.items():
        contract = parsed(path, text)
        if isinstance(contract, str):
            raise GateError(f"{base}'s {contract}")
        published[contract["id"]] = path, contract
    whatever = refusals({cid: c for cid, (_, c) in published.items()}, current)
    for cid, (path, contract) in published.items():
        if cid not in current:
            found.append(f"contract {cid} ({path} on {base}) is gone or has a new id")
            continue
        now = current[cid]
        declarable, never = breaks(contract, now)
        never += whatever.pop(cid, [])
        lines = never + undeclared(contract, now, declarable, never)
        found += [f"{cid}: {line}" for line in lines]
    found += [f"{cid}: {line}" for cid, lines in whatever.items() for line in lines]
    if not found:
        return True, f"contract: no change breaks a consumer of {base}'s contracts"
    lines = "\n".join(f"- {line}" for line in found)
    return False, f"contract: these changes break consumers of {base}'s contracts:\n{lines}"


def check(repo: Path = REPO, base: str = BASE) -> tuple[bool, str]:
    """The working tree's contracts against `base`'s, read from the clone."""
    return compare(published(repo, base), working(repo), base)


def main() -> int:
    try:
        ok, report = check()
    except GateError as e:
        print(f"contract: {e}", file=sys.stderr)
        return 1
    print(report, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
