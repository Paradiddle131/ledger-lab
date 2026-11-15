import json, subprocess, sys
from pathlib import Path
from semantics import versions
from tests.semantics.test_metric_versions import working, changed_from, REAL
PKG = "models/dbt_packages/pkg"
FILES = {
    "models/package-lock.yml": "packages:\n  - local: dbt_packages/pkg\n    name: pkg\n",
    f"{PKG}/dbt_project.yml": ("name: pkg\nversion: '1.0'\nconfig-version: 2\nmodel-paths: [models]\nmacro-paths: [macros]\n"
        "models:\n  pkg:\n    +schema: \"{{ env_var('ANY', 'x') }}\"\n"),
    f"{PKG}/models/pkg_rows.sql": "select 1 as id, date '2024-01-01' as day_at, 2.0 as amount\n",
    f"{PKG}/macros/generate_alias_name.sql": ("{% macro generate_alias_name(custom_alias_name=none, node=none) -%}\n"
        "{{ env_var('ANY', '') ~ node.name }}\n{%- endmacro %}\n"),
    "semantics/metricflow/pkg.yml": ("semantic_models:\n  - name: pkg_rows\n    model: ref('pkg', 'pkg_rows')\n"
        "    defaults: {agg_time_dimension: day_at}\n    entities:\n      - {name: pkg_row, type: primary, expr: id}\n"
        "    dimensions:\n      - {name: day_at, type: time, type_params: {time_granularity: day}}\n"
        "    measures:\n      - {name: pkg_amount, agg: sum, expr: amount}\n"
        "metrics:\n  - name: pkg_amount_total\n    label: Pkg amount\n    description: Sum.\n"
        "    type: simple\n    type_params: {measure: pkg_amount}\n"
        "    config: {meta: {version: 1, owner: metric-owner, term: x, role: x}}\n"),
}
def test_new_on_branch(tmp_path):
    repo = working(tmp_path, lambda f: f.update(FILES))
    subprocess.run(["git", "add", "-f", "-A"], cwd=repo, check=True)
    ok, report = versions.check(repo); print("GATE-new ok:", ok, report)
def test_on_both(tmp_path):
    repo = working(tmp_path, lambda f: None, changed_from(REAL, lambda f: f.update(FILES)))
    ok, report = versions.check(repo); print("GATE-both ok:", ok, report)
