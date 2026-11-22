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
from collections.abc import Callable
from typing import Any

import fences
import hcl2
import pytest
import yaml
from cost_check import (
    BILLED_READ_PERMISSION,
    BILLING_PERMISSIONS,
    KILL_SWITCH_ACCOUNT_PERMISSIONS,
    PROJECT_PERMISSIONS,
    READS,
)
from jsonschema import Draft202012Validator

from access.review import (
    OUTPUT,
    REPO,
    Conflict,
    Grant,
    blocks,
    conflicts,
    declared_grants,
    load_catalogues,
    load_config,
    render,
    review,
    unquote,
)
from tests.platform.test_access_rules import RECORDED, expected_dataset_grants, expected_readers

Doc = dict[str, Any]
Config = dict[str, list[Any]]
# The pairs the catalogues can state today, pinned here, so making another one checkable (or
# dropping this one) is a reviewed change to the data and to this line together.
CHECKABLE = {"enter-and-approve-entry"}
# The roles the owner granted the agent account by hand, as recorded on the tracker after the
# owner narrowed them; adding one is a reviewed change to the data and to these lines together.
# No predefined role on the project or on a table: #218 replaced service usage consumer and the
# cost view's data viewer role with the narrow custom roles below.
HAND_GRANTED: list[str] = []
HAND_GRANTED_CUSTOM = ["labQueryRunner", "labFenceReader"]
HAND_GRANTED_TABLES: list[tuple[str, str]] = []
HAND_GRANTED_CUSTOM_TABLES = [("labViewReader", "the billing export's cost view")]
# What a custom role on the cost view may hold: reading the view and its rows.
VIEW_READ = {"bigquery.tables.get", "bigquery.tables.getData"}
# The project permissions the cost check's REST reads need, from its own table of every read.
CHECK_READS = {p for _, p, scope in READS if scope == "project" and p is not None}
# Billing a read to the project, which the check's billed reads need beside its READS.
BILLED = {BILLED_READ_PERMISSION}
# The only BigQuery permissions a project-wide hand grant may hold: running jobs, for the cost
# query, and reading BigQuery's settings.
QUERY_PERMISSIONS = {"bigquery.jobs.create", "bigquery.config.get"}
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
        *{(TOKENS, f"account {p}") for p in [*personas, "publisher"]},
    }
    assert {k for k, source in rows.items() if source == "granted by hand, not declared"} == {
        ("Runs BigQuery queries (custom role)", "the lab project"),
        ("Reads the lab's spend fences (custom role)", "the lab project"),
        ("Reads the cost view (custom role)", "the billing export's cost view"),
    }
    acts = ", ".join(f"`{p}`" for p in sorted([*personas, "publisher"]))
    assert f"- Acts as: {acts}, holding each one's grants in turn\n" in section
    assert (
        "- Acts as its own owner, `agent-operator`, and every other owner "
        "(`auditor`, `group-controller`)\n"
    ) in section
    for tag in docs["columns"]["tags"]:
        assert f"{tag['id']} clear" in section
    (reach,) = [line for line in section.splitlines() if line.startswith("- Reach:")]
    for words in ("builder role", "no data role on the project", "only through its cost view"):
        assert words in reach, words
    for words in ("data editor", "creates datasets"):
        assert words not in reach, words
    assert (
        "- **Conflict acting as others, not refused:** `enter-and-approve-entry` on chicago: "
        "draft ledger as `proposer`; post ledger as `approver`\n"
    ) in section
    assert "- Conflicts: none" not in section


def test_the_publisher_writes_the_governed_copies_reads_the_marts_and_acts_as_nobody(
    written: str,
) -> None:
    section = sections(written)["publisher"]
    assert {k for k, source in roles(section).items() if source == "declared"} == {
        ("Publishes the governed copies (custom role)", "warehouse dataset payments"),
        ("Runs BigQuery queries (custom role)", "the lab project"),
        ("roles/bigquery.dataViewer", "warehouse dataset intermediate"),
    }
    assert "- Acts as: nobody\n" in section
    assert "- Owner: `group-controller`\n" in section


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


HAND_FIELDS = (
    "project_roles_granted_by_hand",
    "custom_roles_granted_by_hand",
    "table_roles_granted_by_hand",
    "custom_table_roles_granted_by_hand",
)


