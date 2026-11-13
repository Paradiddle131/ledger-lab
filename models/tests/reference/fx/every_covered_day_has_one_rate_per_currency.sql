-- From the first publication date to the last, every day has exactly one rate per currency,
-- carried no more than four days from a publication date, and the euro's is 1: a run of closed
-- days longer than four (a gap in the published rates) fails here instead of translating to
-- null, and no day outside the publication dates has a rate.
with bounds as (
    select
        min(published_on) as first_day,
        max(published_on) as last_day
    from {{ ref('fx_published_rates') }}
),

days as (
    {{ fiscal_spine('day') }}
),

expected as (
    select
        cast(days.date_day as date) as rate_on,
        currencies.currency
    from days
    cross join bounds
    cross join (select distinct currency from {{ ref('fx_published_rates') }}) as currencies
    where cast(days.date_day as date) between bounds.first_day and bounds.last_day
),

counted as (
    select
        expected.rate_on,
        expected.currency,
        count(fx_rates.rate_on) as rates
    from expected
    left join {{ ref('fx_rates') }} as fx_rates
        on expected.rate_on = fx_rates.rate_on and expected.currency = fx_rates.currency
    group by expected.rate_on, expected.currency
)

select
    rate_on,
    currency
from counted
where rates != 1
union all
select
    fx_rates.rate_on,
    fx_rates.currency
from {{ ref('fx_rates') }} as fx_rates
cross join bounds
where
    {{ dbt.datediff('fx_rates.published_on', 'fx_rates.rate_on', 'day') }} not between 0 and 4
    or (fx_rates.currency = 'EUR' and fx_rates.units_per_euro != 1)
    or fx_rates.rate_on not between bounds.first_day and bounds.last_day
