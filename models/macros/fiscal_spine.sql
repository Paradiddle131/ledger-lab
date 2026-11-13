{#- Every `datepart` (day or month) from 1 January of the first fiscal year up to, not including,
    1 January two years after the last: wide enough for a year that starts in any month. -#}
{% macro fiscal_spine(datepart) %}
    {%- set years = var('fiscal_years') -%}
    {{ dbt.date_spine(
        datepart,
        "cast('" ~ years.first ~ "-01-01' as date)",
        "cast('" ~ (years.last + 2) ~ "-01-01' as date)",
    ) }}
{% endmacro %}
