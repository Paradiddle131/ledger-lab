"""`make metric-versions`: fail when a metric's definition changes against `origin/main` under the
same version, or a metric loses its version or its owner.

Every metric carries `version` in its `config.meta`, beside its owner, term and role: an integer
from 1 that only goes up. A metric's definition, which its version names, is:

- its own block (type, type parameters, filter, label, description, glossary term, role, and any
  other key), all but its version and its owner;
- every metric it is built from (a ratio's numerator and denominator, a derived metric's inputs,
  a cumulative metric's input), transitively, and every measure it resolves to;
- the semantic model of each such measure, with the semantic models it can join through a shared
  entity name: their dbt model, entities, dimensions, defaults and every other key, and of their
  measures only the ones the metric resolves to.

What a metric is built from counts for what can change its number: the wording of a component (the
`description` and `label` of an input metric, a measure, a dimension, an entity or a semantic
model) and an input metric's `meta` are left out, since the metric's own label and description are
what a reader is shown of its meaning. The definition is compared as parsed YAML, each string
without the whitespace at its ends, so a comment, key order, quoting or line folding is no change.

The SQL of the dbt model a semantic model reads is not in the definition: the semantic layer
cannot see it. A change there that moves a number is caught, if at all, by the model's own tests,
the payments data contract's gate (`models/contracts/gate.py`), and the values pinned over the
landed build (`make reconcile-landed`), never by a metric's version.

The gate fails when, against `origin/main`:

- a metric `origin/main` defines is gone from the working tree or renamed (no metric is retired
  this way: its consumers cite it by name and version);
- a metric's definition changed and its version did not go up, or its version went down;
- a metric has no version that is an integer of at least 1, or no owner;
- a definition cannot be read or resolved (a file that is not YAML, a metric or measure defined
  twice or referenced and never defined, a metric type the gate does not know).

It passes a new metric, an unchanged one, a changed one with a higher version, a higher version
alone, and an owner changed alone. A metric `origin/main` defines without a version counts as
version 0 there, so the first versions pass.

Each tree's metrics are the ones its own dbt project reads: every `dbt_project.yml` the tree holds,
each `model-paths` entry resolved from that file's folder, and every YAML file under them. So
`origin/main`'s metrics come from `origin/main`'s own configuration, and moving the metric files,
the project or its `model-paths` in the working tree cannot hide them from the comparison. The
comparison reads `origin/main` from the clone, with no network; a clone without it fails and says
how to fetch it, and while `origin/main` defines no metric the first ones pass.
"""

import hashlib
import json
import posixpath
import subprocess
import sys
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
BASE = "origin/main"
PROJECT_FILE = "dbt_project.yml"
# What a component may change without changing the number of a metric built from it.
WORDING = {"description", "label"}
# The meta keys a metric's version does not name: the version itself, and who is accountable.
UNVERSIONED_META = {"version", "owner"}
Block = dict[str, Any]


class GateError(Exception):
    """The comparison cannot be made; the message says why and what to do."""


@dataclass(frozen=True)
class Tree:
    """A tree of files: its paths, and a way to read one."""

    paths: list[str]
    read: Callable[[str], str]


@dataclass(frozen=True)
class Metric:
    """A metric as one tree defines it: its version and owner as written, and its definition."""

    version: object
    owner: object
    definition: Block

    @property
    def digest(self) -> str:
        """The SHA-256 of the definition's canonical JSON: the same exactly when it is."""
        text = json.dumps(self.definition, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        cwd=repo,
    )


def base_tree(repo: Path, base: str = BASE) -> Tree:
    """The tree `base` holds, read from the clone; GateError without `base`."""
    if git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").returncode != 0:
        raise GateError(
            f"{base} is not in this clone, so the metrics cannot be compared with it:"
            f" run `git fetch origin main` and try again"
        )
    paths = git(repo, "ls-tree", "-r", "--name-only", base).stdout.splitlines()
    return Tree(paths, lambda p: git(repo, "show", f"{base}:{p}").stdout)


def working_tree(repo: Path) -> Tree:
    """The working tree's files, tracked or new and not ignored."""
    listed = git(repo, "ls-files", "--cached", "--others", "--exclude-standard").stdout
    paths = [p for p in listed.splitlines() if (repo / p).is_file()]
    return Tree(paths, lambda p: (repo / p).read_text())


