-- Constant currency: with constant_rates_on, the same dollar amount posted on different days
-- translates to one result, that of the named day's rate set (27 March 2024), whatever the day
-- of the posting; at each posting's own rates the results differ, so the mode is what holds them
-- equal. A named closed day (Saturday 30 March) takes its previous business day's set.
with postings as (
    select cast('2024-03-25' as date) as posted_on, 1000.00 as amount
    union all
    select cast('2024-03-30' as date), 1000.00
    union all
    select cast('2024-04-02' as date), 1000.00
    union all
    select cast('2024-04-08' as date), 1000.00
),

constant as (
    {{ translate_currency(
        'postings', 'amount', "'USD'", "'GBP'", constant_rates_on='2024-03-27'
    ) }}
),

closed_day as (
    {{ translate_currency(
        'postings', 'amount', "'USD'", "'GBP'", constant_rates_on='2024-03-30'
    ) }}
),

actual as (
    {{ translate_currency('postings', 'amount', "'USD'", "'GBP'", rate_date='posted_on') }}
),

named_day as (
    {{ translate_currency(
        "(select cast('2024-03-27' as date) as posted_on, 1000.00 as amount)",
        'amount', "'USD'", "'GBP'", rate_date='posted_on'
    ) }}
)

select
    'a constant result differs from the named day' as failure,
    constant.posted_on
from constant
cross join named_day
where
    constant.translated_amount is null
    or named_day.translated_amount is null
    or constant.translated_amount != named_day.translated_amount
    or constant.rates_published_on != cast('2024-03-27' as date)
union all
select
    'a closed named day does not take the previous business day' as failure,
    closed_day.posted_on
from closed_day
where
    closed_day.translated_amount is null
    or closed_day.rates_published_on != cast('2024-03-28' as date)
union all
select
    'the actual rates do not vary, so the test proves nothing' as failure,
    cast(null as date) as posted_on
from (select count(distinct translated_amount) as results from actual) as counted
where counted.results < 4
