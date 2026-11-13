-- Euros to euros, and any known currency to itself, is the amount unchanged on a day the rates
-- cover, a closed day included.
with postings as (
    select cast('2024-03-27' as date) as posted_on, 'EUR' as currency, 1234.56 as amount
    union all
    select cast('2024-03-30' as date), 'EUR', -0.01
    union all
    select cast('2024-04-04' as date), 'GBP', 98765.43
    union all
    select cast('2024-04-04' as date), 'USD', 0.50
),

translated as (
    {{ translate_currency('postings', 'amount', 'currency', 'currency', rate_date='posted_on') }}
)

select
    posted_on,
    currency,
    amount,
    translated_amount
from translated
where translated_amount is null or translated_amount != amount
