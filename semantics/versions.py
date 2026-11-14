"""`make metric-versions`: fail when a metric's definition changes against `origin/main` under the
same version, or a metric loses its version or its owner.

Every metric carries `version` in its `config.meta`, beside its owner, term and role: an integer
from 1 that only goes up. The definitions compared are dbt's own reading of each tree: the gate
exports `origin/main`'s tree (`git archive`) and the working tree (its tracked and new files, not
the ignored ones) into temporary folders and runs `dbt parse` on the dbt project at
`models/dbt_project.yml` of each, on the local target, with no network and under one fixed
environment the gate sets, the same for both. So every path dbt reads metric YAML from, the
`.dbtignore` it honours, the project's configuration (`+enabled` and the rest), the newer spec of
metrics on a model's own entry and the project's time spines are dbt's, and the definitions are
built from the semantic manifest dbt writes. A metric's definition, which its version names, is:

- its own entry of the semantic manifest, all but its version and its owner;
- every metric it is built from, transitively: every metric input anywhere in its type parameters
  (a ratio's numerator and denominator, a derived metric's inputs, and any other kind's) and every
  metric a filter anywhere in it reads (`{{ Metric('x', ...) }}`), each found by MetricFlow's own
  model of the manifest and parser of filters;
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
spine is (but in a filter, which MetricFlow renders itself, and in a description of a model's entry
outside its metrics, wording no definition holds), and any Jinja in the project file. dbt renders
that Jinja with the environment (`env_var`), command-line variables (`var`), the clock and the run,
none of which is in the tree. A symbolic link in either tree, and a file dbt reads outside the
tree, are refused too.

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
import posixpath
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
BASE = "origin/main"
PROJECT = "models"
PROJECT_FILE = f"{PROJECT}/dbt_project.yml"
# The files that declare dbt packages, which the gate does not read.
PACKAGE_FILES = ("packages.yml", "dependencies.yml")
# The project macros that decide a relation's name, which dbt can make read the environment.
RELATION_MACROS = ("generate_alias_name", "generate_schema_name", "generate_database_name")
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
# The schema-file keys of what a semantic model can read or a time spine can be.
READ_KEYS = ("models", "seeds", "snapshots")
JINJA = ("{{", "{%", "{#")
# Git's modes for a file and an executable file; anything else (a link, a submodule) is refused.
FILE_MODES = ("100644", "100755")
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


@dataclass(frozen=True)
class Parsed:
    """What dbt's parse of one tree gives the comparison: its semantic manifest, and the project
    file's `vars` as dbt loads them."""

    manifest: Block
    vars: object


