"""The access review (`python -m access.review`): every identity in the report with its kind,
owner, grants, tag reads and what it can act as, and the duty check that refuses a persona holding
both sides of a conflicting pair.

What the report must say is stated here from the catalogues and from the grants the access rules
tests read in `platform/gcp`, apart from the code under test. Nothing here calls GCP.
"""

import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from access.review import (
    REPO,
    Conflict,
    Grant,
    account_grants,
    conflicts,
    load_catalogues,
    load_config,
    render,
    review,
)

Doc = dict[str, Any]
# The pairs the catalogues can state today, pinned here, so making another one checkable (or
# dropping this one) is a reviewed change to the data and to this line together.
CHECKABLE = {"enter-and-approve-entry"}
# The grants platform/gcp declares to the agent account, as tests/platform/test_access_rules.py
# lists them, and the project roles recorded as granted to it by hand.
AGENT_DECLARED = {
    ("Builds the warehouse models (custom role)", "every warehouse dataset"),
    ("roles/datacatalog.categoryFineGrainedReader", "every policy tag"),
    ("roles/datacatalog.viewer", "the policy-tag taxonomy"),
    ("roles/iam.serviceAccountTokenCreator", "every persona account"),
    ("roles/storage.objectUser", "the landing bucket"),
}
KILL_SWITCH_DECLARED = {
    ("roles/billing.projectManager", "the lab project"),
    ("roles/eventarc.eventReceiver", "the lab project"),
    ("roles/workflows.invoker", "the lab project"),
}
KINDS = {"human": "human persona", "agent": "agent persona"}


@pytest.fixture(scope="module")
def docs() -> dict[str, Doc]:
    return load_catalogues()


@pytest.fixture(scope="module")
def config() -> dict[str, list[Any]]:
    return load_config()


