-- One row per entity and fiscal period. A fiscal year is twelve calendar months from the entity's
-- first month, labelled by the calendar year it starts in (the UK's 2024-25 is 2024); period 1 is
-- its first month. The status comes from the period-status seed.
with months as (
    {{ fiscal_spine('month') }}
),

periods as (
    select
        starts.entity_id,
        extract(year from months.date_month)
        - case when extract(month from months.date_month) < starts.first_month then 1 else 0 end
            as fiscal_year,
        case
            when extract(month from months.date_month) >= starts.first_month
                then extract(month from months.date_month) - starts.first_month + 1
            else extract(month from months.date_month) - starts.first_month + 13
        end as fiscal_period,
        cast(months.date_month as date) as period_start,
        {{ dbt.last_day('months.date_month', 'month') }} as period_end
    from {{ ref('fiscal_year_starts') }} as starts
    cross join months
)

select
    periods.entity_id,
    periods.fiscal_year,
    periods.fiscal_period,
    periods.period_start,
    periods.period_end,
    period_status.status
from periods
left join {{ ref('period_status') }} as period_status
    on
        periods.entity_id = period_status.entity_id
        and periods.fiscal_year = period_status.fiscal_year
        and periods.fiscal_period = period_status.fiscal_period
where periods.fiscal_year between {{ var('fiscal_years').first }} and {{ var('fiscal_years').last }}
