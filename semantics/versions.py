"""`make metric-versions`: fail when a metric's definition changes against `origin/main` under the
same version, or a metric loses its version or its owner.

Every metric carries `version` in its `config.meta`, beside its owner, term and role: an integer
from 1 that only goes up. The definitions compared are dbt's own reading of each tree: the gate
exports `origin/main`'s tree (`git archive`) and the working tree (its tracked and new files, not
the ignored ones) into temporary folders and runs `dbt parse` on the dbt project at
`models/dbt_project.yml` of each, on the local target, with no network and under one fixed
environment the gate sets, the same for both. So every path dbt reads metric YAML from, the
`.dbtignore` it honours, the project's configuration (`+enabled`, `+meta` and the rest) and the
project's time spines are dbt's, and the definitions are built from the semantic manifest dbt
writes. A metric's definition, which its version names, is:

- its own entry of the semantic manifest, all but its version and its owner;
- every metric it is built from, transitively: an input of a ratio, a derived, a cumulative or a
  conversion metric, and a metric read in a filter (`{{ Metric('x', ...) }}`) anywhere in it,
  each found by MetricFlow's own model of the manifest and parser of filters;
- the semantic model of each measure those metrics resolve to and of each semantic model a metric
  aggregates directly, with the semantic models they can join through a shared entity name: their
  dbt relation, entities, dimensions, defaults and every other key, and of their measures only the
  ones resolved to;
- the project's time spines.

What a metric is built from counts for what can change its number: the wording of a component (the
`description` and `label` of an input metric, a measure, a dimension, an entity or a semantic
model) and an input metric's `meta` are left out, since the metric's own label and description are
what a reader is shown of its meaning. Where dbt records the file and lines an entry came from,
that is left out too, and each string is compared without the whitespace at its ends, so moving a
definition, a comment, key order, quoting or line folding is no change.

A definition that could differ for a consumer while both trees read the same is refused: dbt Jinja
in a semantic model or metric entry, or in the entry of a model a semantic model reads or a time
spine is (but a filter, which MetricFlow renders itself, and a model's or column's description,
wording no definition holds), and any Jinja in
the project file. dbt renders that Jinja with the environment (`env_var`), command-line variables,
the clock and the run, none of which is in the tree. A file or symbolic link reaching outside the
tree is refused too.

Two things are outside the definition. The SQL of the dbt model a semantic model reads: the
semantic layer cannot see it, so a change there that moves a number is caught, if at all, by the
model's own tests, the payments data contract's gate (`models/contracts/gate.py`) and the values
pinned over the landed build (`make reconcile-landed`). And the relation names the profile gives on
another target or environment: the gate reads them on the local target under its own environment.

The gate fails when, against `origin/main`:

- the clone holds no `origin/main`, or HEAD does not contain it (merge it first: a branch is gated
  against the newest `origin/main` it holds, so of two branches that bump one metric to the same
  version, the second must merge the first and is then compared with it);
- `origin/main`'s tree cannot be exported, or its project cannot be parsed: never a pass on
  something it could not read;
- the working tree's project cannot be parsed, holds what is refused above, or is gone;
- a metric `origin/main` defines is gone from the working tree or renamed (no metric is retired
  this way: its consumers cite it by name and version);
- a metric's definition changed and its version did not go up, or its version went down;
- a metric has no version that is an integer from 1 to 2^53 - 1, or no owner.

It passes a new metric, an unchanged one, a changed one with a higher version, a higher version
alone, and an owner changed alone. A metric `origin/main` defines without a version counts as
version 0 there, so the first versions pass; while `origin/main` has no dbt project anywhere in its
tree, the first metrics pass.

The comparison reads `origin/main` from the clone, with no network, so it compares with the
`origin/main` last fetched: a stale one cannot be told apart offline, which is why the gates record
of a change is taken after merging the newest default branch into it.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
BASE = "origin/main"
PROJECT = "models"
PROJECT_FILE = f"{PROJECT}/dbt_project.yml"
# The largest integer the audit record (agent/audit.schema.json) carries exactly.
MAX_VERSION = 2**53 - 1
# What a component may change without changing the number of a metric built from it.
WORDING = {"description", "label"}
# Where dbt records the file and lines an entry came from.
PLACE = {"metadata"}
# The meta keys a metric's version does not name: the version itself, and who is accountable.
UNVERSIONED_META = {"version", "owner"}
# The schema-file keys whose entries define metrics and what they read.
SEMANTIC_KEYS = ("semantic_models", "metrics")
JINJA = ("{{", "{%", "{#")
# Pinned by the gate for both parses: nothing else of the caller's environment reaches dbt.
DUCKDB_FILE = "ledger_lab.duckdb"
Block = dict[str, Any]


class GateError(Exception):
    """The comparison cannot be made; the message says why and what to do."""


class Unreadable(Exception):
    """A tree's definitions cannot be read; the message says why."""


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


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        capture_output=True,
        check=False,
        cwd=repo,
    )


