"""`make metric-versions`: fail when a metric's definition changes against `origin/main` under the
same version, or a metric loses its version or its owner.

Every metric carries `version` in its `config.meta`, beside its owner, term and role: an integer
from 1 that only goes up. The gate compares dbt's own parse of two trees:

- `origin/main`'s tree, read from git's objects as stored (`git ls-tree` and `git cat-file`, so no
  export attribute leaves a file out or rewrites one);
- the working tree's tracked and new files, not the ignored ones;

of each, only the dbt project folder (`models/`) and the folders its project file's `*-paths`
settings name are copied into a temporary folder and parsed with `dbt parse` on the local target,
with the network refused, under the same fixed environment for both but for the paths of each
parse's own temporary folder (nothing of the caller's environment but `PATH`).

A metric's definition, which its version names, is built from the semantic manifest dbt writes and
the project file's `vars` as dbt loads them:

- its own entry, all but its version and its owner;
- every metric it is built from, transitively: every metric input in its type parameters (a
  ratio's numerator and denominator, a derived metric's inputs, and any other kind's) and every
  metric a filter in it reads (`{{ Metric('x', ...) }}`), found by MetricFlow's own model of the
  manifest and parser of filters;
- the semantic model of each measure those metrics resolve to and of each semantic model a metric
  aggregates directly, with the semantic models they join through a shared entity name: their
  relation, entities, dimensions, defaults and other keys, of their measures only the ones
  resolved to, and the `schema` config of the node each reads (its BigQuery dataset, below);
- the project's time spines, with the `schema` config of each time spine's node;
- the project file's whole `vars` block, so a var changed means every metric's version goes up.

Left out of it: the wording of a component (the `description` and `label` of an input metric, a
measure, a dimension, an entity or a semantic model), an input metric's `meta`, and the file and
lines dbt records an entry came from; each string is compared without the whitespace at its ends.

Outside the definition, so a change there is not seen by the gate:

- the SQL of the dbt models a semantic model reads (caught, if at all, by the models' tests, the
  payments data contract's gate and the values pinned over the landed build);
- command-line `--vars` a consumer passes to dbt;
- relation names decided by the profile, or on a target other than the local one, but for what
  the routing below holds: the gate compares the local target's parse;
- dbt packages, which the gate refuses rather than reads.

The ways a project's macros can decide relation names that the gate refuses:

- a project macro named `generate_alias_name`, `generate_schema_name` or `generate_database_name`,
  or one of those after a `<prefix>__`, such as `default__generate_alias_name` or
  `duckdb__generate_schema_name`, which dbt's own versions of the three dispatch to, except one
  `generate_schema_name` whose macro block is the gate's own text (`SCHEMA_MACRO`) character for
  character: on BigQuery it returns a node's `schema` config trimmed of surrounding whitespace,
  else the profile's dataset, and on any other target the profile's schema, reading nothing but
  `target.type`, `target.schema` and that config, and calling no macro;
- a project file `dispatch` entry for the `dbt` macro namespace, which those three dispatch in, or
  a `dispatch` setting the gate cannot read as entries for other namespaces.

The routing, which the gate holds (`ROUTES`: a folder of the project mapped to a BigQuery dataset
and the relation names the folder holds; a test holds each to a data product's dataset in
platform/gcp/warehouse.tf and to its `tables` in access/personas.yaml), and the rules the project's
nodes are held to. The placement configs (`PLACEMENT`) are `schema`, `alias`, `enabled`,
`database`, a snapshot's `target_schema` and `target_database`, and dbt-bigquery's names `dataset`,
`project`, `target_dataset` and `target_project`, which dbt translates only on a BigQuery target.

- On either tree, for each model, seed, snapshot and data test of the project, enabled or not, as
  the local parse records it (it runs only the `config()` calls the local target reaches, and
  records a generic test's YAML config already rendered, so those are left to the outcome below):
  - a placement config set in a `config()` call (a node's SQL, or a generic test's own `config`,
    which dbt records the same way) is refused, but a data test's `alias`, which dbt's generic
    tests set there themselves;
  - a placement config whose rendered value is not the value dbt records as written (dbt Jinja,
    `none` included, or a later setting that overrides it) is refused;
  - `database`, `target_schema`, `target_database` and the four dbt-bigquery names, written with a
    value, are refused, and on a data test also `schema`, which places its stored failures;
  - a model, seed or snapshot with a `schema` where the project has no allowed schema macro is
    refused (dbt's own prefixes it with the profile's dataset).
- On either tree, a model, seed, snapshot or data test of the project that the local parse
  disables is refused (a disabled metric or semantic model is read as gone, as before).
- On either tree, Jinja in any file of the tree dbt reads that names what dbt knows of its own
  invocation (`INVOCATION`: `flags`, `invocation_args_dict`, `invocation_id`, and dbt's macros
  `should_full_refresh` and `should_store_failures`, which read a flag while the project is
  parsed) is refused: both parses run `dbt parse`, so a placement turning on the command or its
  flags would be read as a parse gives it, not as a real build's.
- On the working tree: a model, seed or snapshot's `schema` is the dataset `ROUTES` gives its
  folder, and none outside those folders; a routed folder's relation names (an alias, else the
  node's name) are its route's tables, each built once.
- On the working tree, the outcome: dbt-bigquery's own parse of the tree, run from the lock's
  `bigquery` group in uv's cache with the network refused, beside the local parse and under the
  same fixed environment, on each of two throwaway profiles (`PROBES`). Each is the tree's own
  BigQuery output of `models/profiles.yml`, its name and every literal setting kept (the dataset
  `intermediate` among them), with a stand-in token, an invented project and a free-tier region;
  the two differ in project and region, the first region being the lab's. Refused, on either: a
  model, seed, snapshot or function (a UDF, which `dbt build` creates as it does a model) of the
  project that lands anywhere but its folder's route, else the profile's dataset, of the profile's
  project; a data test placed anywhere but `dbt_test__audit` of the profile's project, where dbt
  would put its stored failures; a routed folder whose relations are not its route's tables, each
  once; a relation name holding a character other than a letter, a digit or `_` (dbt-bigquery
  quotes it unescaped); a node's `pre_hook`, `post_hook`, `sql_header` or `grants`, and an
  `on-run-*` hook of the project; a materialization the project defines; and a project macro
  that overrides one of dbt's or dbt-bigquery's own by name, directly or as `<prefix>__<name>`
  of one (the allowed schema macro aside), since no parse shows what such SQL and macros write.
  This sees what the local parse cannot: a generic test's YAML config read rendered, a `config()`
  call only the BigQuery target runs, a node enabled only there, whether they turn on the target's
  type, name or literal settings, or on its project or region.
- When the `bigquery` group is not in uv's cache, the gate fails and says to run `make sync`,
  which caches it; any other failure of the BigQuery parse, dbt-bigquery not importing among them,
  fails the gate with dbt's error class.

Stated limits of the routing: a placement config dbt-bigquery adds after the locked version is not
refused by the local rules until it is listed (a test compares the listed names with the locked
adapter's `_ALIASES`), though the outcome check still sees where it places a node; a placement that
turns on the environment, the clock or command-line `--vars` is read as the gate's fixed
environment gives it, not as a real build's, and the BigQuery output's settings the environment
renders (its method and token) as the probes set them (`PROBED`), not as a real build's; one that
reads the invocation by another name than `INVOCATION`'s is read as a parse gives it; one that
turns on the project or region being one
named value (the real project's id, which no file may hold, or a third region) is read as the two
probes give it; SQL a node's Jinja runs through `run_query` or a `statement` block when dbt
executes it (a parse does not), and a project macro that overrides `ref` or `source` (which dbt
provides as functions, not macros), are not read;
the macro's text is compared, its behaviour rests on dbt's `target` (a project macro named `target`
makes the pinned dbt's parse fail); and since both trees are read with the working tree's gate, a
change to `SCHEMA_MACRO` makes an `origin/main` holding the old text unreadable.

Stated limits of that refusal: it rests on dbt's default implementations of the three, in the dbt
the lock pins, calling no other macro (the database one asks the adapter for a model's catalog
integration, which the local DuckDB adapter offers none of); a macro dbt calls only when it runs
models, not when it parses them, such as `generate_latest_version_pointer_alias` (the name of an
extra view of a versioned model, not the relation a semantic model reads), is not refused.

The gate fails when:

- the clone holds no `origin/main`, or HEAD does not contain it (merge it first, so of two
  branches that bump one metric to the same version, the second is compared with the first);
- git cannot list `origin/main`'s tree or read its files (`ls-tree` or `cat-file` fails, as for
  an object a partial clone cannot fetch);
- either tree has its dbt project file somewhere other than `models/dbt_project.yml`;
- either tree holds, in what dbt reads, a symbolic link or a submodule; declares dbt packages
  (`packages.yml`, `dependencies.yml`); has a project file holding Jinja, or a path setting that is
  absolute or outside the tree; defines a macro that decides relation names or sets a `dispatch`
  entry for the `dbt` namespace (both above); breaks a rule of the routing (above); holds dbt
  Jinja where dbt renders it in a semantic model or metric entry, or in the entry of a model, seed
  or snapshot a semantic model reads or a time spine is (dbt renders that Jinja with the
  environment, command-line variables and the clock);
- dbt cannot parse either tree, or the working tree on the BigQuery target: the message names the
  tree and dbt's error class, never dbt's own output; a parse, local or BigQuery, that fails only
  by leaving a semantic model's `node_relation` unset, which a backward step of the wall clock
  during the parse causes, is run once more first, and the message says when the failure is that
  second parse's;
- the lock's `bigquery` group is not in uv's cache (above);
- a metric `origin/main` defines is gone from the working tree or renamed;
- a metric's definition changed and its version did not go up, or its version went down;
- a metric has no version that is an integer from 1 to 2^53 - 1, or no owner.

It passes a new metric, an unchanged one, a changed one with a higher version, a higher version
alone, and an owner changed alone. A metric `origin/main` defines without a version counts as
version 0 there, so the first versions pass; while `origin/main` holds no `dbt_project.yml` at all,
the first metrics pass.

The comparison reads `origin/main` from the clone, with no network, so it compares with the
`origin/main` last fetched: a stale one cannot be told apart offline, which is why the gates record
of a change is taken after merging the newest default branch into it. A termination signal ends a
run as an exit does, removing its temporary folders.
"""