def git(repo: Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        capture_output=True,
        check=False,
        cwd=repo,
        input=stdin,
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


def project_folders(text: str) -> list[str]:
    """The folders of the tree dbt reads, from the project file's text: the project folder and
    every path its `*-paths` settings name; Unreadable on Jinja, or a path setting that is not a
    relative path inside the tree."""
    if any(mark in text for mark in JINJA):
        raise Unreadable(f"{PROJECT_FILE} holds Jinja, which dbt renders from outside the tree")
    try:
        project = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise Unreadable(f"{PROJECT_FILE} is not YAML") from e
    if not isinstance(project, dict):
        raise Unreadable(f"{PROJECT_FILE} is not a mapping")
    folders = [PROJECT]
    for key, value in project.items():
        if not str(key).endswith(("-paths", "-path")):
            continue
        for entry in value if isinstance(value, list) else [value]:
            if not isinstance(entry, str) or posixpath.isabs(entry):
                raise Unreadable(f"{PROJECT_FILE}'s {key} names a path that is not relative")
            folder = posixpath.normpath(posixpath.join(PROJECT, entry))
            if folder == ".." or folder.startswith("../"):
                raise Unreadable(f"{PROJECT_FILE}'s {key} names a path outside the tree")
            if str(key).endswith("-paths"):
                folders.append("" if folder == "." else folder)
    return folders


def read_by_dbt(path: str, folders: list[str]) -> bool:
    return any(f == "" or path == f or path.startswith(f + "/") for f in folders)


def refuse_packages(paths: Collection[str]) -> None:
    for name in PACKAGE_FILES:
        if f"{PROJECT}/{name}" in paths:
            raise Unreadable(
                f"{PROJECT}/{name} declares dbt packages, which the gate does not read: their"
                " definitions come from outside the tree and are not installed offline"
            )


def blobs(repo: Path, shas: list[str]) -> list[bytes]:
    """Each object's bytes, as git stores them (no attribute, filter or substitution applied)."""
    if not shas:
        return []
    found = git(repo, "cat-file", "--batch", stdin=("\n".join(shas) + "\n").encode())
    if found.returncode != 0:
        raise Unreadable(f"git cat-file failed: {first_line(found)}")
    out, at, read = found.stdout, 0, []
    for sha in shas:
        end = out.find(b"\n", at)
        header = out[at:end].decode(errors="replace").split()
        if end < 0 or len(header) != 3 or header[0] != sha or header[1] != "blob":
            raise Unreadable(f"git cannot read the object {sha}")
        size = int(header[2])
        read.append(out[end + 1 : end + 1 + size])
        at = end + 1 + size + 1
    return read


def export_commit(repo: Path, commit: str, into: Path) -> bool:
    """The files dbt reads of the commit's tree written into `into`, read as git stores them;
    False when the tree has no dbt project; Unreadable on a failed read, a link or a submodule
    among them, or a project file that is not at `models/dbt_project.yml`."""
    listed = git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    if listed.returncode != 0:
        raise Unreadable(f"git ls-tree of {commit} failed: {first_line(listed)}")
    entries: dict[str, tuple[str, str]] = {}
    for line in listed.stdout.decode().split("\0"):
        if line:
            info, path = line.split("\t", 1)
            mode, _, sha = info.split()
            entries[path] = (mode, sha)
    if PROJECT_FILE not in entries:
        if any(posixpath.basename(p) == "dbt_project.yml" for p in entries):
            raise Unreadable(f"there is no dbt project at {PROJECT_FILE}")
        return False
    if entries[PROJECT_FILE][0] not in FILE_MODES:
        raise Unreadable(f"{PROJECT_FILE} is not a file")
    (project_text,) = blobs(repo, [entries[PROJECT_FILE][1]])
    folders = project_folders(project_text.decode())
    chosen = sorted(p for p in entries if read_by_dbt(p, folders))
    refuse_packages(chosen)
    for path in chosen:
        if entries[path][0] not in FILE_MODES:
            raise Unreadable(f"{path} is a link or a submodule, which the gate does not follow")
    for path, content in zip(chosen, blobs(repo, [entries[p][1] for p in chosen]), strict=True):
        (into / path).parent.mkdir(parents=True, exist_ok=True)
        (into / path).write_bytes(content)
    return True


def export_working(repo: Path, into: Path) -> None:
    """The files dbt reads of the working tree (tracked or new, not ignored, a deleted one left
    out) copied into `into`; Unreadable on a failed listing, or a link, a submodule or a path
    outside the clone among them."""
    project_file = repo / PROJECT_FILE
    if project_file.is_symlink() or not project_file.is_file():
        raise Unreadable(f"there is no dbt project at {PROJECT_FILE}")
    folders = project_folders(project_file.read_text())
    listed = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    staged = git(repo, "ls-files", "-z", "--stage")
    for result in (listed, staged):
        if result.returncode != 0:
            raise Unreadable(f"git ls-files failed: {first_line(result)}")
    for line in staged.stdout.decode().split("\0"):
        if line:
            info, path = line.split("\t", 1)
            if read_by_dbt(path, folders) and info.split()[0] not in FILE_MODES:
                raise Unreadable(f"{path} is a link or a submodule, which the gate does not follow")
    chosen = sorted(
        p for p in set(listed.stdout.decode().split("\0")) - {""} if read_by_dbt(p, folders)
    )
    refuse_packages([p for p in chosen if (repo / p).exists()])
    root = repo.resolve()
    for name in chosen:
        source = repo / name
        if source.is_symlink():
            raise Unreadable(f"{name} is a link or a submodule, which the gate does not follow")
        if not source.exists():
            continue
        if not source.resolve().is_relative_to(root) or not source.is_file():
            raise Unreadable(f"{name} is not a file inside the clone")
        (into / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, into / name)


def environment(work: Path) -> dict[str, str]:
    """The environment a parse runs under: the caller's `PATH` and nothing else of its, and the
    same fixed settings for both trees but for the paths of each parse's own temporary folder."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(work / "home"),
        "TMPDIR": str(work / "tmp"),
        "DBT_TARGET_PATH": str(work / "target"),
        "DBT_LOG_PATH": str(work / "logs"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        "LEDGER_LAB_DUCKDB_PATH": str(work / DUCKDB_FILE),
    }


def parse(tree: Path, work: Path, which: str) -> Parsed:
    """What dbt's parse of the exported tree gives; Unreadable when dbt cannot parse it or it
    holds what the gate refuses. The message never carries dbt's own output."""
    project = tree / PROJECT
    for folder in ("home", "tmp"):
        (work / folder).mkdir(parents=True, exist_ok=True)
    report = work / "report.json"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", str(Path(__file__).resolve()), "--parse", str(project), str(report)],
        capture_output=True,
        check=False,
        env=environment(work),
        cwd=work,
    )
    if result.returncode != 0 or not report.is_file():
        raise Unreadable(
            f"dbt could not parse the {which} tree (the parse ended with exit status"
            f" {result.returncode})"
        )
    found = json.loads(report.read_text())
    if found.get("error"):
        raise Unreadable(f"dbt could not parse the {which} tree ({found['error']})")
    if found["refused"]:
        raise Unreadable("; ".join(found["refused"]))
    semantic = json.loads((work / "target" / "semantic_manifest.json").read_text())
    return Parsed(semantic, found["vars"])


