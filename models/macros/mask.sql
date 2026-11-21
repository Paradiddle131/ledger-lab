{#- A column as a masked reader reads it, by the masking rule of its tag in access/columns.yaml,
    rendered as BigQuery's built-in rules render them (access/compile.py's `masked` does the same
    for the persona databases): `hash` is SHA-256 of the value's UTF-8 bytes in Base64, so equal
    values still join and count; `last-four` keeps the last four characters after `XXXXX` and
    hashes a value of four characters or fewer; `nullify` reads null. Any other rule stops the
    build. -#}
{% macro mask(column, rule) %}
    {%- if rule == 'hash' -%}
        {{ return(adapter.dispatch('mask_hash')(column)) }}
    {%- elif rule == 'last-four' -%}
        {{ return("case when length(" ~ column ~ ") > 4 then 'XXXXX' || right(" ~ column ~ ", 4) else "
            ~ adapter.dispatch('mask_hash')(column) ~ " end") }}
    {%- elif rule == 'nullify' -%}
        {{ return("cast(null as " ~ dbt.type_string() ~ ")") }}
    {%- else -%}
        {{ exceptions.raise_compiler_error("mask takes hash, last-four or nullify, not " ~ rule) }}
    {%- endif -%}
{% endmacro %}

{% macro default__mask_hash(column) -%}
    to_base64(unhex(sha256({{ column }})))
{%- endmacro %}

{% macro bigquery__mask_hash(column) -%}
    to_base64(sha256({{ column }}))
{%- endmacro %}