import hashlib
import json
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from functools import partial
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
# The macros that decide a relation's name, which a project can make read the environment: a
# project macro of one of these names, or of one of them after `<prefix>__` (the `default__` and
# `<adapter>__` implementations dbt's own macros dispatch to), is refused.
RELATION_MACROS = ("generate_alias_name", "generate_schema_name", "generate_database_name")
# The one relation macro a project may define: `generate_schema_name` with exactly this text (dbt's
# parse of the macro block, from `{% macro` to `{%- endmacro %}`). On BigQuery it returns a node's
# `schema` config trimmed of surrounding whitespace, else the profile's dataset; on any other target
# the profile's schema.
SCHEMA_MACRO_NAME = "generate_schema_name"
SCHEMA_MACRO = (
    "{% macro generate_schema_name(custom_schema_name, node) -%}\n"
    "{%- if custom_schema_name is none or target.type != 'bigquery' -%}\n"
    "{{ target.schema }}\n"
    "{%- else -%}\n"
    "{{ custom_schema_name | trim }}\n"
    "{%- endif -%}\n"
    "{%- endmacro %}"
)


@dataclass(frozen=True)
class Route:
    """Where the models, seeds and snapshots of one folder are built on BigQuery: the dataset,
    and the relation names the folder holds, which are the data product's `tables`."""

    dataset: str
    tables: tuple[str, ...]