def schema_files(tree: Tree) -> tuple[list[str], list[str]]:
    """Every YAML file a dbt project of the tree reads as a model path, and what cannot be read."""
    problems: list[str] = []
    folders: list[str] = []
    for path in tree.paths:
        if posixpath.basename(path) != PROJECT_FILE:
            continue
        try:
            project = yaml.safe_load(tree.read(path)) or {}
        except yaml.YAMLError as e:
            problems.append(f"{path} is unreadable: {str(e).splitlines()[0]}")
            continue
        home = posixpath.dirname(path)
        for entry in project.get("model-paths") or ["models"]:
            folder = posixpath.normpath(posixpath.join(home, str(entry)))
            if not folder.startswith("../") and folder != "..":
                folders.append("" if folder == "." else folder + "/")
    files = sorted(
        {
            p
            for p in tree.paths
            for f in folders
            if p.startswith(f) and p.endswith((".yml", ".yaml"))
        }
    )
    return files, problems


def blocks(tree: Tree) -> tuple[dict[str, Block], dict[str, Block], list[str]]:
    """The tree's semantic models and metrics by name, and what cannot be read."""
    files, problems = schema_files(tree)
    found: dict[str, dict[str, Block]] = {"semantic_models": {}, "metrics": {}}
    for path in files:
        try:
            content = yaml.safe_load(tree.read(path))
        except yaml.YAMLError as e:
            problems.append(f"{path} is unreadable: {str(e).splitlines()[0]}")
            continue
        if not isinstance(content, dict):
            continue
        for kind, named in found.items():
            for block in content.get(kind) or []:
                name = block.get("name") if isinstance(block, dict) else None
                if not isinstance(name, str):
                    problems.append(f"{path}: an entry of {kind} has no name")
                elif name in named:
                    problems.append(f"{path}: {kind} {name} is defined twice")
                else:
                    named[name] = block
    return found["semantic_models"], found["metrics"], problems


def normal(value: Any, drop: Collection[str] = ()) -> Any:
    """The value as compared: keys in `drop` left out at every depth, strings stripped."""
    if isinstance(value, dict):
        return {str(k): normal(v, drop) for k, v in value.items() if k not in drop}
    if isinstance(value, list):
        return [normal(v, drop) for v in value]
    if isinstance(value, str):
        return value.strip()
    return value


def ref(value: object) -> str:
    """The name a metric or measure reference gives, as a string or a mapping with `name`."""
    name = value.get("name") if isinstance(value, dict) else value
    if not isinstance(name, str):
        raise ValueError(f"a reference without a name: {value!r}")
    return name


def parts(metric: Block) -> tuple[list[str], list[str]]:
    """The metrics and the measures a metric is built from, by name."""
    kind = metric.get("type")
    params = metric.get("type_params") or {}
    if kind == "simple":
        return [], [ref(params.get("measure"))]
    if kind == "ratio":
        return [ref(params.get("numerator")), ref(params.get("denominator"))], []
    if kind == "derived":
        return [ref(m) for m in params.get("metrics") or []], []
    if kind == "cumulative":
        if params.get("metric") is not None:
            return [ref(params["metric"])], []
        return [], [ref(params.get("measure"))]
    if kind == "conversion":
        conversion = params.get("conversion_type_params") or {}
        return [], [ref(conversion.get("base_measure")), ref(conversion.get("conversion_measure"))]
    raise ValueError(f"type {kind!r} is not one the gate can resolve")


