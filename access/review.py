"""The access review: every identity that can touch the lab's data or its controls, with its kind,
owner, catalogue grants, the roles the declared configuration gives it, its tag reads, the
identities it can act as and any conflicting pair of duties it holds.

`python -m access.review [path]` writes the report as Markdown, by default to
`build/access-review.md`, which git ignores: the report is derived, so it is generated, never
committed. It reads, offline and nothing else: the persona catalogue (`access/personas.yaml`), the
tag readers (`access/columns.yaml`), the accounts, owners and conflicting pairs
(`access/review.yaml`) and the declared configuration (`platform/gcp/*.tf`). The same files give
the same bytes. Identities are named by their catalogue ids, never by an account address, so the
report holds no project, account or key.

The review fails closed: it refuses to write a report when the configuration holds a resource
whose type it does not know, a grant it cannot read member by member (an authoritative policy, a
dataset access block), a principal it cannot attribute to a reviewed identity (anyone on the
internet, a person's account, an outside principal, an address held in a local), or a service
account it does not list. Conflicts are checked on each persona's catalogue grants; an identity
that can act as others is shown holding what their grants together hold, which nothing refuses.
"""

import importlib.util
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml
from jsonschema import Draft202012Validator

REPO = Path(__file__).resolve().parents[1]
OUTPUT = REPO / "build/access-review.md"
CATALOGUES = {
    "access": ("access/personas.yaml", "access/schema.json"),
    "columns": ("access/columns.yaml", "access/columns.schema.json"),
    "review": ("access/review.yaml", "access/review.schema.json"),
}


