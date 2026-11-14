-- One row per calendar day and currency from the first ECB publication date to the last: the
-- rates published that day, or on a closed day the previous business day's, carried at most four
-- days, the longest run of closed days in the published rates (Good Friday to Easter Monday, or
-- 25 and 26 December beside a weekend). Each publication date spreads over itself and the days
-- before the next one, so the days follow the published rates, whatever years they cover. A day
-- before the first publication date, after the last, or more than four days after the last
-- publication before it has no row, so a translation on it finds no rate instead of reusing a
-- far-away one.
with published as (
    select
        published_on,
        currency,
        units_per_euro,
        coalesce(
            lead(published_on) over (partition by currency order by published_on),
            cast({{ dbt.dateadd('day', 1, 'published_on') }} as date)
        ) as next_published_on
    from {{ ref('fx_published_rates') }}
),

carried as (
    select 0 as days
    union all
    select 1
    union all
    select 2
    union all
    select 3
    union all
    select 4
),

spread as (
    select
        cast({{ dbt.dateadd('day', 'carried.days', 'published.published_on') }} as date) as rate_on,
        published.currency,
        published.units_per_euro,
        published.published_on,
        published.next_published_on
    from published
    cross join carried
)

select
    rate_on,
    currency,
    units_per_euro,
    published_on
from spread
where rate_on < next_published_on
