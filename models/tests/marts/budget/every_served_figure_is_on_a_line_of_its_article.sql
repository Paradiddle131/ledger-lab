-- Fails on every non-zero figure a served row of a covered year carries that no line of its year,
-- kind, chapter code and article number carries at the same moment: a line made of two rows
-- that differ there, or a sum of a repeat, would drop or change a served figure.
{%- set moments = {
    'StandOWB': 'draft_budget_eur',
    'StandO1': 'first_supplementary_eur',
    'StandSBS': 'september_supplementary_eur',
    'StandO2': 'second_supplementary_eur',
    'Realisatie': 'outturn_eur',
} %}

with line_figures as (
    {%- for moment, column in moments.items() %}
        select
            fiscal_year,
            kind,
            chapter_code,
            article_number,
            '{{ moment }}' as moment,
            {{ column }} as amount_eur
        from {{ ref('budget_against_outturn') }}
        {%- if not loop.last %}
            union all
        {%- endif %}
    {%- endfor %}
),

served as (
    select served.*
    from {{ ref('stg_nl_government__budget_moments') }} as served
    inner join (select distinct fiscal_year from {{ ref('budget_against_outturn') }}) as covered
        on served.fiscal_year = covered.fiscal_year
    where served.amount_eur <> 0
)

select
    served.source_row,
    served.moment,
    served.amount_eur
from served
left join line_figures
    on
        served.fiscal_year = line_figures.fiscal_year
        and served.kind = line_figures.kind
        and served.chapter_code = line_figures.chapter_code
        and served.article_number = line_figures.article_number
        and served.moment = line_figures.moment
        and served.amount_eur = line_figures.amount_eur
where line_figures.amount_eur is null
