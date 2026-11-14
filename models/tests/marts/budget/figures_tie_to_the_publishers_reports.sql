-- Fails on every figure below that the line differs from, and, over the landed file, on every one
-- whose line is missing. The figures are the publisher's own, in thousands of euros as its annual
-- reports print them ("bedragen x € 1.000" on rijksfinancien.nl), so they hold whatever rule
-- makes the lines: the King's allowance outturn (jaarverslag 2024 and 2025, chapter I, table 1),
-- the taxes article's cash outturn and budget as adopted (jaarverslag 2024, chapter IX, article
-- 1, served on two rows), and the Mobiliteitsfonds main roads after the second supplementary
-- budget of 2025 (chapter A, article 12). The committed sample holds only some of these lines.
{%- set published = [
    ('nl-government-2024-U-I-1-1', 'outturn_eur', 12043),
    ('nl-government-2025-U-I-1-1', 'outturn_eur', 12452),
    ('nl-government-2024-U-IX-1-1', 'outturn_eur', 3918024),
    ('nl-government-2024-U-IX-1-1', 'draft_budget_eur', 3475108),
    ('nl-government-2025-U-A-12-1', 'second_supplementary_eur', 4016078),
] %}

with published as (
    {%- for key, column, thousands in published %}
        select
            '{{ key }}' as budget_line_key,
            '{{ column }}' as figure,
            cast({{ thousands }} as {{ dbt.type_numeric() }}) * 1000 as published_eur
        {%- if not loop.last %}
            union all
        {%- endif %}
    {%- endfor %}
),

lines as (
    {%- for column in published | map(attribute=1) | unique %}
        select
            budget_line_key,
            '{{ column }}' as figure,
            {{ column }} as line_eur
        from {{ ref('budget_against_outturn') }}
        {%- if not loop.last %}
            union all
        {%- endif %}
    {%- endfor %}
)

select
    published.budget_line_key,
    published.figure,
    published.published_eur,
    lines.line_eur
from published
left join lines
    on published.budget_line_key = lines.budget_line_key and published.figure = lines.figure
where
    {% if var('source_rows') == 'landed' %}lines.line_eur is null or {% endif -%}
    lines.line_eur <> published.published_eur
