"""`make contract`: fail when a data contract breaks a consumer of the one on `origin/main`.

Every `*.odcs.yaml` file `origin/main` holds, anywhere in its tree, is matched by its contract id
with one in the working tree, wherever that one now sits, and a change is breaking when:

- the Data Contract CLI's `breaking` comparison, from a consumer's point of view, finds one: data
  a consumer could have relied on may now arrive otherwise (a column removed or renamed, any type
  changed, a column made nullable, a key or uniqueness dropped, an accepted value added, a bound
  loosened, a quality rule the CLI can grade loosened or removed);
- a quality rule `origin/main` holds is gone, under its id at the same place, or is changed in
  anything but its wording (`description`, `name`), since the CLI cannot grade a changed SQL query;
  a rule is tightened by adding a new one beside it;
- a column `origin/main` classifies `personal` is classified otherwise, which lowers its handling,
  or is gone beside an added column not classified `personal`;
- a schema `origin/main` holds is gone or renamed, or its table is under another schema;
- a column holds nested properties, which the gate does not read;
- a contract `origin/main` holds is gone from the working tree, or no longer has its id;
- a contract cannot be read.

Not breaking: an optional column added, a nullable column made required, an accepted value
removed, a bound tightened, a description changed, a column newly classified `personal`, a new rule
or a new contract.

A deliberate break passes only under a new major version that lists it. When a contract's
`version` has a major number above the one on `origin/main`, its ODCS `customProperties` entry
`breakingChanges` lists each break of the first two kinds above as `change` (the break exactly as
this gate reports it, without the contract id) and `reason`, and the gate passes when the breaks it
finds equal the breaks listed: it fails on a break not listed, a listed break that did not happen,
an entry without a reason, or a list under a major version that did not go up, where the list is
not read. The list is compared with `origin/main`'s contract, so once a new major version is on
`origin/main` its list excuses nothing: the next break needs the next major version and a list of
its own breaks. These stay breaking whatever the version, and no list can excuse them:

- a column `origin/main` classifies `personal` classified otherwise: the classification decides
  how the column is masked, not what a consumer reads;
- a column `origin/main` classifies `personal` gone in a change that adds any column not
  classified `personal`: a rename cannot be told from a removal and an addition, so each added
  column is the removed one's possible successor (the removal alone stays declarable);
- a schema gone or renamed, or a schema's `physicalName` now under another schema: a table gone
  is a new contract, and the CLI's one `Removed schema` line would excuse every break under it;
- a column holding nested `properties` or `items`: the gate reads one level of columns, so a field
  a level down could hide its classification;
- a contract gone or with a new id, a contract unreadable (to YAML or to the CLI), and two
  contracts with one id: none leaves one contract to hold a version and a list.

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
    tree = git(repo, "ls-tree", "-r", "--name-only", base)
    if tree.returncode != 0:
        raise GateError(f"{base}'s tree cannot be read: {tree.stderr.strip()}")
    texts: dict[str, str] = {}
    for path in tree.stdout.splitlines():
        if path.endswith(SUFFIX):
            shown = git(repo, "show", f"{base}:{path}")
            if shown.returncode != 0:
                raise GateError(f"{base}'s {path} cannot be read: {shown.stderr.strip()}")
            texts[path] = shown.stdout
    return texts


def working(repo: Path) -> dict[str, str]:
    """Every contract file of the working tree, tracked or new, by path, with its text."""
    listed = git(repo, "ls-files", "--cached", "--others", "--exclude-standard").stdout
    paths = [p for p in listed.splitlines() if p.endswith(SUFFIX) and (repo / p).is_file()]
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
    return {
        f"{schema['name']}.{p['name']}"
        for schema in contract.get("schema") or []
        for p in schema.get("properties") or []
        if p.get("classification") == "personal"
    }


def columns(contract: Contract) -> set[str]:
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
    return found, refused(before, after)


def refused(before: Contract, after: Contract) -> list[str]:
    """What in `after` breaks a consumer of `before` whatever its version, each as one line."""
    never = "which no version can declare"
    found = [
        f"{col} nests properties, which this gate does not read, {never}" for col in nested(after)
    ]
    old = {s["name"]: s.get("physicalName") or s["name"] for s in before.get("schema") or []}
    new = {s["name"]: s.get("physicalName") or s["name"] for s in after.get("schema") or []}
    found += [f"schema {name} is gone or renamed, {never}" for name in old if name not in new]
    found += [
        f"table {table} of schema {name} is now under schema {other}, {never}"
        for name, table in old.items()
        for other, moved in new.items()
        if moved == table and other != name
    ]
    found += [
        f"{col} is no longer classified personal, {never}"
        for col in sorted(personal(before) & columns(after) - personal(after))
    ]
    gone = sorted(personal(before) - columns(after))
    unclassified = sorted(columns(after) - columns(before) - personal(after))
    found += [
        f"{col}, classified personal, is gone while {added} is added without that classification,"
        f" {never}: a rename cannot be told from a removal and an addition"
        for col in gone
        for added in unclassified
    ]
    return found


def nested(contract: Contract) -> list[str]:
    """Every column that holds properties of its own (an object's or an array's items)."""
    return [
        f"{schema['name']}.{p['name']}"
        for schema in contract.get("schema") or []
        for p in schema.get("properties") or []
        if "properties" in p or "items" in p
    ]


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
    for path, text in before.items():
        contract = parsed(path, text)
        if isinstance(contract, str):
            raise GateError(f"{base}'s {contract}")
        if contract["id"] not in current:
            found.append(f"contract {contract['id']} ({path} on {base}) is gone or has a new id")
            continue
        now = current[contract["id"]]
        declarable, never = breaks(contract, now)
        lines = never + undeclared(contract, now, declarable, never)
        found += [f"{contract['id']}: {line}" for line in lines]
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