def agent_entry(docs: dict[str, Doc]) -> Doc:
    (agent,) = [a for a in docs["review"]["accounts"] if a["id"] == "agent"]
    return agent


def test_the_hand_granted_roles_are_the_recorded_narrow_set(docs: dict[str, Doc]) -> None:
    agent = agent_entry(docs)
    assert agent.get("project_roles_granted_by_hand", []) == HAND_GRANTED
    assert [r["id"] for r in agent["custom_roles_granted_by_hand"]] == HAND_GRANTED_CUSTOM
    assert [
        (g["role"], g["view"]) for g in agent.get("table_roles_granted_by_hand", [])
    ] == HAND_GRANTED_TABLES
    assert [
        (g["id"], g["view"]) for g in agent["custom_table_roles_granted_by_hand"]
    ] == HAND_GRANTED_CUSTOM_TABLES


def test_the_cost_views_custom_role_only_reads_the_view(docs: dict[str, Doc]) -> None:
    for role in agent_entry(docs)["custom_table_roles_granted_by_hand"]:
        assert set(role["permissions"]) <= VIEW_READ, role["id"]
    others = [a for a in docs["review"]["accounts"] if a["id"] != "agent"]
    assert all(field not in a for a in others for field in HAND_FIELDS)


def declared_custom_roles(config: Config) -> dict[str, Doc]:
    """Each custom role platform/gcp declares, by its role id, with its title and permissions."""
    return {
        unquote(body["role_id"]): {
            "title": unquote(body["title"]),
            "permissions": {unquote(p) for p in body["permissions"]},
        }
        for address, body in blocks(config, "resource").items()
        if address.startswith("google_project_iam_custom_role.")
    }


def test_a_declared_custom_role_granted_by_hand_is_recorded_as_declared(
    docs: dict[str, Doc], config: Config
) -> None:
    """A hand grant of a role platform/gcp declares carries its declared title and permissions;
    a role created by hand is one platform/gcp does not declare."""
    declared = declared_custom_roles(config)
    for role in agent_entry(docs)["custom_roles_granted_by_hand"]:
        if role["created"] == "declared":
            assert role["id"] in declared, role["id"]
            assert role["title"] == declared[role["id"]]["title"]
            assert set(role["permissions"]) == declared[role["id"]]["permissions"]
        else:
            assert role["id"] not in declared, role["id"]


def test_the_fence_reader_holds_what_the_cost_checks_reads_need_and_nothing_else(
    docs: dict[str, Doc],
) -> None:
    """The fence reader holds the project permissions of the cost check's listed reads and billing
    a read to the project; the cost check refuses a read its table does not list
    (tests/platform/test_cost_check.py)."""
    (reader,) = [
        r for r in agent_entry(docs)["custom_roles_granted_by_hand"] if r["id"] == "labFenceReader"
    ]
    assert set(reader["permissions"]) == CHECK_READS | BILLED


def hand_granted_permissions(docs: dict[str, Doc]) -> dict[str, set[str]]:
    """Each role the agent holds by hand with the permissions it holds; an unrecorded predefined
    role holds a marker the pin and the token and access checks refuse."""
    agent = agent_entry(docs)
    held = {r["id"]: set(r["permissions"]) for r in agent["custom_roles_granted_by_hand"]}
    held |= {r["id"]: set(r["permissions"]) for r in agent["custom_table_roles_granted_by_hand"]}
    for role in [
        *agent.get("project_roles_granted_by_hand", []),
        *(g["role"] for g in agent.get("table_roles_granted_by_hand", [])),
    ]:
        held[role] = set(RECORDED.get(role, ["<no recorded permission list>"]))
    return held


# Permissions ending in one of these verbs on a BigQuery or Cloud Storage resource other than jobs
# (what a job may write is granted on the datasets); a backstop beside the allowlist of what a
# project-wide hand role may hold, not a list of every write.
DATA_WRITES = re.compile(
    r"^(bigquery|storage)\.(?!jobs\.)[A-Za-z]+\.(create|delete|update|updateData|updateTag|setCategory|"
    r"overrideTimeTravelRestrictions|restore|undelete)$"
)


