"""The access review (`python -m access.review`): every identity in the report with its kind,
owner, catalogue grants, the roles the configuration gives it, its tag reads and whom it can act
as; the duty check that refuses a persona holding both sides of a conflicting pair; and the
refusal to write a report from a configuration holding a grant the review cannot attribute.

What the report must say is stated here from the catalogues and from the grants the access rules
tests derive in `platform/gcp`, apart from the code under test. Nothing here calls GCP.
"""

import copy
import json
import re
import subprocess
import sys
from typing import Any

import fences
import hcl2
import pytest
from cost_check import BILLING_PERMISSIONS, KILL_SWITCH_ACCOUNT_PERMISSIONS, PROJECT_PERMISSIONS
from jsonschema import Draft202012Validator

from access.review import (
    OUTPUT,
    REPO,
    Conflict,
    Grant,
    conflicts,
    declared_grants,
    load_catalogues,
    load_config,
    render,
    review,
)
from tests.platform.test_access_rules import RECORDED, expected_dataset_grants, expected_readers

Doc = dict[str, Any]
Config = dict[str, list[Any]]
# The pairs the catalogues can state today, pinned here, so making another one checkable (or
# dropping this one) is a reviewed change to the data and to this line together.
CHECKABLE = {"enter-and-approve-entry"}
# The project roles the owner granted the agent account by hand when making it, as recorded on
# the tracker; adding one is a reviewed change to the data and to this line together.
HAND_GRANTED = [
    "roles/viewer",
    "roles/bigquery.jobUser",
    "roles/bigquery.dataEditor",
    "roles/serviceusage.serviceUsageConsumer",
]
# Permissions no hand-granted role may hold: the cost check's fence-moving ones (minting a token
# and acting as an account among them) and setting anyone's access to anything.
REFUSED = {*PROJECT_PERMISSIONS, *BILLING_PERMISSIONS, *KILL_SWITCH_ACCOUNT_PERMISSIONS}
QUERY_ROLE = ("Runs BigQuery queries (custom role)", "the lab project")
CLEAR = "roles/datacatalog.categoryFineGrainedReader"
MASKED = "roles/bigquerydatapolicy.maskedReader"
TOKENS = "roles/iam.serviceAccountTokenCreator"
KINDS = {"human": "human persona", "agent": "agent persona"}
KILL_SWITCH_DECLARED = {
    ("roles/billing.projectManager", "the lab project"),
    ("roles/eventarc.eventReceiver", "the lab project"),
    ("roles/workflows.invoker", "the lab project"),
}


@pytest.fixture(scope="module")
def docs() -> dict[str, Doc]:
    return load_catalogues()


@pytest.fixture(scope="module")
def config() -> Config:
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


def table(section: str, header: str) -> list[list[str]]:
    """The rows of the table in the section whose header starts with `header`."""
    lines = section.splitlines()
    if f"| {header}" not in [line[: len(header) + 2] for line in lines]:
        return []
    start = next(i for i, line in enumerate(lines) if line.startswith(f"| {header}"))
    rows = []
    for line in lines[start + 2 :]:
        if not line.startswith("| "):
            break
        rows.append(line.strip("| ").split(" | "))
    return rows


def roles(section: str) -> dict[tuple[str, str], str]:
    return {(role, on): source for role, on, source in table(section, "Role")}


def expected_roles(persona: str) -> set[tuple[str, str]]:
    """What the configuration gives a persona, as the access rules tests derive it."""
    grants = {
        (r, f"warehouse dataset {d}") for p, d, r in expected_dataset_grants() if p == persona
    }
    query = {QUERY_ROLE} if grants else set()
    clear = {(CLEAR, f"policy tag {t}") for t, p in expected_readers("clear") if p == persona}
    masked = {(MASKED, f"masking rule {t}") for t, p in expected_readers("masked") if p == persona}
    return grants | query | clear | masked