def _fences() -> ModuleType:
    """`platform/fences.py`, the one reader of the `.tf` files, imported by its path: `platform/`
    holds scripts run by path, and its name is the standard library's, so it is no package."""
    if "fences" not in sys.modules:
        spec = importlib.util.spec_from_file_location("fences", REPO / "platform/fences.py")
        if spec is None or spec.loader is None:
            raise ImportError("platform/fences.py cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.modules["fences"] = module
        spec.loader.exec_module(module)
    return sys.modules["fences"]


fences = _fences()

# Resource and data source types that grant nothing to a principal. Any other type that is not a
# member or binding grant is refused, so a new kind of grant cannot pass unread.
GRANTS_NOTHING = {
    "data.google_billing_account",
    "data.google_project",
    "data.google_service_account",
    "google_bigquery_datapolicy_data_policy",
    "google_bigquery_dataset",
    "google_billing_budget",
    "google_data_catalog_policy_tag",
    "google_data_catalog_taxonomy",
    "google_eventarc_trigger",
    "google_project_iam_custom_role",
    "google_pubsub_topic",
    "google_service_account",
    "google_service_usage_consumer_quota_override",
    "google_storage_bucket",
    "google_workflows_workflow",
}
# What a grant lands on, by its resource type, and the attribute naming the one it lands on.
TARGETS = {
    "google_project": ("lab project", None),
    "google_bigquery_dataset": ("warehouse dataset", "dataset_id"),
    "google_data_catalog_policy_tag": ("policy tag", "policy_tag"),
    "google_bigquery_datapolicy_data_policy": ("masking rule", "data_policy_id"),
    "google_data_catalog_taxonomy": ("policy-tag taxonomy", None),
    "google_storage_bucket": ("landing bucket", None),
    "google_service_account": ("account", "service_account_id"),
}
PERSONA_ACCOUNTS = "google_service_account.persona"
CLEAR_READER = "roles/datacatalog.categoryFineGrainedReader"
MASKED_READER = "roles/bigquerydatapolicy.maskedReader"
KINDS = {"human": "human persona", "agent": "agent persona", "automation": "automation account"}
INDEX = re.compile(r'\["([^"]+)"\]')
CUSTOM_ROLE = re.compile(r"^\$\{google_project_iam_custom_role\.([a-z_]+)\.name\}$")

Doc = dict[str, Any]
Config = Mapping[str, list[Any]]


@dataclass(frozen=True, order=True)
class Grant:
    """A grant in the persona catalogue: an access on a product, of one entity or the group."""

    product: str
    entity: str | None
    access: str


@dataclass(frozen=True, order=True)
class CloudGrant:
    """A role an identity holds, as the configuration declares it or as granted by hand."""

    role: str
    on: str
    declared: bool


@dataclass(frozen=True)
class Conflict:
    """A checkable pair whose two sides are held, with the entities they are held on (None for a
    group product)."""

    rule: str
    entities: tuple[str | None, ...]


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
    # Each conflict its own grants and those of the identities it can act as hold together.
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
    return fences.load()


def unquote(value: Any) -> str:
    return str(fences.unquote(value))


def blocks(config: Config, kind: str) -> dict[str, dict[str, Any]]:
    """Resources (or data sources, with `data.` before the address) by address."""
    prefix = "data." if kind == "data" else ""
    found = {}
    for entry in config.get(kind, []):
        for rtype, named in entry.items():
            for name, body in named.items():
                found[f"{prefix}{unquote(rtype)}.{unquote(name)}"] = body
    return found


def conflicts(grants: Iterable[Grant], rules: Sequence[Doc]) -> list[Conflict]:
    """Every checkable pair the grants hold both sides of, within the pair's scope."""
    held = list(grants)
    found = []
    for rule in rules:
        if "sides" not in rule:
            continue
        on = [
            {g.entity for g in held if (g.product, g.access) == (s["product"], s["access"])}
            for s in rule["sides"]
        ]
        if rule["scope"] == "same-entity":
            on = [set.intersection(*on)] * len(on)
        if all(on):
            entities = sorted(set().union(*on), key=lambda e: e or "")
            found.append(Conflict(rule["id"], tuple(entities)))
    return found


def persona_grants(access: Doc) -> dict[str, tuple[Grant, ...]]:
    grants: dict[str, list[Grant]] = {p["id"]: [] for p in access["personas"]}
    for g in access["grants"]:
        grants[g["persona"]].append(Grant(g["product"], g.get("entity"), g["access"]))
    return {persona: tuple(held) for persona, held in grants.items()}


def local_map(config: Config, name: str) -> dict[str, str]:
    values = [entry[name] for entry in config.get("locals", []) if name in entry]
    if len(values) != 1:
        raise ValueError(f"local.{name}: {len(values)} defined, expected one")
    return {unquote(k): unquote(v) for k, v in values[0].items()}


def instances(
    for_each: str | None, docs: Mapping[str, Doc], config: Config
) -> list[dict[str, str]]:
    """Each instance a `for_each` makes, as the `each.` references it substitutes, or one instance
    marked `every` for a for_each over a whole resource collection.

    The generated maps are evaluated here as `platform/gcp` writes them; the access rules tests pin
    their expressions and have OpenTofu evaluate them against the catalogues.
    """
    if for_each is None:
        return [{}]
    access, columns = docs["access"], docs["columns"]
    if for_each == "${local.dataset_grants}" or for_each == "${local.warehouse_readers}":
        roles = local_map(config, "dataset_roles")
        grants = sorted(
            {
                (g["persona"], g["product"].replace("-", "_"), roles[g["access"]])
                for g in access["grants"]
                if g["access"] in roles
            }
        )
        if for_each == "${local.warehouse_readers}":
            return [{"each.key": p} for p in sorted({p for p, _, _ in grants})]
        return [
            {"each.value.persona": p, "each.value.dataset": d, "each.value.role": r}
            for p, d, r in grants
        ]
    for kind in ("clear", "masked"):
        if for_each == f"${{local.{kind}_readers}}":
            return [
                {"each.value.persona": p, "each.value.tag": t["id"]}
                for t in columns["tags"]
                for p in t[kind]
            ]
    if for_each == f"${{{PERSONA_ACCOUNTS}}}":
        return [
            {"each.key": p["id"], "each.value.name": f'{PERSONA_ACCOUNTS}["{p["id"]}"].name'}
            for p in access["personas"]
        ]
    if re.fullmatch(r"\$\{google_[a-z_]+\.[a-z_]+\}", for_each):
        return [{"every": "every"}]
    raise ValueError(f"no reading of for_each {for_each}")


def substitute(expression: str, instance: Mapping[str, str]) -> str:
    for ref, value in instance.items():
        if ref.startswith("each."):
            quoted = value if ref == "each.value.name" else f'"{value}"'
            expression = expression.replace(ref, quoted)
    return expression


def attribute(member: str, addresses: Mapping[str, str], personas: set[str]) -> str | None:
    """The reviewed identity a member expression names, or None."""
    for address, identity in addresses.items():
        if member in (f"serviceAccount:${{{address}.email}}", f"${{{address}.member}}"):
            return identity
    found = re.fullmatch(
        r'(?:serviceAccount:\$\{google_service_account\.persona\["([a-z0-9-]+)"\]\.email\}'
        r'|\$\{google_service_account\.persona\["([a-z0-9-]+)"\]\.member\})',
        member,
    )
    persona = found and (found[1] or found[2])
    return persona if persona in personas else None


def target(rtype: str, body: Mapping[str, Any], every: bool, addresses: Mapping[str, str]) -> str:
    noun, attribute_name = TARGETS[rtype]
    if every:
        return f"every {noun}"
    if attribute_name is None:
        return f"the {noun}"
    expression = unquote(body[attribute_name])
    if rtype == "google_service_account":
        for address, identity in addresses.items():
            if expression in (f"${{{address}.name}}", f"${{{address}.id}}"):
                return f"account {identity}"
    index = INDEX.search(expression)
    if index is None:
        raise ValueError(f"cannot tell which {noun} {expression} names")
    return f"{noun} {index[1]}"


def declared_grants(
    docs: Mapping[str, Doc], config: Config
) -> tuple[dict[str, list[CloudGrant]], dict[str, set[str]]]:
    """Every role the configuration grants, by identity, and whom each identity can act as.

    Refuses the configuration when it declares a service account the review does not list, a
    resource of a type the review does not know, a grant it cannot read member by member, or a
    member no reviewed identity is.
    """
    personas = {p["id"] for p in docs["access"]["personas"]}
    accounts = docs["review"]["accounts"]
    resources, data = blocks(config, "resource"), blocks(config, "data")
    service_accounts = {
        a for a in [*resources, *data] if a.split(".")[-2] == "google_service_account"
    }
    listed = {a["declared"] for a in accounts}
    if service_accounts - listed - {PERSONA_ACCOUNTS}:
        unlisted = service_accounts - listed - {PERSONA_ACCOUNTS}
        raise ValueError(f"platform/gcp declares accounts the review does not list: {unlisted}")
    if listed - service_accounts:
        raise ValueError(
            f"access/review.yaml lists undeclared accounts: {listed - service_accounts}"
        )
    addresses = {a["declared"]: a["id"] for a in accounts}
    roles = {
        address.split(".")[1]: f"{unquote(body['title'])} (custom role)"
        for address, body in resources.items()
        if address.startswith("google_project_iam_custom_role.")
    }
    refused: list[str] = []
    for address, body in sorted({**resources, **data}.items()):
        rtype = address.rsplit(".", 1)[0]
        grant = re.search(r"_iam_(member|binding)$", rtype)
        if rtype == "google_bigquery_dataset" and "access" in body:
            refused.append(f"{address}: an access block, which the review cannot read")
        elif grant is None and rtype not in GRANTS_NOTHING:
            refused.append(f"{address}: a {rtype}, which the review does not know to grant nothing")
        elif grant and rtype[: grant.start()] not in TARGETS:
            refused.append(f"{address}: a grant on a resource the review has no words for")
    found: dict[str, list[CloudGrant]] = {i: [] for i in [*sorted(personas), *addresses.values()]}
    acts_as: dict[str, set[str]] = {i: set() for i in found}
    for address, body in sorted(resources.items()):
        rtype, _ = address.split(".", 1)
        grant = re.search(r"_iam_(member|binding)$", rtype)
        if grant is None or rtype[: grant.start()] not in TARGETS:
            continue
        base = rtype[: grant.start()]
        members = [body["member"]] if grant[1] == "member" else list(body["members"])
        for instance in instances(body.get("for_each"), docs, config):
            role = unquote(substitute(unquote(body["role"]), instance))
            role = role[3:-2] if role.startswith('${"') else role
            custom = CUSTOM_ROLE.fullmatch(role)
            role = roles.get(custom[1], role) if custom else role
            subbed = {
                k: substitute(unquote(v), instance) for k, v in body.items() if k != "for_each"
            }
            on = target(base, subbed, "every" in instance, addresses)
            for member in members:
                who = attribute(substitute(unquote(member), instance), addresses, personas)
                if who is None:
                    refused.append(f"{address}: a member the review cannot attribute")
                    continue
                found[who].append(CloudGrant(role, on, True))
                if base == "google_service_account":
                    if "every" in instance:
                        refused.append(f"{address}: a role on every account of a collection")
                    else:
                        acts_as[who].add(on.removeprefix("account "))
    if refused:
        raise ValueError(
            "the review refuses the configuration:\n" + "\n".join(sorted(set(refused)))
        )
    return found, acts_as


def reachable(acts_as: Mapping[str, set[str]], start: str) -> list[str]:
    """Every identity `start` can act as, directly or through another it can act as."""
    seen: set[str] = set()
    todo = sorted(acts_as[start])
    while todo:
        identity = todo.pop()
        if identity not in seen and identity != start:
            seen.add(identity)
            todo += sorted(acts_as.get(identity, set()))
    return sorted(seen)


def tag_reads(columns: Doc, cloud: Sequence[CloudGrant]) -> dict[str, str]:
    """What the identity reads in each tagged column, from the roles it holds."""
    held = {(c.role, c.on) for c in cloud}
    reads = {}
    for tag in columns["tags"]:
        clear = {(CLEAR_READER, f"policy tag {tag['id']}"), (CLEAR_READER, "every policy tag")}
        masked = {
            (MASKED_READER, f"masking rule {tag['id']}"),
            (MASKED_READER, "every masking rule"),
        }
        if held & clear:
            reads[tag["id"]] = "clear"
        elif held & masked:
            reads[tag["id"]] = f"masked ({tag['masking']})"
        else:
            reads[tag["id"]] = "no role"
    return reads


def review(docs: Mapping[str, Doc], config: Config) -> list[Reviewed]:
    """Every identity: the personas in catalogue order, then the accounts."""
    access, columns, data = docs["access"], docs["columns"], docs["review"]
    rules = data["conflicts"]
    products = {p["id"] for p in access["data_products"]}
    unknown = {s["product"] for r in rules for s in r.get("sides", [])} - products
    if unknown:
        # A side on no product would never be held, so its pair would pass unseen.
        raise ValueError(f"a conflicting pair names a product the catalogue lacks: {unknown}")
    owners = data["owners"]
    entries: list[tuple[Doc, bool]] = [(p, True) for p in access["personas"]]
    entries += [(a, False) for a in data["accounts"]]
    identities = [e["id"] for e, _ in entries]
    if sorted(owners) != sorted(identities) or len(set(identities)) != len(identities):
        raise ValueError("access/review.yaml must name one owner for each identity, and no other")
    grants = persona_grants(access)
    cloud, acts_as = declared_grants(docs, config)
    reviewed = []
    for entry, persona in entries:
        held = grants[entry["id"]] if persona else ()
        by_hand = [
            CloudGrant(role, "the lab project", False)
            for role in entry.get("project_roles_granted_by_hand", [])
        ]
        rows = sorted(cloud[entry["id"]]) + by_hand
        acting = reachable(acts_as, entry["id"])
        reviewed.append(
            Reviewed(
                id=entry["id"],
                kind=KINDS[entry["kind"]],
                owner=owners[entry["id"]],
                duties=entry["duties"],
                reach=entry.get("reach", ""),
                grants=held,
                tags=tag_reads(columns, rows),
                cloud=tuple(rows),
                acts_as=tuple(acting),
                limits=tuple(entry.get("limits", [])),
                conflicts=tuple(conflicts(held, rules)),
                conflicts_acting=tuple(
                    c
                    for c in conflicts(
                        [*held, *(g for i in acting for g in grants.get(i, ()))], rules
                    )
                    if acting and c not in conflicts(held, rules)
                ),
            )
        )
    return reviewed


def where(conflict: Conflict) -> str:
    on = " and ".join(e or "the group" for e in conflict.entities)
    return f"`{conflict.rule}` on {on}"


def holders(conflict: Conflict, rules: Sequence[Doc], access: Doc, who: Sequence[str]) -> str:
    """Which of the identity itself and those it acts as give each side of the conflict."""
    (rule,) = [r for r in rules if r["id"] == conflict.rule]
    sides = []
    for side in rule["sides"]:
        giving = sorted(
            {
                g["persona"]
                for g in access["grants"]
                if g["persona"] in who
                and (g["product"], g["access"]) == (side["product"], side["access"])
            }
        )
        sides.append(f"{side['access']} {side['product']} as {', '.join(f'`{p}`' for p in giving)}")
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
    ("Acts as", "may mint the identity's token or run as it, and so hold its grants"),
    ("Declared", "written in the configuration, which the project owner applies"),
    ("Conflicting duties", "two duties one identity must not hold together"),
    ("Not checkable", "a pair the catalogues cannot state, so the review neither passes nor fails"),
]