@pytest.fixture(scope="module")
def written(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The report as the command writes it."""
    path = tmp_path_factory.mktemp("review") / "access-review.md"
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "access.review", str(path)], cwd=REPO, check=True, timeout=120
    )
    return path.read_text()


def sections(report: str) -> dict[str, str]:
    """Each identity's section of the report, by identity id."""
    parts = re.split(r"^## `([a-z0-9-]+)`$", report, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_the_report_covers_every_persona_with_its_kind_owner_grants_and_tags(
    written: str, docs: dict[str, Doc]
) -> None:
    found = sections(written)
    owners = docs["review"]["owners"]
    for persona in docs["access"]["personas"]:
        section = found.pop(persona["id"])
        assert f"- Kind: {KINDS[persona['kind']]}\n" in section, persona["id"]
        assert f"- Owner: `{owners[persona['id']]}`\n" in section, persona["id"]
        rows = {
            line
            for line in section.splitlines()
            if line.startswith("| ") and not line.startswith(("| Product", "| ---"))
        }
        assert rows == {
            f"| {g['product']} | {g.get('entity', 'group')} | {g['access']} |"
            for g in docs["access"]["grants"]
            if g["persona"] == persona["id"]
        }, persona["id"]
        for tag in docs["columns"]["tags"]:
            read = (
                "clear"
                if persona["id"] in tag["clear"]
                else f"masked ({tag['masking']})"
                if persona["id"] in tag["masked"]
                else "no role"
            )
            assert f"{tag['id']} {read}" in section, (persona["id"], tag["id"])
    assert set(found) == {a["id"] for a in docs["review"]["accounts"]}


def cloud_rows(section: str) -> dict[tuple[str, str], str]:
    rows = [line.strip("| ").split(" | ") for line in section.splitlines() if line.startswith("| ")]
    return {(role, on): source for role, on, source in rows if role not in {"Role", "---"}}


def test_the_agent_account_shows_its_declared_and_hand_granted_roles_and_every_persona(
    written: str, docs: dict[str, Doc]
) -> None:
    section = sections(written)["agent"]
    rows = cloud_rows(section)
    assert {k for k, source in rows.items() if source == "declared"} == AGENT_DECLARED
    (agent,) = [a for a in docs["review"]["accounts"] if a["id"] == "agent"]
    assert {k for k, source in rows.items() if source == "granted by hand, not declared"} == {
        (role, "the lab project") for role in agent["project_roles_granted_by_hand"]
    }
    personas = ", ".join(f"`{p['id']}`" for p in docs["access"]["personas"])
    assert f"- Acts as: every persona ({personas})" in section
    for tag in docs["columns"]["tags"]:
        assert f"{tag['id']} clear" in section
    assert "- Reach: The largest concentration of access in the lab" in section
    # Acting as the proposer and the approver it holds both sides of entering and approving.
    assert (
        "- **Conflict acting as personas, not refused:** `enter-and-approve-entry` on chicago: "
        "draft ledger as `proposer`; post ledger as `approver`\n"
    ) in section


def test_the_kill_switch_shows_its_project_roles_and_reads_nothing(written: str) -> None:
    section = sections(written)["kill-switch"]
    assert set(cloud_rows(section)) == KILL_SWITCH_DECLARED
    assert "- Acts as: nobody\n" in section
    assert "clear" not in section.split("- Tagged columns:")[1].splitlines()[0]


def test_the_report_is_the_same_bytes_each_time_and_names_no_account_or_project(
    written: str,
) -> None:
    again = subprocess.run(  # noqa: S603
        [sys.executable, "-c", "import access.review as r; print(r.report(), end='')"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout
    assert again == written
    for leak in ("@", "gserviceaccount", "projects/", "${", "var."):
        assert leak not in written, leak


def test_every_identity_has_one_owner_a_human_persona_other_than_itself(
    docs: dict[str, Doc],
) -> None:
    humans = {p["id"] for p in docs["access"]["personas"] if p["kind"] == "human"}
    identities = [p["id"] for p in docs["access"]["personas"]]
    identities += [a["id"] for a in docs["review"]["accounts"]]
    owners = docs["review"]["owners"]
    assert sorted(owners) == sorted(identities)
    for identity, owner in owners.items():
        assert owner in humans, identity
        assert owner != identity, identity


def test_an_identity_without_an_owner_or_an_owner_of_no_identity_is_refused(
    docs: dict[str, Doc], config: dict[str, list[Any]]
) -> None:
    for change in ("drop", "add"):
        planted = copy.deepcopy(docs)
        if change == "drop":
            del planted["review"]["owners"]["proposer"]
        else:
            planted["review"]["owners"]["nobody-at-all"] = "auditor"
        with pytest.raises(ValueError, match="one owner for each identity"):
            review(planted, config)


def test_a_service_account_the_review_does_not_list_is_refused(
    docs: dict[str, Doc], config: dict[str, list[Any]]
) -> None:
    accounts = docs["review"]["accounts"]
    personas = [p["id"] for p in docs["access"]["personas"]]
    planted = copy.deepcopy(config)
    planted["resource"].append({"google_service_account": {"unlisted": {"account_id": "x"}}})
    with pytest.raises(ValueError, match="does not list"):
        account_grants(planted, accounts, personas)
    ghost = [*accounts, {**accounts[0], "id": "ghost", "declared": "google_service_account.ghost"}]
    with pytest.raises(ValueError, match="undeclared"):
        account_grants(config, ghost, personas)


def test_a_grant_to_an_account_on_a_resource_the_review_cannot_describe_is_refused(
    docs: dict[str, Doc], config: dict[str, list[Any]]
) -> None:
    planted = copy.deepcopy(config)
    planted["resource"].append(
        {
            "google_pubsub_topic_iam_member": {
                "agent_publishes": {
                    "role": '"roles/pubsub.publisher"',
                    "member": '"serviceAccount:${data.google_service_account.agent.email}"',
                }
            }
        }
    )
    personas = [p["id"] for p in docs["access"]["personas"]]
    with pytest.raises(ValueError, match="no words"):
        account_grants(planted, docs["review"]["accounts"], personas)


def test_no_persona_holds_a_conflicting_pair(docs: dict[str, Doc]) -> None:
    rules = docs["review"]["conflicts"]
    for persona in docs["access"]["personas"]:
        held = [
            Grant(g["product"], g.get("entity"), g["access"])
            for g in docs["access"]["grants"]
            if g["persona"] == persona["id"]
        ]
        assert conflicts(held, rules) == [], persona["id"]


def checkable(docs: dict[str, Doc]) -> list[str]:
    return sorted(r["id"] for r in docs["review"]["conflicts"] if "sides" in r)


def plant(docs: dict[str, Doc], rule_id: str, entities: tuple[str, str]) -> tuple[Doc, str]:
    """A copy of the catalogue whose first persona holding neither side gains both, the first on
    `entities[0]` and the second on `entities[1]` where the product is entity-scoped."""
    access = copy.deepcopy(docs["access"])
    (rule,) = [r for r in docs["review"]["conflicts"] if r["id"] == rule_id]
    scope = {p["id"]: p["scope"] for p in access["data_products"]}
    sides = {(s["product"], s["access"]) for s in rule["sides"]}
    persona = next(
        p["id"]
        for p in access["personas"]
        if not any(
            (g["product"], g["access"]) in sides
            for g in access["grants"]
            if g["persona"] == p["id"]
        )
    )
    for side, entity in zip(rule["sides"], entities, strict=True):
        grant = {"persona": persona, "product": side["product"], "access": side["access"]}
        if scope[side["product"]] == "entity":
            grant["entity"] = entity
        access["grants"].append(grant)
    return access, persona


def test_the_pinned_pairs_are_the_checkable_ones(docs: dict[str, Doc]) -> None:
    assert checkable(docs) == sorted(CHECKABLE)


@pytest.mark.parametrize("rule_id", sorted(CHECKABLE))
def test_a_planted_conflict_fails_the_duty_check(
    docs: dict[str, Doc], config: dict[str, list[Any]], rule_id: str
) -> None:
    entity = docs["access"]["entities"][0]["id"]
    access, persona = plant(docs, rule_id, (entity, entity))
    (rule,) = [r for r in docs["review"]["conflicts"] if r["id"] == rule_id]
    scope = {p["id"]: p["scope"] for p in access["data_products"]}
    on = entity if scope[rule["sides"][0]["product"]] == "entity" else None
    reviewed = {r.id: r for r in review({**docs, "access": access}, config)}
    assert reviewed[persona].conflicts == (Conflict(rule_id, on),)
    assert [r.id for r in reviewed.values() if r.conflicts] == [persona]
    with pytest.raises(AssertionError, match=persona):
        test_no_persona_holds_a_conflicting_pair({**docs, "access": access})
    report = render({**docs, "access": access}, list(reviewed.values()))
    assert (
        f"- **Conflict:** holds `{rule_id}` on {on or 'the group'}\n" in sections(report)[persona]
    )


def test_both_sides_on_two_entities_are_no_conflict(docs: dict[str, Doc]) -> None:
    first, second = (e["id"] for e in docs["access"]["entities"][:2])
    for rule_id in sorted(CHECKABLE):
        access, persona = plant(docs, rule_id, (first, second))
        held = [
            Grant(g["product"], g.get("entity"), g["access"])
            for g in access["grants"]
            if g["persona"] == persona
        ]
        assert conflicts(held, docs["review"]["conflicts"]) == [], rule_id


def test_a_side_naming_no_product_is_refused(
    docs: dict[str, Doc], config: dict[str, list[Any]]
) -> None:
    planted = copy.deepcopy(docs)
    rule = next(r for r in planted["review"]["conflicts"] if "sides" in r)
    rule["sides"][0]["product"] = "ledgers"
    with pytest.raises(ValueError, match="ledgers"):
        review(planted, config)


def test_a_pair_the_grants_cannot_state_is_reported_as_not_checkable(
    written: str, docs: dict[str, Doc]
) -> None:
    head = written.split("\n## `")[0]
    for rule in docs["review"]["conflicts"]:
        line = next(line for line in head.splitlines() if f"(`{rule['id']}`)" in line)
        if rule["id"] in CHECKABLE:
            assert ": checked, as " in line, rule["id"]
        else:
            assert f": not checkable. {rule['not_checkable']}" in line, rule["id"]
    unchecked = len(docs["review"]["conflicts"]) - len(CHECKABLE)
    for identity, section in sections(written).items():
        assert f"- Not checkable: {unchecked} of the" in section, identity


def test_the_schema_takes_a_pair_with_sides_or_a_reason_never_both_or_neither() -> None:
    validator = Draft202012Validator(json.loads((REPO / "access/review.schema.json").read_text()))
    sides = [{"product": "ledger", "access": "draft"}, {"product": "ledger", "access": "post"}]
    base = {"accounts": [], "owners": {}}
    pair = {"id": "a-pair", "duties": ["make", "check"]}
    assert validator.is_valid({**base, "conflicts": [{**pair, "sides": sides}]})
    assert validator.is_valid({**base, "conflicts": [{**pair, "not_checkable": "No grant."}]})
    for bad in (
        pair,
        {**pair, "sides": sides, "not_checkable": "No grant."},
        {**pair, "sides": sides[:1]},
        {**pair, "duties": ["make"], "sides": sides},
    ):
        assert not validator.is_valid({**base, "conflicts": [bad]}), bad


def test_the_report_is_written_where_git_ignores_it() -> None:
    from access.review import OUTPUT

    ignored = subprocess.run(  # noqa: S603
        ["git", "check-ignore", "-q", str(OUTPUT.relative_to(REPO))],  # noqa: S607
        cwd=REPO,
        check=False,
    )
    assert ignored.returncode == 0
    assert Path(OUTPUT).suffix == ".md"
