"""The warehouse's access rules in `platform/gcp`, read offline: datasets, the agent's data-owner
rights, the persona grants on datasets, the purposes' datasets of views and their grants, policy
tags and masking rules, all generated from `access/personas.yaml`, `access/purposes.yaml` and
`access/columns.yaml`, and none giving anyone a fence-moving permission.

The tests in `make check` read the HCL; the `tofu` ones (run by `make fences-validate`, after
`tofu init`) have OpenTofu evaluate the generated maps and compare them with what the catalogues
say. Nothing here plans, applies or calls GCP.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from cost_check import (
    BILLING_PERMISSIONS,
    KILL_SWITCH_ACCOUNT_PERMISSIONS,
    PROJECT_PERMISSIONS,
)
from cost_check import (
    READS as COST_READS,
)
from fences import GCP, blocks, local, one, unquote

from access.identities import READS

REPO = GCP.parents[1]
RECORDED = json.loads((Path(__file__).parent / "predefined_roles.json").read_text())["roles"]
FENCE_PERMISSIONS = {*PROJECT_PERMISSIONS, *BILLING_PERMISSIONS, *KILL_SWITCH_ACCOUNT_PERMISSIONS}

AGENT = "serviceAccount:${data.google_service_account.agent.email}"
KILL_SWITCH = "serviceAccount:${google_service_account.kill_switch.email}"
PERSONA_IN_MAP = "serviceAccount:${google_service_account.persona[each.value.persona].email}"
PERSONA_IN_SET = "serviceAccount:${google_service_account.persona[each.key].email}"
PUBLISHER = "serviceAccount:${google_service_account.publisher.email}"
# The model layers no persona reads, beside one dataset per data product.
LAYERS = {"landed", "intermediate"}
DATASET_ROLES = {"read": "roles/bigquery.dataViewer", "edit": "roles/bigquery.dataEditor"}
MASKING = {"nullify": "ALWAYS_NULL", "hash": "SHA256", "last-four": "LAST_FOUR_CHARACTERS"}
CLEAR = "roles/datacatalog.categoryFineGrainedReader"
MASKED = "roles/bigquerydatapolicy.maskedReader"
OWNER = "roles/bigquery.dataOwner"
VIEWER = "roles/datacatalog.viewer"
BUILDER = "${google_project_iam_custom_role.warehouse_builder.name}"
QUERY_RUNNER = "${google_project_iam_custom_role.query_runner.name}"
ROW_PUBLISHER = "${google_project_iam_custom_role.row_publisher.name}"
# Who reads every tag in clear: the payables clerk, who pays vendors, and the auditor, who reads
# everything; pinned here apart from the catalogue, so widening it is a reviewed change to both.
CLEAR_READERS = {"payables-clerk", "auditor"}
# Who reads every tag masked: the other personas that read payments, the one product carrying a
# tagged column, by rows. The metric owner reads payments only in aggregate and the agent operator
# not at all, so neither holds a role on a tag; pinned apart from the catalogue as above.
MASKED_READERS = {
    "accountant-chicago",
    "accountant-uk-transport",
    "accountant-nl-government",
    "group-controller",
    "approver",
}


def catalogue(name: str) -> dict[str, Any]:
    return yaml.safe_load((REPO / "access" / name).read_text())


def dataset_id(product: str) -> str:
    return product.replace("-", "_")


def aggregates_dataset(product: str) -> str:
    return f"{dataset_id(product)}_aggregates"


def expected_aggregate_datasets() -> dict[str, str]:
    """Each aggregates dataset, by the product whose aggregate tables it holds."""
    return {p: aggregates_dataset(p) for p in catalogue("aggregates.yaml")["products"]}


def expected_purpose_datasets() -> dict[str, str]:
    """Each purpose's dataset of views, by purpose id: the id with `_` for `-`."""
    return {u["id"]: dataset_id(u["id"]) for u in catalogue("purposes.yaml")["purposes"]}


def expected_datasets() -> set[str]:
    products = {dataset_id(p["id"]) for p in catalogue("personas.yaml")["data_products"]}
    views = set(expected_aggregate_datasets().values()) | set(expected_purpose_datasets().values())
    return LAYERS | products | views


def expected_built() -> set[str]:
    """The datasets the agent builds in: every one but those of views, which read as themselves."""
    views = set(expected_aggregate_datasets().values()) | set(expected_purpose_datasets().values())
    return expected_datasets() - views


def purpose_products() -> set[str]:
    """The products the models build tables of, whose rows are read only through purposes."""
    return {p["id"] for p in catalogue("personas.yaml")["data_products"] if p.get("tables")}


def table_products() -> dict[str, str]:
    return {
        table: p["id"]
        for p in catalogue("personas.yaml")["data_products"]
        for table in p.get("tables", [])
    }


