"""The `vendor_entity` model maps each landed vendor name to a legal entity or to none, with the
rule and score that matched it, and makes no wrong match on a labelled sample of public
companies.

`vendor_sample.yaml` holds the sample: organisation names as the landed files spell them, each
with the LEI it should match (none where the register has no record of it), and every record of
the pinned register that shares a name with one of them before or after its legal form, so the
fixture build sees the same candidates the real one does. The landed test runs the same sample
against the real landing.
"""

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from ingest.entities import REGISTER, Register, match_key
from ingest.fetch import landing_path
from ingest.individuals import is_organisation
from ingest.uk_transport.spend import BODIES
from tests.models.dbt_build import HASHED, SAMPLE, Build, built, dbt, query

SOURCES = ("chicago-contracts", "chicago-payments", "uk-transport-spend-2025")
# Of the sample's names that have a legal entity, the share the rules match to it: a floor set just
# under what they reach, so a rule that stops matching what it matched turns the test red. Wrong
# matches (a name matched to another entity than its own, or to any when it has none) must be none.
RECALL = 0.80


def labelled() -> list[dict[str, Any]]:
    return yaml.safe_load(SAMPLE.read_text())["vendors"]


def scored(build: Build) -> dict[str, Any]:
    """The model's answer for every sample name, and the precision, recall and counts by rule."""
    found = {
        (entity, name): (lei, f"{status} {rule or ''}".strip())
        for entity, name, lei, status, rule in query(
            build, "select entity_id, vendor_name, lei, match_status, match_rule from vendor_entity"
        )
    }
    by_rule: dict[str, dict[str, int]] = {}
    matched = right = expected = 0
    wrong = []
    for vendor in labelled():
        lei, rule = found[(vendor["entity"], vendor["name"])]
        counts = by_rule.setdefault(rule, {"names": 0, "right": 0})
        counts["names"] += 1
        counts["right"] += lei == vendor["lei"]
        expected += vendor["lei"] is not None
        if lei is not None:
            matched += 1
            right += lei == vendor["lei"]
            if lei != vendor["lei"]:
                wrong.append(vendor["name"])
    return {
        "precision": right / matched,
        "recall": right / expected,
        "matched": matched,
        "expected": expected,
        "wrong": wrong,
        "by rule": by_rule,
    }


def test_the_sample_holds_fifty_organisations_with_their_expected_match() -> None:
    vendors = labelled()
    assert len(vendors) >= 50
    assert len({(v["entity"], v["name"]) for v in vendors}) == len(vendors)
    assert sum(v["lei"] is not None for v in vendors) >= 40
    assert any(v["lei"] is None for v in vendors), "some names have no record to match"
    leis = {r["lei"] for r in yaml.safe_load(SAMPLE.read_text())["register"]}
    assert {v["lei"] for v in vendors if v["lei"]} <= leis


def test_the_matching_makes_no_wrong_match_on_the_labelled_sample(build: Build) -> None:
    score = scored(build)
    print("labelled sample:", score)
    assert score["wrong"] == []
    assert score["recall"] >= RECALL
    assert not any(rule.startswith("hashed") for rule in score["by rule"])


def test_a_hashed_name_is_never_matched(build: Build) -> None:
    rows = query(
        build,
        "select entity_id, lei, match_status, match_rule, match_score from vendor_entity"
        " where vendor_name like 'person:%' order by entity_id",
    )
    assert rows == [(e, None, "hashed", None, None) for e in ("chicago", "uk-transport")]
    assert query(build, "select count(*) from vendor_entity where vendor_name = ?", HASHED) == [
        (1,)
    ]


def model_keys(build: Build, names: list[str]) -> list[str]:
    """Each name's key by the models' own `vendor_match_key`, compiled by dbt and run on DuckDB."""
    result = dbt(
        build.env,
        build.cwd,
        "compile",
        "--inline",
        "select {{ vendor_match_key('n') }} as k from names order by i",
        "--output",
        "json",
        "--quiet",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    sql = json.loads(result.stdout)["compiled"]
    con = duckdb.connect()
    con.execute("create table names (i bigint, n varchar)")
    con.executemany("insert into names values (?, ?)", list(enumerate(names)))
    return [str(k) for (k,) in con.execute(sql).fetchall()]


def test_the_models_key_is_the_landings_key(build: Build) -> None:
    names = [
        "Apple Inc.",
        "ACME (U.K.) LTD",
        "Doe & Roe,  Limited",
        "Coöperatie Example U.A.",
        "Coo\u0308peratie Example",
        "Ex_ample's  Garage—North",
        "“Example”   Holdings",
        "Example² Holdings",
        "Großhandel Example GmbH",
        "Example™ Brands",
        "ＥＸＡＭＰＬＥ ＬＴＤ",
        "Example\u200bWidgets",
        "Example\u00a0Widgets",
        "İstanbul Example",
    ]
    assert model_keys(build, names) == [match_key(n) for n in names]


@pytest.mark.landed
def test_the_real_landing_matches_the_sample_and_every_vouched_name(tmp_path: Path) -> None:
    """Over the real landing: the sample's precision, the names by rule, and every name the
    landing kept in clear only because the register vouched for it matched exactly."""
    landing = landing_path().resolve()
    for name in (*SOURCES, REGISTER):
        if not (landing / name).exists():
            pytest.skip(f"{name} is not landed")
    build = built(tmp_path, landing)
    score = scored(build)
    print("labelled sample on the real register:", score)
    assert score["wrong"] == []
    assert score["recall"] >= RECALL
    by_rule = query(
        build,
        "select entity_id, match_status, match_rule, count(*), sum(landed_rows)"
        " from vendor_entity group by all order by all",
    )
    print("names and rows by entity and rule:", by_rule)
    register = Register.landed()
    clear = [
        (name, rule)
        for name, status, rule in query(
            build,
            "select vendor_name, match_status, match_rule from vendor_entity"
            " where match_status <> 'hashed' and vendor_name <> 'REDACTED'",
        )
        if not is_organisation(name, BODIES)
    ]
    print("names in clear on the register's word alone:", len(clear))
    assert clear, "the register vouches for some names"
    assert all(register.vouches(name) for name, _ in clear), "no clear name lacks evidence"
    assert {rule for _, rule in clear} == {"exact"}, "the model finds each vouching record"
    names = [
        str(n)
        for (n,) in query(
            build,
            "select distinct vendor_name from vendor_entity where match_status <> 'hashed'",
        )
    ]
    names += [
        str(n)
        for (n,) in duckdb.connect()
        .execute(
            'select distinct "Entity.LegalName" from read_parquet(?)', [str(landing / REGISTER)]
        )
        .fetchall()
    ]
    differ = sum(a != match_key(n) for a, n in zip(model_keys(build, names), names, strict=True))
    print(f"names whose model key differs from the landing's: {differ} of {len(names)}")
    assert differ == 0
