"""`python -m semantics.export`: write the metric context, `semantics/context.json`, the one file
the agent and the catalog read the governed metrics, the glossary and the verified queries from.

The metrics come from dbt's own parse of the tree, the one the metric-versions gate runs
(`semantics/versions.py`: the files dbt reads copied out with `export_working`, then `parse`, with
the network refused and a fixed environment), so a metric's version, owner, term and role in the
export are its `config.meta` as dbt reads it. The glossary comes from `semantics/glossary.yaml` and
the verified queries from `semantics/verified_queries.yaml`, each checked against its schema.

What the file holds:

- `metrics`, by name: each metric's label, description (without the whitespace at its ends), type,
  version, owner, glossary term and role, its own type parameters and filter as dbt parsed them
  (the measure, numerator and denominator, the expression and input metrics; the measures dbt
  resolves through input metrics, `input_measures`, left out), its time granularity if set, and the
  verified queries that answer it (id, question, grain, filter and SQL), sorted by id;
- `glossary`, by term id: each term's name, definition, owner and related terms.

Null values and empty lists and mappings are left out. The file is JSON with sorted keys, two
spaces of indentation and a final newline; it holds no timestamp, and no path but what the
definitions themselves say.

The export refuses to write, naming each problem, when:

- dbt cannot parse the tree, or the tree holds what the metric-versions gate refuses;
- the glossary or the verified queries break their schema;
- a metric's glossary term is not in the glossary;
- a verified query's metric is not defined, or its version or owner is not the metric's.

`--check` writes nothing: it compares a fresh export with the committed file, byte for byte, and
fails naming each entry, by section and name, that differs from a fresh export, that only a fresh
export holds, or that only the committed file holds, and, when the entries agree but the text
differs, saying so; a missing file, one that is not JSON and one that is not a JSON object fail
too.

Stated limits:

- a metric's `config.meta` keys other than version, owner, term and role are not exported;
- the definitions of measures, dimensions and semantic models are not exported, so a change there
  shows in the export only when it changes a metric's own entry (its version, among others);
- the export does not judge versions: the metric-versions gate does (`make metric-versions`);
- whether a verified query's SQL agrees with `mf` is checked by
  `tests/semantics/test_verified_queries.py`, not here;
- the check compares with the working tree's definitions, not with `origin/main`'s.
"""

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from semantics import versions

REPO = Path(__file__).resolve().parents[1]
CONTEXT = Path("semantics") / "context.json"
GLOSSARY = Path("semantics") / "glossary"
VERIFIED = Path("semantics") / "verified_queries"
# The keys of a metric's `config.meta` the export carries.
META = ("version", "owner", "term", "role")
# A parsed metric's own keys the export carries, beside its `config.meta`.
OWN = ("label", "type", "type_params", "filter", "time_granularity")
# The measures dbt resolves through a metric's inputs: theirs, not the metric's own.
RESOLVED = {"input_measures"}
Block = dict[str, Any]


class ExportError(Exception):
    """The export cannot be written; the message names each problem."""


def pruned(value: Any) -> Any:
    """The value with every null, empty list and empty mapping left out, at every depth."""
    if isinstance(value, dict):
        kept = {str(k): pruned(v) for k, v in value.items() if k not in RESOLVED}
        return {k: v for k, v in kept.items() if v not in (None, [], {})}
    if isinstance(value, list):
        return [pruned(v) for v in value]
    return value


def checked(repo: Path, stem: Path) -> Block:
    """The YAML file `<stem>.yaml` of the tree, checked against `<stem>.schema.json`."""
    data = repo / stem.with_suffix(".yaml")
    schema = json.loads((repo / stem.with_suffix(".schema.json")).read_text())
    doc = yaml.safe_load(data.read_text())
    errors = sorted(
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in Draft202012Validator(schema).iter_errors(doc)
    )
    if errors:
        raise ExportError(f"{stem.with_suffix('.yaml')} breaks its schema: {'; '.join(errors)}")
    return doc


def parsed_metrics(repo: Path) -> list[Block]:
    """The metrics of the semantic manifest dbt's parse of the tree writes, as the metric-versions
    gate parses it."""
    with tempfile.TemporaryDirectory(prefix="metric-context-") as scratch:
        tree = Path(scratch) / "tree"
        tree.mkdir()
        try:
            versions.export_working(repo, tree)
            manifest = versions.parse(tree, Path(scratch), "working").manifest
        except versions.Unreadable as e:
            raise ExportError(f"the metrics cannot be read: {e}") from e
    return manifest.get("metrics") or []


