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
- a column `origin/main` classifies `personal` is classified otherwise, which lowers its handling;
- a contract `origin/main` holds is gone from the working tree, or no longer has its id;
- a contract cannot be read.

Not breaking: an optional column added, a nullable column made required, an accepted value
removed, a bound tightened, a description changed, a column newly classified `personal`, a new rule
or a new contract.

The comparison reads `origin/main` from the local clone and runs the CLI in this environment, so
it needs no network. A clone that does not hold `origin/main` cannot be compared, so the gate fails
and says how to fetch it, rather than passing a change it never saw. While `origin/main` holds no
contract there is nothing published to break, so the first contracts pass, and the gate says so.
"""

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
    paths = git(repo, "ls-tree", "-r", "--name-only", base).stdout.splitlines()
    return {p: git(repo, "show", f"{base}:{p}").stdout for p in paths if p.endswith(SUFFIX)}


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


def breaks(before: Contract, after: Contract) -> list[str]:
    """What in `after` breaks a consumer of `before`, each as one line."""
    found: list[str] = []
    try:
        result = DataContract(data_contract_str=yaml.safe_dump(before), inline_references=False)
        graded = result.breaking(
            DataContract(data_contract_str=yaml.safe_dump(after), inline_references=False)
        )
    except Exception as e:  # the CLI raises its own and pydantic's errors on a bad contract
        return [f"contract {after['id']} is unreadable: {str(e).splitlines()[0]}"]
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
    for col in sorted(personal(before) & columns(after) - personal(after)):
        found.append(f"{col} is no longer classified personal")
    return found


def compare(before: dict[str, str], after: dict[str, str], base: str = BASE) -> tuple[bool, str]:
    """Whether the contract files `after` keep every promise the files `before` made, and why."""
    if not before:
        return True, f"contract: {base} holds no contract, so nothing published can break"
    found: list[str] = []
    current: dict[str, Contract] = {}
    for path, text in after.items():
        contract = parsed(path, text)
        if isinstance(contract, str):
            found.append(contract)
        else:
            current[contract["id"]] = contract
    for path, text in before.items():
        contract = parsed(path, text)
        if isinstance(contract, str):
            raise GateError(f"{base}'s {contract}")
        if contract["id"] not in current:
            found.append(f"contract {contract['id']} ({path} on {base}) is gone or has a new id")
            continue
        found += [f"{contract['id']}: {line}" for line in breaks(contract, current[contract["id"]])]
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
