-- A Saturday and a Sunday translate at the Friday's rates: 6 and 7 April 2024 take 5 April's,
-- the same result as the Friday itself, and the Friday's is its published rate (1,000 dollars
-- or pounds within half a cent of 1,000 ÷ 1.0841 or 1,000 ÷ 0.85773 euros).
with postings as (
    select cast('2024-04-05' as date) as posted_on, 'USD' as currency, 1000.00 as amount
    union all
    select cast('2024-04-06' as date), 'USD', 1000.00
    union all
    select cast('2024-04-07' as date), 'USD', 1000.00
    union all
    select cast('2024-04-05' as date), 'GBP', 1000.00
    union all
    select cast('2024-04-06' as date), 'GBP', 1000.00
    union all
    select cast('2024-04-07' as date), 'GBP', 1000.00
),

translated as (
    {{ translate_currency('postings', 'amount', 'currency', "'EUR'", rate_date='posted_on') }}
),

fridays as (
    select
        currency,
        translated_amount,
        case currency when 'USD' then 1.0841 else 0.85773 end as published_rate
    from translated
    where posted_on = cast('2024-04-05' as date)
)

select
    translated.posted_on,
    translated.currency,
    translated.translated_amount
from translated
left join fridays on translated.currency = fridays.currency
where
    translated.rates_published_on is null
    or translated.rates_published_on != cast('2024-04-05' as date)
    or fridays.translated_amount is null
    or translated.translated_amount != fridays.translated_amount
    or abs(fridays.translated_amount * fridays.published_rate - translated.amount)
    > 0.005 * fridays.published_rate
