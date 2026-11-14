"""The access review: every identity that can touch the lab's data or its controls, with its kind,
owner, grants, tag reads, the personas it can act as and any conflicting pair of duties it holds.

`python -m access.review [path]` writes the report as Markdown, by default to
`build/access-review.md`, which git ignores: the report is derived, so it is generated, never
committed. It reads, offline and nothing else: the persona catalogue (`access/personas.yaml`), the
tag readers (`access/columns.yaml`), the accounts, owners and conflicting pairs
(`access/review.yaml`) and the declared configuration (`platform/gcp/*.tf`). The same files give
the same bytes. Identities are named by their catalogue ids, never by an account address, so the
report holds no project, account or key.

Conflicts are checked on what each identity holds itself; for an account that can act as
personas the report also shows what the union of their grants would hold, which no check refuses.
"""

import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import hcl2
import yaml
from jsonschema import Draft202012Validator

REPO = Path(__file__).resolve().parents[1]
GCP = REPO / "platform/gcp"
OUTPUT = REPO / "build/access-review.md"
CATALOGUES = {
    "access": ("access/personas.yaml", "access/schema.json"),
    "columns": ("access/columns.yaml", "access/columns.schema.json"),
    "review": ("access/review.yaml", "access/review.schema.json"),
}

# What a grant in platform/gcp lands on, by its resource type: one of them, or each with for_each.
TARGETS = {
    "google_project_iam_member": "lab project",
    "google_bigquery_dataset_iam_member": "warehouse dataset",
    "google_data_catalog_policy_tag_iam_member": "policy tag",
    "google_data_catalog_taxonomy_iam_member": "policy-tag taxonomy",
    "google_storage_bucket_iam_member": "landing bucket",
    "google_service_account_iam_member": "persona account",
}
PERSONA_ACCOUNTS = "google_service_account.persona"
MINTS_TOKENS = "roles/iam.serviceAccountTokenCreator"
CLEAR_READER = "roles/datacatalog.categoryFineGrainedReader"
KINDS = {"human": "human persona", "agent": "agent persona", "automation": "automation account"}

Doc = dict[str, Any]


@dataclass(frozen=True, order=True)
class Grant:
    """A grant in the persona catalogue: an access on a product, of one entity or the group."""

    product: str
    entity: str | None
    access: str


@dataclass(frozen=True)
class CloudGrant:
    """A role an account holds in the project, as platform/gcp declares it or as granted by hand."""

    role: str
    on: str
    declared: bool


@dataclass(frozen=True)
class Conflict:
    """A checkable pair held on one entity's product, or on a group product (entity None)."""

    rule: str
    entity: str | None


@dataclass(frozen=True)
class Reviewed:
    id: str
    kind: str
    owner: str
    duties: str
    reach: str
    grants: tuple[Grant, ...]
    tags: Mapping[str, str]
    cloud: tuple[CloudGrant, ...]
    acts_as: tuple[str, ...]
    limits: tuple[str, ...]
    conflicts: tuple[Conflict, ...]
    # For an account acting as personas: each conflict the union of their grants holds.
    conflicts_acting: tuple[Conflict, ...]


def load_catalogues() -> dict[str, Doc]:
    """The three catalogues, each refused with every error its schema finds."""
    docs: dict[str, Doc] = {}
    for name, (data, schema) in CATALOGUES.items():
        doc = yaml.safe_load((REPO / data).read_text())
        validator = Draft202012Validator(json.loads((REPO / schema).read_text()))
        errors = sorted(e.message for e in validator.iter_errors(doc))
        if errors:
            raise ValueError(f"{data} breaks {schema}: {'; '.join(errors)}")
        docs[name] = doc
    return docs


def load_config() -> dict[str, list[Any]]:
    """Every top-level block of every `.tf` file, merged by kind, as fences.py reads them."""
    merged: dict[str, list[Any]] = {}
    for path in sorted(GCP.glob("*.tf")):
        with path.open() as handle:
            for key, value in hcl2.load(handle).items():
                if isinstance(value, list):
                    merged.setdefault(key, []).extend(value)
    return merged


