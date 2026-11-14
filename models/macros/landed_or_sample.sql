{#- The rows a model reads, as the `source_rows` var picks them for every source: `landed`, the
    file the fetch kit landed (the source `source_name`.`table_name`), or `sample`, the committed
    seed of real rows `sample_seed` the tests pass explicitly. Any other value stops the build, and
    so does `landed` while `LEDGER_LAB_LANDING` names the landing bucket: the models read a local
    landing, and BigQuery reads the bucket's files only once load jobs have loaded them. -#}
{% macro landed_or_sample(source_name, table_name, sample_seed) %}
    {%- set chosen = var('source_rows') -%}
    {%- if chosen == 'landed' and env_var('LEDGER_LAB_LANDING', 'landing').startswith('gs://') -%}
        {{ exceptions.raise_compiler_error(
            "the models read a local landing, and LEDGER_LAB_LANDING names the landing bucket"
        ) }}
    {%- elif chosen == 'landed' -%}
        {{ return(source(source_name, table_name)) }}
    {%- elif chosen == 'sample' -%}
        {{ return(ref(sample_seed)) }}
    {%- else -%}
        {{ exceptions.raise_compiler_error(
            "the source_rows var is 'sample' or 'landed', not '" ~ chosen ~ "'"
        ) }}
    {%- endif -%}
{% endmacro %}