def jinja_at(
    key: str, entry: object, renders: Callable[[tuple[str | int, ...]], bool]
) -> Iterator[str]:
    """Every place in a raw YAML entry of `key` where a string holds Jinja dbt renders: where
    dbt's own renderer renders it (`renders`), and a description, which dbt renders later, but a
    description outside the metrics of a model, seed or snapshot entry, wording no definition
    holds."""
    pending: list[tuple[tuple[str | int, ...], object]] = [((), entry)]
    while pending:
        path, value = pending.pop()
        if isinstance(value, dict):
            pending += [((*path, str(k)), v) for k, v in value.items()]
        elif isinstance(value, list):
            pending += [((*path, n), v) for n, v in enumerate(value)]
        elif isinstance(value, str) and any(mark in value for mark in JINJA):
            wording = path[-1:] == ("description",) and (
                key in READ_KEYS and path[:1] != ("metrics",)
            )
            if renders(path) or (path[-1:] == ("description",) and not wording):
                yield ".".join(str(p) for p in path)


def parse_and_refuse(project: Path, report: Path) -> int:
    """Run in a process of its own: `dbt parse` of the project with the network refused, writing
    to `report` the project's vars, dbt's error class and in-tree path when it fails, and what dbt
    read that the gate refuses."""
    sys.path.insert(0, str(HERE / "offline"))
    from refusal import refuse_the_network  # pyright: ignore[reportMissingImports]

    sys.addaudithook(refuse_the_network)
    import dbt
    from dbt.cli.main import dbtRunner
    from dbt.clients.yaml_helper import load_yaml_text
    from dbt.contracts.files import FilePath, SchemaSourceFile
    from dbt.contracts.graph.manifest import Manifest
    from dbt.parser.schema_renderer import SchemaYamlRenderer

    tree = project.resolve().parent
    args = ["parse", "--no-partial-parse", "--no-use-colors", "--quiet", "--project-dir"]
    result = dbtRunner().invoke(
        [*args, str(project), "--profiles-dir", str(project), "--target", "local"]
    )
    manifest = result.result
    if not result.success or not isinstance(manifest, Manifest):
        report.write_text(json.dumps({"error": failure(result.exception, project, tree)}))
        return 0
    project_vars = (load_yaml_text((project / "dbt_project.yml").read_text()) or {}).get("vars")
    # dbt's and its adapter's own macros, installed beside dbt.
    installed = Path(dbt.__file__).resolve().parents[1]
    root = manifest.metadata.project_name
    read_by_semantics = {
        manifest.nodes[uid].name
        for model in manifest.semantic_models.values()
        for uid in model.depends_on.nodes
        if uid in manifest.nodes
    } | {node.name for node in manifest.nodes.values() if getattr(node, "time_spine", None)}
    refused = [
        f"{macro.original_file_path}: macro {macro.name} decides relation names, which the gate"
        " does not compare"
        for macro in manifest.macros.values()
        if macro.package_name == root and macro.name in RELATION_MACROS
    ]
    for file in manifest.files.values():
        if not isinstance(file.path, FilePath):
            continue
        found_at = Path(file.path.full_path).resolve()
        if found_at.is_relative_to(installed):
            continue
        if not found_at.is_relative_to(tree):
            refused.append("dbt read a file outside the tree")
            continue
        if not isinstance(file, SchemaSourceFile):
            continue
        path = found_at.relative_to(tree).as_posix()
        for key, entries in (file.dict_from_yaml or {}).items():
            if key not in SEMANTIC_KEYS and key not in READ_KEYS:
                continue
            renders = SchemaYamlRenderer({}, key).should_render_keypath
            for entry in entries if isinstance(entries, list) else []:
                name = entry.get("name") if isinstance(entry, dict) else None
                if key in READ_KEYS and name not in read_by_semantics:
                    continue
                for place in jinja_at(key, entry, renders):
                    refused.append(f"{path}: {key} {name} holds dbt Jinja at {place}")
    report.write_text(json.dumps({"refused": sorted(set(refused)), "vars": project_vars}))
    return 0