# The routing that macro implements, which the project file's `+schema` settings must match: each
# folder of the project (as dbt gives a node's path, relative to `models/`) mapped to the BigQuery
# dataset its models, seeds and snapshots are built in and the relations it holds. A node in no
# folder here has no `schema` config. A test holds each route to a data product's dataset in
# platform/gcp/warehouse.tf and to that product's `tables` in access/personas.yaml.
ROUTES = {
    "models/marts/group_totals": Route("group_totals", ("group_totals", "group_totals_left_out")),
}
# The kinds of node that are built as relations, whose routing the gate checks.
BUILT = ("model", "seed", "snapshot")
# Configs that would place a node outside the routing: another database (a BigQuery project), and
# a snapshot's own schema or database, which dbt uses in place of the schema macro.
UNROUTED = ("database", "target_schema", "target_database")
# dbt-bigquery's own names for the placement configs (`BigQueryCredentials._ALIASES`, which a test
# compares with the locked adapter's): dbt translates them only on a BigQuery target, so the local
# parse the gate reads keeps them under these names; each is refused. Written as pairs, which the
# fresh-clone scan does not read as a project setting.
BIGQUERY_ALIASES: dict[str, str] = dict(
    (
        ("dataset", "schema"),
        ("project", "database"),
        ("target_dataset", "target_schema"),
        ("target_project", "target_database"),
    )
)
# Every config key the gate refuses outright on a node, where dbt records it as written; a data
# test also may not set `schema`, which places its stored failures.
REFUSED_CONFIGS = (*UNROUTED, *BIGQUERY_ALIASES)
# The configs that decide whether and where a node is built: each, when a node sets it, must be the
# literal dbt renders, and none may be set in a `config()` call (a node's SQL, or a generic test's
# own `config`, which dbt records the same way), so the local parse places the node as BigQuery's.
PLACEMENT = ("schema", "alias", "enabled", *REFUSED_CONFIGS)
# The kinds of node held to the placement rules: the built ones, and data tests, whose stored
# failures are built as tables.
PLACED = (*BUILT, "test")
# The macro namespace dbt's own macros, the three above among them, dispatch in.
DBT_NAMESPACE = "dbt"
# The outcome check: dbt-bigquery's parse of the working tree, run in the lock's `bigquery` group
# from uv's cache with no network (`uv run --isolated --frozen --offline --only-group bigquery`,
# every proxy variable pointed at a port nothing listens on, `semantics/offline` on `PYTHONPATH`,
# whose `sitecustomize` ends the process at its first connection or name lookup), on a throwaway
# profile that names an invented project and a stand-in token.
BIGQUERY_GROUP = "bigquery"
CLOSED = "http://127.0.0.1:9"
PROXIES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
# What uv prints when `--offline` finds a package missing from its cache.
NOT_CACHED = "Network connectivity is disabled"
NOT_CACHED_ADVICE = (
    "the lock's bigquery group is not in uv's cache, so where the nodes land on BigQuery cannot be"
    " read: run `make sync`, which caches it"
)
# The two throwaway profiles the outcome check parses the working tree on: each is the tree's own
# `models/profiles.yml` BigQuery output, its name and every setting written as a literal kept, with
# an invented project, a stand-in token and one of the free-tier regions in place of what the real
# build reads from the environment. They differ in project and region (the first is the lab's,
# platform/gcp/variables.tf), so a placement that turns on either is off its route in one of them.
PROBES = (("probe-project", "us-central1"), ("probe-other-project", "us-east1"))
PROFILES_FILE = f"{PROJECT}/profiles.yml"
BIGQUERY_TYPE = "bigquery"
# What the real output reads from the environment, which each probe sets itself.
PROBED = {"method": "oauth-secrets", "token": "stand-in"}
# The dataset dbt places every data test in, where its stored failures would be written.
TEST_SCHEMA = "dbt_test__audit"
# The kinds of node the outcome check places: the placed ones, and dbt's functions (UDFs), which
# `dbt build` creates in a dataset as it does a model; an `operation` (an `on-run-*` hook) runs SQL
# no parse places, and is refused.
OUTCOME_KINDS = (*PLACED, "function")
OPERATION = "operation"
# A node's configs that run SQL besides its build, which no parse places (`grants` among them:
# dbt grants each listed principal on the relation): each is refused.
SIDE_SQL = ("pre_hook", "post_hook", "sql_header", "grants")
# The characters a relation name may hold on the outcome check, so that dbt-bigquery's quoted
# identifier is the relation the parse names and no more.
RELATION_NAME = re.compile(r"[A-Za-z0-9_]+")
# The prefix dbt gives a materialization's macro (`materialization_<name>_<adapter>`).
MATERIALIZATION = "materialization_"
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
# What Jinja can read of the dbt invocation itself, which differs between the gate's `dbt parse`
# and a real build (`flags.WHICH`, `--full-refresh`, `--store-failures` and the rest): dbt's
# `flags`, `invocation_args_dict` and `invocation_id`, and dbt's own macros that read a flag while a
# project is parsed. A placement turning on any of them is not the build's, so the gate refuses each
# read by name, in every file of the tree dbt reads.
INVOCATION = (
    "flags",
    "invocation_args_dict",
    "invocation_id",
    "should_full_refresh",
    "should_store_failures",
)
JINJA_SPAN = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
INVOCATION_READ = re.compile(r"\b(?:" + "|".join(INVOCATION) + r")\b")
# Git's modes for a file and an executable file; anything else (a link, a submodule) is refused.
FILE_MODES = ("100644", "100755")
DUCKDB_FILE = "ledger_lab.duckdb"
Block = dict[str, Any]