def test_no_project_wide_hand_role_holds_a_bigquery_or_storage_permission_but_running_queries(
    docs: dict[str, Doc],
) -> None:
    """A project-wide hand grant holds no BigQuery permission but running jobs and reading
    BigQuery's settings, and no Cloud Storage permission."""
    held = hand_granted_permissions(docs)
    data = {
        p
        for role in [*HAND_GRANTED, *HAND_GRANTED_CUSTOM]
        for p in held[role]
        if p.split(".")[0] in ("bigquery", "storage") and p not in QUERY_PERMISSIONS
    }
    assert data == set()


def test_no_hand_granted_role_holds_a_listed_data_write_verb(docs: dict[str, Doc]) -> None:
    held = hand_granted_permissions(docs)
    assert {p for perms in held.values() for p in perms if DATA_WRITES.match(p)} == set()


@pytest.mark.parametrize(
    "extra",
    [
        "bigquery.tables.getData",
        "bigquery.routines.get",
        "bigquery.models.getData",
        "bigquery.rowAccessPolicies.list",
        "storage.objects.get",
    ],
)
def test_a_project_wide_hand_role_reading_data_is_refused(docs: dict[str, Doc], extra: str) -> None:
    planted = copy.deepcopy(docs)
    for role in agent_entry(planted)["custom_roles_granted_by_hand"]:
        if role["id"] == "labFenceReader":
            role["permissions"].append(extra)
    with pytest.raises(AssertionError):
        test_no_project_wide_hand_role_holds_a_bigquery_or_storage_permission_but_running_queries(
            planted
        )


@pytest.mark.parametrize(
    "extra",
    [
        "bigquery.models.delete",
        "bigquery.rowAccessPolicies.delete",
        "storage.objects.create",
        "bigquery.tables.updateData",
        "bigquery.datasets.update",
    ],
)
def test_a_hand_role_writing_data_is_refused(docs: dict[str, Doc], extra: str) -> None:
    planted = copy.deepcopy(docs)
    for role in agent_entry(planted)["custom_roles_granted_by_hand"]:
        if role["id"] == "labFenceReader":
            role["permissions"].append(extra)
    with pytest.raises(AssertionError):
        test_no_hand_granted_role_holds_a_listed_data_write_verb(planted)


def test_a_hand_made_custom_role_holds_only_the_checks_reads_and_running_queries(
    docs: dict[str, Doc],
) -> None:
    """An allowlist: a custom role granted by hand on the project holds only permissions the cost
    check's listed reads need or the two that run queries, so a token, access or data permission
    cannot be added to one without a row in the cost check's READS."""
    allowed = CHECK_READS | QUERY_PERMISSIONS | BILLED
    for role in agent_entry(docs)["custom_roles_granted_by_hand"]:
        assert set(role["permissions"]) <= allowed, role["id"]


@pytest.mark.parametrize(
    "extra",
    [
        "iam.serviceAccounts.getAccessToken",
        "iam.serviceAccounts.signJwt",
        "iam.serviceAccounts.signBlob",
        "iam.serviceAccounts.getOpenIdToken",
        "iam.serviceAccounts.implicitDelegation",
        "datacatalog.categories.fineGrainedGet",
        "bigquery.tables.restoreSnapshot",
    ],
)
def test_a_planted_permission_outside_the_allowlist_is_refused(
    docs: dict[str, Doc], extra: str
) -> None:
    planted = copy.deepcopy(docs)
    agent_entry(planted)["custom_roles_granted_by_hand"].append(
        {"id": "labPlanted", "title": "Planted", "created": "by hand", "permissions": [extra]}
    )
    with pytest.raises(AssertionError):
        test_a_hand_made_custom_role_holds_only_the_checks_reads_and_running_queries(planted)


@pytest.mark.parametrize(
    "extra",
    [
        "iam.serviceAccounts.getAccessToken",
        "iam.serviceAccounts.signJwt",
        "iam.serviceAccounts.signBlob",
        "iam.serviceAccounts.getOpenIdToken",
        "iam.serviceAccounts.implicitDelegation",
        "bigquery.datasets.setIamPolicy",
    ],
)
def test_a_custom_role_minting_tokens_or_setting_access_is_refused(
    docs: dict[str, Doc], extra: str
) -> None:
    planted = copy.deepcopy(docs)
    for role in agent_entry(planted)["custom_roles_granted_by_hand"]:
        if role["id"] == "labFenceReader":
            role["permissions"].append(extra)
    with pytest.raises(AssertionError):
        test_no_hand_granted_custom_role_mints_tokens_sets_access_or_moves_a_fence(
            planted, "labFenceReader"
        )


