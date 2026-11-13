-- Results round once, half away from zero, to two decimal places, exactly, including halves a
-- float division misrounds. Each expected figure is the exact quotient at the real rates,
-- rounded by hand: 542.05 dollars on 5 April 2024 are exactly 500 euros (at 1.0841), which are
-- exactly 428.865 pounds (at 0.85773), so 428.87, where a float gives 428.86; 10.00 euros on
-- 25 March are exactly 10.835 dollars (at 1.0835), so 10.84.
with postings as (
    select
        cast('2024-04-05' as date) as posted_on,
        'USD' as from_currency,
        'GBP' as to_currency,
        542.05 as amount,
        428.87 as expected
    union all
    select cast('2024-04-05' as date), 'USD', 'GBP', -542.05, -428.87
    union all
    select cast('2024-04-05' as date), 'USD', 'GBP', 1084100542.05, 857730428.87
    union all
    select cast('2024-04-05' as date), 'USD', 'EUR', 542.05, 500.00
    union all
    select cast('2024-03-25' as date), 'EUR', 'USD', 10.00, 10.84
    union all
    select cast('2024-03-25' as date), 'EUR', 'USD', -10.00, -10.84
    union all
    select cast('2024-03-25' as date), 'EUR', 'GBP', 0.01, 0.01
    union all
    select cast('2024-03-25' as date), 'GBP', 'USD', 0.00, 0.00
),

translated as (
    {{ translate_currency(
        'postings', 'amount', 'from_currency', 'to_currency', rate_date='posted_on'
    ) }}
)

select
    posted_on,
    from_currency,
    to_currency,
    amount,
    expected,
    translated_amount
from translated
where translated_amount is null or translated_amount != expected