# dbt's error when its parse leaves a semantic model's `node_relation` unset: it skips setting it
# for a node whose wall-clock creation time is earlier than the parse's start, which a backward
# step of the clock during the parse makes so. The error alone: one validation error, of that
# field, for that reason. A text log breaks its lines; a JSON log writes the breaks as `\n`.
NODE_RELATION_UNSET = re.compile(
    r"(?<!\d)1 validation error for PydanticSemanticModel(?:\s|\\n)+node_relation(?:\s|\\n)+"
    r"none is not an allowed value \(type=type_error\.none\.not_allowed\)"
)


def node_relation_unset(output: str) -> bool:
    """Whether dbt's output, or its error's text, holds the error of an unset `node_relation`."""
    return NODE_RELATION_UNSET.search(output) is not None


# What a parse's failure message adds when it is the second parse's (`once_more`).
SECOND_PARSE = ", on a second parse after the first left a semantic model's node_relation unset"


def once_more[R](run: Callable[[], R], again: Callable[[R], bool]) -> tuple[R, bool]:
    """`run`'s result, from a second run when the first one's result is `again`'s, and whether it
    is the second run's: one more run at most, so a failure that repeats is reported."""
    first = run()
    if not again(first):
        return first, False
    return run(), True


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
    routes: Block


@dataclass(frozen=True)
class Landed:
    """Where dbt-bigquery's parse places one node of the project: its kind, name, path (relative
    to `models/`), place in the tree, project, dataset and relation name."""

    kind: str
    name: str
    path: str
    place: str
    database: str | None
    schema: str
    relation: str
    # The configs set on the node that run SQL of their own besides its build (`SIDE_SQL`).
    side: tuple[str, ...] = ()


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


def parse_command(project: Path, report: Path) -> list[str]:
    """The command of one parse: this file, in a process of its own (`parse_and_refuse`)."""
    return [
        sys.executable,
        "-I",
        str(Path(__file__).resolve()),
        "--parse",
        str(project),
        str(report),
    ]


def parse(tree: Path, work: Path, which: str, routed: bool = False) -> Parsed:
    """What dbt's parse of the exported tree gives; Unreadable when dbt cannot parse it or it
    holds what the gate refuses, and, when `routed`, a node off the gate's routing. A parse whose
    only error is an unset `node_relation` is run once more. The message never carries dbt's own
    output."""
    project = tree / PROJECT
    for folder in ("home", "tmp"):
        (work / folder).mkdir(parents=True, exist_ok=True)
    report = work / "report.json"

    def run() -> subprocess.CompletedProcess[bytes]:
        report.unlink(missing_ok=True)
        return subprocess.run(  # noqa: S603
            parse_command(project, report),
            capture_output=True,
            check=False,
            env=environment(work),
            cwd=work,
        )

    def unset(result: subprocess.CompletedProcess[bytes]) -> bool:
        if result.returncode != 0 or not report.is_file():
            return False
        return json.loads(report.read_text()).get("node_relation_unset") is True

    result, second = once_more(run, unset)
    retried = SECOND_PARSE if second else ""
    if result.returncode != 0 or not report.is_file():
        raise Unreadable(
            f"dbt could not parse the {which} tree (the parse ended with exit status"
            f" {result.returncode}){retried}"
        )
    found = json.loads(report.read_text())
    if found.get("error"):
        raise Unreadable(f"dbt could not parse the {which} tree ({found['error']}){retried}")
    refused = found["refused"] + (found["off_routes"] if routed else [])
    if refused:
        raise Unreadable("; ".join(refused))
    semantic = json.loads((work / "target" / "semantic_manifest.json").read_text())
    return Parsed(semantic, found["vars"], found["routes"])


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


