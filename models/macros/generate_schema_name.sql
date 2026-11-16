{#- The schema (on BigQuery, the dataset) a model, seed or snapshot is built in.

    On BigQuery it is the node's `schema` config exactly as dbt_project.yml sets it by layer, one of
    the datasets platform/gcp/warehouse.tf declares, with no target prefix; a model, seed or snapshot
    with no `schema` config stops the build here, so nothing is written to the profile's own
    dataset, which is not declared. A test or unit test keeps the profile's dataset: dbt builds no
    relation for one (no test here stores its failures).
    On any other engine every node keeps the target's schema (`main` on DuckDB), the `schema`
    config unread, so the local build is laid out as it was before the datasets were routed. -#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if target.type != 'bigquery' -%}
        {{ target.schema }}
    {%- elif custom_schema_name is not none -%}
        {{ custom_schema_name | trim }}
    {%- elif node.resource_type in ['model', 'seed', 'snapshot'] -%}
        {{ exceptions.raise_compiler_error(
            node.resource_type ~ " " ~ node.name ~ " has no dataset: set its `schema` config to "
            ~ "a dataset platform/gcp/warehouse.tf declares"
        ) }}
    {%- else -%}
        {{ target.schema }}
    {%- endif -%}
{%- endmacro %}
