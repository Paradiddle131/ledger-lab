-- Every day a publication covers has exactly one rate per currency: the publication date and the
-- days before the next one, at most four of them, carried no more than four days, and the euro's
-- is 1; no day outside the publication dates has a rate. The days are counted from the published
-- rates themselves, whatever years they cover. A run of closed days longer than four leaves days
-- with no rate, which this test allows, since the committed sample is three windows of real
-- rates (29 December 2023, a fortnight of 2024 and March 2026); the landed test holds the landed
-- file to one unbroken run from its first rate to its last.
with published as (
    select
        currency,
        published_on,
        lead(published_on) over (partition by currency order by published_on) as next_published_on
    from {{ ref('fx_published_rates') }}
),

bounds as (
    select
        currency,
        min(published_on) as first_day,
        max(published_on) as last_day,
        sum(
            case
                when next_published_on is null then 1
                when {{ dbt.datediff('published_on', 'next_published_on', 'day') }} > 5 then 5
                else {{ dbt.datediff('published_on', 'next_published_on', 'day') }}
            end
        ) as days
    from published
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
    or covered.days != bounds.days
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