def invocation_reads(path: str, text: str) -> list[str]:
    """What the gate refuses of a file's Jinja reading the dbt invocation (`INVOCATION`)."""
    read = sorted(
        {name for span in JINJA_SPAN.findall(text) for name in INVOCATION_READ.findall(span)}
    )
    return [
        f"{path}: its Jinja reads {name}, which differs between the gate's parse and a real build,"
        " which the gate refuses"
        for name in read
    ]


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
        error = failure(result.exception, project, tree)
        unset = node_relation_unset(str(result.exception))
        report.write_text(json.dumps({"error": error, "node_relation_unset": unset}))
        return 0
    project_file = load_yaml_text((project / "dbt_project.yml").read_text()) or {}
    project_vars = project_file.get("vars")
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
        f"{in_tree(project, macro.original_file_path, tree)}: macro {macro.name} decides relation"
        " names, which the gate does not compare"
        + (
            f", other than {SCHEMA_MACRO_NAME} with the gate's own text (semantics/versions.py)"
            if macro.name == SCHEMA_MACRO_NAME
            else ""
        )
        for macro in manifest.macros.values()
        if macro.package_name == root
        and names_relations(macro.name)
        and not allowed_macro(macro.name, macro.macro_sql)
    ]
    built = [
        node
        for node in manifest.nodes.values()
        if node.package_name == root and node.resource_type.value in BUILT
    ]
    macro_allowed = any(
        macro.package_name == root and allowed_macro(macro.name, macro.macro_sql)
        for macro in manifest.macros.values()
    )
    disabled = [
        node
        for versions_of in manifest.disabled.values()
        for node in versions_of
        if node.package_name == root and node.resource_type.value in PLACED
    ]
    refused += [
        f"{in_tree(project, node.original_file_path, tree)}: {node.resource_type.value}"
        f" {node.name} is disabled in the local parse, which the gate does not read"
        for node in disabled
    ]
    for node in [*manifest.nodes.values(), *disabled]:
        if node.package_name != root or node.resource_type.value not in PLACED:
            continue
        kind = node.resource_type.value
        what = f"{in_tree(project, node.original_file_path, tree)}: {kind} {node.name}"
        refused += unroutable(
            what,
            kind,
            {key: node.config.get(key) for key in PLACEMENT},
            dict(node.unrendered_config),
            dict(getattr(node, "config_call_dict", None) or {}),
            macro_allowed,
        )
    off_routes: list[str] = []
    for node in built:
        what = f"{in_tree(project, node.original_file_path, tree)}: {node.resource_type.value}"
        what += f" {node.name}"
        off_routes += off_route(what, node.original_file_path, node.config.schema)
    off_routes += off_tables(
        [
            (
                node.original_file_path,
                in_tree(project, node.original_file_path, tree),
                node.alias or node.name,
            )
            for node in built
        ]
    )
    schema_of = {node.unique_id: node.config.schema for node in built}
    routes = {
        "semantic_models": {
            model.name: next(
                (schema_of[uid] for uid in model.depends_on.nodes if uid in schema_of), None
            )
            for model in manifest.semantic_models.values()
        },
        "time_spines": {
            node.name: node.config.schema
            for node in built
            if getattr(node, "time_spine", None) is not None
        },
    }
    if routes_dbt_macros(project_file.get("dispatch")):
        refused.append(
            f"{PROJECT_FILE}'s dispatch sets where dbt's own macros are found, among them the"
            " ones that decide relation names, which the gate does not compare"
        )
    for file in manifest.files.values():
        if not isinstance(file.path, FilePath):
            continue
        found_at = Path(file.path.full_path).resolve()
        if found_at.is_relative_to(installed):
            continue
        if not found_at.is_relative_to(tree):
            # A second guard: the path settings checked before the parse keep dbt in the tree.
            refused.append("dbt read a file outside the tree")
            continue
        path = found_at.relative_to(tree).as_posix()
        refused += invocation_reads(path, found_at.read_text(errors="replace"))
        if not isinstance(file, SchemaSourceFile):
            continue
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
    report.write_text(
        json.dumps(
            {
                "refused": sorted(set(refused)),
                "off_routes": sorted(off_routes),
                "vars": project_vars,
                "routes": routes,
            }
        )
    )
    return 0


def offline_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment of a command in the `bigquery` group: the caller's, which uv needs to find
    its cache, with every proxy variable pointed at `CLOSED` and `semantics/offline` on
    `PYTHONPATH`."""
    env = dict(os.environ) | {name: CLOSED for name in PROXIES}
    env |= {"NO_PROXY": "", "no_proxy": "", "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
    env["PYTHONPATH"] = str(HERE / "offline")
    return env | (extra or {})


def offline_command(*command: str) -> list[str]:
    uv = os.environ.get("UV") or shutil.which("uv") or "uv"
    return [
        uv,
        "run",
        "--isolated",
        "--frozen",
        "--offline",
        "--only-group",
        BIGQUERY_GROUP,
        *command,
    ]


def offline_run(
    *command: str, extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """A command run in the `bigquery` group from uv's cache, with no network."""
    return subprocess.run(  # noqa: S603
        offline_command(*command),
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
        env=offline_environment(extra),
    )


def group_failure(stderr: str) -> str | None:
    """Why the `bigquery` group could not run, from what the command wrote, or None when the
    failure is not the group's: its packages are not in uv's cache."""
    if NOT_CACHED in stderr:
        return NOT_CACHED_ADVICE
    return None


def probe_profile(tree: Path, project: str, location: str) -> Block:
    """The throwaway profile of one probe: the tree's own BigQuery output of `models/profiles.yml`
    (the one output whose `type` is the literal `bigquery`, of the profile `dbt_project.yml`
    names), as its target, with its name and every literal setting kept and `PROBED`, `project` and
    `location` in place of the rest; Unreadable when there is no such output, or its `dataset` is
    not a literal."""
    try:
        name = yaml.safe_load((tree / PROJECT_FILE).read_text())["profile"]
        outputs = yaml.safe_load((tree / PROFILES_FILE).read_text())[name]["outputs"]
    except (OSError, yaml.YAMLError, KeyError, TypeError) as e:
        raise Unreadable(f"{PROFILES_FILE} names no outputs for the project's profile") from e
    found = [
        key
        for key, out in outputs.items()
        if isinstance(out, dict) and out.get("type") == BIGQUERY_TYPE
    ]
    if len(found) != 1:
        raise Unreadable(f"{PROFILES_FILE} holds {len(found)} outputs of type bigquery, not one")
    (output,) = found
    literal = {
        key: value
        for key, value in outputs[output].items()
        if not (isinstance(value, str) and any(mark in value for mark in JINJA))
    }
    if not isinstance(literal.get("dataset"), str):
        raise Unreadable(f"{PROFILES_FILE}'s {output} output has no literal dataset")
    target = literal | PROBED | {"project": project, "location": location}
    return {name: {"target": output, "outputs": {output: target}}}