def first_line(result: subprocess.CompletedProcess[bytes]) -> str:
    lines = result.stderr.decode(errors="replace").strip().splitlines()
    return lines[0] if lines else f"exit status {result.returncode}"


def base_commit(repo: Path, base: str = BASE) -> str:
    """The commit `base` names, which HEAD must contain; GateError otherwise."""
    found = git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    if found.returncode != 0:
        raise GateError(
            f"{base} is not in this clone, so the metrics cannot be compared with it:"
            f" run `git fetch origin main` and try again"
        )
    commit = found.stdout.decode().strip()
    contained = git(repo, "merge-base", "--is-ancestor", commit, "HEAD")
    if contained.returncode == 1:
        raise GateError(
            f"HEAD does not contain {base}, so a version another change published there could be"
            f" published again here: merge {base} into this branch and run the gate again"
        )
    if contained.returncode != 0:
        raise GateError(f"cannot tell whether HEAD contains {base}: {first_line(contained)}")
    return commit


def tree_id(repo: Path, commit: str) -> str:
    found = git(repo, "rev-parse", "--verify", f"{commit}^{{tree}}")
    if found.returncode != 0:
        raise GateError(f"cannot read the tree of {commit}: {first_line(found)}")
    return found.stdout.decode().strip()


def export_commit(repo: Path, commit: str, into: Path) -> None:
    """The commit's whole tree written into `into`; Unreadable on any failure or a link."""
    archive = git(repo, "archive", "--format=tar", commit)
    if archive.returncode != 0:
        raise Unreadable(f"git archive of {commit} failed: {first_line(archive)}")
    with tarfile.open(fileobj=BytesIO(archive.stdout)) as tar:
        for member in tar.getmembers():
            if member.issym() or member.islnk():
                raise Unreadable(f"{member.name} is a link, which the gate does not follow")
        tar.extractall(into, filter="data")


def export_working(repo: Path, into: Path) -> None:
    """The working tree's tracked and new files, not ignored ones, copied into `into`; a deleted
    file stays out; Unreadable on a link, a path outside the clone or a failed listing."""
    listed = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listed.returncode != 0:
        raise Unreadable(f"git ls-files failed: {first_line(listed)}")
    root = repo.resolve()
    for name in sorted(set(listed.stdout.decode().split("\0")) - {""}):
        source = repo / name
        if source.is_symlink():
            raise Unreadable(f"{name} is a link, which the gate does not follow")
        if not source.exists():
            continue
        if not source.resolve().is_relative_to(root) or not source.is_file():
            raise Unreadable(f"{name} is not a file inside the clone")
        (into / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, into / name)