def test_the_report_covers_every_persona_with_its_kind_owner_grants_roles_and_tags(
    written: str, docs: dict[str, Doc]
) -> None:
    found = sections(written)
    owners = docs["review"]["owners"]
    for persona in docs["access"]["personas"]:
        section = found.pop(persona["id"])
        assert f"- Kind: {KINDS[persona['kind']]}\n" in section, persona["id"]
        assert f"- Owner: `{owners[persona['id']]}`\n" in section, persona["id"]
        assert {tuple(row) for row in table(section, "Product")} == {
            (g["product"], g.get("entity", "group"), g["access"])
            for g in docs["access"]["grants"]
            if g["persona"] == persona["id"]
        }, persona["id"]
        assert set(roles(section).values()) <= {"declared"}, persona["id"]
        assert set(roles(section)) == expected_roles(persona["id"]), persona["id"]
        for tag in docs["columns"]["tags"]:
            read = (
                "clear"
                if persona["id"] in tag["clear"]
                else f"masked ({tag['masking']})"
                if persona["id"] in tag["masked"]
                else "no role"
            )
            assert f"{tag['id']} {read}" in section, (persona["id"], tag["id"])
        assert "- Acts as: nobody\n" in section, persona["id"]
    assert set(found) == {a["id"] for a in docs["review"]["accounts"]}


def test_the_agent_account_shows_its_roles_its_reach_and_the_conflict_it_holds(
    written: str, docs: dict[str, Doc]
) -> None:
    section = sections(written)["agent"]
    personas = [p["id"] for p in docs["access"]["personas"]]
    rows = roles(section)
    assert {k for k, source in rows.items() if source == "declared"} == {
        ("Builds the warehouse models (custom role)", "every warehouse dataset"),
        (CLEAR, "every policy tag"),
        ("roles/datacatalog.viewer", "the policy-tag taxonomy"),
        ("roles/storage.objectUser", "the landing bucket"),
        *{(TOKENS, f"account {p}") for p in personas},
    }
    assert {k for k, source in rows.items() if source == "granted by hand, not declared"} == {
        (role, "the lab project") for role in HAND_GRANTED
    }
    acts = ", ".join(f"`{p}`" for p in sorted(personas))
    assert f"- Acts as: {acts}, holding each one's grants in turn\n" in section
    assert (
        "- Acts as its own owner, `agent-operator`, and every other owner "
        "(`auditor`, `group-controller`)\n"
    ) in section
    for tag in docs["columns"]["tags"]:
        assert f"{tag['id']} clear" in section
    (reach,) = [line for line in section.splitlines() if line.startswith("- Reach:")]
    for words in ("data editor", "every dataset", "billing export", "creates datasets"):
        assert words in reach, words
    assert (
        "- **Conflict acting as others, not refused:** `enter-and-approve-entry` on chicago: "
        "draft ledger as `proposer`; post ledger as `approver`\n"
    ) in section
    assert "- Conflicts: none" not in section


def test_the_kill_switch_shows_its_project_roles_and_reads_nothing(written: str) -> None:
    section = sections(written)["kill-switch"]
    assert set(roles(section)) == KILL_SWITCH_DECLARED
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
    for leak in ("@", "gserviceaccount", "projects/", "${", "var.", "each."):
        assert leak not in written, leak


def test_the_report_says_what_declared_and_by_hand_mean_and_who_is_not_reviewed(
    written: str,
) -> None:
    head = written.split("\n## ")[0]
    assert "may not be in force yet" in head
    assert "this review does not verify it" in head
    assert "The project owner, who applies the configuration and administers access" in head
    assert "`auditor` and `group-controller` own each other." in head


def summary(report: str) -> list[list[str]]:
    return table(report.split("\n## ")[0], "Identity")


def test_no_summary_cell_reads_as_a_pass_and_every_row_counts_the_unchecked_pairs(
    written: str, docs: dict[str, Doc]
) -> None:
    unchecked = len(docs["review"]["conflicts"]) - len(CHECKABLE)
    rows = summary(written)
    assert len(rows) == len(docs["access"]["personas"]) + len(docs["review"]["accounts"])
    for row in rows:
        assert row[-1] == f"{unchecked} pairs", row
        for cell in row:
            assert not re.search(r"\b(pass|passes|passed|ok|clean)\b", cell, re.IGNORECASE), row


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
    docs: dict[str, Doc], config: Config
) -> None:
    for change in ("drop", "add"):
        planted = copy.deepcopy(docs)
        if change == "drop":
            del planted["review"]["owners"]["proposer"]
        else:
            planted["review"]["owners"]["nobody-at-all"] = "auditor"
        with pytest.raises(ValueError, match="one owner for each identity"):
            review(planted, config)