def bigquery_command(project: Path, work: Path) -> list[str]:
    """The command of one BigQuery parse: this file, in the `bigquery` group
    (`parse_on_bigquery`)."""
    return offline_command(
        "python", str(Path(__file__).resolve()), "--bigquery-parse", str(project), str(work)
    )


def start_bigquery(
    tree: Path, work: Path, probe: tuple[str, str] = PROBES[0]
) -> subprocess.Popen[str]:
    """dbt-bigquery's parse of the exported tree (`parse_on_bigquery`) on one probe's profile
    (`probe_profile`), started in a process group of its own, so it runs beside the local parse and
    can be ended whole; Unreadable when the tree's profile has no BigQuery output to probe."""
    (work / "profiles").mkdir(parents=True, exist_ok=True)
    profile = probe_profile(tree, *probe)
    (work / "profiles" / "profiles.yml").write_text(yaml.safe_dump(profile))
    return subprocess.Popen(  # noqa: S603
        bigquery_command(tree / PROJECT, work),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO,
        env=offline_environment(),
        start_new_session=True,
    )


def end(child: subprocess.Popen[str]) -> None:
    """The BigQuery parse ended, with every process it started, if it still runs."""
    if child.poll() is None:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()


@dataclass(frozen=True)
class Finished:
    """A finished BigQuery parse: the report it wrote (None when it wrote none or failed), its
    exit status and its standard error."""

    found: Block | None
    status: int
    stderr: str


def bigquery_report(child: subprocess.Popen[str], work: Path) -> Finished:
    """The BigQuery parse `child`, waited for."""
    _, stderr = child.communicate()
    report = work / "report.json"
    ok = child.returncode == 0 and report.is_file()
    found = json.loads(report.read_text()) if ok else None
    return Finished(found, child.returncode, stderr or "")


def bigquery_refusals(
    child: subprocess.Popen[str], work: Path, restart: Callable[[], subprocess.Popen[str]]
) -> list[str]:
    """What the BigQuery parse found off the gate's routing, each marked as BigQuery's; GateError
    when the `bigquery` group cannot run, Unreadable when dbt cannot parse the tree on BigQuery. A
    parse whose only error is an unset `node_relation` is run once more, by `restart`. The message
    never carries dbt's own output."""
    started = [child]

    def run() -> Finished:
        if started:
            return bigquery_report(started.pop(), work)
        (work / "report.json").unlink(missing_ok=True)
        again = restart()
        try:
            return bigquery_report(again, work)
        finally:
            end(again)

    def unset(done: Finished) -> bool:
        return done.found is not None and done.found.get("node_relation_unset") is True

    done, second = once_more(run, unset)
    retried = SECOND_PARSE if second else ""
    found = done.found
    if found is None:
        reason = group_failure(done.stderr)
        if reason:
            raise GateError(reason)
        raise Unreadable(
            "dbt could not parse the working tree on a BigQuery target (the parse ended with exit"
            f" status {done.status}){retried}"
        )
    if found.get("error"):
        raise Unreadable(
            f"dbt could not parse the working tree on a BigQuery target ({found['error']}){retried}"
        )
    return [f"on BigQuery, {line}" for line in found["refused"]]


def parse_on_bigquery(project: Path, work: Path) -> int:
    """Run in the `bigquery` group (`start_bigquery`): `dbt parse` of the project on the probe's
    profile in `work/profiles`, under the gate's fixed environment (`environment`) with every proxy
    variable pointed at `CLOSED`, writing to
    `work/report.json` dbt's error class when it fails, else where each node of the project lands
    (`landed`) and what lands off the routing (`placed_off`)."""
    fixed = environment(work) | {name: CLOSED for name in PROXIES}
    os.environ.clear()
    os.environ.update(fixed)
    for folder in ("home", "tmp"):
        (work / folder).mkdir(parents=True, exist_ok=True)
    profile = yaml.safe_load((work / "profiles" / "profiles.yml").read_text())
    ((_, chosen),) = profile.items()
    output = chosen["outputs"][chosen["target"]]
    from dbt.cli.main import dbtRunner
    from dbt.contracts.graph.manifest import Manifest

    tree = project.resolve().parent
    args = ["parse", "--no-partial-parse", "--no-use-colors", "--quiet", "--project-dir"]
    result = dbtRunner().invoke([*args, str(project), "--profiles-dir", str(work / "profiles")])
    manifest = result.result
    report = work / "report.json"
    if not result.success or not isinstance(manifest, Manifest):
        error = failure(result.exception, project, tree)
        unset = node_relation_unset(str(result.exception))
        report.write_text(json.dumps({"error": error, "node_relation_unset": unset}))
        return 0
    root = manifest.metadata.project_name
    nodes = [
        Landed(
            node.resource_type.value,
            node.name,
            node.original_file_path,
            in_tree(project, node.original_file_path, tree),
            node.database,
            node.schema,
            node.alias or node.name,
            tuple(key for key in SIDE_SQL if side_sql(node.config, key)),
        )
        for node in [*manifest.nodes.values(), *manifest.functions.values()]
        if node.package_name == root and node.resource_type.value in OUTCOME_KINDS
    ]
    operations = [
        in_tree(project, node.original_file_path, tree)
        for node in manifest.nodes.values()
        if node.package_name == root and node.resource_type.value == OPERATION
    ]
    macros = [
        (macro.name, in_tree(project, macro.original_file_path, tree), macro.macro_sql)
        for macro in manifest.macros.values()
        if macro.package_name == root
    ]
    installed = {macro.name for macro in manifest.macros.values() if macro.package_name != root}
    landed = {f"{n.kind} {n.name}": [n.database, n.schema] for n in nodes}
    refused = placed_off(nodes, macros, installed, output["project"], output["dataset"])
    refused += [
        f"{place}: an on-run hook of the project runs SQL no parse places, which the gate refuses"
        for place in sorted(set(operations))
    ]
    report.write_text(json.dumps({"refused": refused, "landed": landed}))
    return 0