def failure(error: object, project: Path, tree: Path) -> str:
    """What the gate says of dbt's failure: the error's class and, when dbt names one inside the
    tree, the file's path in the tree; never dbt's own text."""
    said = type(error).__name__ if error is not None else "it reported errors"
    node = getattr(error, "node", None)
    original = getattr(node, "original_file_path", None)
    if isinstance(original, str) and not posixpath.isabs(original):
        place = (project / original).resolve()
        if place.is_relative_to(tree):
            said += f" in {place.relative_to(tree).as_posix()}"
    return said


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


def definitions(manifest: Block, project_vars: object = None) -> dict[str, Metric]:
    """Every metric of a semantic manifest dbt wrote, with its definition; every definition holds
    the project file's `vars`."""
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
            "vars": normal(project_vars),
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
        if current not in built:
            raise Unreadable(f"metric {name} is built from metric {current}, which is not defined")
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


def read(tree: Path, work: Path, which: str) -> dict[str, Metric]:
    """Every metric dbt reads from the exported tree's project, with its definition; Unreadable
    when the tree cannot be parsed or holds what the gate refuses."""
    parsed = parse(tree, work, which)
    return definitions(parsed.manifest, parsed.vars)


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
    report = f"metric-versions: {len(published)} metrics compared with {base} keep their versions"
    if changed:
        report += f"; changed with a new version: {', '.join(changed)}"
    new = sorted(current.keys() - published.keys())
    return True, report + (f"; new: {', '.join(new)}" if new else "")


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
            has_project = export_commit(repo, commit, tree)
            _PUBLISHED[key] = read(tree, work, f"{base}'s") if has_project else {}
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
            current = read(tree, Path(scratch) / "work", "working")
        except Unreadable as e:
            return False, f"metric-versions: the working tree's metrics cannot be read: {e}"
    return compare(published, current, base)


def stop(signum: int, _frame: object) -> None:
    """A termination ends the run as an exit does, so its temporary folders are removed."""
    sys.exit(128 + signum)


def main(argv: list[str], run: Callable[[], tuple[bool, str]] = check) -> int:
    if argv[:1] == ["--parse"] and len(argv) == 3:
        return parse_and_refuse(Path(argv[1]), Path(argv[2]))
    signal.signal(signal.SIGTERM, stop)
    try:
        ok, report = run()
    except GateError as e:
        print(f"metric-versions: {e}", file=sys.stderr)
        return 1
    print(report, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
