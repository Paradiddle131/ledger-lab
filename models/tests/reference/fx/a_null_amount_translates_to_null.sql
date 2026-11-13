-- A null amount translates to null and raises nothing, even where no rate applies (an unknown
-- currency, a day after the last publication date); an amount with no rate stops the query,
-- which the Python tests show.
with postings as (
    select
        cast('2024-04-04' as date) as posted_on,
        'USD' as currency,
        cast(null as {{ dbt.type_numeric() }}) as amount
    union all
    select cast('2024-04-04' as date), 'JPY', cast(null as {{ dbt.type_numeric() }})
    union all
    select cast('2099-01-01' as date), 'USD', cast(null as {{ dbt.type_numeric() }})
),

translated as (
    {{ translate_currency('postings', 'amount', 'currency', "'GBP'", rate_date='posted_on') }}
)

select
    posted_on,
    currency,
    translated_amount
from translated
where translated_amount is not null
union all
select
    cast(null as date) as posted_on,
    cast(null as {{ dbt.type_string() }}) as currency,
    cast(null as {{ dbt.type_numeric() }}) as translated_amount
from (select count(*) as probes from translated) as counted
where counted.probes != 3