def render(docs: Mapping[str, Doc], reviewed: Sequence[Reviewed]) -> str:
    access, rules, owners = docs["access"], docs["review"]["conflicts"], docs["review"]["owners"]
    checkable = [r for r in rules if "sides" in r]
    unchecked = len(rules) - len(checkable)
    mutual = sorted(f"`{a}` and `{b}`" for a, b in owners.items() if a < b and owners.get(b) == a)
    lines = [
        "# Access review",
        "",
        "Every identity that can touch the lab's data or its controls, generated from the "
        "catalogues in `access/` and the declared configuration in `platform/gcp/`, "
        "never from the live project.",
        "",
        "A role marked declared is written in the configuration, which the project owner applies, "
        "so it may not be in force yet; a role marked granted by hand is recorded from the "
        "project owner's statements, and this review does not verify it.",
        "",
        "The project owner, who applies the configuration and administers access, is outside the "
        "catalogue and is not reviewed here.",
    ]
    lines += [f"{pair} own each other." for pair in mutual]
    lines += [
        "",
        "| Identity | Kind | Owner | Acts as | Checked conflicts | Not checkable |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in reviewed:
        acts = f"{len(r.acts_as)} identities" if r.acts_as else "nobody"
        held = [where(c) for c in r.conflicts]
        held += [f"{where(c)} acting as others" for c in r.conflicts_acting]
        found = "; ".join(held) or f"none found in {len(checkable)} checked"
        lines.append(
            f"| `{r.id}` | {r.kind} | `{r.owner}` | {acts} | {found} | {unchecked} pairs |"
        )
    lines += ["", "## Conflicting duties", ""]
    for rule in rules:
        pair = " and ".join(rule["duties"])
        if "sides" in rule:
            sides = " and ".join(f"{s['access']} on {s['product']}" for s in rule["sides"])
            scope = "on one entity" if rule["scope"] == "same-entity" else "on any entities"
            lines.append(f"- **{pair}** (`{rule['id']}`): checked, as {sides} {scope}.")
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
        if r.cloud:
            lines += ["", "| Role | On | Source |", "| --- | --- | --- |"]
            lines += [
                f"| {c.role} | {c.on} | "
                f"{'declared' if c.declared else 'granted by hand, not declared'} |"
                for c in r.cloud
            ]
        if not r.grants and not r.cloud:
            lines += ["", "No grant and no role."]
        tags = ", ".join(f"{tag} {read}" for tag, read in r.tags.items())
        lines += ["", f"- Tagged columns: {tags}"]
        if r.acts_as:
            names = ", ".join(f"`{p}`" for p in r.acts_as)
            lines.append(f"- Acts as: {names}, holding each one's grants in turn")
            owning = sorted({owners[i] for i in owners} & set(r.acts_as))
            if r.owner in r.acts_as:
                others = ", ".join(f"`{o}`" for o in owning if o != r.owner)
                lines.append(
                    f"- Acts as its own owner, `{r.owner}`, and every other owner ({others})"
                )
        else:
            lines.append("- Acts as: nobody")
        for limit in r.limits:
            lines.append(f"- Limit: {limit}")
        lines += [f"- **Conflict:** holds {where(c)}" for c in r.conflicts]
        lines += [
            f"- **Conflict acting as others, not refused:** {where(c)}: "
            f"{holders(c, rules, access, [r.id, *r.acts_as])}"
            for c in r.conflicts_acting
        ]
        if not r.conflicts and not r.conflicts_acting:
            ids = ", ".join(f"`{rule['id']}`" for rule in checkable)
            lines.append(f"- Conflicts: none found in the checked pairs ({ids})")
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