def test_the_hand_granted_roles_are_the_recorded_four(docs: dict[str, Doc]) -> None:
    (agent,) = [a for a in docs["review"]["accounts"] if a["id"] == "agent"]
    assert agent["project_roles_granted_by_hand"] == HAND_GRANTED
    others = [a for a in docs["review"]["accounts"] if a["id"] != "agent"]
    assert all("project_roles_granted_by_hand" not in a for a in others)


def refused_permissions(role: str) -> set[str]:
    """What a role holds that no hand-granted role may: a fence-moving permission, minting a
    token or acting as an account, or setting anyone's access; an unrecorded role is refused."""
    if role not in RECORDED:
        return {"<no recorded permission list>"}
    held = set(RECORDED[role])
    return (held & REFUSED) | {p for p in held if p.endswith(".setIamPolicy")}


@pytest.mark.parametrize("role", HAND_GRANTED)
def test_no_hand_granted_role_mints_tokens_sets_access_or_moves_a_fence(role: str) -> None:
    assert refused_permissions(role) == set()


@pytest.mark.parametrize(
    ("role", "refused"),
    [
        ("roles/bigquery.dataOwner", {"bigquery.datasets.setIamPolicy"}),
        ("roles/iam.serviceAccountTokenCreator", {"<no recorded permission list>"}),
    ],
)
def test_a_role_that_sets_access_or_is_unrecorded_is_refused(role: str, refused: set[str]) -> None:
    assert refused <= refused_permissions(role)


def test_a_service_account_the_review_does_not_list_is_refused(
    docs: dict[str, Doc], config: Config
) -> None:
    planted = copy.deepcopy(config)
    planted["resource"].append({"google_service_account": {"unlisted": {"account_id": "x"}}})
    with pytest.raises(ValueError, match="does not list"):
        declared_grants(docs, planted)
    ghost = copy.deepcopy(docs)
    account = {**ghost["review"]["accounts"][0], "id": "ghost"}
    ghost["review"]["accounts"].append({**account, "declared": "google_service_account.ghost"})
    with pytest.raises(ValueError, match="undeclared"):
        declared_grants(ghost, config)


def planted(config: Config, hcl: str) -> Config:
    """The configuration with the blocks of `hcl` added, as a `.tf` file beside the others."""
    added = hcl2.loads(hcl)
    merged = copy.deepcopy(config)
    for kind, value in added.items():
        if isinstance(value, list):
            merged.setdefault(kind, []).extend(value)
    return merged


AGENT = "serviceAccount:${data.google_service_account.agent.email}"
APPROVER = 'serviceAccount:${google_service_account.persona["approver"].email}'
# Grants the review cannot attribute or read, each of which must stop the report.
REFUSED_PLANTS = {
    "anyone on the internet on the bucket": """
resource "google_storage_bucket_iam_member" "public" {
  bucket = google_storage_bucket.landing.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}""",
    "a person's account as data owner of a dataset": """
resource "google_bigquery_dataset_iam_member" "someone" {
  dataset_id = google_bigquery_dataset.warehouse["payments"].dataset_id
  role       = "roles/bigquery.dataOwner"
  member     = "user:${var.someone}"
}""",
    "a dataset access block grant": """
resource "google_bigquery_dataset_access" "agent_owns" {
  dataset_id    = google_bigquery_dataset.warehouse["payments"].dataset_id
  role          = "OWNER"
  user_by_email = data.google_service_account.agent.email
}""",
    "an authoritative project policy": f"""
data "google_iam_policy" "admin" {{
  binding {{
    role    = "roles/owner"
    members = ["{AGENT}"]
  }}
}}
resource "google_project_iam_policy" "agent_policy" {{
  project     = data.google_project.lab.project_id
  policy_data = data.google_iam_policy.admin.policy_data
}}""",
    "the agent named through a local": f"""
locals {{
  agent_member = "{AGENT}"
}}
resource "google_project_iam_member" "agent_owner_local" {{
  project = data.google_project.lab.project_id
  role    = "roles/owner"
  member  = local.agent_member
}}""",
    "an outside principal acting as the approver": """
resource "google_iam_workload_identity_pool" "ext" {
  workload_identity_pool_id = "ext"
}
resource "google_service_account_iam_member" "ext_acts_as_approver" {
  service_account_id = google_service_account.persona["approver"].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.ext.name}/*"
}""",
    "a dataset with an inline access block": """
resource "google_bigquery_dataset" "side" {
  dataset_id = "side"
  access {
    role          = "OWNER"
    user_by_email = "someone"
  }
}""",
    "a grant on a resource the review has no words for": f"""
resource "google_pubsub_topic_iam_member" "agent_publishes" {{
  topic  = google_pubsub_topic.budget.name
  role   = "roles/pubsub.publisher"
  member = "{AGENT}"
}}""",
}


