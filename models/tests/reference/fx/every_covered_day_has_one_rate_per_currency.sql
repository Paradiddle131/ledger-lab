-- From the first publication date to the last, every day has exactly one rate per currency,
-- carried no more than four days from a publication date, and the euro's is 1: a run of closed
-- days longer than four (a gap in the published rates) fails here instead of failing the
-- translation, and no day outside the publication dates has a rate. The days are counted from
-- the published rates themselves, whatever years they cover.
with bounds as (
    select
        currency,
        min(published_on) as first_day,
        max(published_on) as last_day
    from {{ ref('fx_published_rates') }}
    group by currency
),

covered as (
    select
        currency,
        count(*) as rates,
        count(distinct rate_on) as days
    from {{ ref('fx_rates') }}
    group by currency
)

select
    bounds.currency,
    bounds.first_day as rate_on
from bounds
left join covered on bounds.currency = covered.currency
where
    covered.days is null
    or covered.rates != covered.days
    or covered.days != {{ dbt.datediff('bounds.first_day', 'bounds.last_day', 'day') }} + 1
union all
select
    fx_rates.currency,
    fx_rates.rate_on
from {{ ref('fx_rates') }} as fx_rates
inner join bounds on fx_rates.currency = bounds.currency
where
    {{ dbt.datediff('fx_rates.published_on', 'fx_rates.rate_on', 'day') }} not between 0 and 4
    or (fx_rates.currency = 'EUR' and fx_rates.units_per_euro != 1)
    or fx_rates.rate_on not between bounds.first_day and bounds.last_day