def environment(work: Path) -> dict[str, str]:
    """The one environment both parses run under: no variable of the caller's but `PATH`."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(work / "home"),
        "TMPDIR": str(work / "tmp"),
        "DBT_TARGET_PATH": str(work / "target"),
        "DBT_LOG_PATH": str(work / "logs"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        "LEDGER_LAB_DUCKDB_PATH": str(work / DUCKDB_FILE),
    }


def has_project(tree: Path) -> bool:
    return any(tree.rglob("dbt_project.yml"))


def parse(tree: Path, work: Path) -> Block:
    """The semantic manifest dbt writes for the tree's project; Unreadable when it cannot."""
    project = tree / PROJECT
    if not (project / "dbt_project.yml").is_file():
        raise Unreadable(f"there is no dbt project at {PROJECT_FILE}")
    if any(mark in (project / "dbt_project.yml").read_text() for mark in JINJA):
        raise Unreadable(f"{PROJECT_FILE} holds Jinja, which dbt renders from outside the tree")
    for folder in ("home", "tmp"):
        (work / folder).mkdir(parents=True, exist_ok=True)
    report = work / "refused.json"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", str(Path(__file__).resolve()), "--parse", str(project), str(report)],
        capture_output=True,
        text=True,
        check=False,
        env=environment(work),
        cwd=work,
    )
    if result.returncode != 0 or not report.is_file():
        out = [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]
        raise Unreadable("dbt parse failed: " + " | ".join(out[-6:]))
    refused: list[str] = json.loads(report.read_text())
    if refused:
        raise Unreadable("; ".join(refused))
    return json.loads((work / "target" / "semantic_manifest.json").read_text())


def jinja_at(entry: object, model: bool) -> Iterator[str]:
    """Every place in a raw YAML entry where a string holds Jinja dbt renders, but a `filter`,
    which MetricFlow renders itself, and, in a model's entry (`model`), a description outside its
    metrics, which is wording no definition holds."""
    pending: list[tuple[tuple[str | int, ...], object]] = [((), entry)]
    while pending:
        path, value = pending.pop()
        if isinstance(value, dict):
            pending += [((*path, str(k)), v) for k, v in value.items()]
        elif isinstance(value, list):
            pending += [((*path, n), v) for n, v in enumerate(value)]
        elif isinstance(value, str) and any(mark in value for mark in JINJA):
            if "filter" in path[-2:]:
                continue
            if model and path[-1:] == ("description",) and "metrics" not in path:
                continue
            yield ".".join(str(p) for p in path)


def parse_and_refuse(project: Path, report: Path) -> int:
    """Run in a process of its own: `dbt parse` of the project with the network refused, writing
    to `report` what dbt read that the gate refuses."""
    sys.path.insert(0, str(HERE / "offline"))
    from refusal import refuse_the_network  # pyright: ignore[reportMissingImports]

    sys.addaudithook(refuse_the_network)
    import dbt
    from dbt.cli.main import dbtRunner
    from dbt.contracts.files import FilePath, SchemaSourceFile
    from dbt.contracts.graph.manifest import Manifest

    args = ["parse", "--no-partial-parse", "--no-use-colors", "--project-dir", str(project)]
    result = dbtRunner().invoke([*args, "--profiles-dir", str(project), "--target", "local"])
    manifest = result.result
    if not result.success or not isinstance(manifest, Manifest):
        print(f"dbt parse did not succeed: {result.exception}", file=sys.stderr)
        return 1
    tree = project.resolve().parent
    # dbt's and its adapter's own macros, installed beside dbt.
    installed = Path(dbt.__file__).resolve().parents[1]
    read_by_semantics = {
        manifest.nodes[uid].name
        for model in manifest.semantic_models.values()
        for uid in model.depends_on.nodes
        if uid in manifest.nodes
    } | {node.name for node in manifest.nodes.values() if getattr(node, "time_spine", None)}
    refused: list[str] = []
    for file in manifest.files.values():
        if not isinstance(file.path, FilePath):
            continue
        path = file.path.original_file_path
        found_at = Path(file.path.full_path).resolve()
        if found_at.is_relative_to(installed):
            continue
        if not found_at.is_relative_to(tree):
            refused.append(f"dbt read {path}, which is outside the tree")
        if not isinstance(file, SchemaSourceFile):
            continue
        for key, entries in (file.dict_from_yaml or {}).items():
            if key not in SEMANTIC_KEYS and key != "models":
                continue
            for entry in entries if isinstance(entries, list) else []:
                name = entry.get("name") if isinstance(entry, dict) else None
                if key == "models" and name not in read_by_semantics:
                    continue
                for place in jinja_at(entry, key == "models"):
                    refused.append(f"{path}: {key} {name} holds dbt Jinja at {place}")
    report.write_text(json.dumps(refused))
    return 0