def expected_purpose_grants() -> set[tuple[str, str]]:
    """Each persona of a purpose, as a read of the purpose's dataset."""
    return {
        (persona, dataset_id(u["id"]))
        for u in catalogue("purposes.yaml")["purposes"]
        for persona in u["personas"]
    }


def expected_purpose_sources() -> set[tuple[str, str]]:
    """Each purpose dataset with each dataset its views read: those of its tables' products."""
    products = table_products()
    return {
        (dataset_id(u["id"]), dataset_id(products[table]))
        for u in catalogue("purposes.yaml")["purposes"]
        for table in u["tables"]
    }


def expected_aggregate_grants() -> set[tuple[str, str]]:
    """Each aggregate grant on a product with aggregate tables, as the read of its views."""
    tables = expected_aggregate_datasets()
    return {
        (g["persona"], tables[g["product"]])
        for g in catalogue("personas.yaml")["grants"]
        if g["access"] == "aggregate" and g["product"] in tables
    }


def expected_dataset_grants() -> set[tuple[str, str, str]]:
    """Each read or edit grant on a product the models build no tables of yet, as its dataset role;
    a product with tables is read through purposes alone."""
    return {
        (g["persona"], dataset_id(g["product"]), DATASET_ROLES[g["access"]])
        for g in catalogue("personas.yaml")["grants"]
        if g["access"] in DATASET_ROLES and g["product"] not in purpose_products()
    }


def expected_readers(kind: str) -> set[tuple[str, str]]:
    return {(t["id"], p) for t in catalogue("columns.yaml")["tags"] for p in t[kind]}


def iam_members() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (rtype, name, body)
        for (rtype, name), body in blocks("resource").items()
        if rtype.endswith("_iam_member")
    ]


def squash(expression: str) -> str:
    return re.sub(r"\s+", " ", expression)


# The generated maps, as the HCL writes them; the `tofu` tests check what they evaluate to.
PERSONAS_FILE = 'yamldecode(file("${path.module}/../../access/personas.yaml"))'
COLUMNS_FROM_FILE = (
    '${{for t in yamldecode(file("${path.module}/../../access/columns.yaml")).tags : t.id => t}}'
)
DATASETS_FROM_FILE = re.compile(
    r"\$\{merge\(\{landed = \"[^\"]+\", intermediate = \"[^\"]+\"\}, "
    r'\{for p in local\.access\.data_products : replace\(p\.id, "-", "_"\) => p\.holds\}, '
    r'\{for d, p in local\.aggregate_datasets : d => "[^"]+"\}, '
    r'\{for d, u in local\.purpose_datasets : d => "[^"]+"\}\)\}'
)
AGGREGATES_FILE = 'yamldecode(file("${path.module}/../../access/aggregates.yaml"))'
AGGREGATE_DATASETS = (
    '${{for p in keys(local.aggregates.products) : "${replace(p, "-", "_")}_aggregates" => p}}'
)
AGGREGATE_GRANTS = (
    "${{for g in distinct([for g in local.access.grants : {persona = g.persona, "
    'dataset = "${replace(g.product, "-", "_")}_aggregates"} if g.access == "aggregate" && '
    "contains(keys(local.aggregates.products), g.product)]) : "
    '"${g.persona} ${g.dataset}" => g}}'
)
GRANTS_FROM_FILE = (
    "${{for g in distinct([for g in local.access.grants : {persona = g.persona, "
    'dataset = replace(g.product, "-", "_"), role = local.dataset_roles[g.access]} '
    "if contains(keys(local.dataset_roles), g.access) && "
    "!contains(local.purpose_products, g.product)]) : "
    '"${g.persona} ${g.dataset} ${g.role}" => g}}'
)
PURPOSES_FILE = 'yamldecode(file("${path.module}/../../access/purposes.yaml"))'
PURPOSE_DATASETS = '${{for u in local.purposes.purposes : replace(u.id, "-", "_") => u}}'
PURPOSE_PRODUCTS = (
    "${toset([for p in local.access.data_products : p.id if length(try(p.tables, [])) > 0])}"
)
PURPOSE_GRANTS = (
    "${{for g in distinct(flatten([for d, u in local.purpose_datasets : "
    "[for p in u.personas : {persona = p, dataset = d}]])) : "
    '"${g.persona} ${g.dataset}" => g}}'
)
TABLE_DATASETS = (
    "${merge([for p in local.access.data_products : "
    '{for t in try(p.tables, []) : t => replace(p.id, "-", "_")}] ...)}'
)
PURPOSE_SOURCES = (
    "${{for s in distinct(flatten([for d, u in local.purpose_datasets : "
    "[for t in keys(u.tables) : {views = d, source = local.table_datasets[t]}]])) : "
    '"${s.views} ${s.source}" => s}}'
)