def build(metrics: list[Block], glossary: Block, verified: Block) -> Block:
    """The context from the parsed metrics, the glossary and the verified queries; ExportError
    naming each inconsistency between them."""
    terms = {t["id"]: t for t in glossary["terms"]}
    problems: list[str] = []
    entries: dict[str, Block] = {}
    for metric in metrics:
        name = metric["name"]
        meta = (metric.get("config") or {}).get("meta") or {}
        entry = {k: metric.get(k) for k in OWN}
        description = metric.get("description")
        entry["description"] = description.strip() if isinstance(description, str) else None
        entry |= {k: meta.get(k) for k in META}
        if meta.get("term") not in terms:
            problems.append(f"metric {name}'s term {meta.get('term')} is not in the glossary")
        entry["verified_queries"] = []
        entries[name] = entry
    for query in sorted(verified["verified_queries"], key=lambda q: q["id"]):
        qid, name = query["id"], query["metric"]
        entry = entries.get(name)
        if entry is None:
            problems.append(f"verified query {qid}'s metric {name} is not defined")
            continue
        for key in ("version", "owner"):
            if query[key] != entry[key]:
                problems.append(
                    f"verified query {qid} has {key} {query[key]}, but {name} has {entry[key]}"
                )
        said = ("id", "question", "group_by", "where", "sql")
        entry["verified_queries"].append({k: query.get(k) for k in said})
    if problems:
        raise ExportError("; ".join(problems))
    glossary_out = {
        tid: {k: v for k, v in term.items() if k != "id"} for tid, term in terms.items()
    }
    return pruned({"metrics": entries, "glossary": glossary_out})


def context(repo: Path = REPO) -> Block:
    """The tree's metric context: its metrics as dbt parses them, its glossary and its verified
    queries."""
    glossary = checked(repo, GLOSSARY)
    verified = checked(repo, VERIFIED)
    return build(parsed_metrics(repo), glossary, verified)


def render(found: Block) -> str:
    """The context as the file holds it."""
    return json.dumps(found, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def differences(committed: str | None, fresh: Block) -> list[str]:
    """How the committed file's text differs from the fresh context: each entry added, removed or
    changed, by section and name, or, when the entries agree, that the text alone differs."""
    if committed is None:
        return [f"{CONTEXT} is missing"]
    if committed == render(fresh):
        return []
    try:
        old = json.loads(committed)
    except json.JSONDecodeError:
        return [f"{CONTEXT} is not JSON"]
    if not isinstance(old, dict):
        return [f"{CONTEXT} is not a JSON object"]
    found: list[str] = []
    for section in sorted(old.keys() | fresh.keys()):
        was, now = old.get(section), fresh.get(section)
        if not isinstance(was, dict) or not isinstance(now, dict):
            if was != now:
                found.append(f"{section}: differs")
            continue
        for name in sorted(was.keys() | now.keys()):
            if name not in was:
                found.append(f"{section}.{name}: in a fresh export, not in the committed file")
            elif name not in now:
                found.append(f"{section}.{name}: in the committed file, not in a fresh export")
            elif was[name] != now[name]:
                found.append(f"{section}.{name}: differs from a fresh export")
    return found or [f"{CONTEXT}'s text differs from what the export writes, its entries agree"]


def main(argv: list[str], repo: Path = REPO) -> int:
    if argv not in ([], ["--check"]):
        print("usage: python -m semantics.export [--check]", file=sys.stderr)
        return 2
    try:
        fresh = context(repo)
    except ExportError as e:
        print(f"metric context: {e}", file=sys.stderr)
        return 1
    target = repo / CONTEXT
    if argv == ["--check"]:
        committed = target.read_text() if target.is_file() else None
        found = differences(committed, fresh)
        if found:
            lines = "\n".join(f"- {line}" for line in found)
            print(
                f"metric context: {CONTEXT} is stale against the definitions:\n{lines}\n"
                "Run `uv run python -m semantics.export` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"metric context: {CONTEXT} is current ({len(fresh['metrics'])} metrics)")
        return 0
    target.write_text(render(fresh))
    print(f"metric context: wrote {CONTEXT} ({len(fresh['metrics'])} metrics)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
