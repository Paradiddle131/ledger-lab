-- One row per entity and day: the fiscal year and period the day falls in.
with days as (
    {{ fiscal_spine('day') }}
)

select
    fiscal_periods.entity_id,
    cast(days.date_day as date) as calendar_date,
    fiscal_periods.fiscal_year,
    fiscal_periods.fiscal_period
from days
inner join {{ ref('fiscal_periods') }} as fiscal_periods
    on cast(days.date_day as date) between fiscal_periods.period_start and fiscal_periods.period_end