def readers_from_columns(kind: str) -> str:
    return (
        f"${{{{for r in flatten([for t in local.columns : [for p in t.{kind} : "
        f'{{tag = t.id, persona = p}}]]) : "${{r.tag}} ${{r.persona}}" => r}}}}'
    )


def test_the_generated_maps_read_the_catalogues() -> None:
    assert local("access") == f"${{{PERSONAS_FILE}}}"
    assert DATASETS_FROM_FILE.fullmatch(squash(local("datasets")))
    assert squash(local("dataset_grants")) == GRANTS_FROM_FILE
    assert squash(local("warehouse_readers")) == (
        "${toset(concat([for g in local.dataset_grants : g.persona], "
        "[for g in local.aggregate_grants : g.persona], "
        "[for g in local.purpose_grants : g.persona]))}"
    )
    assert local("aggregates") == f"${{{AGGREGATES_FILE}}}"
    assert squash(local("aggregate_datasets")) == AGGREGATE_DATASETS
    assert squash(local("aggregate_grants")) == AGGREGATE_GRANTS
    assert squash(local("view_datasets")) == (
        "${setunion(keys(local.aggregate_datasets), keys(local.purpose_datasets))}"
    )
    assert squash(local("built")) == (
        "${{for d, _ in local.datasets : d => d if !contains(local.view_datasets, d)}}"
    )
    assert local("purposes") == f"${{{PURPOSES_FILE}}}"
    assert squash(local("purpose_datasets")) == PURPOSE_DATASETS
    assert squash(local("purpose_products")) == PURPOSE_PRODUCTS
    assert squash(local("purpose_grants")) == PURPOSE_GRANTS
    assert squash(local("table_datasets")) == TABLE_DATASETS
    assert squash(local("purpose_sources")) == PURPOSE_SOURCES
    assert squash(local("aggregate_sources")) == (
        "${{for d, p in local.aggregate_datasets : d => (contains(local.governed, "
        'replace(p, "-", "_")) ? "intermediate" : replace(p, "-", "_"))}}'
    )
    assert {unquote(k): unquote(v) for k, v in local("basic_roles").items()} == {
        "roles/bigquery.dataViewer": "READER",
        "roles/bigquery.dataEditor": "WRITER",
    }
    assert squash(local("columns")) == COLUMNS_FROM_FILE
    assert squash(local("clear_readers")) == readers_from_columns("clear")
    assert squash(local("masked_readers")) == readers_from_columns("masked")


def test_only_reading_accesses_take_a_dataset_role() -> None:
    roles = {k: unquote(v) for k, v in local("dataset_roles").items()}
    assert roles == DATASET_ROLES
    assert set(roles) == READS


def test_the_catalogues_masking_rules_are_bigquerys_built_in_ones() -> None:
    rules = {unquote(k): unquote(v) for k, v in local("masking_expressions").items()}
    assert rules == MASKING
    schema = json.loads((REPO / "access/columns.schema.json").read_text())
    tag = schema["properties"]["tags"]["items"]["properties"]
    assert set(tag["masking"]["enum"]) == set(MASKING)


def test_datasets_sit_in_the_landing_buckets_region() -> None:
    (body,) = [b for (t, _), b in blocks("resource").items() if t == "google_bigquery_dataset"]
    assert body["for_each"] == "${local.datasets}"
    assert body["dataset_id"] == "${each.key}"
    assert body["location"] == one("google_storage_bucket")["location"] == "${var.region}"
    # Exactly these settings: a table expiry, a billing model or a key would each change cost or
    # reach, and none is needed.
    assert set(body) - {"__is_block__"} == {"for_each", "dataset_id", "location", "description"}


def test_column_rules_sit_in_the_datasets_region() -> None:
    taxonomy = one("google_data_catalog_taxonomy")
    assert taxonomy["region"] == "${var.region}"
    assert [unquote(t) for t in taxonomy["activated_policy_types"]] == [
        "FINE_GRAINED_ACCESS_CONTROL"
    ]
    policy = blocks("resource")[("google_bigquery_datapolicy_data_policy", "masking")]
    assert policy["location"] == "${var.region}"


def test_one_policy_tag_and_one_built_in_masking_rule_per_catalogued_tag() -> None:
    resources = blocks("resource")
    tag = resources[("google_data_catalog_policy_tag", "sensitive")]
    assert tag["for_each"] == "${local.columns}"
    assert tag["taxonomy"] == "${google_data_catalog_taxonomy.sensitive.id}"
    assert tag["display_name"] == "${each.key}"
    policy = resources[("google_bigquery_datapolicy_data_policy", "masking")]
    assert policy["for_each"] == "${local.columns}"
    assert policy["policy_tag"] == "${google_data_catalog_policy_tag.sensitive[each.key].name}"
    assert unquote(policy["data_policy_type"]) == "DATA_MASKING_POLICY"
    (masking,) = policy["data_masking_policy"]
    # A built-in rule only: a custom masking routine is not confirmed free on on-demand BigQuery.
    assert set(masking) - {"__is_block__"} == {"predefined_expression"}
    assert masking["predefined_expression"] == "${local.masking_expressions[each.value.masking]}"


