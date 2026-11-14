-- One row per figure an entity brings to the group totals: every payment of the payments mart and
-- every cash-expenditure line of the Dutch budget against outturn, each with the span it counts
-- in (its fiscal period, or its fiscal year when it has no day), its basis, the rule that leaves
-- it out of the totals (null when it counts) and, when it counts, its amount in the reporting
-- currency at actual and at constant rates.
--
-- The rules, first match deciding: a payment the source serves again (`repeat`); a figure of a
-- year the fiscal calendars do not cover (`outside-calendars`); a Dutch line of a year the file
-- serves no outturn for (`no-outturn`), since a budget never stands in for spend; a figure whose
-- span the ECB rates do not cover (`no-rate`), its first publication more than four days after
-- the span's start or its last more than four days before its end, the run of closed days the
-- rates carry over, so a period is left out whole, never in part.
--
-- At actual rates a payment with a day takes that day's rates (`translate_currency`), and a
-- payment inside a covered period on a day with no rate stops the query; a figure with a year and
-- no day takes, for each currency, the mean of the rates the ECB published on the span's days,
-- rounded to six decimal places, which is how the ECB averages its own rates over a year. At
-- constant rates every figure takes the rates of one named day. The reporting currency and that
-- day are the `group_totals` var's.
{%- set reporting = "'" ~ var('group_totals').reporting_currency ~ "'" %}
{%- set constant_rates_on = var('group_totals').constant_rates_on %}

with periods as (
    select
        entity_id,
        fiscal_year,
        fiscal_period,
        period_start,
        period_end
    from {{ ref('fiscal_periods') }}
),

years as (
    select
        entity_id,
        fiscal_year,
        min(period_start) as period_start,
        max(period_end) as period_end
    from periods
    group by entity_id, fiscal_year
),

served as (
    select
        payment_key as figure_key,
        entity_id,
        case
            when entity_id = 'uk-transport' then 'payments-over-25k'
            when entity_id = 'chicago' and payment_date is null then 'payments-year-rollup'
            when entity_id = 'chicago' then 'payments'
        end as basis,
        fiscal_year,
        fiscal_period,
        payment_date,
        currency,
        amount,
        is_repeat,
        true as has_figure
    from {{ ref('payments') }}

    union all

    select
        budget_line_key as figure_key,
        entity_id,
        'cash-outturn' as basis,
        fiscal_year,
        cast(null as {{ dbt.type_int() }}) as fiscal_period,
        cast(null as date) as payment_date,
        currency,
        outturn_eur as amount,
        false as is_repeat,
        outturn_eur is not null as has_figure
    from {{ ref('budget_against_outturn') }}
    where kind = 'U'
),

spanned as (
    select
        served.*,
        coalesce(periods.period_start, years.period_start) as period_start,
        coalesce(periods.period_end, years.period_end) as period_end
    from served
    left join periods
        on
            served.entity_id = periods.entity_id
            and served.fiscal_year = periods.fiscal_year
            and served.fiscal_period = periods.fiscal_period
    left join years
        on
            served.entity_id = years.entity_id
            and served.fiscal_year = years.fiscal_year
            and served.fiscal_period is null
),

spans as (
    select distinct
        period_start,
        period_end
    from spanned
    where period_start is not null
),

span_rates as (
    select
        spans.period_start,
        spans.period_end,
        rates.currency,
        min(rates.published_on) as first_published_on,
        max(rates.published_on) as last_published_on,
        cast(round(avg(rates.units_per_euro), 6) as {{ dbt.type_numeric() }})
            as average_units_per_euro
    from spans
    inner join {{ ref('fx_published_rates') }} as rates
        on rates.published_on between spans.period_start and spans.period_end
    group by spans.period_start, spans.period_end, rates.currency
),

covered as (
    select
        period_start,
        period_end,
        currency,
        average_units_per_euro
    from span_rates
    where
        first_published_on <= cast({{ dbt.dateadd('day', 4, 'period_start') }} as date)
        and last_published_on >= cast({{ dbt.dateadd('day', -4, 'period_end') }} as date)
),

classified as (
    select
        spanned.*,
        from_rates.average_units_per_euro as from_average_units_per_euro,
        to_rates.average_units_per_euro as to_average_units_per_euro,
        case
            when spanned.is_repeat then 'repeat'
            when spanned.period_start is null then 'outside-calendars'
            when not spanned.has_figure then 'no-outturn'
            when from_rates.currency is null or to_rates.currency is null then 'no-rate'
        end as left_out_by
    from spanned
    left join covered as from_rates
        on
            spanned.period_start = from_rates.period_start
            and spanned.period_end = from_rates.period_end
            and spanned.currency = from_rates.currency
    left join covered as to_rates
        on
            spanned.period_start = to_rates.period_start
            and spanned.period_end = to_rates.period_end
            and to_rates.currency = {{ reporting }}
),

counted as (
    select * from classified
    where left_out_by is null
),

dated as (
    select * from counted
    where payment_date is not null
),

dated_at_actual_rates as (
    {{ translate_currency('dated', 'amount', 'currency', reporting, rate_date='payment_date') }}
),

at_actual_rates as (
    select
        figure_key,
        translated_amount as amount_at_actual_rates
    from dated_at_actual_rates

    union all

    select
        figure_key,
        cast({{ convert_through_euro(
            'cast(amount as ' ~ dbt.type_numeric() ~ ')',
            'from_average_units_per_euro',
            'to_average_units_per_euro',
        ) }} as {{ dbt.type_numeric() }}) as amount_at_actual_rates
    from counted
    where payment_date is null
),

at_constant_rates as (
    {{ translate_currency(
        'counted', 'amount', 'currency', reporting, constant_rates_on=constant_rates_on
    ) }}
)

select
    classified.figure_key,
    classified.entity_id,
    classified.basis,
    classified.fiscal_year,
    classified.fiscal_period,
    classified.period_start,
    classified.period_end,
    classified.payment_date,
    classified.currency,
    classified.amount,
    classified.left_out_by,
    {{ reporting }} as reporting_currency,
    at_actual_rates.amount_at_actual_rates,
    at_constant_rates.translated_amount as amount_at_constant_rates,
    cast('{{ constant_rates_on }}' as date) as constant_rates_on
from classified
left join at_actual_rates on classified.figure_key = at_actual_rates.figure_key
left join at_constant_rates on classified.figure_key = at_constant_rates.figure_key
