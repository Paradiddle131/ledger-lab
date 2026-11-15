import json, os, subprocess, sys
from pathlib import Path
from semantics import versions
from tests.semantics.test_metric_versions import FOLDER, SEED_READER, PROJECT_FILE, working
M = "{% macro return(value) -%}\n{{ env_var('ANY', '') ~ value }}\n{%- endmacro %}\n"
def test_return_override(tmp_path):
    ok, report = versions.check(working(tmp_path, lambda f: f.update({"models/macros/relation_probe.sql": M})))
    print("RETURN", ok, report)
def test_ns_list(tmp_path):
    edit = lambda f: f.update({PROJECT_FILE: f[PROJECT_FILE] + "\ndispatch:\n  - macro_namespace: [dbt]\n    search_order: [ledger_lab, dbt]\n"})
    ok, report = versions.check(working(tmp_path, edit)); print("NSLIST", ok, report)
def test_exact_names(tmp_path):
    for n in ("generate_schema_name", "generate_database_name"):
        m = "{% macro " + n + "(custom_schema_name=none, node=none) -%}\n{{ env_var('ANY', '') ~ target.schema }}\n{%- endmacro %}\n"
        ok, report = versions.check(working(tmp_path / n, lambda f: f.update({f"models/macros/{n}.sql": m})))
        print("EXACT", n, ok, report)
SNAPSHOT_SQL = ("{% snapshot period_status_history %}\n"
    "{{ config(target_schema=env_var('ANY', 'main') ~ '', unique_key='entity_id', strategy='check', check_cols='all') }}\n"
    "select * from {{ ref('period_status') }}\n{% endsnapshot %}\n")
def snapshot_sql(files):
    files["models/snapshots/history.sql"] = SNAPSHOT_SQL
    files[f"{FOLDER}/snapshots.yml"] = (SEED_READER.replace("period_statuses", "period_history")
        .replace("ref('period_status')", "ref('period_status_history')")
        .replace("period_status_row", "period_history_row").replace("closed_periods", "closed_history"))
def schemas(repo, v):
    env = {"PATH": os.environ["PATH"], "HOME": str(repo/"h"), "DBT_TARGET_PATH": str(repo/f"t{v}"),
           "DBT_SEND_ANONYMOUS_USAGE_STATS": "false", "LEDGER_LAB_DUCKDB_PATH": str(repo/"x.duckdb")}
    if v is not None: env["ANY"] = v
    subprocess.run([str(Path(sys.executable).parent/"dbt"), "parse", "--no-partial-parse", "--project-dir", str(repo/"models"),
        "--profiles-dir", str(repo/"models"), "--target", "local"], env=env, check=True, capture_output=True, cwd=repo)
    sm = json.loads((repo/f"t{v}"/"semantic_manifest.json").read_text())
    return {m["name"]: m["node_relation"]["schema_name"] for m in sm["semantic_models"] if m["name"] == "period_history"}
def test_snapshot_sql_config(tmp_path):
    repo = working(tmp_path, snapshot_sql)
    ok, report = versions.check(repo); print("SNAP ok=", ok, report[:300])
    print("SNAP", schemas(repo, None), schemas(repo, "consumer_schema"))
def outside(files):
    files["models/dbt_project.yml"] += "\npackages-install-path: ../pkgs\n"
    files["pkgs/evil/dbt_project.yml"] = "name: evil\nversion: '1.0.0'\nconfig-version: 2\n"
def test_install_path(tmp_path):
    repo = working(tmp_path, outside)
    ok, report = versions.check(repo); print("INSTALL GATE ok=", ok, report[:200])
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "DBT_TARGET_PATH": str(tmp_path/"t"),
           "LEDGER_LAB_DUCKDB_PATH": str(tmp_path/"x.duckdb"), "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
    done = subprocess.run([str(Path(sys.executable).parent/"dbt"), "parse", "--no-partial-parse", "--project-dir", str(repo/"models"),
        "--profiles-dir", str(repo/"models"), "--target", "local"], env=env, capture_output=True, text=True)
    print("INSTALL dbt parse of the clone exit", done.returncode, done.stdout.strip().splitlines()[-1])