def unquote(value: Any) -> Any:
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def blocks(config: Mapping[str, list[Any]], kind: str) -> dict[str, dict[str, Any]]:
    """Resources (or data sources, with `data.` before the address) by address."""
    prefix = "data." if kind == "data" else ""
    found = {}
    for entry in config.get(kind, []):
        for rtype, named in entry.items():
            for name, body in named.items():
                found[f"{prefix}{unquote(rtype)}.{unquote(name)}"] = body
    return found


def conflicts(grants: Iterable[Grant], rules: Sequence[Doc]) -> list[Conflict]:
    """Every checkable pair the grants hold both sides of on one entity, or on a group product."""
    held = {(g.product, g.access, g.entity) for g in grants}
    entities = {g.entity for g in grants}
    found = []
    for rule in rules:
        if "sides" not in rule:
            continue
        for entity in sorted(entities, key=lambda e: e or ""):
            if all((s["product"], s["access"], entity) in held for s in rule["sides"]):
                found.append(Conflict(rule["id"], entity))
    return found


def persona_grants(access: Doc) -> dict[str, tuple[Grant, ...]]:
    grants: dict[str, list[Grant]] = {p["id"]: [] for p in access["personas"]}
    for g in access["grants"]:
        grants[g["persona"]].append(Grant(g["product"], g.get("entity"), g["access"]))
    return {persona: tuple(held) for persona, held in grants.items()}


def tag_reads(columns: Doc, persona: str) -> dict[str, str]:
    reads = {}
    for tag in columns["tags"]:
        if persona in tag["clear"]:
            reads[tag["id"]] = "clear"
        elif persona in tag["masked"]:
            reads[tag["id"]] = f"masked ({tag['masking']})"
        else:
            reads[tag["id"]] = "no role"
    return reads


def account_grants(
    config: Mapping[str, list[Any]], accounts: Sequence[Doc], personas: Sequence[str]
) -> dict[str, tuple[list[CloudGrant], list[str]]]:
    """Each account's declared grants and the personas it may act as, read from platform/gcp.

    Refuses a service account platform/gcp declares that is neither a persona account nor listed
    in access/review.yaml, and a grant to an account it cannot describe, so none is left out.
    """
    declared = set(blocks(config, "resource")) | set(blocks(config, "data"))
    service_accounts = {a for a in declared if a.split(".")[-2] == "google_service_account"}
    listed = {a["declared"] for a in accounts}
    unlisted = service_accounts - listed - {PERSONA_ACCOUNTS}
    if unlisted:
        raise ValueError(f"platform/gcp declares accounts the review does not list: {unlisted}")
    if listed - service_accounts:
        raise ValueError(
            f"access/review.yaml lists undeclared accounts: {listed - service_accounts}"
        )
    member = {f"serviceAccount:${{{a['declared']}.email}}": a["id"] for a in accounts}
    roles: dict[str, str] = {
        f"${{{address}.name}}": f"{unquote(body['title'])} (custom role)"
        for address, body in blocks(config, "resource").items()
        if address.startswith("google_project_iam_custom_role.")
    }
    found: dict[str, tuple[list[CloudGrant], list[str]]] = {a["id"]: ([], []) for a in accounts}
    for address, body in sorted(blocks(config, "resource").items()):
        rtype = address.split(".")[0]
        if not rtype.endswith("_iam_member"):
            continue
        who = member.get(unquote(body["member"]))
        if who is None:
            continue
        if rtype not in TARGETS:
            raise ValueError(f"{address}: no words for what a {rtype} grant lands on")
        role = str(unquote(body["role"]))
        each = "for_each" in body
        found[who][0].append(
            CloudGrant(
                roles.get(role, role), f"{'every' if each else 'the'} {TARGETS[rtype]}", True
            )
        )
        if role == MINTS_TOKENS:
            if body.get("for_each") != f"${{{PERSONA_ACCOUNTS}}}":
                raise ValueError(f"{address}: token creation on an account that is no persona's")
            found[who][1].extend(personas)
    return found