@pytest.mark.parametrize("plant", sorted(REFUSED_PLANTS))
def test_a_grant_the_review_cannot_attribute_or_read_stops_the_report(
    docs: dict[str, Doc], config: Config, plant: str
) -> None:
    with pytest.raises(ValueError, match="refuses the configuration"):
        review(docs, planted(config, REFUSED_PLANTS[plant]))


# Grants the review attributes, each with a line the report then holds in that identity's section.
SHOWN_PLANTS = {
    "the project owner role for the approver": (
        f"""
resource "google_project_iam_member" "approver_owner" {{
  project = data.google_project.lab.project_id
  role    = "roles/owner"
  member  = "{APPROVER}"
}}""",
        "approver",
        "| roles/owner | the lab project | declared |",
    ),
    "the proposer minting the approver's token": (
        """
resource "google_service_account_iam_member" "proposer_acts_as_approver" {
  service_account_id = google_service_account.persona["approver"].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.persona["proposer"].email}"
}""",
        "proposer",
        "- **Conflict acting as others, not refused:** `enter-and-approve-entry` on chicago",
    ),
    "the agent as owner by a binding": (
        f"""
resource "google_project_iam_binding" "agent_owner" {{
  project = data.google_project.lab.project_id
  role    = "roles/owner"
  members = ["{AGENT}"]
}}""",
        "agent",
        "| roles/owner | the lab project | declared |",
    ),
    "the agent named by its member attribute": (
        """
resource "google_project_iam_member" "agent_owner_attr" {
  project = data.google_project.lab.project_id
  role    = "roles/owner"
  member  = data.google_service_account.agent.member
}""",
        "agent",
        "| roles/owner | the lab project | declared |",
    ),
    "the kill switch named by its member attribute": (
        """
resource "google_project_iam_member" "ks_editor" {
  project = data.google_project.lab.project_id
  role    = "roles/editor"
  member  = google_service_account.kill_switch.member
}""",
        "kill-switch",
        "| roles/editor | the lab project | declared |",
    ),
    "the agent acting as the kill switch": (
        f"""
resource "google_service_account_iam_member" "agent_acts_as_ks" {{
  service_account_id = google_service_account.kill_switch.name
  role               = "roles/iam.serviceAccountUser"
  member             = "{AGENT}"
}}""",
        "agent",
        "`kill-switch`",
    ),
    "the kill switch minting every persona's token": (
        """
resource "google_service_account_iam_member" "ks_mints" {
  for_each           = google_service_account.persona
  service_account_id = each.value.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.kill_switch.email}"
}""",
        "kill-switch",
        "- **Conflict acting as others, not refused:** `enter-and-approve-entry` on chicago",
    ),
    "the kill switch acting as the agent, and through it as every persona": (
        """
resource "google_service_account_iam_member" "ks_acts_as_agent" {
  service_account_id = data.google_service_account.agent.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.kill_switch.email}"
}""",
        "kill-switch",
        "`agent`, `agent-operator`, `approver`",
    ),
    "the agent as masked reader of every rule": (
        f"""
resource "google_bigquery_datapolicy_data_policy_iam_member" "agent_masked" {{
  for_each       = google_bigquery_datapolicy_data_policy.masking
  data_policy_id = each.value.data_policy_id
  role           = "roles/bigquerydatapolicy.maskedReader"
  member         = "{AGENT}"
}}""",
        "agent",
        "| roles/bigquerydatapolicy.maskedReader | every masking rule | declared |",
    ),
}


