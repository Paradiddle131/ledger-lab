-- TARGET is closed from Good Friday to Easter Monday (29 March to 1 April 2024), so all four days
-- take Thursday 28 March's rates in every currency, and Tuesday 2 April takes its own.
with expected as (
    select cast('2024-03-28' as date) as rate_on, cast('2024-03-28' as date) as published_on
    union all
    select cast('2024-03-29' as date), cast('2024-03-28' as date)
    union all
    select cast('2024-03-30' as date), cast('2024-03-28' as date)
    union all
    select cast('2024-03-31' as date), cast('2024-03-28' as date)
    union all
    select cast('2024-04-01' as date), cast('2024-03-28' as date)
    union all
    select cast('2024-04-02' as date), cast('2024-04-02' as date)
),

currencies as (
    select 'EUR' as currency
    union all
    select 'GBP'
    union all
    select 'USD'
)

select
    expected.rate_on,
    currencies.currency,
    fx_rates.published_on
from expected
cross join currencies
left join {{ ref('fx_rates') }} as fx_rates
    on expected.rate_on = fx_rates.rate_on and currencies.currency = fx_rates.currency
left join {{ ref('fx_published_rates') }} as published
    on expected.published_on = published.published_on and currencies.currency = published.currency
where
    fx_rates.published_on is null
    or published.units_per_euro is null
    or fx_rates.published_on != expected.published_on
    or fx_rates.units_per_euro != published.units_per_euro