def review(docs: Mapping[str, Doc], config: Mapping[str, list[Any]]) -> list[Reviewed]:
    """Every identity: the personas in catalogue order, then the accounts."""
    access, columns, data = docs["access"], docs["columns"], docs["review"]
    rules = data["conflicts"]
    products = {p["id"] for p in access["data_products"]}
    unknown = {s["product"] for r in rules for s in r.get("sides", [])} - products
    if unknown:
        # A side on no product would never be held, so its pair would pass unseen.
        raise ValueError(f"a conflicting pair names a product the catalogue lacks: {unknown}")
    owners = data["owners"]
    identities = [p["id"] for p in access["personas"]] + [a["id"] for a in data["accounts"]]
    if sorted(owners) != sorted(identities) or len(set(identities)) != len(identities):
        raise ValueError("access/review.yaml must name one owner for each identity, and no other")
    grants = persona_grants(access)
    reviewed = [
        Reviewed(
            id=p["id"],
            kind=KINDS[p["kind"]],
            owner=owners[p["id"]],
            duties=p["duties"],
            reach="",
            grants=grants[p["id"]],
            tags=tag_reads(columns, p["id"]),
            cloud=(),
            acts_as=(),
            limits=(),
            conflicts=tuple(conflicts(grants[p["id"]], rules)),
            conflicts_acting=(),
        )
        for p in access["personas"]
    ]
    cloud = account_grants(config, data["accounts"], list(grants))
    for account in data["accounts"]:
        declared, acts_as = cloud[account["id"]]
        by_hand = [
            CloudGrant(role, "the lab project", False)
            for role in account.get("project_roles_granted_by_hand", [])
        ]
        clear_on_every_tag = CloudGrant(CLEAR_READER, "every policy tag", True) in declared
        acting = [g for persona in acts_as for g in grants[persona]]
        reviewed.append(
            Reviewed(
                id=account["id"],
                kind=KINDS[account["kind"]],
                owner=owners[account["id"]],
                duties=account["duties"],
                reach=account["reach"],
                grants=(),
                tags={
                    t["id"]: "clear" if clear_on_every_tag else "no role" for t in columns["tags"]
                },
                cloud=tuple(declared + by_hand),
                acts_as=tuple(acts_as),
                limits=tuple(account["limits"]),
                conflicts=(),
                conflicts_acting=tuple(conflicts(acting, rules)),
            )
        )
    return reviewed


def where(conflict: Conflict) -> str:
    return f"`{conflict.rule}` on {conflict.entity or 'the group'}"


def holders(conflict: Conflict, rules: Sequence[Doc], access: Doc, acts_as: Sequence[str]) -> str:
    """Which of the personas an account acts as give each side of the conflict."""
    (rule,) = [r for r in rules if r["id"] == conflict.rule]
    sides = []
    for side in rule["sides"]:
        who = sorted(
            g["persona"]
            for g in access["grants"]
            if g["persona"] in acts_as
            and (g["product"], g["access"], g.get("entity"))
            == (side["product"], side["access"], conflict.entity)
        )
        sides.append(f"{side['access']} {side['product']} as {', '.join(f'`{p}`' for p in who)}")
    return "; ".join(sides)


GLOSSARY = [
    ("Identity", "a service account that can touch the lab's data or its controls"),
    ("Persona", "a role in the finance team, held by one service account named after it"),
    ("Automation account", "a service account that is no persona and that software runs as"),
    ("Owner", "the persona who confirms in a review that each grant is still needed"),
    ("read", "reads the product's rows, masking still applied"),
    ("aggregate", "reads only aggregates above the minimum group, and today no rows at all"),
    ("edit", "reads and changes the definitions"),
    ("draft", "proposes an entry, never posts it"),
    ("post", "posts an approved entry"),
    ("Tagged column", "a column marked as holding bank details or standing for a person"),
    ("clear, masked", "reads a tagged column's value, or the value with its masking rule applied"),
    ("Acts as", "may mint the persona's token and hold that persona's grants"),
    ("Conflicting duties", "two duties one identity must not hold together"),
    ("Not checkable", "a pair the catalogues cannot state, so the review neither passes nor fails"),
]


