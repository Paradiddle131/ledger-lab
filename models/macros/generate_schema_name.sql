{#- Where a node is built: on BigQuery, the node's `schema` config trimmed of surrounding
    whitespace when it has one (the data products' folders in dbt_project.yml), the profile's
    dataset otherwise; on any other
    target, the profile's schema, so the local build keeps one schema. The metric versions gate
    (semantics/versions.py) allows this macro only while its text is the gate's own copy. -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
{%- if custom_schema_name is none or target.type != 'bigquery' -%}
{{ target.schema }}
{%- else -%}
{{ custom_schema_name | trim }}
{%- endif -%}
{%- endmacro %}
