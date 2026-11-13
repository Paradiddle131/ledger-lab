-- What the rates cannot translate is null, never a far-away rate: an unknown currency either
-- way, a currency code not in upper case, and the day before the first publication date and the
-- day after the last, euros to euros included. A model using the macro tests its translated
-- amount not_null, so such a row fails its build.
with bounds as (
    select
        min(rate_on) as first_day,
        max(rate_on) as last_day
    from {{ ref('fx_rates') }}
),

postings as (
    select
        bounds.first_day as posted_on,
        'JPY' as from_currency,
        'EUR' as to_currency,
        100.00 as amount
    from bounds
    union all
    select bounds.first_day, 'USD', 'JPY', 100.00 from bounds
    union all
    select bounds.first_day, 'usd', 'GBP', 100.00 from bounds
    union all
    select cast({{ dbt.dateadd('day', -1, 'bounds.first_day') }} as date), 'USD', 'GBP', 100.00
    from bounds
    union all
    select cast({{ dbt.dateadd('day', 1, 'bounds.last_day') }} as date), 'USD', 'GBP', 100.00
    from bounds
    union all
    select cast({{ dbt.dateadd('day', 1, 'bounds.last_day') }} as date), 'EUR', 'EUR', 100.00
    from bounds
),

translated as (
    {{ translate_currency(
        'postings', 'amount', 'from_currency', 'to_currency', rate_date='posted_on'
    ) }}
)

select
    posted_on,
    from_currency,
    translated_amount
from translated
where translated_amount is not null or rates_published_on is not null
union all
select
    cast(null as date) as posted_on,
    cast(null as {{ dbt.type_string() }}) as from_currency,
    cast(null as {{ dbt.type_numeric() }}) as translated_amount
from (select count(*) as probes from translated) as counted
where counted.probes != 6
