-- Fails on every span (a fiscal period, or a fiscal year for figures with a year and no day) whose
-- group total in the mart, the sum of its rows across entities at actual and at constant rates,
-- differs from the same sum worked out here straight from the entity marts, or whose count of
-- figures differs. This side translates each figure itself and never rounds it: a payment at the
-- latest ECB rates published on or up to four days before its day, a figure with a year and no
-- day at the mean of the rates published in its span to six places, and at constant rates at the
-- latest rates on or before the `group_totals` var's day; so a span may differ by at most half a
-- cent per figure. A span counts when the rates of the figure's currency and the reporting
-- currency were first published at most four days after its start and last at most four days
-- before its end.
{%- set settings = var('group_totals') %}
{%- set reporting = "'" ~ settings.reporting_currency ~ "'" %}
{%- set constant_day = "cast('" ~ settings.constant_rates_on ~ "' as date)" %}

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
        payments.currency,
        payments.amount,
        payments.payment_date,
        coalesce(periods.period_start, years.period_start) as period_start,
        coalesce(periods.period_end, years.period_end) as period_end
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
        lines.currency,
        lines.outturn_eur,
        cast(null as date),
        years.period_start,
        years.period_end
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
        max(rates.published_on) as last_published_on,
        round(avg(rates.units_per_euro), 6) as average_units_per_euro
    from (select distinct period_start, period_end from figures) as spans
    inner join {{ ref('fx_published_rates') }} as rates
        on rates.published_on between spans.period_start and spans.period_end
    group by spans.period_start, spans.period_end, rates.currency
),

days as (
    select distinct payment_date as rate_day from figures where payment_date is not null
    union all
    select {{ constant_day }}
),

day_rates as (
    select
        days.rate_day,
        rates.currency,
        rates.units_per_euro,
        row_number() over (
            partition by days.rate_day, rates.currency order by rates.published_on desc
        ) as latest
    from days
    inner join {{ ref('fx_published_rates') }} as rates
        on
            rates.published_on <= days.rate_day
            and rates.published_on >= cast({{ dbt.dateadd('day', -4, 'days.rate_day') }} as date)
),

translated as (
    select
        figures.period_start,
        figures.period_end,
        case
            when figures.payment_date is null
                then figures.amount * to_span.average_units_per_euro
                / from_span.average_units_per_euro
            else figures.amount * to_day.units_per_euro / from_day.units_per_euro
        end as at_actual_rates,
        figures.amount * to_constant.units_per_euro / from_constant.units_per_euro
            as at_constant_rates
    from figures
    inner join span_rates as from_span
        on
            figures.period_start = from_span.period_start
            and figures.period_end = from_span.period_end
            and figures.currency = from_span.currency
    inner join span_rates as to_span
        on
            figures.period_start = to_span.period_start
            and figures.period_end = to_span.period_end
            and to_span.currency = {{ reporting }}
    left join day_rates as from_day
        on
            figures.payment_date = from_day.rate_day
            and figures.currency = from_day.currency
            and from_day.latest = 1
    left join day_rates as to_day
        on
            figures.payment_date = to_day.rate_day
            and to_day.currency = {{ reporting }}
            and to_day.latest = 1
    inner join day_rates as from_constant
        on
            from_constant.rate_day = {{ constant_day }}
            and figures.currency = from_constant.currency
            and from_constant.latest = 1
    inner join day_rates as to_constant
        on
            to_constant.rate_day = {{ constant_day }}
            and to_constant.currency = {{ reporting }}
            and to_constant.latest = 1
    where
        from_span.first_published_on <= cast({{ dbt.dateadd('day', 4, 'figures.period_start') }} as date)
        and to_span.first_published_on <= cast({{ dbt.dateadd('day', 4, 'figures.period_start') }} as date)
        and from_span.last_published_on >= cast({{ dbt.dateadd('day', -4, 'figures.period_end') }} as date)
        and to_span.last_published_on >= cast({{ dbt.dateadd('day', -4, 'figures.period_end') }} as date)
),

worked_out as (
    select
        period_start,
        period_end,
        count(*) as figures,
        count(at_actual_rates) as translated,
        sum(at_actual_rates) as at_actual_rates,
        sum(at_constant_rates) as at_constant_rates
    from translated
    group by period_start, period_end
),

group_totals as (
    select
        period_start,
        period_end,
        sum(figures) as figures,
        sum(amount_at_actual_rates) as at_actual_rates,
        sum(amount_at_constant_rates) as at_constant_rates
    from {{ ref('group_totals') }}
    group by period_start, period_end
)

select
    coalesce(group_totals.period_start, worked_out.period_start) as period_start,
    coalesce(group_totals.period_end, worked_out.period_end) as period_end,
    group_totals.figures as figures_in_mart,
    worked_out.figures as figures_worked_out,
    group_totals.at_actual_rates as actual_in_mart,
    worked_out.at_actual_rates as actual_worked_out,
    group_totals.at_constant_rates as constant_in_mart,
    worked_out.at_constant_rates as constant_worked_out
from group_totals
full outer join worked_out
    on
        group_totals.period_start = worked_out.period_start
        and group_totals.period_end = worked_out.period_end
where
    group_totals.figures is null
    or worked_out.figures is null
    or group_totals.figures <> worked_out.figures
    or worked_out.at_actual_rates is null
    or worked_out.figures <> worked_out.translated
    or abs(group_totals.at_actual_rates - worked_out.at_actual_rates)
    > 0.005 * worked_out.figures
    or abs(group_totals.at_constant_rates - worked_out.at_constant_rates)
    > 0.005 * worked_out.figures