def side_sql(config: Any, key: str) -> bool:
    """Whether a node's config sets `key` (a hook list or a SQL header) to anything."""
    value = getattr(config, key, None)
    if value is None and hasattr(config, "get"):
        value = config.get(key)
    return bool(value)


def unprefixed(macro: str) -> str:
    """A macro's name without the `<prefix>__` of a dispatched implementation."""
    return macro.split("__", 1)[-1]


def placed_off(
    nodes: list[Landed],
    macros: list[tuple[str, str, str]],
    installed: Collection[str],
    project: str,
    dataset: str,
) -> list[str]:
    """What dbt-bigquery's parse of the working tree places off the gate's routing: a model, seed
    or snapshot outside its route's dataset (`ROUTES`, else the profile's `dataset`) or the
    profile's `project`; a data test outside `TEST_SCHEMA` or that project; a routed folder whose
    relations are not its route's tables; and a macro of the project that is a materialization,
    or that overrides one of the `installed` macros of dbt and dbt-bigquery by name (directly, or
    as `<prefix>__<name>` of one), whose writes no parse shows, but the allowed schema macro.
    `macros` holds each project macro's name, place in the tree and text."""
    found: list[str] = []
    bases = {unprefixed(name) for name in installed}
    for name, place, text in macros:
        if allowed_macro(name, text):
            continue
        if name.startswith(MATERIALIZATION):
            found.append(
                f"{place}: macro {name} is a materialization of the project, whose writes no"
                " parse shows, which the gate refuses"
            )
        elif unprefixed(name) in bases:
            found.append(
                f"{place}: macro {name} overrides one of dbt's or dbt-bigquery's own macros, whose"
                " writes no parse shows, which the gate refuses"
            )
    built: list[tuple[str, str, str]] = []
    for node in nodes:
        where = node.schema
        if node.database != project:
            where += f" of the project {node.database}"
        what = f"{node.place}: {node.kind} {node.name}"
        found += [
            f"{what} sets {key}, which runs SQL no parse places, which the gate refuses"
            for key in node.side
        ]
        if not RELATION_NAME.fullmatch(node.relation):
            found.append(
                f"{what} names its relation {node.relation!r}, which holds a character other than"
                " a letter, a digit or `_`, which the gate refuses"
            )
        if node.kind == "test":
            if (node.database, node.schema) != (project, TEST_SCHEMA):
                found.append(
                    f"{what} is placed in {where}, where the gate holds a data test to"
                    f" {TEST_SCHEMA} of the profile's project"
                )
            continue
        built.append((node.path, node.place, node.relation))
        expected = route(node.path) or dataset
        if (node.database, node.schema) != (project, expected):
            found.append(
                f"{what} lands in {where}, where the gate's routing (semantics/versions.py"
                f" ROUTES) builds it in {expected} of the profile's project"
            )
    return sorted(set(found + off_tables(built)))


def allowed_macro(name: str, text: str) -> bool:
    """Whether a project macro that decides relation names is the one the gate allows: named
    `generate_schema_name`, its macro block exactly `SCHEMA_MACRO`, character for character."""
    return name == SCHEMA_MACRO_NAME and text == SCHEMA_MACRO


def folder_of(path: str) -> str | None:
    """The folder of `ROUTES` that holds a node at this path (relative to `models/`), or None
    when no folder, or more than one, does."""
    found = [folder for folder in ROUTES if path == folder or path.startswith(folder + "/")]
    return found[0] if len(found) == 1 else None


def route(path: str) -> str | None:
    """The dataset `ROUTES` builds a node at this path (relative to `models/`) in, or None for
    the profile's dataset."""
    folder = folder_of(path)
    return ROUTES[folder].dataset if folder else None


def unroutable(
    what: str, kind: str, config: Block, unrendered: Block, in_call: Block, macro_allowed: bool
) -> list[str]:
    """Why a node's placement cannot be read from its parse, on either tree, or nothing. `kind` is
    its resource type, `config` its rendered `PLACEMENT` configs, `unrendered` the config dbt
    records as written (the project file, properties files and `config()` calls), and `in_call`
    what a `config()` call set. Refused: a `PLACEMENT` key in a `config()` call (but a data test's
    `alias`, which dbt's generic tests set there themselves); a `PLACEMENT` key whose rendered value
    is not its written value (dbt Jinja, or a later setting that overrides it); a key of
    `REFUSED_CONFIGS`, and on a data test `schema`, written anywhere; and on a model, seed or
    snapshot, a `schema` where the project has no allowed schema macro (dbt's own prefixes it with
    the profile's dataset)."""
    found: list[str] = []
    test = kind == "test"
    for key in PLACEMENT:
        if key in in_call and not (test and key == "alias"):
            found.append(
                f"{what} sets its {key} in a config() call (in its SQL, or a generic test's own"
                " config), which the gate does not read"
            )
        if key in unrendered and config.get(key) != unrendered[key]:
            found.append(
                f"{what} sets its {key} other than as the literal dbt renders (dbt Jinja, or a"
                " setting it overrides), which the gate does not compare"
            )
    for key in (*REFUSED_CONFIGS, *(("schema",) if test else ())):
        if unrendered.get(key) is None:
            continue
        alias = BIGQUERY_ALIASES.get(key)
        named = f" (dbt-bigquery's name for {alias}, which the local parse does not read)"
        found.append(
            f"{what} sets {key}{named if alias else ''}, which places it outside the gate's routing"
        )
    if not test and config.get("schema") is not None and not macro_allowed:
        found.append(
            f"{what} sets a schema while the project has no {SCHEMA_MACRO_NAME} with the gate's"
            " own text, so dbt would prefix it with the profile's dataset"
        )
    return found


