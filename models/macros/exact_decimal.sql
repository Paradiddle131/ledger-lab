{#- A decimal literal, such as `exact_decimal('0.05')`, typed as an exact decimal on each engine:
    DuckDB types `0.05` as a DECIMAL already, so it is written as given; BigQuery types it as a
    FLOAT64, so there it is a NUMERIC literal, `numeric '0.05'`. The text must be digits,
    optionally after a minus, with optionally a point followed by more digits; anything else stops
    the build. -#}
{% macro exact_decimal(text) %}
    {%- if not modules.re.fullmatch("-?[0-9]+(\.[0-9]+)?", text | string) -%}
        {{ exceptions.raise_compiler_error("exact_decimal takes a decimal's digits, not " ~ text) }}
    {%- endif -%}
    {{ return(adapter.dispatch('exact_decimal')(text | string)) }}
{% endmacro %}

{% macro default__exact_decimal(text) -%}
    {{ text }}
{%- endmacro %}

{% macro bigquery__exact_decimal(text) -%}
    numeric '{{ text }}'
{%- endmacro %}