@pytest.mark.parametrize("missing", ["serviceusage.quotas.get", BILLED_READ_PERMISSION])
def test_a_fence_reader_lacking_a_service_usage_permission_is_refused(
    docs: dict[str, Doc], missing: str
) -> None:
    planted = copy.deepcopy(docs)
    for role in agent_entry(planted)["custom_roles_granted_by_hand"]:
        if role["id"] == "labFenceReader":
            role["permissions"].remove(missing)
    with pytest.raises(AssertionError):
        test_the_fence_reader_holds_what_the_cost_checks_reads_need_and_nothing_else(planted)


@pytest.mark.parametrize(
    "extra",
    ["bigquery.tables.export", "bigquery.tables.getIamPolicy", "bigquery.tables.updateData"],
)
def test_a_cost_view_role_beyond_reading_is_refused(docs: dict[str, Doc], extra: str) -> None:
    planted = copy.deepcopy(docs)
    agent_entry(planted)["custom_table_roles_granted_by_hand"][0]["permissions"].append(extra)
    with pytest.raises(AssertionError):
        test_the_cost_views_custom_role_only_reads_the_view(planted)


def test_a_hand_made_role_that_platform_gcp_declares_is_refused(
    docs: dict[str, Doc], config: Config
) -> None:
    planted = copy.deepcopy(docs)
    for role in agent_entry(planted)["custom_roles_granted_by_hand"]:
        if role["id"] == "labQueryRunner":
            role["created"] = "by hand"
    with pytest.raises(AssertionError):
        test_a_declared_custom_role_granted_by_hand_is_recorded_as_declared(planted, config)


@pytest.mark.parametrize(
    "break_it",
    [
        lambda r: r["custom_roles_granted_by_hand"][0].update(permissions=[]),
        lambda r: r["custom_roles_granted_by_hand"][0].pop("created"),
        lambda r: r["custom_roles_granted_by_hand"][0].update(created="maybe"),
        lambda r: r["custom_roles_granted_by_hand"][0].pop("permissions"),
        lambda r: r["custom_table_roles_granted_by_hand"][0].pop("view"),
        lambda r: r["custom_table_roles_granted_by_hand"][0].pop("permissions"),
        lambda r: r.update(
            table_roles_granted_by_hand=[{"role": "bigquery.dataViewer", "view": "x"}]
        ),
        lambda r: r["custom_roles_granted_by_hand"][0]["permissions"].append(
            "BigQuery.tables.getData"
        ),
        lambda r: r["custom_roles_granted_by_hand"][0].update(permission=["x.y.get"]),
        lambda r: r["custom_table_roles_granted_by_hand"][0].update(table="x.y"),
    ],
)
def test_a_malformed_hand_grant_is_refused_by_the_schema(break_it: Callable[[Doc], None]) -> None:
    review_doc = yaml.safe_load((REPO / "access/review.yaml").read_text())
    (agent,) = [a for a in review_doc["accounts"] if a["id"] == "agent"]
    break_it(agent)
    validator = Draft202012Validator(json.loads((REPO / "access/review.schema.json").read_text()))
    assert list(validator.iter_errors(review_doc)) != []


def refused_permissions(role: str) -> set[str]:
    """What a role holds that no hand-granted role may: a fence-moving permission, minting a
    token or acting as an account, or setting anyone's access; an unrecorded role is refused."""
    if role not in RECORDED:
        return {"<no recorded permission list>"}
    held = set(RECORDED[role])
    return (held & REFUSED) | {p for p in held if p.endswith(".setIamPolicy")}


def test_no_hand_granted_predefined_role_mints_tokens_sets_access_or_moves_a_fence() -> None:
    for role in [*HAND_GRANTED, *(r for r, _ in HAND_GRANTED_TABLES)]:
        assert refused_permissions(role) == set(), role


@pytest.mark.parametrize(
    "role", [*HAND_GRANTED_CUSTOM, *(r for r, _ in HAND_GRANTED_CUSTOM_TABLES)]
)
def test_no_hand_granted_custom_role_mints_tokens_sets_access_or_moves_a_fence(
    docs: dict[str, Doc], role: str
) -> None:
    held = hand_granted_permissions(docs)[role]
    assert (held & REFUSED) | {p for p in held if p.endswith(".setIamPolicy")} == set()


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