def test_every_grant_goes_to_a_persona_the_agent_the_publisher_or_the_kill_switch() -> None:
    for rtype, name, body in iam_members():
        member = unquote(body["member"])
        assert member in {AGENT, KILL_SWITCH, PERSONA_IN_MAP, PERSONA_IN_SET, PUBLISHER}, (
            rtype,
            name,
        )
        assert "condition" not in body, (rtype, name)


# The predefined role that holds the stateless chat permission and one other, nothing more.
NATIVE_AGENT_USER = "roles/geminidataanalytics.dataAgentStatelessUser"

# Each grant of the access rules: (resource, for_each, role, member).
ACCESS_GRANTS = {
    ("google_data_catalog_taxonomy_iam_member", "agent_reads_taxonomy"): (None, VIEWER, AGENT),
    ("google_data_catalog_policy_tag_iam_member", "persona_reads_in_clear"): (
        "${local.clear_readers}",
        CLEAR,
        PERSONA_IN_MAP,
    ),
    ("google_bigquery_datapolicy_data_policy_iam_member", "persona_reads_masked"): (
        "${local.masked_readers}",
        MASKED,
        PERSONA_IN_MAP,
    ),
    ("google_data_catalog_policy_tag_iam_member", "agent_reads_in_clear"): (
        "${google_data_catalog_policy_tag.sensitive}",
        CLEAR,
        AGENT,
    ),
    ("google_project_iam_member", "persona_runs_queries"): (
        "${local.warehouse_readers}",
        QUERY_RUNNER,
        PERSONA_IN_SET,
    ),
    ("google_project_iam_member", "publisher_runs_queries"): (None, QUERY_RUNNER, PUBLISHER),
    ("google_project_iam_member", "persona_asks_native_agent"): (
        "${local.warehouse_readers}",
        NATIVE_AGENT_USER,
        PERSONA_IN_SET,
    ),
}


def test_the_access_rules_grant_exactly_these_and_the_fences_their_own() -> None:
    found = {(t, n): b for t, n, b in iam_members()}
    fences = {
        ("google_project_iam_member", "kill_switch_billing"),
        ("google_project_iam_member", "kill_switch_receives_events"),
        ("google_project_iam_member", "kill_switch_invokes_workflows"),
        ("google_service_account_iam_member", "agent_mints_persona_tokens"),
        ("google_service_account_iam_member", "agent_mints_publisher_token"),
        ("google_service_account_iam_member", "agent_reads_kill_switch_account"),
        ("google_pubsub_topic_iam_member", "agent_reads_budget_topic"),
        ("google_storage_bucket_iam_member", "agent_lands_objects"),
    }
    assert set(found) == set(ACCESS_GRANTS) | fences
    for key, (for_each, role, member) in ACCESS_GRANTS.items():
        body = found[key]
        assert body.get("for_each") == for_each, key
        assert unquote(body["role"]) == role, key
        assert unquote(body["member"]) == member, key


# Each entry of a dataset's access list: (for_each, dataset, role, the account's email), its
# dataset and email as the expressions `platform/gcp` writes them.
DATASET = "${{google_bigquery_dataset.warehouse[{}].dataset_id}}"
VIEWER_BASIC = '${local.basic_roles["roles/bigquery.dataViewer"]}'
DATASET_ACCESS = {
    "agent_builds_warehouse": (
        "${local.built}",
        DATASET.format("each.key"),
        BUILDER,
        "${data.google_service_account.agent.email}",
    ),
    "persona_reads_product": (
        "${local.dataset_grants}",
        DATASET.format("each.value.dataset"),
        "${local.basic_roles[each.value.role]}",
        "${google_service_account.persona[each.value.persona].email}",
    ),
    "persona_reads_aggregates": (
        "${local.aggregate_grants}",
        DATASET.format("each.value.dataset"),
        VIEWER_BASIC,
        "${google_service_account.persona[each.value.persona].email}",
    ),
    "persona_reads_purposes": (
        "${local.purpose_grants}",
        DATASET.format("each.value.dataset"),
        VIEWER_BASIC,
        "${google_service_account.persona[each.value.persona].email}",
    ),
    "publisher_writes_governed_copies": (
        "${local.governed}",
        DATASET.format("each.key"),
        ROW_PUBLISHER,
        "${google_service_account.publisher.email}",
    ),
    "publisher_reads_marts": (
        None,
        DATASET.format('"intermediate"'),
        VIEWER_BASIC,
        "${google_service_account.publisher.email}",
    ),
}


