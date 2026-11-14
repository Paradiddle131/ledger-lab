-- Fails on every entity, fiscal year and period (null for a figure with a year and no day) whose
-- own-currency total or currency in the mart differs from the total worked out here straight
-- from the entity marts, or that only one side has: the payments mart's payments that are not
-- repeats, by the fiscal period their day falls in or, without a day, by their fiscal year, and
-- the Dutch cash-expenditure outturn by year. A span the ECB rates do not cover (its currency's or
-- the reporting currency's rates first published more than four days after its start or last
-- more than four days before its end) is left out of both.
{%- set reporting = "'" ~ var('group_totals').reporting_currency ~ "'" %}

with periods as (
    select entity_id, fiscal_year, fiscal_period, period_start, period_end
    from {{ ref('fiscal_periods') }}
),

years as (
    select entity_id, fiscal_year, min(period_start) as period_start, max(period_end) as period_end
    from periods
    group by entity_id, fiscal_year
),

figures as (
    select
        payments.entity_id,
        coalesce(periods.fiscal_year, years.fiscal_year) as fiscal_year,
        periods.fiscal_period,
        coalesce(periods.period_start, years.period_start) as period_start,
        coalesce(periods.period_end, years.period_end) as period_end,
        payments.currency,
        payments.amount
    from {{ ref('payments') }} as payments
    left join periods
        on
            payments.entity_id = periods.entity_id
            and payments.payment_date between periods.period_start and periods.period_end
    left join years
        on
            payments.payment_date is null
            and payments.entity_id = years.entity_id
            and payments.fiscal_year = years.fiscal_year
    where not payments.is_repeat

    union all

    select
        lines.entity_id,
        lines.fiscal_year,
        null,
        years.period_start,
        years.period_end,
        lines.currency,
        lines.outturn_eur
    from {{ ref('budget_against_outturn') }} as lines
    inner join years
        on lines.entity_id = years.entity_id and lines.fiscal_year = years.fiscal_year
    where lines.kind = 'U' and lines.outturn_eur is not null
),

span_rates as (
    select
        spans.period_start,
        spans.period_end,
        rates.currency,
        min(rates.published_on) as first_published_on,
        max(rates.published_on) as last_published_on
    from (select distinct period_start, period_end from figures) as spans
    inner join {{ ref('fx_published_rates') }} as rates
        on rates.published_on between spans.period_start and spans.period_end
    group by spans.period_start, spans.period_end, rates.currency
),

covered as (
    select period_start, period_end, currency
    from span_rates
    where
        first_published_on <= cast({{ dbt.dateadd('day', 4, 'period_start') }} as date)
        and last_published_on >= cast({{ dbt.dateadd('day', -4, 'period_end') }} as date)
),

worked_out as (
    select
        figures.entity_id,
        figures.fiscal_year,
        figures.fiscal_period,
        min(figures.currency) as currency,
        max(figures.currency) as last_currency,
        sum(figures.amount) as amount
    from figures
    inner join covered as from_rates
        on
            figures.period_start = from_rates.period_start
            and figures.period_end = from_rates.period_end
            and figures.currency = from_rates.currency
    inner join covered as to_rates
        on
            figures.period_start = to_rates.period_start
            and figures.period_end = to_rates.period_end
            and to_rates.currency = {{ reporting }}
    group by figures.entity_id, figures.fiscal_year, figures.fiscal_period
),

group_totals as (
    select entity_id, fiscal_year, fiscal_period, currency, amount
    from {{ ref('group_totals') }}
)

select
    coalesce(group_totals.entity_id, worked_out.entity_id) as entity_id,
    coalesce(group_totals.fiscal_year, worked_out.fiscal_year) as fiscal_year,
    coalesce(group_totals.fiscal_period, worked_out.fiscal_period) as fiscal_period,
    group_totals.currency as currency_in_mart,
    worked_out.currency as currency_worked_out,
    group_totals.amount as amount_in_mart,
    worked_out.amount as amount_worked_out
from group_totals
full outer join worked_out
    on
        group_totals.entity_id = worked_out.entity_id
        and group_totals.fiscal_year = worked_out.fiscal_year
        and coalesce(group_totals.fiscal_period, 0) = coalesce(worked_out.fiscal_period, 0)
where
    group_totals.amount is null
    or worked_out.amount is null
    or group_totals.amount <> worked_out.amount
    or worked_out.currency <> worked_out.last_currency
    or group_totals.currency <> worked_out.currency