def off_tables(nodes: list[tuple[str, str, str]]) -> list[str]:
    """The working tree's routed folders whose relations are not their route's `tables`: a node
    in such a folder whose relation name (its alias, else its name) is not one of them, and a
    table none of them builds, or more than one does. `nodes` holds each built node's path, its
    place in the tree and its relation name."""
    found: list[str] = []
    held: dict[str, list[str]] = {folder: [] for folder in ROUTES}
    for path, place, relation in sorted(nodes):
        folder = folder_of(path)
        if folder is None:
            continue
        held[folder].append(relation)
        if relation not in ROUTES[folder].tables:
            found.append(
                f"{place}: builds {relation} in {folder}, which the gate's routing"
                f" (semantics/versions.py ROUTES) holds to {', '.join(ROUTES[folder].tables)}"
            )
    for folder, relations in held.items():
        for table in ROUTES[folder].tables:
            if relations.count(table) != 1:
                found.append(
                    f"{folder} builds {table} {relations.count(table)} times, where the gate's"
                    " routing (semantics/versions.py ROUTES) holds it to once"
                )
    return found


def off_route(what: str, path: str, schema: object) -> list[str]:
    """A built node of the working tree whose `schema` config is not the one `ROUTES` gives its
    path (none outside the routes), or nothing."""
    expected = route(path)
    if schema == expected:
        return []
    where = f"schema {expected}" if expected else "the profile's dataset"
    return [
        f"{what} sets schema {schema!r}, where the gate's routing (semantics/versions.py ROUTES)"
        f" builds it in {where}"
    ]


def names_relations(macro: str) -> bool:
    """Whether a project macro of this name decides relation names: one of `RELATION_MACROS`, or
    one of them after a `<prefix>__`, which dbt's dispatch of them can reach."""
    return any(macro == name or macro.endswith(f"__{name}") for name in RELATION_MACROS)


def routes_dbt_macros(dispatch: object) -> bool:
    """Whether the project file's `dispatch` setting holds an entry for dbt's own macro
    namespace, or one the gate cannot read as an entry for another namespace."""
    if dispatch is None:
        return False
    if not isinstance(dispatch, list):
        return True
    return any(
        not isinstance(entry, dict) or entry.get("macro_namespace") in (DBT_NAMESPACE, None)
        for entry in dispatch
    )


def in_tree(project: Path, original: str, tree: Path) -> str:
    """A path dbt gives relative to the project, as a path in the tree, or a mark of none."""
    place = (project / original).resolve()
    return (
        place.relative_to(tree).as_posix() if place.is_relative_to(tree) else "(outside the tree)"
    )


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


def definitions(
    manifest: Block, project_vars: object = None, routes: Block | None = None
) -> dict[str, Metric]:
    """Every metric of a semantic manifest dbt wrote, with its definition; every definition holds
    the project file's `vars`, and the `schema` config (`routes`, from the parse) of the node each
    semantic model it reads and each time spine is built from."""
    routes = routes or {}
    model_routes = routes.get("semantic_models") or {}
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
    time["routes"] = normal(routes.get("time_spines") or {})
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
            "semantic_models": semantic(closure, built, measures, raw_models, model_routes),
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
    closure: set[str],
    built: dict[str, Parts],
    measures: set[str],
    models: dict[str, Block],
    routes: Block,
) -> dict[str, Block]:
    """The semantic models the metrics read, with every one they join through a shared entity,
    each with only the measures resolved to and the `schema` config of the node it reads."""
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
        model["route"] = normal(routes.get(name))
        found[name] = model
    return found


def read(tree: Path, work: Path, which: str, routed: bool = False) -> dict[str, Metric]:
    """Every metric dbt reads from the exported tree's project, with its definition; Unreadable
    when the tree cannot be parsed or holds what the gate refuses."""
    parsed = parse(tree, work, which, routed)
    return definitions(parsed.manifest, parsed.vars, parsed.routes)


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
        children: list[tuple[subprocess.Popen[str], Path, tuple[str, str]]] = []
        try:
            export_working(repo, tree)
            for n, probe in enumerate(PROBES):
                work = Path(scratch) / f"bigquery-{n}"
                children.append((start_bigquery(tree, work, probe), work, probe))
            current = read(tree, Path(scratch) / "work", "working", routed=True)
            refused = sorted(
                {
                    line
                    for child, folder, probe in children
                    for line in bigquery_refusals(
                        child, folder, partial(start_bigquery, tree, folder, probe)
                    )
                }
            )
            if refused:
                raise Unreadable("; ".join(refused))
        except Unreadable as e:
            return False, f"metric-versions: the working tree's metrics cannot be read: {e}"
        finally:
            for child, _, _ in children:
                end(child)
    return compare(published, current, base)


def stop(signum: int, _frame: object) -> None:
    """A termination ends the run as an exit does, so its temporary folders are removed."""
    sys.exit(128 + signum)


def main(argv: list[str], run: Callable[[], tuple[bool, str]] = check) -> int:
    if argv[:1] == ["--parse"] and len(argv) == 3:
        return parse_and_refuse(Path(argv[1]), Path(argv[2]))
    if argv[:1] == ["--bigquery-parse"] and len(argv) == 3:
        return parse_on_bigquery(Path(argv[1]), Path(argv[2]))
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