def definition(name: str, models: dict[str, Block], metrics: dict[str, Block]) -> Block:
    """The metric's definition: its own block but version and owner, the metrics it is built from
    transitively, and the semantic models of every measure it resolves to, with what they join."""
    own = normal(metrics[name])
    meta = (own.get("config") or {}).get("meta")
    if isinstance(meta, dict):
        own["config"]["meta"] = {k: v for k, v in meta.items() if k not in UNVERSIONED_META}
    inputs: dict[str, Block] = {}
    measures: set[str] = set()
    pending = [name]
    while pending:
        current = pending.pop()
        if current not in metrics:
            raise ValueError(f"metric {current} is referenced and not defined")
        built_from, used = parts(metrics[current])
        measures.update(used)
        for input_name in built_from:
            if input_name not in inputs and input_name != name:
                block = normal(metrics.get(input_name) or {}, WORDING)
                (block.get("config") or {}).pop("meta", None)
                inputs[input_name] = block
                pending.append(input_name)
    owners = {
        m.get("name"): model_name
        for model_name, model in models.items()
        for m in model.get("measures") or []
    }
    missing = sorted(measures - owners.keys())
    if missing:
        raise ValueError(f"measure {', '.join(missing)} is referenced and not defined")
    reached = {owners[m] for m in measures}
    entities = {
        model_name: {e.get("name") for e in model.get("entities") or []}
        for model_name, model in models.items()
    }
    grown = True
    while grown:
        names = set().union(*(entities[m] for m in reached))
        joined = {m for m, own_entities in entities.items() if own_entities & names}
        grown = not joined <= reached
        reached |= joined
    semantic: dict[str, Block] = {}
    for model_name in sorted(reached):
        model = normal(models[model_name], WORDING)
        model["measures"] = [m for m in model.get("measures") or [] if m.get("name") in measures]
        semantic[model_name] = model
    return {"metric": own, "inputs": inputs, "semantic_models": semantic}


def read(tree: Tree) -> tuple[dict[str, Metric], list[str]]:
    """Every metric the tree's dbt projects define, and what cannot be read or resolved."""
    models, metrics, problems = blocks(tree)
    found: dict[str, Metric] = {}
    for name, block in metrics.items():
        meta = (block.get("config") or {}).get("meta") or {}
        try:
            resolved = definition(name, models, metrics)
        except (ValueError, AttributeError, TypeError) as e:
            problems.append(f"metric {name} cannot be resolved: {e}")
            continue
        found[name] = Metric(meta.get("version"), meta.get("owner"), resolved)
    return found, problems


def valid_version(version: object) -> bool:
    return isinstance(version, int) and not isinstance(version, bool) and version >= 1


def compare(before: Tree, after: Tree, base: str = BASE) -> tuple[bool, str]:
    """Whether every metric of `after` keeps its version's promise against `before`, and why."""
    published, unresolved = read(before)
    if unresolved:
        raise GateError(f"{base}'s metrics cannot be read: " + "; ".join(unresolved))
    current, found = read(after)
    for name, metric in sorted(current.items()):
        if not valid_version(metric.version):
            found.append(f"metric {name} has no version that is an integer of at least 1")
        if not isinstance(metric.owner, str) or not metric.owner.strip():
            found.append(f"metric {name} has no owner")
    changed: list[str] = []
    for name, old in sorted(published.items()):
        new = current.get(name)
        if new is None:
            found.append(f"metric {name} ({base} defines it) is gone or renamed")
            continue
        if not valid_version(new.version):
            continue
        was = old.version if isinstance(old.version, int) and valid_version(old.version) else 0
        now = new.version if isinstance(new.version, int) else 0
        if now < was:
            found.append(f"metric {name}'s version went down from {was} to {now}")
        elif new.digest != old.digest:
            if now == was:
                found.append(f"metric {name}'s definition changed and its version is still {was}")
            else:
                changed.append(f"{name} {was} to {now}")
    if found:
        lines = "\n".join(f"- {line}" for line in found)
        return (
            False,
            f"metric-versions: these metrics break their versions against {base}:\n{lines}",
        )
    if not published:
        return True, f"metric-versions: {base} defines no metric, so the first ones pass"
    report = f"metric-versions: {len(current)} metrics keep their versions against {base}"
    return True, report + (f"; changed with a new version: {', '.join(changed)}" if changed else "")


def check(repo: Path = REPO, base: str = BASE) -> tuple[bool, str]:
    """The working tree's metrics against `base`'s, read from the clone."""
    return compare(base_tree(repo, base), working_tree(repo), base)


def main() -> int:
    try:
        ok, report = check()
    except GateError as e:
        print(f"metric-versions: {e}", file=sys.stderr)
        return 1
    print(report, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