def normal(value: Any, drop: Collection[str] = ()) -> Any:
    """The value as compared: keys in `drop` and dbt's places left out at every depth, strings
    stripped."""
    if isinstance(value, dict):
        return {
            str(k): normal(v, drop) for k, v in value.items() if k not in drop and k not in PLACE
        }
    if isinstance(value, list):
        return [normal(v, drop) for v in value]
    if isinstance(value, str):
        return value.strip()
    return value


@dataclass
class Parts:
    """What one metric is built from, as MetricFlow's model of the manifest names it."""

    metrics: set[str]
    measures: set[str]
    semantic_models: set[str]


def parts(metric: object, granularities: list[str]) -> Parts:
    """The metrics, measures and semantic models a parsed metric names anywhere in it: its input
    metrics, its input measures, every metric its filters read, and the semantic model a metric
    with its own aggregation reads."""
    from metricflow_semantic_interfaces.implementations.filters.where_filter import (
        PydanticWhereFilter,
    )
    from metricflow_semantic_interfaces.implementations.metric import (
        PydanticMetricAggregationParams,
        PydanticMetricInput,
        PydanticMetricInputMeasure,
    )

    found = Parts(set(), set(), set())

    def visit(value: object) -> None:
        if isinstance(value, PydanticMetricInput):
            found.metrics.add(value.name)
        elif isinstance(value, PydanticMetricInputMeasure):
            found.measures.add(value.name)
        elif isinstance(value, PydanticMetricAggregationParams):
            found.semantic_models.add(value.semantic_model)
        elif isinstance(value, PydanticWhereFilter):
            calls = value.call_parameter_sets(granularities).metric_call_parameter_sets
            found.metrics |= {call.metric_reference.element_name for call in calls}
        if isinstance(value, (list, tuple, set)):
            for inner in value:
                visit(inner)
        elif hasattr(value, "__fields__"):
            for field in getattr(value, "__fields__"):  # noqa: B009
                visit(getattr(value, field))

    visit(metric)
    return found


def definitions(manifest: Block) -> dict[str, Metric]:
    """Every metric of a semantic manifest dbt wrote, with its definition."""
    from metricflow_semantic_interfaces.implementations.semantic_manifest import (
        PydanticSemanticManifest,
    )

    parsed = PydanticSemanticManifest.parse_obj(manifest)
    spines = manifest.get("project_configuration") or {}
    granularities = [
        g.name for s in parsed.project_configuration.time_spines for g in s.custom_granularities
    ]
    raw_metrics = {m["name"]: m for m in manifest.get("metrics") or []}
    raw_models = {m["name"]: m for m in manifest.get("semantic_models") or []}
    built = {m.name: parts(m, granularities) for m in parsed.metrics}
    time = normal({k: spines.get(k) for k in ("time_spines", "time_spine_table_configurations")})
    found: dict[str, Metric] = {}
    for name, raw in raw_metrics.items():
        meta = (raw.get("config") or {}).get("meta") or {}
        closure = reached(name, built)
        own = normal(raw)
        own_meta = (own.get("config") or {}).get("meta")
        if isinstance(own_meta, dict):
            own["config"]["meta"] = {k: v for k, v in own_meta.items() if k not in UNVERSIONED_META}
        inputs = {}
        for each in sorted(closure - {name}):
            block = normal(raw_metrics[each], WORDING)
            (block.get("config") or {}).pop("meta", None)
            inputs[each] = block
        measures = set().union(*(built[m].measures for m in closure))
        definition = {
            "metric": own,
            "inputs": inputs,
            "semantic_models": semantic(closure, built, measures, raw_models),
            "time_spines": time,
        }
        found[name] = Metric(meta.get("version"), meta.get("owner"), definition)
    return found


def reached(name: str, built: dict[str, Parts]) -> set[str]:
    """The metric and every metric it is built from, transitively."""
    closure: set[str] = set()
    pending = [name]
    while pending:
        current = pending.pop()
        if current in closure:
            continue
        closure.add(current)
        pending += sorted(built[current].metrics - closure)
    return closure