def test_every_dataset_grant_is_an_access_entry_and_none_a_dataset_iam_member() -> None:
    """The provider's dataset IAM member rewrites the whole access list from its role entries,
    dropping a view authorization, so no dataset grant is one."""
    resources = blocks("resource")
    assert not [k for k in resources if k[0].startswith("google_bigquery_dataset_iam_")]
    entries = {n: b for (t, n), b in resources.items() if t == "google_bigquery_dataset_access"}
    assert set(entries) == {*DATASET_ACCESS, "aggregates_read_source", "purposes_read_sources"}
    for name, (for_each, dataset, role, email) in DATASET_ACCESS.items():
        body = entries[name]
        assert set(body) - {"__is_block__"} == {"dataset_id", "role", "user_by_email"} | (
            {"for_each"} if for_each else set()
        ), name
        assert body.get("for_each") == for_each, name
        assert body["dataset_id"] == dataset, name
        assert unquote(body["role"]) == role, name
        assert unquote(body["user_by_email"]) == email, name


def test_each_aggregates_dataset_authorizes_its_views_on_its_source_alone() -> None:
    body = blocks("resource")[("google_bigquery_dataset_access", "aggregates_read_source")]
    assert set(body) - {"__is_block__"} == {"for_each", "dataset_id", "dataset"}
    assert body["for_each"] == "${local.aggregate_sources}"
    assert body["dataset_id"] == DATASET.format("each.value")
    (entry,) = body["dataset"]
    assert [unquote(t) for t in entry["target_types"]] == ["VIEWS"]
    (views,) = entry["dataset"]
    assert views["dataset_id"] == DATASET.format("each.key")
    assert views["project_id"] == "${var.project}"


def test_each_purpose_dataset_authorizes_its_views_on_its_sources_alone() -> None:
    body = blocks("resource")[("google_bigquery_dataset_access", "purposes_read_sources")]
    assert set(body) - {"__is_block__"} == {"for_each", "dataset_id", "dataset"}
    assert body["for_each"] == "${local.purpose_sources}"
    assert body["dataset_id"] == DATASET.format("each.value.source")
    (entry,) = body["dataset"]
    assert [unquote(t) for t in entry["target_types"]] == ["VIEWS"]
    (views,) = entry["dataset"]
    assert views["dataset_id"] == DATASET.format("each.value.views")
    assert views["project_id"] == "${var.project}"


def test_a_purposes_views_read_its_tables_products_and_never_the_marts() -> None:
    # The governed copy, under its row access policies, and a group product's own table: a view
    # authorized on `intermediate` would read the marts, which carry no row access policy.
    sources = expected_purpose_sources()
    assert {source for _, source in sources} == {dataset_id(p) for p in purpose_products()}
    assert not {source for _, source in sources} & LAYERS
    assert sources == {
        ("payment_review", "payments"),
        ("group_close", "payments"),
        ("group_close", "group_totals"),
        ("audit", "payments"),
        ("audit", "group_totals"),
    }


def test_a_purpose_dataset_is_no_other_declared_dataset() -> None:
    others = LAYERS | {dataset_id(p["id"]) for p in catalogue("personas.yaml")["data_products"]}
    others |= set(expected_aggregate_datasets().values())
    purposes = list(expected_purpose_datasets().values())
    assert len(set(purposes)) == len(purposes)
    assert not set(purposes) & others


def test_no_persona_reads_a_product_with_tables_but_through_its_purposes() -> None:
    """Every row grant on a product with tables is served by a purpose of it, so moving the grant
    to the purposes' datasets takes no reader's rows away, and no product dataset role is left on
    such a product. An edit grant there would lose its writes, so none is held."""
    products = table_products()
    held = {
        (persona, products[table])
        for u in catalogue("purposes.yaml")["purposes"]
        for persona in u["personas"]
        for table in u["tables"]
    }
    rows = {
        (g["persona"], g["product"], g["access"])
        for g in catalogue("personas.yaml")["grants"]
        if g["access"] in DATASET_ROLES and g["product"] in purpose_products()
    }
    assert rows, "the catalogue has a row reader of a product with tables to hold"
    assert {(p, product) for p, product, _ in rows} == held
    assert {access for _, _, access in rows} == {"read"}
    assert not {d for _, d, _ in expected_dataset_grants()} & {
        dataset_id(p) for p in purpose_products()
    }
    assert purpose_products() == {"payments", "group-totals"}


