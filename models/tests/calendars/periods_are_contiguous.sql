-- Each entity's periods run back to back with no gap, twelve a year, one per period number, and
-- the calendar holds every day of every period.
with ordered as (
    select
        entity_id,
        fiscal_year,
        fiscal_period,
        period_start,
        period_end,
        lead(period_start) over (
            partition by entity_id order by period_start
        ) as next_start,
        lag(fiscal_year) over (partition by entity_id order by period_start) as previous_year,
        lag(fiscal_period) over (partition by entity_id order by period_start) as previous_period
    from {{ ref('fiscal_periods') }}
),

days as (
    select
        entity_id,
        fiscal_year,
        fiscal_period,
        count(*) as days
    from {{ ref('fiscal_calendar') }}
    group by entity_id, fiscal_year, fiscal_period
)

select
    ordered.entity_id,
    ordered.fiscal_year,
    ordered.fiscal_period
from ordered
left join days
    on
        ordered.entity_id = days.entity_id
        and ordered.fiscal_year = days.fiscal_year
        and ordered.fiscal_period = days.fiscal_period
where
    ordered.next_start != {{ dbt.dateadd('day', 1, 'ordered.period_end') }}
    or (ordered.fiscal_period = 1 and ordered.previous_period != 12)
    or (ordered.fiscal_period = 1 and ordered.previous_year != ordered.fiscal_year - 1)
    or (ordered.fiscal_period > 1 and ordered.previous_period != ordered.fiscal_period - 1)
    or (ordered.fiscal_period > 1 and ordered.previous_year != ordered.fiscal_year)
    or days.days is null
    or days.days != {{ dbt.datediff('ordered.period_start', 'ordered.period_end', 'day') }} + 1
