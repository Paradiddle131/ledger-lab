-- The period-status seed holds one row per period, and none for a period the calendars lack.
select
    period_status.entity_id,
    period_status.fiscal_year,
    period_status.fiscal_period
from {{ ref('period_status') }} as period_status
left join {{ ref('fiscal_periods') }} as fiscal_periods
    on
        period_status.entity_id = fiscal_periods.entity_id
        and period_status.fiscal_year = fiscal_periods.fiscal_year
        and period_status.fiscal_period = fiscal_periods.fiscal_period
group by period_status.entity_id, period_status.fiscal_year, period_status.fiscal_period
having count(fiscal_periods.entity_id) != 1