def test_grants_land_on_the_resource_their_map_names() -> None:
    resources = blocks("resource")
    clear = resources[("google_data_catalog_policy_tag_iam_member", "persona_reads_in_clear")]
    assert clear["policy_tag"] == "${google_data_catalog_policy_tag.sensitive[each.value.tag].name}"
    masked = resources["google_bigquery_datapolicy_data_policy_iam_member", "persona_reads_masked"]
    taxonomy = resources["google_data_catalog_taxonomy_iam_member", "agent_reads_taxonomy"]
    assert taxonomy["taxonomy"] == "${google_data_catalog_taxonomy.sensitive.name}"
    assert masked["data_policy_id"] == (
        "${google_bigquery_datapolicy_data_policy.masking[each.value.tag].data_policy_id}"
    )
    assert masked["location"] == "${var.region}"


def test_project_roles_reach_only_the_kill_switch_and_the_personas_query_and_agent_roles() -> None:
    project = {
        name: (unquote(b["role"]), unquote(b["member"]))
        for (t, name), b in blocks("resource").items()
        if t == "google_project_iam_member"
    }
    assert project == {
        "kill_switch_billing": ("roles/billing.projectManager", KILL_SWITCH),
        "kill_switch_receives_events": ("roles/eventarc.eventReceiver", KILL_SWITCH),
        "kill_switch_invokes_workflows": ("roles/workflows.invoker", KILL_SWITCH),
        "persona_runs_queries": (QUERY_RUNNER, PERSONA_IN_SET),
        "publisher_runs_queries": (QUERY_RUNNER, PUBLISHER),
        "persona_asks_native_agent": (NATIVE_AGENT_USER, PERSONA_IN_SET),
    }


def test_the_native_agent_turns_on_its_two_apis_and_keeps_them_on_a_destroy() -> None:
    service = blocks("resource")[("google_project_service", "native_agent")]
    assert service["for_each"] == (
        '${toset(["geminidataanalytics.googleapis.com", "cloudaicompanion.googleapis.com"])}'
    )
    assert service["disable_on_destroy"] is False
    assert [n for t, n in blocks("resource") if t == "google_project_service"] == ["native_agent"]


def custom_role(name: str) -> set[str]:
    role = blocks("resource")[("google_project_iam_custom_role", name)]
    return {unquote(p) for p in role["permissions"]}


def test_only_the_query_builder_row_publisher_and_fence_item_reader_roles_are_custom() -> None:
    custom = {n for (t, n) in blocks("resource") if t == "google_project_iam_custom_role"}
    assert custom == {
        "query_runner",
        "warehouse_builder",
        "row_publisher",
        "topic_reader",
        "account_reader",
    }


# The cost check's reads held on one resource alone, by where its READS table says they are held:
# the custom role that carries each and the binding that grants it to the agent on that resource.
HELD_ON = {
    "budget topic": (
        "topic_reader",
        ("google_pubsub_topic_iam_member", "agent_reads_budget_topic"),
        "topic",
        "${google_pubsub_topic.budget.name}",
    ),
    "kill switch account": (
        "account_reader",
        ("google_service_account_iam_member", "agent_reads_kill_switch_account"),
        "service_account_id",
        "${google_service_account.kill_switch.name}",
    ),
}


def test_each_cost_check_read_held_on_one_resource_is_granted_there_alone() -> None:
    """A READS row held on one resource names a declared role holding exactly that read, bound to
    the agent on that resource and nowhere else; no other read is held off the project."""
    held = {scope: p for _, p, scope in COST_READS if scope in HELD_ON}
    assert set(held) == set(HELD_ON)
    assert {scope for _, _, scope in COST_READS} <= {*HELD_ON, "project", "billing account", "none"}
    resources = blocks("resource")
    for scope, (role, binding, attribute, on) in HELD_ON.items():
        assert custom_role(role) == {held[scope]}, scope
        assert custom_role(role).isdisjoint(FENCE_PERMISSIONS), scope
        body = resources[binding]
        assert body[attribute] == on, scope
        assert unquote(body["role"]) == f"${{google_project_iam_custom_role.{role}.name}}", scope
        assert unquote(body["member"]) == AGENT, scope
        bound = [
            k for k, b in resources.items() if f"google_project_iam_custom_role.{role}." in str(b)
        ]
        assert bound == [binding], scope


def test_the_row_publisher_role_only_replaces_a_tables_rows() -> None:
    permissions = custom_role("row_publisher")
    assert permissions == {
        "bigquery.tables.get",
        "bigquery.tables.getData",
        "bigquery.tables.updateData",
    }
    assert permissions.isdisjoint(FENCE_PERMISSIONS)


GOVERNED_FROM_FILE = (
    '${toset([for p in local.access.data_products : replace(p.id, "-", "_") '
    'if p.scope == "entity" && length(try(p.tables, [])) > 0])}'
)