@pytest.mark.parametrize("plant", sorted(SHOWN_PLANTS))
def test_a_grant_the_review_attributes_shows_in_its_identitys_section(
    docs: dict[str, Doc], config: Config, written: str, plant: str
) -> None:
    hcl, identity, line = SHOWN_PLANTS[plant]
    report = render(docs, review(docs, planted(config, hcl)))
    assert line in sections(report)[identity]
    assert line not in sections(written)[identity]


def test_the_review_reads_the_tf_files_through_the_fences_reader(config: Config) -> None:
    assert config == fences.load()


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
    docs: dict[str, Doc], config: Config, rule_id: str
) -> None:
    entity = docs["access"]["entities"][0]["id"]
    access, persona = plant(docs, rule_id, (entity, entity))
    (rule,) = [r for r in docs["review"]["conflicts"] if r["id"] == rule_id]
    scope = {p["id"]: p["scope"] for p in access["data_products"]}
    on = entity if scope[rule["sides"][0]["product"]] == "entity" else None
    reviewed = {r.id: r for r in review({**docs, "access": access}, config)}
    assert reviewed[persona].conflicts == (Conflict(rule_id, (on,)),)
    assert [r.id for r in reviewed.values() if r.conflicts] == [persona]
    with pytest.raises(AssertionError, match=persona):
        test_no_persona_holds_a_conflicting_pair({**docs, "access": access})
    report = render({**docs, "access": access}, list(reviewed.values()))
    assert (
        f"- **Conflict:** holds `{rule_id}` on {on or 'the group'}\n" in sections(report)[persona]
    )
    (row,) = [row for row in summary(report) if row[0] == f"`{persona}`"]
    assert row[4] == f"`{rule_id}` on {on or 'the group'}"


@pytest.mark.parametrize("scope", ["any-entity", "same-entity"])
def test_sides_on_two_entities_conflict_only_in_an_any_entity_pair(
    docs: dict[str, Doc], scope: str
) -> None:
    first, second = (e["id"] for e in docs["access"]["entities"][:2])
    rules = copy.deepcopy(docs["review"]["conflicts"])
    for rule in rules:
        if "sides" in rule:
            rule["scope"] = scope
    for rule_id in sorted(CHECKABLE):
        access, persona = plant(docs, rule_id, (first, second))
        held = [
            Grant(g["product"], g.get("entity"), g["access"])
            for g in access["grants"]
            if g["persona"] == persona
        ]
        found = conflicts(held, rules)
        assert found == ([Conflict(rule_id, (first, second))] if scope == "any-entity" else [])


def test_a_side_naming_no_product_is_refused(docs: dict[str, Doc], config: Config) -> None:
    changed = copy.deepcopy(docs)
    rule = next(r for r in changed["review"]["conflicts"] if "sides" in r)
    rule["sides"][0]["product"] = "ledgers"
    with pytest.raises(ValueError, match="ledgers"):
        review(changed, config)


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


def test_the_schema_takes_a_pair_with_sides_and_scope_or_a_reason_never_both_or_neither() -> None:
    validator = Draft202012Validator(json.loads((REPO / "access/review.schema.json").read_text()))
    sides = [{"product": "ledger", "access": "draft"}, {"product": "ledger", "access": "post"}]
    base = {"accounts": [], "owners": {}}
    pair = {"id": "a-pair", "duties": ["make", "check"]}
    checked = {**pair, "sides": sides, "scope": "any-entity"}
    assert validator.is_valid({**base, "conflicts": [checked]})
    assert validator.is_valid({**base, "conflicts": [{**pair, "not_checkable": "No grant."}]})
    for bad in (
        pair,
        {**checked, "not_checkable": "No grant."},
        {**pair, "sides": sides},
        {**pair, "not_checkable": "No grant.", "scope": "any-entity"},
        {**checked, "sides": sides[:1]},
        {**checked, "duties": ["make"]},
        {**checked, "scope": "anywhere"},
    ):
        assert not validator.is_valid({**base, "conflicts": [bad]}), bad


def test_the_report_is_written_where_git_ignores_it() -> None:
    ignored = subprocess.run(  # noqa: S603
        ["git", "check-ignore", "-q", str(OUTPUT.relative_to(REPO))],  # noqa: S607
        cwd=REPO,
        check=False,
    )
    assert ignored.returncode == 0
    assert OUTPUT.suffix == ".md"