def semantic(
    closure: set[str], built: dict[str, Parts], measures: set[str], models: dict[str, Block]
) -> dict[str, Block]:
    """The semantic models the metrics read, with every one they join through a shared entity,
    each with only the measures resolved to."""
    owners = {
        m["name"]: name for name, model in models.items() for m in model.get("measures") or []
    }
    entities = {
        name: {e["name"] for e in model.get("entities") or []} for name, model in models.items()
    }
    named = set().union(*(built[m].semantic_models for m in closure))
    reach = {owners[m] for m in measures} | named
    grown = True
    while grown:
        shared = set().union(*(entities[m] for m in reach))
        joined = {m for m, own in entities.items() if own & shared}
        grown = not joined <= reach
        reach |= joined
    found: dict[str, Block] = {}
    for name in sorted(reach):
        model = normal(models[name], WORDING)
        model["measures"] = [m for m in model.get("measures") or [] if m.get("name") in measures]
        found[name] = model
    return found


def read(tree: Path, work: Path) -> dict[str, Metric]:
    """Every metric dbt reads from the tree's project, with its definition; Unreadable when the
    tree cannot be parsed or holds what the gate refuses."""
    return definitions(parse(tree, work))


def valid_version(version: object) -> bool:
    return (
        isinstance(version, int) and not isinstance(version, bool) and 1 <= version <= MAX_VERSION
    )


def compare(
    published: dict[str, Metric], current: dict[str, Metric], base: str = BASE
) -> tuple[bool, str]:
    """Whether every metric of `current` keeps its version's promise against `published`."""
    found: list[str] = []
    for name, metric in sorted(current.items()):
        if not valid_version(metric.version):
            found.append(f"metric {name} has no version that is an integer from 1 to 2^53 - 1")
        if not isinstance(metric.owner, str) or not metric.owner.strip():
            found.append(f"metric {name} has no owner")
    changed: list[str] = []
    for name, old in sorted(published.items()):
        new = current.get(name)
        if new is None:
            found.append(f"metric {name} ({base} defines it) is gone or renamed")
            continue
        if not isinstance(new.version, int) or not valid_version(new.version):
            continue
        was = old.version if isinstance(old.version, int) and valid_version(old.version) else 0
        now = new.version
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


# Each tree id's definitions, once read in this process: a tree id names every byte of the tree,
# and the gate fixes the environment, so a second parse of the same id reads the same.
_PUBLISHED: dict[str, dict[str, Metric]] = {}


def published_metrics(repo: Path, commit: str, base: str, work: Path) -> dict[str, Metric]:
    """`base`'s metrics, read from its exported tree; GateError when it cannot be read."""
    key = tree_id(repo, commit)
    if key not in _PUBLISHED:
        tree = work / "tree"
        tree.mkdir()
        try:
            export_commit(repo, commit, tree)
            if not (tree / PROJECT_FILE).is_file() and not has_project(tree):
                _PUBLISHED[key] = {}
            else:
                _PUBLISHED[key] = read(tree, work)
        except Unreadable as e:
            raise GateError(f"{base}'s metrics cannot be read, so nothing is compared: {e}") from e
    return _PUBLISHED[key]


def check(repo: Path = REPO, base: str = BASE) -> tuple[bool, str]:
    """The working tree's metrics against `base`'s, both as dbt reads them."""
    commit = base_commit(repo, base)
    with tempfile.TemporaryDirectory(prefix="metric-versions-") as scratch:
        (Path(scratch) / "base").mkdir()
        (Path(scratch) / "work").mkdir()
        published = published_metrics(repo, commit, base, Path(scratch) / "base")
        tree = Path(scratch) / "work" / "tree"
        tree.mkdir()
        try:
            export_working(repo, tree)
            current = read(tree, Path(scratch) / "work")
        except Unreadable as e:
            return False, f"metric-versions: the working tree's metrics cannot be read: {e}"
    return compare(published, current, base)


def main(argv: list[str], run: Callable[[], tuple[bool, str]] = check) -> int:
    if argv[:1] == ["--parse"] and len(argv) == 3:
        return parse_and_refuse(Path(argv[1]), Path(argv[2]))
    try:
        ok, report = run()
    except GateError as e:
        print(f"metric-versions: {e}", file=sys.stderr)
        return 1
    print(report, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