def expected_governed() -> set[str]:
    """The datasets holding a governed copy: the entity-scoped products with tables, read as
    access/row_policies.py reads them."""
    from access.row_policies import entity_tables

    return {dataset for dataset, _ in entity_tables(catalogue("personas.yaml"))}


def test_the_publisher_writes_the_governed_datasets_only() -> None:
    assert squash(local("governed")) == GOVERNED_FROM_FILE
    assert expected_governed() == {"payments"}
    assert "intermediate" not in expected_governed()


def test_the_query_role_only_runs_jobs() -> None:
    permissions = custom_role("query_runner")
    assert permissions == {"bigquery.config.get", "bigquery.jobs.create"}
    assert permissions.isdisjoint(FENCE_PERMISSIONS)


def granted_roles() -> set[str]:
    """Every predefined role a resource-level grant of the access rules names."""
    roles = {role for _, role, _ in ACCESS_GRANTS.values() if role.startswith("roles/")}
    return roles | set(DATASET_ROLES.values())


def test_every_granted_role_has_a_recorded_permission_list() -> None:
    assert granted_roles() <= set(RECORDED)
    assert all(RECORDED[role] for role in granted_roles())


@pytest.mark.parametrize("role", sorted(RECORDED))
def test_no_granted_role_holds_a_fence_moving_permission(role: str) -> None:
    assert set(RECORDED[role]) & FENCE_PERMISSIONS == set()


# What the builder role must never hold: who may use a dataset or table, or deleting a dataset.
BUILDER_REFUSES = {
    "bigquery.datasets.setIamPolicy",
    "bigquery.tables.setIamPolicy",
    "bigquery.datasets.delete",
    "bigquery.datasets.update",
}


def test_the_builder_role_grants_no_access_deletes_no_dataset_and_moves_no_fence() -> None:
    builder = custom_role("warehouse_builder")
    assert builder & BUILDER_REFUSES == set()
    # Setting a row access policy's grantees is the one IAM write it needs.
    iam_writes = {p for p in builder if p.endswith(".setIamPolicy")}
    assert iam_writes == {"bigquery.rowAccessPolicies.setIamPolicy"}
    assert builder.isdisjoint(FENCE_PERMISSIONS)


def test_the_builder_role_holds_only_real_data_owner_permissions_on_bigquery() -> None:
    # Each is drawn from data owner's recorded list, so a misspelt one fails here, not at apply.
    builder = custom_role("warehouse_builder")
    assert builder <= set(RECORDED[OWNER])
    assert all(p.startswith("bigquery.") for p in builder)


# Data editor's table permissions the models do not need: search indexes, replicas, resource tags.
UNNEEDED_TABLE_PERMISSIONS = {
    "bigquery.tables.createIndex",
    "bigquery.tables.deleteIndex",
    "bigquery.tables.updateIndex",
    "bigquery.tables.replicateData",
    "bigquery.tables.updateTag",
}


def test_the_builder_role_builds_tables_with_row_policies_and_policy_tags() -> None:
    builder = custom_role("warehouse_builder")
    rows = {p for p in RECORDED[OWNER] if p.startswith("bigquery.rowAccessPolicies.")}
    assert rows <= builder, "creating, listing, deleting and granting row access policies"
    editor = RECORDED["roles/bigquery.dataEditor"]
    tables = {p for p in editor if p.startswith("bigquery.tables.")}
    assert tables - UNNEEDED_TABLE_PERMISSIONS <= builder
    assert "bigquery.tables.setCategory" in builder
    # Masking follows the policy tag, so attaching a data policy to a column directly is not needed.
    assert "bigquery.tables.setColumnDataPolicy" not in builder


def test_each_tag_is_read_in_clear_by_the_pinned_readers_only() -> None:
    for tag in catalogue("columns.yaml")["tags"]:
        assert set(tag["clear"]) == CLEAR_READERS, tag["id"]


def test_each_tag_is_read_masked_by_the_pinned_readers_only() -> None:
    for tag in catalogue("columns.yaml")["tags"]:
        assert set(tag["masked"]) == MASKED_READERS, tag["id"]


def test_the_metric_owner_and_the_agent_operator_read_no_group_totals_or_payments_dataset() -> None:
    # The metric owner reads payments and the group totals only in aggregate, which takes no
    # dataset role; the agent operator reads only the metric definitions and the audit log.
    grants = expected_dataset_grants()
    assert {g for g in grants if g[0] == "metric-owner"} == {
        ("metric-owner", "metric_definitions", DATASET_ROLES["edit"])
    }
    assert {g for g in grants if g[0] == "agent-operator"} == {
        ("agent-operator", "metric_definitions", DATASET_ROLES["read"]),
        ("agent-operator", "audit_log", DATASET_ROLES["read"]),
    }


