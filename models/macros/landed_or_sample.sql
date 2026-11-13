{#- The rows a model reads, as the `source_rows` var picks them for every source: `landed`, the
    file the fetch kit landed (the source `source_name`.`table_name`), or `sample`, the committed
    seed of real rows `sample_seed` the tests pass explicitly. Any other value stops the build. -#}
{% macro landed_or_sample(source_name, table_name, sample_seed) %}
    {%- set chosen = var('source_rows') -%}
    {%- if chosen == 'landed' -%}
        {{ return(source(source_name, table_name)) }}
    {%- elif chosen == 'sample' -%}
        {{ return(ref(sample_seed)) }}
    {%- else -%}
        {{ exceptions.raise_compiler_error(
            "the source_rows var is 'sample' or 'landed', not '" ~ chosen ~ "'"
        ) }}
    {%- endif -%}
{% endmacro %}