def render(docs: Mapping[str, Doc], reviewed: Sequence[Reviewed]) -> str:
    access, rules = docs["access"], docs["review"]["conflicts"]
    lines = [
        "# Access review",
        "",
        "Every identity that can touch the lab's data or its controls, generated from the "
        "catalogues in `access/` and the declared configuration in `platform/gcp/`, "
        "never from the live project.",
        "",
        "| Identity | Kind | Owner | Acts as | Conflicts |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in reviewed:
        acts = f"{len(r.acts_as)} personas" if r.acts_as else "nobody"
        held = [where(c) for c in r.conflicts]
        held += [f"{where(c)} acting as personas" for c in r.conflicts_acting]
        lines.append(
            f"| `{r.id}` | {r.kind} | `{r.owner}` | {acts} | {'; '.join(held) or 'none'} |"
        )
    checkable = [r for r in rules if "sides" in r]
    lines += ["", "## Conflicting duties", ""]
    for rule in rules:
        pair = " and ".join(rule["duties"])
        if "sides" in rule:
            sides = " and ".join(f"{s['access']} on {s['product']}" for s in rule["sides"])
            lines.append(f"- **{pair}** (`{rule['id']}`): checked, as {sides} on one entity.")
        else:
            lines.append(f"- **{pair}** (`{rule['id']}`): not checkable. {rule['not_checkable']}")
    for r in reviewed:
        lines += ["", f"## `{r.id}`", "", f"- Kind: {r.kind}", f"- Owner: `{r.owner}`"]
        lines.append(f"- Duties: {r.duties}")
        if r.reach:
            lines.append(f"- Reach: {r.reach}")
        if r.grants:
            lines += ["", "| Product | Entity | Access |", "| --- | --- | --- |"]
            lines += [f"| {g.product} | {g.entity or 'group'} | {g.access} |" for g in r.grants]
        elif not r.cloud:
            lines += ["", "No grant on any data product."]
        if r.cloud:
            lines += ["", "| Role | On | Source |", "| --- | --- | --- |"]
            lines += [
                f"| {c.role} | {c.on} | "
                f"{'declared' if c.declared else 'granted by hand, not declared'} |"
                for c in r.cloud
            ]
        tags = ", ".join(f"{tag} {read}" for tag, read in r.tags.items())
        lines += ["", f"- Tagged columns: {tags}"]
        if r.acts_as:
            names = ", ".join(f"`{p}`" for p in r.acts_as)
            lines.append(f"- Acts as: every persona ({names}), holding each one's grants in turn")
        else:
            lines.append("- Acts as: nobody")
        for limit in r.limits:
            lines.append(f"- Limit: {limit}")
        if r.conflicts:
            lines += [f"- **Conflict:** holds {where(c)}" for c in r.conflicts]
        else:
            ids = ", ".join(f"`{rule['id']}`" for rule in checkable)
            lines.append(f"- Conflicts: none of the checked pairs ({ids})")
        lines += [
            f"- **Conflict acting as personas, not refused:** {where(c)}: "
            f"{holders(c, rules, access, r.acts_as)}"
            for c in r.conflicts_acting
        ]
        unchecked = len(rules) - len(checkable)
        lines.append(f"- Not checkable: {unchecked} of the {len(rules)} pairs, listed above")
    lines += ["", "## Glossary", ""]
    lines += [f"- **{term}**: {meaning}." for term, meaning in GLOSSARY]
    return "\n".join(lines) + "\n"


def report() -> str:
    docs = load_catalogues()
    return render(docs, review(docs, load_config()))


def main(argv: Sequence[str]) -> int:
    if len(argv) > 1:
        print("usage: python -m access.review [path]", file=sys.stderr)
        return 2
    path = Path(argv[0]) if argv else OUTPUT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report())
    print(f"access review written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
