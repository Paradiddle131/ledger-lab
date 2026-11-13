-- One row per calendar day and currency from the first ECB publication date to the last: the
-- rates published that day, or on a closed day the previous business day's, carried at most four
-- days, the longest run of closed days in the published rates (Good Friday to Easter Monday, or a
-- weekend before 25 and 26 December). A day before the first publication date, after the last,
-- or more than four days after the last publication before it has no row, so a translation on it
-- finds no rate instead of reusing a far-away one.
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

days as (
    {{ fiscal_spine('day') }}
)

select
    cast(days.date_day as date) as rate_on,
    published.currency,
    published.units_per_euro,
    published.published_on
from published
inner join days
    on
        cast(days.date_day as date) >= published.published_on
        and cast(days.date_day as date) < published.next_published_on
        and cast(days.date_day as date)
        <= cast({{ dbt.dateadd('day', 4, 'published.published_on') }} as date)
