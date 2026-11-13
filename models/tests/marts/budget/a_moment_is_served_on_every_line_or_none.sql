-- Fails on every year and moment that is null on some lines and not on others, that is null
-- although the year's served rows carry a non-zero amount at it, or that holds figures although
-- they carry none: a moment the file does not serve for a year is null on every line, a served
-- one is a figure on every line, a zero as served included.
{%- set moments = {
    'StandOWB': 'draft_budget_eur',
    'StandO1': 'first_supplementary_eur',
    'StandSBS': 'september_supplementary_eur',
    'StandO2': 'second_supplementary_eur',
    'Realisatie': 'outturn_eur',
} %}

with lines as (
    {%- for moment, column in moments.items() %}
        select
            fiscal_year,
            '{{ moment }}' as moment,
            count(*) as lines,
            count({{ column }}) as figures
        from {{ ref('budget_against_outturn') }}
        group by fiscal_year
        {%- if not loop.last %}
            union all
        {%- endif %}
    {%- endfor %}
),

served as (
    select
        fiscal_year,
        moment,
        max(case when amount_eur <> 0 then 1 else 0 end) as has_non_zero
    from {{ ref('stg_nl_government__budget_moments') }}
    group by fiscal_year, moment
)

select
    lines.fiscal_year,
    lines.moment,
    lines.lines,
    lines.figures,
    served.has_non_zero
from lines
inner join served on lines.fiscal_year = served.fiscal_year and lines.moment = served.moment
where
    (lines.figures <> 0 and lines.figures <> lines.lines)
    or (served.has_non_zero = 1 and lines.figures = 0)
    or (served.has_non_zero = 0 and lines.figures <> 0)