def test_masking_rule_ids_are_the_tag_ids_with_underscores() -> None:
    policy = blocks("resource")[("google_bigquery_datapolicy_data_policy", "masking")]
    assert policy["data_policy_id"] == '${replace(each.key, "-", "_")}'


def test_the_taxonomy_and_its_tags_refuse_to_be_destroyed_by_an_apply() -> None:
    resources = blocks("resource")
    for key in (
        ("google_data_catalog_taxonomy", "sensitive"),
        ("google_data_catalog_policy_tag", "sensitive"),
    ):
        (lifecycle,) = resources[key]["lifecycle"]
        assert lifecycle["prevent_destroy"] is True, key


def tofu_eval(expression: str) -> Any:
    """Evaluate an expression over this configuration's locals, with placeholder variables."""
    # The pinned OpenTofu `make tofu` installs, which `make fences-validate` passes as TOFU.
    tofu = os.environ.get("TOFU", str(Path.home() / ".local/bin/tofu"))
    if not Path(tofu).exists():
        pytest.fail("no tofu: run `make fences-validate`")
    result = subprocess.run(  # noqa: S603
        [
            tofu,
            f"-chdir={GCP}",
            "console",
            "-var=project=example-project",
            "-var=billing_account=000000-000000-000000",
            "-var=agent_account_id=example-agent",
        ],
        input=f"jsonencode({expression})\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return json.loads(json.loads(result.stdout))


@pytest.mark.tofu
def test_tofu_generates_one_dataset_per_product_and_layer() -> None:
    assert set(tofu_eval("keys(local.datasets)")) == expected_datasets()


@pytest.mark.tofu
def test_tofu_generates_the_governed_datasets() -> None:
    assert set(tofu_eval("local.governed")) == expected_governed()


@pytest.mark.tofu
def test_tofu_generates_each_personas_dataset_grants_and_no_others() -> None:
    grants = tofu_eval("values(local.dataset_grants)")
    assert {(g["persona"], g["dataset"], g["role"]) for g in grants} == expected_dataset_grants()
    assert len(grants) == len(expected_dataset_grants())
    readers = (
        {p for p, _, _ in expected_dataset_grants()}
        | {p for p, _ in expected_aggregate_grants()}
        | {p for p, _ in expected_purpose_grants()}
    )
    assert set(tofu_eval("local.warehouse_readers")) == readers


@pytest.mark.tofu
def test_tofu_generates_the_purpose_reads_their_sources_and_keeps_the_agent_out() -> None:
    grants = tofu_eval("values(local.purpose_grants)")
    assert {(g["persona"], g["dataset"]) for g in grants} == expected_purpose_grants()
    assert len(grants) == len(expected_purpose_grants())
    sources = tofu_eval("values(local.purpose_sources)")
    assert {(s["views"], s["source"]) for s in sources} == expected_purpose_sources()
    assert len(sources) == len(expected_purpose_sources())
    assert set(tofu_eval("keys(local.purpose_datasets)")) == set(
        expected_purpose_datasets().values()
    )
    assert set(tofu_eval("keys(local.built)")) == expected_built()


@pytest.mark.tofu
def test_tofu_generates_the_aggregate_reads_their_sources_and_the_built_datasets() -> None:
    grants = tofu_eval("values(local.aggregate_grants)")
    assert {(g["persona"], g["dataset"]) for g in grants} == expected_aggregate_grants()
    assert len(grants) == len(expected_aggregate_grants())
    # payments is entity-scoped with a governed copy: its views read the mart, never the copy.
    assert tofu_eval("local.aggregate_sources") == {
        "payments_aggregates": "intermediate",
        "group_totals_aggregates": "group_totals",
    }
    assert set(tofu_eval("keys(local.datasets)")) == expected_datasets()
    assert set(tofu_eval("keys(local.built)")) == expected_built()


@pytest.mark.tofu
@pytest.mark.parametrize("kind", ["clear", "masked"])
def test_tofu_generates_each_tags_readers_and_no_others(kind: str) -> None:
    readers = tofu_eval(f"values(local.{kind}_readers)")
    assert {(r["tag"], r["persona"]) for r in readers} == expected_readers(kind)
    assert len(readers) == len(expected_readers(kind))


@pytest.mark.tofu
def test_tofu_maps_each_tag_to_its_built_in_masking_rule() -> None:
    rules = tofu_eval(
        '{for k, t in local.columns : replace(k, "-", "_") => local.masking_expressions[t.masking]}'
    )
    tags = catalogue("columns.yaml")["tags"]
    assert rules == {dataset_id(t["id"]): MASKING[t["masking"]] for t in tags}
